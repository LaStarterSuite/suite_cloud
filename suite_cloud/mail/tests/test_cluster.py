import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import dns, plan
from suite_cloud.mail.stalwart.errors import StalwartUnavailableError
from suite_cloud.mail.tests.fixtures import (
    ROOT_DOMAIN,
    configure_settings,
    make_cluster,
    make_mail_region,
    make_node,
    make_owner,
    make_store,
    make_zone,
    no_dns_provider,
    remove_cluster,
)
from suite_cloud.utils import user_context


class TestStalwartCluster(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_a_config_push_waits_for_the_one_running(self) -> None:
        from suite_cloud.mail.doctype.stalwart_cluster import stalwart_cluster

        cluster = make_cluster()
        held = frappe.cache.lock(f"{frappe.local.site}:suite_cloud:push-config:{cluster.name}", timeout=30)
        self.assertTrue(held.acquire(blocking=False))
        self.addCleanup(held.release)
        # A push gives up after its wait, before planning anything; Sync Config waits briefly.
        with patch.object(plan, "cluster_plan") as planned:
            self.assertRaisesRegex(
                frappe.ValidationError, "Another configuration push", cluster.push_config, wait=0.1
            )
        planned.assert_not_called()
        self.assertLess(stalwart_cluster.SYNC_CONFIG_WAIT, stalwart_cluster.PUSH_TIMEOUT)

    def test_a_queued_push_locks_before_it_reads_and_keeps_its_failure(self) -> None:
        from suite_cloud.mail.cluster import push
        from suite_cloud.mail.doctype.stalwart_cluster import stalwart_cluster

        cluster = make_cluster()
        cluster.db_set("status", "Active")
        steps = []

        @contextmanager
        def recording_lock(name, wait=stalwart_cluster.PUSH_TIMEOUT):
            steps.append("lock")
            yield

        load = frappe.get_doc
        with (
            patch.object(stalwart_cluster, "push_lock", recording_lock),
            patch("frappe.get_doc", side_effect=lambda *args: steps.append("read") or load(*args)),
            patch.object(plan, "cluster_plan", side_effect=StalwartUnavailableError("cluster down")),
        ):
            self.assertRaises(StalwartUnavailableError, push.resync_cluster_job, cluster.name)
        self.assertEqual(steps[:2], ["lock", "read"])
        self.assertIn(
            "cluster down", frappe.db.get_value("Stalwart Cluster", cluster.name, "last_config_sync_error")
        )

        # The next push that goes through clears it.
        with patch.object(plan, "cluster_plan", return_value=[]), patch.object(type(cluster), "get_client"):
            frappe.get_doc("Stalwart Cluster", cluster.name).push_config()
        self.assertIsNone(frappe.db.get_value("Stalwart Cluster", cluster.name, "last_config_sync_error"))

    def test_a_push_that_outlived_its_lock_is_logged_not_raised(self) -> None:
        from suite_cloud.mail.doctype.stalwart_cluster import stalwart_cluster

        lock = frappe.cache.lock(f"{frappe.local.site}:suite_cloud:test-expired-lock", timeout=30)
        self.assertTrue(lock.acquire(blocking=False))
        lock.redis.delete(lock.name)  # as if the push outlived the lock's timeout
        with patch.object(stalwart_cluster, "log_error") as log_error:
            stalwart_cluster.release_push_lock(lock, "c1")
        log_error.assert_called_once()

    def test_desk_changes_queue_one_waiting_push_per_cluster(self) -> None:
        from suite_cloud.mail.cluster import push

        frappe.flags.do_not_enqueue = False
        self.addCleanup(frappe.cache.delete, push.pending_push_key("c1"))
        frappe.cache.delete(push.pending_push_key("c1"))
        queued = len(frappe.db.after_commit)
        push.resync_cluster_after_commit("c1")
        self.assertEqual(len(frappe.db.after_commit), queued + 1)  # only once the save commits
        frappe.db.after_commit.cut(queued)

        with patch("frappe.enqueue") as enqueue:
            push.queue_cluster_push("c1")
            push.queue_cluster_push("c1")  # a push still waiting covers this change too
        enqueue.assert_called_once_with(push.resync_cluster_job, cluster="c1", queue="long")

        frappe.cache.delete(push.pending_push_key("c1"))  # what the job does when it starts
        with patch("frappe.enqueue") as enqueue:
            push.queue_cluster_push("c1")
        enqueue.assert_called_once()

    def test_tests_reach_only_test_hosts(self) -> None:
        from suite_cloud.mail.stalwart import get_client
        from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

        cluster = make_cluster()
        self.assertIn(".test", cluster.base_url)
        cluster.base_url = "https://mail.c1.frappe.email"
        self.assertRaisesRegex(frappe.ValidationError, "only reach .test hosts", get_client, cluster)

        node = make_node(cluster)
        node.db_set("hostname", "n9.c9.frappe.email")
        with patch("suite_cloud.provisioning.ansible.PlaybookRun.run") as run:
            self.assertRaisesRegex(
                frappe.ValidationError,
                "only reach .test hosts",
                create_server_job,
                node,
                "install-agent.yml",
                title="Install agent",
            )
        run.assert_not_called()

    def test_connections_pin_the_host_key_recorded_at_first_contact(self) -> None:
        from suite_cloud.provisioning.ssh import SSHTarget, UnknownHostError, inventory_line, known_hosts_file

        target = SSHTarget("203.0.113.1", "root", 22, "k", host_keys="203.0.113.1 ssh-ed25519 AAAAtest")
        with known_hosts_file(target) as path:
            self.assertEqual(open(path).read(), "203.0.113.1 ssh-ed25519 AAAAtest\n")
            line = inventory_line("n1", target, "/k", path)
        self.assertIn("StrictHostKeyChecking=yes", line)
        self.assertIn(f"UserKnownHostsFile={path}", line)
        self.assertNotIn("/dev/null", line)
        # Never verified: nothing to check the server against, so no connection at all.
        with self.assertRaises(UnknownHostError), known_hosts_file(SSHTarget("203.0.113.1", "root", 22, "k")):
            pass

    def test_reset_host_keys_lets_verify_ssh_record_the_new_ones(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.1")
        node.db_set("ssh_verified", 1)

        # A reinstalled server presents new keys; the old pin must go before it can be trusted.
        node.reset_ssh_host_keys()
        node.reload()
        self.assertEqual((node.ssh_host_keys, node.ssh_verified), (None, 0))
        audit = {"reference_doctype": "Stalwart Node", "reference_name": node.name, "comment_type": "Info"}
        self.assertEqual(frappe.get_all("Comment", audit, pluck="comment_email"), [frappe.session.user])

        module = "suite_cloud.mail.doctype.stalwart_node.stalwart_node"
        with (
            patch(f"{module}.scan_host_keys", return_value="203.0.113.1 ssh-ed25519 AAAAnew"),
            patch(f"{module}.ping", return_value=(True, "")),
        ):
            self.assertTrue(node.verify_ssh())
        node.reload()
        self.assertEqual((node.ssh_host_keys, node.ssh_verified), ("203.0.113.1 ssh-ed25519 AAAAnew", 1))

    def test_only_managers_can_reset_host_keys(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.1")

        # Dropping the pin lets the next Verify SSH trust any server, so other users are refused.
        with user_context(make_owner()), self.assertRaises(frappe.PermissionError):
            node.reset_ssh_host_keys()
        self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "ssh_host_keys"), node.ssh_host_keys)

    def test_ssh_user_is_a_plain_login_name(self) -> None:
        cluster = make_cluster()
        cluster.ssh_user = "root ansible_connection=local"
        self.assertRaisesRegex(frappe.ValidationError, "plain login name", cluster.save)
        cluster.reload()
        cluster.ssh_user = "deploy-user"
        cluster.save()
        from suite_cloud.provisioning.ssh import SSHTarget, inventory_line

        self.assertRaises(
            ValueError, inventory_line, "n1", SSHTarget("203.0.113.1", "root\nx", 22, "k"), "/k", "/kh"
        )

    def test_cluster_derives_zone_url_and_coordinator(self) -> None:
        cluster = make_cluster()

        self.assertEqual(cluster.default_domain, f"mail.blr.{ROOT_DOMAIN}")
        self.assertEqual(cluster.base_url, f"https://mail.blr.{ROOT_DOMAIN}")
        self.assertEqual(cluster.coordinator, "Default")
        self.assertEqual(cluster.status, "Pending")
        self.assertTrue(cluster.ssh_public_key.startswith("ssh-ed25519 "))
        self.assertEqual(len(cluster.get_password("admin_password")), 32)
        self.assertEqual(
            cluster.stalwart_version, frappe.db.get_single_value("Suite Cloud Settings", "stalwart_version")
        )

    def test_the_region_names_the_cluster_and_its_hosts(self) -> None:
        bad = frappe.get_doc({"doctype": "Mail Region", "code": "Bad Code!", "title": "Bad"})
        self.assertRaisesRegex(frappe.ValidationError, "Code must be", bad.insert)
        make_mail_region("bom", "Mumbai")
        for label in ("bom", "bom-2", "bom-3"):
            remove_cluster(f"mail.{label}.{ROOT_DOMAIN}")

        store = make_store("Data", "PostgreSql", host="db", auth_secret="x")
        new = {"doctype": "Stalwart Cluster", "region": "bom", "data_store": store.name}
        first = frappe.get_doc(new).insert()
        second = frappe.get_doc(new).insert()
        self.assertEqual(
            [(c.label, c.name, c.default_domain, c.title) for c in (first, second)],
            [
                ("bom", f"mail.bom.{ROOT_DOMAIN}", f"mail.bom.{ROOT_DOMAIN}", "Mumbai"),
                ("bom-2", f"mail.bom-2.{ROOT_DOMAIN}", f"mail.bom-2.{ROOT_DOMAIN}", "Mumbai 2"),
            ],
        )
        self.assertEqual(make_node(first).name, f"n1.mail.bom.{ROOT_DOMAIN}")

        # The next cluster counts past the highest number still in the zone.
        remove_cluster(first.name)
        third = frappe.get_doc(new).insert()
        self.assertEqual((third.label, third.title), ("bom-3", "Mumbai 3"))
        remove_cluster(second.name)
        remove_cluster(third.name)

    def test_there_is_always_exactly_one_default_cluster(self) -> None:
        frappe.db.set_value("Stalwart Cluster", {"is_default": 1}, "is_default", 0)  # rolled back
        first = make_cluster("first", hostname=f"mail.first.{ROOT_DOMAIN}")
        second = make_cluster("second", hostname=f"mail.second.{ROOT_DOMAIN}")
        self.assertEqual((first.is_default, second.is_default), (1, 0))

        first.is_default = 0
        self.assertRaisesRegex(frappe.ValidationError, "Mark another cluster", first.save)

        second.is_default = 1
        second.save()
        self.assertEqual(frappe.get_all("Stalwart Cluster", {"is_default": 1}, pluck="name"), [second.name])

    def test_clusters_without_a_default_get_the_oldest_enabled_one(self) -> None:
        from suite_cloud.patches.v1_0 import mark_default_cluster

        older = make_cluster("older", hostname=f"mail.older.{ROOT_DOMAIN}")
        newer = make_cluster("newer", hostname=f"mail.newer.{ROOT_DOMAIN}")
        # A site from before the rule: no default, and only these two enabled (rolled back).
        cluster = frappe.qb.DocType("Stalwart Cluster")
        frappe.qb.update(cluster).set(cluster.is_default, 0).set(cluster.enabled, 0).run()
        frappe.db.set_value("Stalwart Cluster", {"name": ["in", [older.name, newer.name]]}, "enabled", 1)

        mark_default_cluster.execute()

        self.assertEqual(frappe.get_all("Stalwart Cluster", {"is_default": 1}, pluck="name"), [older.name])

    def test_a_disabled_default_gives_way_to_an_enabled_cluster(self) -> None:
        from suite_cloud.patches.v1_0 import mark_default_cluster

        disabled = make_cluster("disabled", hostname=f"mail.disabled.{ROOT_DOMAIN}")
        enabled = make_cluster("enabled", hostname=f"mail.enabled.{ROOT_DOMAIN}")
        # Allowed before the rule: a disabled default (rolled back).
        cluster = frappe.qb.DocType("Stalwart Cluster")
        frappe.qb.update(cluster).set(cluster.is_default, 0).set(cluster.enabled, 0).run()
        frappe.db.set_value("Stalwart Cluster", disabled.name, "is_default", 1)
        frappe.db.set_value("Stalwart Cluster", enabled.name, "enabled", 1)

        mark_default_cluster.execute()

        self.assertEqual(frappe.get_all("Stalwart Cluster", {"is_default": 1}, pluck="name"), [enabled.name])

    def test_the_default_cluster_cannot_be_disabled_or_deleted(self) -> None:
        cluster = make_cluster()
        cluster.reload()
        cluster.is_default = 1
        cluster.save()
        other = make_cluster("other", hostname=f"mail.other.{ROOT_DOMAIN}")

        cluster.enabled = 0
        self.assertRaisesRegex(frappe.ValidationError, "cannot be disabled", cluster.save)
        cluster.reload()
        self.assertRaisesRegex(frappe.ValidationError, "before deleting", cluster.delete)
        other.enabled = 0
        other.save()  # any other cluster may be

    def test_store_kind_is_enforced(self) -> None:
        blob = make_store("Blob", "S3", region="us-east-1", bucket="b", access_key="a", secret_key="s")
        cluster = frappe.get_doc(
            {
                "doctype": "Stalwart Cluster",
                "region": make_mail_region("kind"),
                "data_store": blob.name,
            }
        )
        self.assertRaisesRegex(frappe.ValidationError, "Data store", cluster.insert)

    def test_s3_store_names_its_region_or_carries_its_endpoint(self) -> None:
        bucket = {"bucket": "mail", "access_key": "AK", "secret_key": "SK"}

        # Stalwart takes the region, key id and secret as tagged unions, and a bare string for
        # none of them (checked on v0.16.20). On AWS the region is one it names.
        aws = make_store("Blob", "S3", region="ap-south-1", **bucket)
        self.assertEqual(
            aws.config,
            {
                "@type": "S3",
                "region": {"@type": "ApSouth1"},
                "bucket": "mail",
                "accessKey": {"@type": "Value", "value": "AK"},
                "secretKey": {"@type": "Value", "secret": "SK"},
                "timeout": 15000,
                "maxRetries": 3,
                "verifyAfterWrite": True,
            },
        )

        # An S3-compatible service is a Custom region holding the endpoint: the store itself has
        # no endpoint property, and Stalwart refuses one.
        endpoint = "https://s3.fr-par.scw.cloud"
        compatible = make_store("Blob", "S3", region="fr-par", endpoint=endpoint, **bucket)
        self.assertEqual(
            compatible.config["region"],
            {"@type": "Custom", "customEndpoint": endpoint, "customRegion": "fr-par"},
        )
        self.assertNotIn("endpoint", compatible.config)

        # A region Stalwart does not name cannot be sent without its endpoint.
        unnamed = {"region": "ap-south-2", **bucket}
        self.assertRaisesRegex(
            frappe.ValidationError, "no built-in endpoint", make_store, "Blob", "S3", **unnamed
        )
        make_store("Blob", "S3", endpoint="https://s3.ap-south-2.amazonaws.com", **unnamed)
        self.assertRaisesRegex(
            frappe.ValidationError, "Region is required", make_store, "Blob", "S3", **bucket
        )

    def test_embedded_stores_pin_the_cluster_to_one_full_node(self) -> None:
        cluster = make_cluster(name="solo", hostname=f"mail.solo.{ROOT_DOMAIN}", multi_node=False)
        self.assertEqual(cluster.single_node, 1)
        self.assertEqual(cluster.coordinator, "Disabled")
        self.assertRaisesRegex(
            frappe.ValidationError, "full role", make_node, cluster, "203.0.113.1", role="frontend"
        )
        make_node(cluster, "203.0.113.1")

        self.assertRaisesRegex(frappe.ValidationError, "one node", make_node, cluster, "203.0.113.2")

        # Redis on a single node is allowed but coordinates nothing.
        redis = make_store("In-Memory", "Redis", title="solo redis", url="redis://redis.example.test:6379")
        cluster.reload()  # the node insert re-saved it
        cluster.in_memory_store = redis.name
        cluster.save()
        self.assertEqual(cluster.coordinator, "Disabled")

    def test_enabling_a_second_node_needs_a_redis_in_memory_store(self) -> None:
        # Shared stores but no Redis: one node is fine, a second must not start counting on its own.
        cluster = make_cluster(name="pair", hostname=f"mail.pair.{ROOT_DOMAIN}", in_memory_store=None)
        make_node(cluster, "203.0.113.1")
        frappe.db.savepoint("second_node")
        self.assertRaisesRegex(frappe.ValidationError, "needs a Redis", make_node, cluster, "203.0.113.2")
        frappe.db.rollback(save_point="second_node")  # as the refused request would

        second = make_node(cluster, "203.0.113.2", enabled=0)
        second.enabled = 1
        self.assertRaisesRegex(frappe.ValidationError, "needs a Redis", second.save)

    def test_node_hostnames_are_handed_out_in_order(self) -> None:
        cluster = make_cluster()
        first = make_node(cluster, "203.0.113.1")
        second = make_node(cluster, "203.0.113.2")
        self.assertEqual(
            (first.name, second.hostname), (f"n1.{cluster.default_domain}", f"n2.{cluster.default_domain}")
        )

        # A typed hostname is ignored, and numbers are never handed out twice while a node holds them.
        frappe.delete_doc(
            "Stalwart Node", first.name, force=True, ignore_permissions=True, ignore_on_trash=True
        )
        third = make_node(cluster, "203.0.113.3", hostname=f"custom.{cluster.default_domain}")
        self.assertEqual(third.hostname, f"n3.{cluster.default_domain}")

    def test_node_creates_its_dns_records_and_spf_tracks_ips(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.10", ipv6_address="2001:db8::10")

        records = frappe.get_all(
            "DNS Record", {"managed_by": node.name}, ["host", "type", "value", "category"], order_by="type"
        )
        self.assertEqual(
            [(r.host, r.type, r.value, r.category) for r in records],
            [("n1.mail.blr", "A", "203.0.113.10", "Node"), ("n1.mail.blr", "AAAA", "2001:db8::10", "Node")],
        )
        # Not yet in ingress: the cluster hostname does not point at a pending node.
        self.assertFalse(
            frappe.db.exists(
                "DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mail.blr", "managed_by": node.name}
            )
        )

        node.db_set("status", "Active")
        dns.sync_node_records(node, include_ingress=True)
        dns.sync_spf_record(cluster)
        addresses = {"dns_zone": ROOT_DOMAIN, "host": "mail.blr", "type": ["in", ["A", "AAAA"]]}
        self.assertEqual(
            frappe.get_all("DNS Record", addresses, pluck="value", order_by="type"),
            ["203.0.113.10", "2001:db8::10"],
        )
        spf = frappe.db.get_value(
            "DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf.mail.blr", "type": "TXT"}, "value"
        )
        self.assertEqual(spf, "v=spf1 ip4:203.0.113.10 ip6:2001:db8::10 -all")
        # The default domain is the hostname, so its own SPF sits beside the ingress addresses.
        zone_spf = frappe.db.get_value(
            "DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mail.blr", "type": "TXT"}, "value"
        )
        self.assertEqual(zone_spf, f"v=spf1 include:spf.mail.blr.{ROOT_DOMAIN} -all")

        dns.sync_node_records(node, include_ingress=False)
        self.assertFalse(frappe.db.exists("DNS Record", addresses))
        self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "in_ingress_dns"), 0)

    def test_removing_a_node_updates_spf_and_frees_a_failed_bootstrap(self) -> None:
        from suite_cloud.mail.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.10")
        node.db_set({"status": "Active", "is_bootstrap_node": 1})
        dns.sync_spf_record(cluster)
        self.assertIn(
            "ip4:203.0.113.10",
            frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf.mail.blr"}, "value"),
        )

        bootstrap.drain_node(node)  # Draining nodes still send
        self.assertIn(
            "ip4:203.0.113.10",
            frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf.mail.blr"}, "value"),
        )
        node.db_set("status", "Failed")
        cluster.db_set({"status": "Failed", "bootstrap_node": node.name})

        frappe.get_doc("Stalwart Node", node.name).delete()

        self.assertEqual(
            frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf.mail.blr"}, "value"),
            "v=spf1 -all",
        )
        self.assertEqual(
            frappe.db.get_value("Stalwart Cluster", cluster.name, ["status", "bootstrap_node"]),
            ("Pending", None),
        )

    def test_active_nodes_are_not_failed_by_transient_checks(self) -> None:
        from suite_cloud.mail.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.10")
        node.db_set({"status": "Active", "provisioned_at": frappe.utils.add_to_date(None, hours=-5)})
        self.assertFalse(bootstrap._not_ready(node, "registry hiccup"))
        self.assertEqual(
            frappe.db.get_value("Stalwart Node", node.name, ["status", "last_error"]),
            ("Active", "registry hiccup"),
        )

        node.db_set("status", "Provisioned")
        self.assertFalse(bootstrap._not_ready(node, "still down"))
        self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "status"), "Failed")

    def test_dns_records_may_be_wanted_by_two_owners(self) -> None:
        from suite_cloud.suite_cloud.doctype.dns_record.dns_record import reconcile_managed_records

        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.10")
        wanted = [
            {
                "dns_zone": ROOT_DOMAIN,
                "host": "n1.mail.blr",
                "type": "A",
                "value": "203.0.113.10",
                "category": "Egress",
            }
        ]
        reconcile_managed_records("Stalwart Cluster", cluster.name, wanted)
        wanted_filters = {"dns_zone": ROOT_DOMAIN, "host": "n1.mail.blr", "type": "A"}
        self.assertEqual(frappe.db.count("DNS Record", wanted_filters), 2)

        with patch("suite_cloud.suite_cloud.doctype.dns_record.dns_record.get_dns_provider") as provider:
            provider.return_value.delete_dns_record.return_value = True
            reconcile_managed_records("Stalwart Cluster", cluster.name, [])
            provider.return_value.delete_dns_record.assert_not_called()  # the node still wants it
            frappe.get_doc("Stalwart Node", node.name).delete()  # Pending nodes delete normally
        self.assertFalse(frappe.db.exists("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "n1.mail.blr"}))

    def test_finish_bootstrap_and_key_rotation_through_the_fake(self) -> None:
        from suite_cloud.mail.cluster import bootstrap
        from suite_cloud.mail.stalwart import forget_sessions
        from suite_cloud.mail.tests.fake_stalwart import FakeStalwart
        from suite_cloud.mail.tests.fixtures import clear_request_cache

        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.10")
        fake = FakeStalwart(base_url=cluster.base_url, admin_password=cluster.get_password("admin_password"))
        fake.objects["Certificate"]["cert1"] = {
            "id": "cert1",
            "subjectAlternativeNames": {cluster.hostname: True},
        }
        with fake.install():
            forget_sessions(cluster)
            clear_request_cache()
            job = frappe.get_doc(
                {
                    "doctype": "Server Job",
                    "title": "x",
                    "server_doctype": "Stalwart Node",
                    "server": node.name,
                    "playbook": "run-commands.yml",
                }
            )
            with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.enqueue"):
                job.insert()
            bootstrap.provision_node(node)
            node.reload()

            # No registry lease yet: a Redis-coordinated cluster keeps waiting (nothing fails).
            bootstrap.after_provision(node, job)
            self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Bootstrapping")

            fake.add_cluster_node(node.hostname, node_id=7)
            # The first attempt minted a key. A re-bootstrapped data store forgets it, and the stored
            # key is then replaced rather than trusted.
            first_key = frappe.get_doc("Stalwart Cluster", cluster.name).get_password("api_key")
            self.assertIn(first_key, fake.tokens)
            fake.objects["ApiKey:" + fake.admin_id].clear()
            fake.tokens.clear()
            self.assertTrue(bootstrap.finish_bootstrap(frappe.get_doc("Stalwart Cluster", cluster.name)))

            cluster.reload()
            node.reload()
            self.assertEqual((cluster.status, node.status, node.node_id), ("Active", "Active", 7))
            self.assertIn(cluster.get_password("api_key"), fake.tokens)
            # Activation pushes the full plan: the disabled-accounts role appears only now.
            self.assertTrue(fake.find("Role", description="suite-disabled"))
            # Refs resolve to ids and secrets are never echoed: neither may count as drift.
            self.assertEqual(
                frappe.get_doc("Stalwart Cluster", cluster.name).check_drift()["differences"], []
            )
            # The default domain's keys can be replaced after a leak, under the same selector.
            default = fake.find("Domain", name=cluster.default_domain)
            before = fake.find("DkimSignature", domainId=default["id"])
            frappe.get_doc("Stalwart Cluster", cluster.name).replace_dkim_keys()
            after = fake.find("DkimSignature", domainId=default["id"])
            self.assertNotEqual(before["id"], after["id"])
            self.assertEqual(after["selector"], "frappemail-rsa")
            self.assertEqual(
                fake.find("Domain", name=cluster.default_domain)["dkimManagement"]["@type"], "Automatic"
            )
            self.assertTrue(
                plan.same_value(
                    {"acmeProviderId": "x", "secret": "hidden"}, {"acmeProviderId": "#acme", "secret": "s"}
                )
            )
            self.assertFalse(plan.same_value({"a": {"0": {"x": 1}}}, {"a": {"0": {"x": 2}}}))
            self.assertTrue(
                frappe.db.exists(
                    "DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mail.blr", "managed_by": node.name}
                )
            )
            self.assertIn(cluster.get_password("api_key"), fake.tokens)
            self.assertNotEqual(cluster.get_password("api_key"), first_key)
            self.assertEqual(fake.singletons["SystemSettings"]["defaultCertificateId"], "cert1")
            self.assertEqual(len(fake.all("ApiKey:" + fake.admin_id)), 1)

            # The minted key works for management calls and rotation revokes the old one.
            old_key = cluster.get_password("api_key")
            clear_request_cache()
            self.assertEqual(cluster.get_client().cluster_nodes.find_by_hostname(node.hostname)["nodeId"], 7)
            cluster.label = "Unsaved edit"  # the form sends its unsaved edits with the call
            cluster.rotate_api_key()
            self.assertNotEqual(cluster.get_password("api_key"), old_key)
            self.assertNotEqual(
                frappe.db.get_value("Stalwart Cluster", cluster.name, "label"), "Unsaved edit"
            )
            self.assertEqual(len(fake.all("ApiKey:" + fake.admin_id)), 1)

    def test_client_falls_back_to_the_admin_password_without_an_api_key(self) -> None:
        from suite_cloud.mail.stalwart import forget_sessions, has_credentials
        from suite_cloud.mail.tests.fake_stalwart import FakeStalwart
        from suite_cloud.mail.tests.fixtures import clear_request_cache

        cluster = make_cluster()
        cluster.db_set("status", "Active")
        fake = FakeStalwart(base_url=cluster.base_url, admin_password=cluster.get_password("admin_password"))
        fake.add_cluster_node("n1.example.test", node_id=7)
        with fake.install():
            forget_sessions(cluster)
            clear_request_cache()
            self.assertIsNone(cluster.get_password("api_key", raise_exception=False))
            self.assertTrue(has_credentials(cluster))
            self.assertEqual(
                cluster.get_client().cluster_nodes.find_by_hostname("n1.example.test")["nodeId"], 7
            )

    def test_reprovisioning_the_node_of_a_single_node_cluster_bootstraps_again(self) -> None:
        from suite_cloud.mail.cluster import bootstrap

        # Its embedded store lives on the node, so the node may come back empty.
        solo = make_cluster(name="solo", hostname=f"mail.solo.{ROOT_DOMAIN}", multi_node=False)
        # A shared store outlives any node: the node only rejoins.
        shared = make_cluster()
        for cluster, ip in ((solo, "203.0.113.1"), (shared, "203.0.113.10")):
            node = make_node(cluster, ip)
            node.db_set("is_bootstrap_node", 1)
            cluster.db_set({"status": "Active", "bootstrap_node": node.name})

        target = "suite_cloud.mail.cluster.bootstrap.create_server_job"
        with patch(target) as create:
            bootstrap.provision_node(frappe.get_doc("Stalwart Node", solo.bootstrap_node))
            bootstrap.provision_node(frappe.get_doc("Stalwart Node", shared.bootstrap_node))
        self.assertEqual(
            [c.args[1] for c in create.call_args_list], ["bootstrap-cluster.yml", "configure-node.yml"]
        )
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", solo.name, "status"), "Bootstrapping")
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", shared.name, "status"), "Active")

    def test_retried_bootstrap_recovers_a_failed_cluster(self) -> None:
        from suite_cloud.mail.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.10")
        node.db_set({"is_bootstrap_node": 1, "status": "Provisioning"})
        cluster.db_set({"status": "Failed", "bootstrap_node": node.name})
        job = frappe._dict(retries=1, max_retries=1, error_log="boom")

        node.after_provision_failed(job)  # first failure keeps the cluster retryable
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Failed")

        with (
            patch("suite_cloud.mail.cluster.bootstrap.check_node", return_value=False),
            patch("suite_cloud.mail.cluster.dns.sync_node_records"),
            patch("suite_cloud.mail.cluster.dns.sync_spf_record"),
        ):
            bootstrap.after_provision(node, job)
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Bootstrapping")

        node.after_provision_failed(frappe._dict(retries=2, max_retries=1, error_log="boom"))
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Failed")

    def test_outbound_nodes_stay_out_of_ingress(self) -> None:
        from suite_cloud.mail.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "203.0.113.20", role="outbound")
        self.assertFalse(bootstrap.serves_clients(node))
        self.assertEqual(bootstrap.build_node_variables({"node": node.name})["wait_ports"], [])
        bootstrap.activate_node(node)
        self.assertFalse(
            frappe.db.exists(
                "DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mail.blr", "managed_by": node.name}
            )
        )
        self.assertIn(
            "ip4:203.0.113.20",
            frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf.mail.blr"}, "value"),
        )
        self.assertRaisesRegex(frappe.ValidationError, "must serve clients", bootstrap.provision_node, node)

    def test_metrics_password_is_backfilled_for_existing_clusters(self) -> None:
        from frappe.utils.password import get_decrypted_password

        from suite_cloud.patches.v1_0 import fill_metrics_passwords as patch

        cluster = make_cluster()
        frappe.db.delete(
            "__Auth", {"doctype": "Stalwart Cluster", "name": cluster.name, "fieldname": "metrics_password"}
        )
        self.assertIsNone(
            get_decrypted_password(
                "Stalwart Cluster", cluster.name, "metrics_password", raise_exception=False
            )
        )

        patch.execute()
        first = get_decrypted_password("Stalwart Cluster", cluster.name, "metrics_password")
        self.assertEqual(len(first), 32)
        patch.execute()
        self.assertEqual(get_decrypted_password("Stalwart Cluster", cluster.name, "metrics_password"), first)

    def test_bootstrap_and_cluster_plans(self) -> None:
        cluster = make_cluster()

        bootstrap = plan.bootstrap_plan(cluster)[0]
        self.assertEqual(bootstrap["object"], "Bootstrap")
        value = bootstrap["value"]
        self.assertEqual(value["serverHostname"], f"mail.blr.{ROOT_DOMAIN}")
        self.assertEqual(value["defaultDomain"], f"mail.blr.{ROOT_DOMAIN}")
        self.assertEqual(value["dataStore"]["@type"], "PostgreSql")
        self.assertEqual(value["dataStore"]["authSecret"], {"@type": "Value", "secret": "pg-secret"})
        self.assertEqual(value["blobStore"]["@type"], "S3")
        self.assertEqual(value["inMemoryStore"]["@type"], "Redis")
        self.assertEqual(value["searchStore"], {"@type": "Default"})
        self.assertFalse(value["requestTlsCertificate"])
        self.assertEqual((value["tracer"]["@type"], value["tracer"]["path"]), ("Log", "/var/log/stalwart"))

        operations = {op["object"]: op for op in plan.cluster_plan(cluster)}
        # Tracers have no name: matched by kind, so bootstrap's journal is turned down, not doubled.
        self.assertEqual(operations["Tracer"]["matchOn"], ["@type"])
        self.assertEqual(operations["Tracer"]["value"]["log"]["rotate"], "daily")
        self.assertEqual(operations["Tracer"]["value"]["journal"]["level"], "warn")
        # The exporter the agent scrapes, behind the cluster's own generated password.
        metrics_password = cluster.get_password("metrics_password")
        self.assertEqual(len(metrics_password), 32)
        self.assertEqual(
            operations["Metrics"]["value"]["prometheus"],
            {
                "@type": "Enabled",
                "authUsername": "telemetry",
                "authSecret": {"@type": "Value", "secret": metrics_password},
            },
        )
        self.assertIn(metrics_password, plan.secret_strings(plan.cluster_plan(cluster)))
        self.assertNotIn(metrics_password, plan.redacted(plan.cluster_plan(cluster)))
        cluster.enable_metrics = 0
        off = {op["object"]: op for op in plan.cluster_plan(cluster)}
        self.assertEqual(off["Metrics"]["value"], {"prometheus": {"@type": "Disabled"}})
        cluster.enable_metrics = 1
        self.assertEqual(operations["Coordinator"]["value"], {"@type": "Default"})
        self.assertEqual(set(operations["ClusterRole"]["value"]), {"full", "frontend", "outbound"})
        self.assertNotIn("DnsServer", operations)  # no DNS provider configured in tests
        self.assertEqual(
            operations["ClusterRole"]["value"]["frontend"]["tasks"]["taskTypes"], {"outboundMta": True}
        )
        self.assertEqual(operations["AcmeProvider"]["matchOn"], ["directory"])
        self.assertNotIn("description", operations["AcmeProvider"]["value"]["acme"])
        domain = operations["Domain"]["value"]["default"]
        self.assertEqual(
            domain["certificateManagement"]["subjectAlternativeNames"],
            {cluster.hostname: True, f"*.{cluster.default_domain}": True},
        )
        self.assertEqual(domain["dnsManagement"], {"@type": "Manual"})
        self.assertEqual(domain["dkimManagement"]["@type"], "Automatic")
        self.assertEqual(domain["dkimManagement"]["algorithms"], {"Dkim1RsaSha256": True})
        self.assertEqual(domain["dkimManagement"]["selectorTemplate"], "frappemail-{algorithm}")
        self.assertEqual(
            operations["DnsResolver"]["value"],
            {
                "@type": "Custom",
                "servers": {
                    "0": {"address": "127.0.0.1", "port": 53, "protocol": "udp"},
                    "1": {"address": "127.0.0.1", "port": 53, "protocol": "tcp"},
                },
            },
        )
        self.assertEqual(
            operations["SpamSettings"]["value"]["spamFilterRulesUrl"],
            "https://github.com/stalwartlabs/spam-filter/releases/download/v3.0.1/spam-filter-rules.json.gz",
        )
        # The first normal start names a cluster role and checks its resolver for DNSSEC, so both
        # come before it with the pin.
        self.assertEqual(
            plan.defaults_plan(),
            [operations["ClusterRole"], operations["DnsResolver"], operations["SpamSettings"]],
        )

        configure_settings(spam_filter_rules_version="")
        self.addCleanup(configure_settings)
        # Stalwart's own default: the latest release
        self.assertEqual([op["object"] for op in plan.defaults_plan()], ["ClusterRole", "DnsResolver"])

        configure_settings(sign_with_ed25519=1)
        self.addCleanup(configure_settings, sign_with_ed25519=0)
        operations = {op["object"]: op for op in plan.cluster_plan(cluster)}
        self.assertEqual(
            operations["Domain"]["value"]["default"]["dkimManagement"]["algorithms"],
            {"Dkim1Ed25519Sha256": True, "Dkim1RsaSha256": True},
        )
        self.assertEqual(
            operations["SystemSettings"]["value"]["mailExchangers"],
            {"0": {"hostname": cluster.hostname, "priority": 10}},
        )
        self.assertEqual(operations["AcmeProvider"]["value"]["acme"]["contact"], {"ops@example.test": True})
        self.assertEqual(
            operations["Role"]["value"]["disabled"]["enabledPermissions"], {"emailReceive": True}
        )

        self.assertRaisesRegex(
            ValueError,
            "label #x",
            plan.to_ndjson,
            [
                {"@type": "upsert", "object": "Domain", "value": {"x": {}}},
                {"@type": "upsert", "object": "Role", "value": {"x": {}}},
            ],
        )
        redacted = plan.redacted(plan.bootstrap_plan(cluster))
        self.assertNotIn("pg-secret", redacted)
        self.assertIn(plan.SECRET_MARKER, redacted)
        self.assertEqual(
            plan.to_ndjson(plan.cluster_plan(cluster)).count("\n"), len(plan.cluster_plan(cluster))
        )

    def test_node_env_and_config(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, role="frontend")

        self.assertEqual(
            plan.node_env(node),
            {
                "STALWART_HOSTNAME": node.hostname,
                "STALWART_PUBLIC_URL": cluster.base_url,
                "STALWART_ROLE": "frontend",
            },
        )
        recovery = plan.node_env(node, "recovery")
        self.assertEqual(recovery["STALWART_RECOVERY_MODE"], "1")
        self.assertTrue(recovery["STALWART_RECOVERY_ADMIN"].startswith("admin:"))
        self.assertNotIn("STALWART_RECOVERY_MODE", plan.node_env(node, "bootstrap"))
        self.assertEqual(json.loads(frappe.as_json(plan.node_config(cluster)))["@type"], "PostgreSql")
        self.assertIn("EnvironmentFile=/etc/stalwart/stalwart.env", plan.systemd_unit())

    def test_dns_server_object_maps_the_zone_provider(self) -> None:
        make_zone(dns_provider="Cloudflare", dns_provider_token="cf-token")
        with no_dns_provider():
            cluster = make_cluster()
        self.assertEqual(cluster.dns_zone, ROOT_DOMAIN)
        self.assertEqual(plan.dns_server_object(cluster)["@type"], "Cloudflare")
        self.assertEqual(plan.dns_server_object(cluster)["secret"], {"@type": "Value", "secret": "cf-token"})

        domain = {op["object"]: op for op in plan.cluster_plan(cluster)}["Domain"]["value"]["default"]
        self.assertEqual(domain["dnsManagement"]["@type"], "Automatic")
        self.assertEqual(domain["dnsManagement"]["origin"], ROOT_DOMAIN)
        self.assertEqual(domain["dnsManagement"]["publishRecords"], {"mx": True, "dkim": True, "dmarc": True})
