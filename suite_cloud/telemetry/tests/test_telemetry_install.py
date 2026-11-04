import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import bootstrap, egress, server_token
from suite_cloud.mail.tests.fixtures import configure_settings, make_cluster, make_node
from suite_cloud.provisioning.ansible import playbook_task_names
from suite_cloud.telemetry import install, status
from suite_cloud.utils import clear_config_cache

EXECUTE = "suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"


class TestTelemetryInstall(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        self.addCleanup(lambda: setattr(frappe.flags, "do_not_enqueue", False))
        configure_settings()
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 1
        settings.public_url = "https://cloud.example.test"
        settings.save()  # queues a roll-out at most, which runs after a commit a test never makes
        clear_config_cache()
        self.cluster = make_cluster()
        self.node = make_node(self.cluster)
        self.gateway = frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "cluster": self.cluster.name,
                "ipv4_address": "203.0.113.50",
                "ssh_host_keys": "203.0.113.50 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTests",
            }
        ).insert()
        self.addCleanup(
            lambda: frappe.delete_doc(
                "Egress Gateway", self.gateway.name, force=True, ignore_permissions=True, ignore_on_trash=True
            )
        )

    def test_node_and_gateway_jobs_carry_the_token_as_a_secret(self) -> None:
        node = bootstrap.build_node_variables({"node": self.node.name})
        gateway = egress.build_gateway_variables({"gateway": self.gateway.name})
        for server, variables in ((self.node, node), (self.gateway, gateway)):
            self.assertIn("env_normal", variables["__secret_keys__"])
            token = server_token.get_token(server)
            self.assertIn(f"SUITE_CLOUD_TOKEN={token}", variables["env_normal"])
            self.assertIn(token, variables["__secret_values__"])
            self.assertFalse(variables["restart_stalwart"])
            self.assertFalse({key for key in variables if key.startswith("agent_")})
        self.assertTrue(install.token_variables({"restart_stalwart": True})["restart_stalwart"])

        upgrade = playbook_task_names("upgrade-stalwart.yml")
        self.assertLess(upgrade.index("Write the runtime environment"), upgrade.index("Restart Stalwart"))
        self.assertNotIn("Install the agent binary", playbook_task_names("configure-node.yml"))

    def test_the_install_job_removes_the_retired_agent_first(self) -> None:
        names = playbook_task_names("install-token.yml")
        self.assertEqual(
            names[:3],
            ["Stop the retired agent", "Delete the retired agent", "Reload systemd without the agent"],
        )
        self.assertLess(
            names.index("Reload systemd without the agent"), names.index("Write the runtime environment")
        )

    def test_install_token_jobs(self) -> None:
        with patch(EXECUTE):
            job = install.install_token(self.node)
        self.assertEqual(
            (job.playbook, job.callback, job.server),
            ("install-token.yml", "after_install_token", self.node.name),
        )
        self.assertEqual(job.title, f"Install Suite Cloud token on {self.node.hostname}")
        self.assertEqual(frappe.db.get_value("Stalwart Node", self.node.name, "status"), self.node.status)
        self.assertFalse(bootstrap.build_node_variables(json.loads(job.context))["restart_stalwart"])

        with patch(EXECUTE):
            name = self.node.install_suite_cloud_token()
        self.assertEqual(frappe.db.get_value("Server Job", name, "callback"), "after_install_token")
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertRaises(frappe.PermissionError, self.node.install_suite_cloud_token)

    def test_the_last_token_switches_the_push_on(self) -> None:
        self.cluster.db_set("status", "Active")
        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        cluster_push = (
            "suite_cloud.mail.doctype.stalwart_cluster.stalwart_cluster.StalwartCluster.push_config"
        )
        gateway_push = "suite_cloud.mail.doctype.egress_gateway.egress_gateway.EgressGateway.push_config"

        def titles(names: list[str]) -> set[str]:
            return {frappe.db.get_value("Server Job", name, "title") for name in names}

        with patch(EXECUTE), patch(cluster_push) as pushed, patch(gateway_push) as gateway_pushed:
            # Install Suite Cloud Tokens: every server gets its token.
            jobs = install.install_tokens(self.cluster)
            self.assertEqual(
                titles(jobs),
                {f"Install Suite Cloud token on {s.hostname}" for s in (self.node, self.gateway)},
            )
            pushed.assert_not_called()

            before = set(frappe.get_all("Server Job", pluck="name"))
            self.node.after_install_token(frappe._dict())
            self.assertTrue(
                frappe.db.get_value("Stalwart Node", self.node.name, "suite_cloud_token_installed")
            )
            pushed.assert_called_once()
            started = set(frappe.get_all("Server Job", pluck="name")) - before
            self.assertEqual(titles(list(started)), {f"Start telemetry push on {self.node.hostname}"})
            job = frappe.get_doc("Server Job", started.pop())
            self.assertTrue(bootstrap.build_node_variables(json.loads(job.context))["restart_stalwart"])

            # A second token for the same cluster changes nothing; the gateway's own switches its push.
            self.node.after_install_token(frappe._dict())
            pushed.assert_called_once()
            self.gateway.after_install_token(frappe._dict())
            gateway_pushed.assert_called_once()

            # Once all have it, the action pushes and restarts again, which also retries a failure.
            jobs = self.cluster.install_suite_cloud_tokens()
            self.assertEqual(
                titles(jobs), {f"Start telemetry push on {s.hostname}" for s in (self.node, self.gateway)}
            )

    def test_a_rotated_token_is_written_by_a_job(self) -> None:
        token = server_token.get_token(self.node)
        self.assertIsNone(self.node.rotate_suite_cloud_token())  # not running: provisioning writes it
        self.node.db_set("status", "Active")
        with patch(EXECUTE):
            job = self.node.rotate_suite_cloud_token()
        node = frappe.get_doc("Stalwart Node", self.node.name)
        self.assertNotEqual(server_token.get_token(node), token)
        self.assertEqual(frappe.db.get_value("Server Job", job, "playbook"), "install-token.yml")
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertRaises(frappe.PermissionError, self.gateway.rotate_suite_cloud_token)

    def test_the_last_token_pushes_the_hook_on_with_telemetry_off(self) -> None:
        self.cluster.db_set("status", "Active")
        self.node.db_set("status", "Active")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        settings.save()
        clear_config_cache()
        cluster_push = (
            "suite_cloud.mail.doctype.stalwart_cluster.stalwart_cluster.StalwartCluster.push_config"
        )
        before = set(frappe.get_all("Server Job", pluck="name"))
        with patch(EXECUTE), patch(cluster_push) as pushed:
            self.node.after_install_token(frappe._dict())
        pushed.assert_called_once()  # the sending-limit hook needs it whatever telemetry is set to
        self.assertEqual(set(frappe.get_all("Server Job", pluck="name")) - before, set())  # no restart

    def test_the_patch_renames_the_token(self) -> None:
        from suite_cloud.patches.v1_0 import rename_suite_cloud_token

        with patch(EXECUTE):
            job = install.token_job(self.node)
        job.db_set("callback", "after_install_telemetry")  # a job queued before the deploy
        frappe.db.sql(
            "insert into `__Auth` (doctype, name, fieldname, password, encrypted) values (%s, %s, %s, %s, 1)",
            ("Stalwart Cluster", self.cluster.name, "quota_hook_token", "x"),
        )
        with patch.object(rename_suite_cloud_token, "rename_field") as rename:
            rename_suite_cloud_token.execute()
        renamed = {call.args for call in rename.call_args_list}
        self.assertIn(("Stalwart Node", "telemetry_secret", "suite_cloud_secret"), renamed)
        self.assertIn(("Egress Gateway", "telemetry_token_installed", "suite_cloud_token_installed"), renamed)
        self.assertIn(
            ("Suite Cloud Settings", "agent_requests_per_minute", "server_requests_per_minute"), renamed
        )
        self.assertEqual(len(renamed), 7)
        self.assertEqual(frappe.db.get_value("Server Job", job.name, "callback"), "after_install_token")
        self.assertFalse(frappe.db.exists("__Auth", {"fieldname": "quota_hook_token"}))

    def test_a_settings_change_is_rolled_out_once(self) -> None:
        self.cluster.db_set("status", "Active")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        settings.save()
        clear_config_cache()
        with patch.object(install.push, "resync_cluster_after_commit") as resync:
            install.roll_out_telemetry()
            # Off: each active cluster gets a push that switches the push off.
            resync.assert_any_call(self.cluster.name)
            # A second job queued for the same change, say by a quick second turn, finds it done.
            resync.reset_mock()
            self.assertEqual(install.roll_out_telemetry(), [])
            resync.assert_not_called()

    def test_the_settings_save_leaves_the_roll_out_to_a_background_job(self) -> None:
        # Even with do_not_enqueue on, as in setUp: the roll-out reaches every server of the site.
        before = frappe.get_all("Server Job", pluck="name")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        with patch("frappe.enqueue") as enqueue:
            settings.save()
        enqueue.assert_called_once_with(install.roll_out_telemetry, queue="long", enqueue_after_commit=True)
        self.assertEqual(frappe.get_all("Server Job", {"name": ["not in", before]}), [])

    def test_turning_telemetry_on_installs_every_token(self) -> None:
        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        draining = make_node(self.cluster, ipv4="203.0.113.12")
        draining.db_set("status", "Draining")
        make_node(self.cluster, ipv4="203.0.113.13").db_set("status", "Provisioned")
        servers = (self.node, draining, self.gateway)
        settings = frappe.get_single("Suite Cloud Settings")

        # Only this cluster's servers: the site may hold clusters of its own.
        ours = [
            name
            for doctype in ("Stalwart Node", "Egress Gateway")
            for name in frappe.get_all(doctype, {"cluster": self.cluster.name}, pluck="name")
        ]

        def jobs_after(enabled: bool) -> list[dict]:
            before = frappe.get_all("Server Job", pluck="name")
            settings.reload()
            settings.enable_telemetry = int(enabled)
            with patch("frappe.enqueue") as enqueue:
                settings.save()
                # The save only queues the roll-out; it runs here as a worker would, without playbooks.
                if any(call.args[:1] == (install.roll_out_telemetry,) for call in enqueue.call_args_list):
                    with patch(EXECUTE), patch.object(install.push, "resync_cluster_after_commit"):
                        install.roll_out_telemetry()
            filters = {"name": ["not in", before], "server": ["in", ours]}
            return frappe.get_all("Server Job", filters, ["server", "title"])

        self.assertEqual(jobs_after(False), [])  # off only pushes the plans
        jobs = jobs_after(True)
        self.assertEqual({job.server for job in jobs}, {s.name for s in servers})
        self.assertEqual({job.title.split(" on ")[0] for job in jobs}, {"Install Suite Cloud token"})

        # A save that leaves the switch as it is changes nothing.
        self.assertEqual(jobs_after(True), [])

    def test_the_status_follows_the_last_batch(self) -> None:
        status.forget_contacts()
        self.addCleanup(status.forget_contacts)
        self.assertEqual(status.server_status(self.node)["state"], "unknown")

        status.record_contact(self.node.ipv4_address)
        status.record_contact(self.gateway.ipv4_address)
        self.assertEqual(status.server_status(self.gateway)["state"], "ok")

        stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2).isoformat()
        frappe.cache.hset(status.CACHE_KEY, self.node.ipv4_address, {"seen_at": stale})
        self.assertEqual(status.server_status(self.node)["state"], "silent")

        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        summary = status.cluster_summary(self.cluster)
        self.assertEqual(summary["state"], "silent")
        self.assertEqual(summary["message"], f"Telemetry: 1 reporting, 1 silent ({self.node.hostname}).")
        self.assertEqual(status.get_server_status("Stalwart Node", self.node.name)["state"], "silent")
        self.assertEqual(status.get_cluster_summary(self.cluster.name)["state"], "silent")
        self.assertRaises(
            frappe.ValidationError, status.get_server_status, "Stalwart Cluster", self.cluster.name
        )
