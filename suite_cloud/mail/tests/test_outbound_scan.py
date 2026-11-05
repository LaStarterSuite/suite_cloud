"""The outbound spam scan: the MtaMilter in the cluster plan, the trusted accounts' Memory Lookup
Keys, and the Rspamd files and install state of each node."""

import json
import re
from unittest.mock import call, patch

import frappe

from suite_cloud.api.mail import accounts, domains
from suite_cloud.mail.cluster import bootstrap, egress, outbound_scan, plan, push, quota, rspamd, telemetry
from suite_cloud.mail.stalwart.errors import StalwartError
from suite_cloud.mail.tests import fake_stalwart
from suite_cloud.mail.tests.fixtures import make_node
from suite_cloud.mail.tests.test_tenant_api import TenantApiTestCase
from suite_cloud.patches.v1_0 import fill_outbound_scan_defaults
from suite_cloud.provisioning.ansible import playbook_task_names
from suite_cloud.tests.fixtures import configure_settings

EXECUTE = "suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"
TRUSTED = "alice@acme.com"


def get_milter(cluster) -> dict:
    return outbound_scan.milter_operation(cluster)["value"]["outbound-scan"]


def make_running_node(cluster, ipv4: str = "203.0.113.10", status: str = "Active"):
    """A node inserts as Pending; the status a job would have left it in is set after."""

    node = make_node(cluster, ipv4=ipv4)
    node.db_set("status", status)
    return node


class OutboundScanTestCase(TenantApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account(TRUSTED, "secret-pw")
        frappe.set_user("Administrator")
        # A settings change pushes every active cluster, and the site may hold a real one.
        self.resync = self.enterContext(patch.object(push, "resync_cluster_after_commit"))
        configure_settings(outbound_scan_mode="Enforce", outbound_scan_reject_score=15)
        self.node = make_running_node(self.cluster)

    def exempt_keys(self) -> list[str]:
        return sorted(
            key["key"]
            for key in self.fake.all(outbound_scan.LOOKUP_KEY)
            if key["namespace"] == outbound_scan.EXEMPT_NAMESPACE
        )

    def pushed_milter(self) -> dict | None:
        return self.fake.find("MtaMilter", hostname=rspamd.MILTER_HOST, port=rspamd.MILTER_PORT)

    def lookup_reloads(self) -> int:
        return sum(action["@type"] == "ReloadLookupStores" for action in self.fake.all("Action"))

    def set_trusted(self, value: int, email: str = TRUSTED) -> None:
        account = frappe.get_doc("Mail Account", email)
        account.skip_outbound_scan = value
        account.save()


class TestMilterPlan(OutboundScanTestCase):
    def test_the_plan_holds_the_milter(self) -> None:
        operations = [op for op in plan.cluster_plan(self.cluster) if op["object"] == "MtaMilter"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["matchOn"], ["hostname", "port"])
        milter = operations[0]["value"]["outbound-scan"]
        self.assertEqual(
            {key: value for key, value in milter.items() if key != "enable"},
            {
                "hostname": "127.0.0.1",
                "port": 11332,
                "stages": {"data": True},
                "tempFailOnError": False,
                "timeoutConnect": 1000,
                "timeoutCommand": 5000,
                "timeoutData": 6000,
                "protocolVersion": "v6",
                "useTls": False,
                "maxResponseSize": 52428800,
            },
        )
        condition = outbound_scan.SCANNED["match"]["0"]["if"]
        self.assertEqual(
            condition,
            "!is_empty(authenticated_as) && "
            "!key_exists('suite-cloud-outbound-scan-exempt', authenticated_as)",
        )
        # The recovery plan carries it too, off until a node has the scanner.
        self.assertIn("MtaMilter", [op["object"] for op in plan.recovery_plan(self.cluster)])

    def test_the_milter_waits_for_every_running_node_and_the_mode(self) -> None:
        self.assertEqual(get_milter(self.cluster)["enable"], quota.NEVER)
        self.node.db_set("outbound_scanner_installed", 1)
        self.assertEqual(get_milter(self.cluster)["enable"], outbound_scan.SCANNED)

        # Nodes that run no plan do not count.
        for status in ("Pending", "Failed", "Disabled"):
            make_running_node(self.cluster, ipv4=f"203.0.113.{20 + len(status)}", status=status)
        self.assertEqual(get_milter(self.cluster)["enable"], outbound_scan.SCANNED)
        draining = make_running_node(self.cluster, ipv4="203.0.113.40", status="Draining")
        self.assertEqual(get_milter(self.cluster)["enable"], quota.NEVER)
        draining.db_set("outbound_scanner_installed", 1)

        configure_settings(outbound_scan_mode="Off")
        self.assertEqual(get_milter(self.cluster)["enable"], quota.NEVER)
        configure_settings(outbound_scan_mode="Observe")
        self.assertEqual(get_milter(self.cluster)["enable"], outbound_scan.SCANNED)

    def test_turning_the_scan_off_pushes_each_active_cluster(self) -> None:
        self.node.db_set("outbound_scanner_installed", 1)
        self.cluster.push_config()
        self.assertEqual(self.pushed_milter()["enable"], outbound_scan.SCANNED)

        self.resync.reset_mock()
        configure_settings(outbound_scan_mode="Off")
        self.assertIn(call(self.cluster.name), self.resync.call_args_list)
        self.cluster.push_config()
        self.assertEqual(self.pushed_milter()["enable"], quota.NEVER)
        self.assertEqual(len(self.fake.all("MtaMilter")), 1)  # matched again, not added

        self.resync.reset_mock()
        configure_settings(outbound_scan_reject_score=10)  # in the Rspamd files only
        self.resync.assert_not_called()

    def test_existing_sites_get_the_defaults(self) -> None:
        settings = fill_outbound_scan_defaults.SETTINGS
        for field in fill_outbound_scan_defaults.FIELDS:
            frappe.db.set_single_value(settings, field, None)
        fill_outbound_scan_defaults.execute()
        self.assertEqual(frappe.db.get_single_value(settings, "outbound_scan_mode"), "Enforce")
        self.assertEqual(frappe.db.get_single_value(settings, "outbound_scan_reject_score"), 15)

        frappe.db.set_single_value(settings, "outbound_scan_mode", "Observe")
        fill_outbound_scan_defaults.execute()
        self.assertEqual(frappe.db.get_single_value(settings, "outbound_scan_mode"), "Observe")

    def test_enforce_needs_a_reject_score(self) -> None:
        self.assertRaises(
            frappe.ValidationError,
            configure_settings,
            outbound_scan_mode="Enforce",
            outbound_scan_reject_score=0,
        )


class TestTrustedAccounts(OutboundScanTestCase):
    def test_a_system_manager_marks_an_account_as_trusted(self) -> None:
        self.set_trusted(1)
        self.assertEqual(self.exempt_keys(), [TRUSTED])
        key = self.fake.find(outbound_scan.LOOKUP_KEY, key=TRUSTED)
        self.assertFalse(key["isGlobPattern"])
        self.assertEqual(self.lookup_reloads(), 1)

        self.set_trusted(0)
        self.assertEqual(self.exempt_keys(), [])
        self.assertEqual(self.lookup_reloads(), 2)

    def test_a_new_trusted_account_gets_its_key(self) -> None:
        frappe.get_doc({"doctype": "Mail Account", "email": "bob@acme.com", "skip_outbound_scan": 1}).insert()
        self.assertEqual(self.exempt_keys(), ["bob@acme.com"])

    def test_a_tenant_cannot_trust_its_own_account(self) -> None:
        self.act_as(self.tenant)
        account = frappe.get_doc("Mail Account", TRUSTED)
        account.skip_outbound_scan = 1
        account.save()
        self.assertEqual(frappe.db.get_value("Mail Account", TRUSTED, "skip_outbound_scan"), 0)
        self.assertEqual(self.exempt_keys(), [])
        self.assertNotIn("skip_outbound_scan", accounts.get_account(TRUSTED))

    def test_deleting_a_trusted_account_deletes_its_key(self) -> None:
        self.set_trusted(1)
        frappe.delete_doc("Mail Account", TRUSTED)
        self.assertEqual(self.exempt_keys(), [])

    def test_a_refused_key_fails_the_save(self) -> None:
        frappe.db.savepoint("refused_key")
        with patch.dict(fake_stalwart.QUERY_FILTERS, {"MemoryLookupKey": set()}):
            self.assertRaises(StalwartError, self.set_trusted, 1)
        frappe.db.rollback(save_point="refused_key")
        self.assertEqual(frappe.db.get_value("Mail Account", TRUSTED, "skip_outbound_scan"), 0)
        self.assertEqual(self.exempt_keys(), [])

    def test_a_sync_repairs_the_keys(self) -> None:
        self.set_trusted(1)
        service = self.cluster.get_client().objects(outbound_scan.LOOKUP_KEY)
        service.delete(self.fake.find(outbound_scan.LOOKUP_KEY, key=TRUSTED)["id"])
        service.create(outbound_scan.lookup_key("stray@acme.com"))
        other = service.create({"namespace": "another", "key": "kept", "isGlobPattern": False})
        reloads = self.lookup_reloads()

        self.assertTrue(outbound_scan.sync_exempt_accounts(self.cluster))
        self.assertEqual(self.exempt_keys(), [TRUSTED])
        self.assertIsNotNone(self.fake.get(outbound_scan.LOOKUP_KEY, other["id"]))
        self.assertEqual(self.lookup_reloads(), reloads + 1)

        self.assertFalse(outbound_scan.sync_exempt_accounts(self.cluster))
        self.assertEqual(self.lookup_reloads(), reloads + 1)

    def test_a_config_push_syncs_the_keys(self) -> None:
        self.set_trusted(1)
        self.fake.objects[outbound_scan.LOOKUP_KEY].clear()
        self.cluster.push_config()
        self.assertEqual(self.exempt_keys(), [TRUSTED])


class TestRspamd(OutboundScanTestCase):
    def test_the_files_follow_the_mode_and_score(self) -> None:
        files = rspamd.get_rspamd_files()
        settings = files["/etc/rspamd/local.d/settings.conf"]
        self.assertIn("reject = 15;", settings)
        self.assertIn('groups_disabled = ["fuzzy", "neural", "statistics", "asn", "mx_check"];', settings)
        proxy = files["/etc/rspamd/local.d/worker-proxy.inc"]
        self.assertIn('bind_socket = "127.0.0.1:11332";', proxy)
        self.assertIn(f"count = {rspamd.CPU_COUNT_PLACEHOLDER};", proxy)
        self.assertEqual(files["/etc/rspamd/local.d/worker-normal.inc"], "enabled = false;\n")
        # Ansible templates the variables it is given, so no file may hold Jinja.
        for path, content in files.items():
            with self.subTest(path):
                self.assertIsNone(re.search(r"\{\{|\{%|\{#", content))
        digest = rspamd.get_config_digest()

        configure_settings(outbound_scan_reject_score=12.5)
        self.assertIn("reject = 12.5;", rspamd.get_rspamd_files()["/etc/rspamd/local.d/settings.conf"])
        self.assertNotEqual(rspamd.get_config_digest(), digest)

        configure_settings(outbound_scan_mode="Observe")
        self.assertIn("reject = null;", rspamd.get_rspamd_files()["/etc/rspamd/local.d/settings.conf"])
        observe = rspamd.get_config_digest()
        configure_settings(outbound_scan_reject_score=20)
        self.assertEqual(rspamd.get_config_digest(), observe)  # Observe refuses at no score

    def test_the_node_records_each_scan_and_ships_it(self) -> None:
        from suite_cloud.telemetry.tests.test_outbound_scans import load_shipper

        files = rspamd.get_rspamd_files()
        recorder = files[rspamd.RECORDER_PATH]
        self.assertIn("type = 'idempotent'", recorder)  # runs after the score, refused mail too
        self.assertIn(f'local SPOOL = "{load_shipper().SPOOL}"', recorder)
        self.assertEqual(
            files[rspamd.SHIPPER_PATH], (rspamd.SOURCE_DIRECTORY / "ship_outbound_scans.py").read_text()
        )
        service = files["/etc/systemd/system/suite-cloud-scans.service"]
        self.assertIn(f"Environment=SUITE_CLOUD_SCANS_URL={telemetry.api_url('outbound_scans')}", service)
        self.assertIn("EnvironmentFile=-/etc/stalwart/stalwart.env", service)  # the node's token
        self.assertIn(f"ExecStart=/usr/bin/python3 {rspamd.SHIPPER_PATH}", service)
        self.assertIn("OnUnitActiveSec=1min", files["/etc/systemd/system/suite-cloud-scans.timer"])

        names = playbook_task_names("install-rspamd.yml")
        self.assertLess(
            names.index("Create the configuration directories"), names.index("Write the Rspamd configuration")
        )
        self.assertLess(
            names.index("Reload systemd with the scan shipper"), names.index("Start the scan shipper timer")
        )

    def test_only_a_node_installs_rspamd(self) -> None:
        variables = bootstrap.build_node_variables({"node": self.node.name})
        self.assertTrue(variables["install_rspamd"])
        self.assertEqual(variables["rspamd_files"], rspamd.get_rspamd_files())
        self.assertEqual(variables["rspamd_digest"], rspamd.get_config_digest())
        self.assertEqual(variables["rspamd_version"], "")

        gateway = frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "cluster": self.cluster.name,
                "ipv4_address": "203.0.113.50",
                "ssh_host_keys": "203.0.113.50 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTests",
            }
        ).insert()
        self.assertNotIn("install_rspamd", egress.build_gateway_variables({"gateway": gateway.name}))

        for playbook in ("bootstrap-cluster.yml", "configure-node.yml", "upgrade-stalwart.yml"):
            with self.subTest(playbook):
                names = playbook_task_names(playbook)
                self.assertLess(
                    names.index("Check that Unbound validates DNSSEC"), names.index("Install Rspamd")
                )
        names = playbook_task_names("install-rspamd.yml")
        self.assertLess(
            names.index("Write the Rspamd configuration"), names.index("Check the Rspamd configuration")
        )

    def test_install_jobs(self) -> None:
        with patch(EXECUTE):
            name = self.node.install_outbound_scanner()
            jobs = self.cluster.install_outbound_scanners()
        job = frappe.get_doc("Server Job", name)
        self.assertEqual(
            (job.playbook, job.callback, job.server),
            ("install-rspamd.yml", "after_install_outbound_scanner", self.node.name),
        )
        self.assertEqual(len(jobs), 1)  # only the running node
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertRaises(frappe.PermissionError, self.node.install_outbound_scanner)

    def test_the_last_scanner_switches_the_milter_on(self) -> None:
        other = make_running_node(self.cluster, ipv4="203.0.113.11")
        job = frappe._dict(variables=json.dumps({"rspamd_digest": "written"}))

        self.node.after_install_outbound_scanner(job)
        self.assertEqual(
            frappe.db.get_value(
                "Stalwart Node", self.node.name, ["outbound_scanner_installed", "outbound_scanner_digest"]
            ),
            (1, "written"),
        )
        self.resync.assert_not_called()

        other.after_install_outbound_scanner(job)
        self.resync.assert_called_once_with(self.cluster.name)
        other.after_install_outbound_scanner(job)
        self.resync.assert_called_once()  # once is enough

    def test_the_cluster_lists_outdated_nodes(self) -> None:
        self.assertEqual(rspamd.get_outdated_nodes(self.cluster), [self.node.name])
        self.node.db_set("outbound_scanner_digest", rspamd.get_config_digest())
        self.assertEqual(rspamd.get_outdated_nodes(self.cluster), [])
        configure_settings(outbound_scan_reject_score=10)
        self.assertEqual(rspamd.get_outdated_nodes(self.cluster), [self.node.name])
