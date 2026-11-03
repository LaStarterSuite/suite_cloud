from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import dns, egress, plan, push
from suite_cloud.mail.stalwart import forget_sessions
from suite_cloud.mail.stalwart.errors import StalwartError
from suite_cloud.mail.tests.fake_stalwart import FakeStalwart
from suite_cloud.mail.tests.fixtures import (
    ROOT_DOMAIN,
    activate_cluster,
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_node,
    make_owner,
    make_tenant,
    remove_cluster,
)
from suite_cloud.utils import user_context


class TestEgress(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        self.cluster = activate_cluster(make_cluster())
        self.fake = FakeStalwart(
            base_url=self.cluster.base_url, admin_password=self.cluster.get_password("admin_password")
        )
        self.fake.add_token("test-token")
        self.fake.singletons["SystemSettings"] = {
            "mailExchangers": {"0": {"hostname": self.cluster.hostname, "priority": 10}}
        }
        self.fake.singletons["MtaOutboundStrategy"] = {
            "route": {
                "match": {"0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}},
                "else": "'mx'",
            }
        }
        self._install = self.fake.install()
        self._install.__enter__()
        self.addCleanup(self._install.__exit__, None, None, None)
        forget_sessions(self.cluster)
        clear_request_cache()
        self.tenant = make_tenant(self.cluster)
        self.gateway = frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "cluster": self.cluster.name,
                "ipv4_address": "203.0.113.50",
                "ssh_host_keys": "203.0.113.50 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTests",
            }
        ).insert()

    def tearDown(self) -> None:
        # Only the fixture cluster's documents: deleting anyone else's pushes to a live server.
        mine = {"cluster": self.cluster.name}
        for name in frappe.get_all("Mail Domain", mine, pluck="name"):
            frappe.delete_doc("Mail Domain", name, force=True, ignore_permissions=True, ignore_on_trash=True)
        frappe.db.set_value("Stalwart Cluster", self.cluster.name, "default_egress_pool", None)
        frappe.db.set_value("Suite Tenant", self.tenant.name, "egress_pool", None)
        for name in frappe.get_all("Egress IP Pool", mine, pluck="name"):
            frappe.delete_doc(
                "Egress IP Pool", name, force=True, ignore_permissions=True, ignore_on_trash=True
            )
        frappe.db.delete("DNS Record", {"managed_by_doctype": ["in", ["Egress IP Pool", "Egress Gateway"]]})
        frappe.delete_doc(
            "Egress Gateway", self.gateway.name, force=True, ignore_permissions=True, ignore_on_trash=True
        )
        frappe.flags.do_not_enqueue = False

    def make_pool(self, ips: tuple[str, ...] = ("203.0.113.51",), **fields):
        """Pools name themselves p1, p2, ... and their addresses p1-1, p1-2, ..."""

        pool = frappe.get_doc(
            {
                "doctype": "Egress IP Pool",
                "cluster": self.cluster.name,
                "addresses": [{"gateway": self.gateway.name, "ip_address": ip} for ip in ips],
                **fields,
            }
        )
        pool.insert()
        return pool

    def test_gateway_defaults_and_dns(self) -> None:
        self.assertEqual(self.gateway.status, "Pending")
        self.assertEqual(self.gateway.name, f"g1.{self.cluster.default_domain}")
        self.assertEqual(self.gateway.base_url, f"https://g1.{self.cluster.default_domain}")
        self.assertEqual(frappe.db.get_value("Stalwart Store", self.gateway.data_store, "type"), "RocksDb")
        self.assertEqual(len(self.gateway.get_password("admin_password")), 32)
        record = frappe.get_all(
            "DNS Record", {"managed_by": self.gateway.name}, ["host", "value", "category"], order_by="type"
        )
        # Its address, and SPF for its own domain: notifications leave from g1.<zone>.
        self.assertEqual(
            [(r.host, r.value, r.category) for r in record],
            [
                ("g1.mail.blr", "203.0.113.50", "Egress"),
                ("g1.mail.blr", f"v=spf1 include:spf.{self.cluster.default_domain} -all", "SPF"),
            ],
        )

        # A new address means a new server as far as SSH is concerned.
        self.gateway.db_set("ssh_verified", 1)
        self.gateway.reload()
        self.gateway.ipv4_address = "203.0.113.60"
        self.gateway.save()
        self.assertEqual(self.gateway.ssh_verified, 0)
        self.assertEqual(
            frappe.db.get_value("DNS Record", {"managed_by": self.gateway.name, "type": "A"}, "value"),
            "203.0.113.60",
        )

    def test_reset_host_keys_lets_verify_ssh_record_the_new_ones(self) -> None:
        self.gateway.db_set("ssh_verified", 1)

        # A reinstalled server presents new keys; the old pin must go before it can be trusted.
        self.gateway.reset_ssh_host_keys()
        self.gateway.reload()
        self.assertEqual((self.gateway.ssh_host_keys, self.gateway.ssh_verified), (None, 0))
        audit = {
            "reference_doctype": "Egress Gateway",
            "reference_name": self.gateway.name,
            "comment_type": "Info",
        }
        self.assertEqual(frappe.get_all("Comment", audit, pluck="comment_email"), [frappe.session.user])

        module = "suite_cloud.mail.doctype.egress_gateway.egress_gateway"
        with (
            patch(f"{module}.scan_host_keys", return_value="203.0.113.50 ssh-ed25519 AAAAnew"),
            patch(f"{module}.ping", return_value=(True, "")),
        ):
            self.assertTrue(self.gateway.verify_ssh())
        self.gateway.reload()
        self.assertEqual(
            (self.gateway.ssh_host_keys, self.gateway.ssh_verified), ("203.0.113.50 ssh-ed25519 AAAAnew", 1)
        )

    def test_only_managers_can_reset_host_keys(self) -> None:
        # Dropping the pin lets the next Verify SSH trust any server, so other users are refused.
        with user_context(make_owner()), self.assertRaises(frappe.PermissionError):
            self.gateway.reset_ssh_host_keys()
        pinned = frappe.db.get_value("Egress Gateway", self.gateway.name, "ssh_host_keys")
        self.assertEqual(pinned, self.gateway.ssh_host_keys)

    def test_pool_assigns_ports_hostnames_and_records(self) -> None:
        pool = self.make_pool(("203.0.113.51", "203.0.113.52"))
        second = self.make_pool(("203.0.113.53",))

        self.assertEqual((pool.relay_port, second.relay_port), (2525, 2526))
        self.assertEqual((pool.pool_name, second.pool_name), ("p1", "p2"))
        self.assertEqual(pool.hostname, f"p1.out.{self.cluster.default_domain}")
        hosts = sorted(
            (r.host, r.value)
            for r in frappe.get_all("DNS Record", {"managed_by": pool.name}, ["host", "value"])
        )
        # The gateway is still Pending, so the pool hostname lists nobody yet.
        self.assertEqual(hosts, [("p1-1.mail.blr", "203.0.113.51"), ("p1-2.mail.blr", "203.0.113.52")])
        self.gateway.set_status("Active")
        hosts = sorted(
            (r.host, r.value)
            for r in frappe.get_all("DNS Record", {"managed_by": pool.name}, ["host", "value"])
        )
        self.assertEqual(
            hosts,
            [
                ("p1-1.mail.blr", "203.0.113.51"),
                ("p1-2.mail.blr", "203.0.113.52"),
                ("p1.out.mail.blr", "203.0.113.50"),
            ],
        )
        self.gateway.set_status("Disabled")
        self.assertFalse(frappe.db.exists("DNS Record", {"managed_by": pool.name, "host": "p1.out.mail.blr"}))
        spf = frappe.db.get_value("DNS Record", {"host": "spf.mail.blr"}, "value")
        for ip in ("203.0.113.51", "203.0.113.52", "203.0.113.53"):
            self.assertIn(f"ip4:{ip}", spf)

        self.assertRaisesRegex(frappe.ValidationError, "already belongs", self.make_pool, ("203.0.113.51",))
        # A typed pool name is ignored: names are handed out in order.
        self.assertEqual(self.make_pool(("203.0.113.54",), pool_name="custom").pool_name, "p3")

    def test_a_pool_change_pushes_the_cluster_before_its_gateways(self) -> None:
        pool = self.make_pool()
        self.gateway.db_set("status", "Active")
        steps = []
        with (
            patch.object(egress, "resync_cluster", side_effect=lambda cluster: steps.append("cluster")),
            patch.object(type(self.gateway), "push_config", side_effect=lambda: steps.append("gateway")),
            patch.object(egress, "open_relay_port") as open_port,
        ):
            egress.apply_pool_changes_job(pool.name, [self.gateway.name])
        self.assertEqual(steps, ["cluster", "gateway"])
        open_port.assert_called_once()

        # A cluster that refuses the push leaves the gateways as they were, and says so on its form.
        steps.clear()
        with (
            patch.object(egress, "resync_cluster", side_effect=StalwartError("cluster down")),
            patch.object(type(self.gateway), "push_config", side_effect=lambda: steps.append("gateway")),
        ):
            self.assertRaises(StalwartError, egress.apply_pool_changes_job, pool.name, [])
        self.assertEqual(steps, [])
        self.assertIn(
            "cluster down",
            frappe.db.get_value("Stalwart Cluster", self.cluster.name, "last_config_sync_error"),
        )

    def test_cluster_routes_follow_pool_assignment(self) -> None:
        pool = self.make_pool()
        domain = frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": "acme.com", "tenant": self.tenant.name}
        ).insert()
        frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": "direct.com", "tenant": self.tenant.name}
        ).insert()

        # No assignment yet: just the local rule (ours, not stacked on the pre-existing one) and no
        # relay route.
        operations = egress.cluster_operations(self.cluster)
        self.assertEqual([op["object"] for op in operations], ["MtaOutboundStrategy"])
        self.assertEqual(
            operations[0]["value"]["route"]["match"],
            {"0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}},
        )

        domain.egress_pool = pool.name
        domain.save()
        operations = {op["object"]: op for op in egress.cluster_operations(self.cluster)}
        route = operations["MtaRoute"]["value"]["egress-p1"]
        self.assertEqual(
            (route["address"], route["port"], route["authUsername"]), (pool.hostname, 2525, "relay")
        )
        self.assertEqual(route["authSecret"]["secret"], self.cluster.get_password("relay_password"))
        rules = egress.expression_rules(operations["MtaOutboundStrategy"]["value"]["route"])
        self.assertEqual(rules[0]["then"], "'local'")  # cluster-to-cluster mail never leaves
        self.assertEqual(rules[1], {"if": "sender_domain == 'acme.com'", "then": "'egress-p1'"})
        # The save synced the running cluster: the fake now carries the relay route and rules.
        self.assertEqual(self.fake.find("MtaRoute", name="egress-p1")["port"], 2525)
        live_rules = egress.expression_rules(self.fake.singletons["MtaOutboundStrategy"]["route"])
        self.assertEqual([r["then"] for r in live_rules], ["'local'", "'egress-p1'"])

        # Cluster default pool pulls every unassigned domain in; site and domain overrides win.
        self.cluster.db_set("default_egress_pool", pool.name)
        frappe.clear_document_cache("Stalwart Cluster", self.cluster.name)
        grouped = egress.domains_by_pool(frappe.get_doc("Stalwart Cluster", self.cluster.name))
        self.assertEqual(grouped, {pool.name: ["acme.com", "direct.com"]})

        # Re-applying is idempotent: our rules are replaced, not stacked. The default pool is the
        # fallback of the expression, so domains on it need no rule of their own.
        push.resync_cluster(frappe.get_doc("Stalwart Cluster", self.cluster.name))
        live = self.fake.singletons["MtaOutboundStrategy"]["route"]
        self.assertEqual([r["then"] for r in egress.expression_rules(live)], ["'local'"])
        self.assertEqual(live["else"], "'egress-p1'")

    def test_default_pool_is_the_fallback_route(self) -> None:
        cluster = frappe.get_doc("Stalwart Cluster", self.cluster.name)

        # A default pool without addresses cannot relay: nothing changes.
        empty = frappe.get_doc(
            {"doctype": "Egress IP Pool", "cluster": cluster.name, "pool_name": "empty"}
        ).insert()
        cluster.db_set("default_egress_pool", empty.name)
        operations = egress.cluster_operations(cluster)
        self.assertEqual([op["object"] for op in operations], ["MtaOutboundStrategy"])
        self.assertEqual(operations[0]["value"]["route"]["else"], "'mx'")

        # A populated default pool (p2: the empty one took p1) gets its relay route and the else branch
        # before any domain exists.
        pool = self.make_pool()
        cluster.db_set("default_egress_pool", pool.name)
        push.resync_cluster(cluster)
        self.assertEqual(self.fake.find("MtaRoute", name="egress-p2")["address"], pool.hostname)
        live = self.fake.singletons["MtaOutboundStrategy"]["route"]
        self.assertEqual([r["then"] for r in egress.expression_rules(live)], ["'local'"])
        self.assertEqual(live["else"], "'egress-p2'")

        # A domain on another pool is the only one that needs a rule; both pools keep a route.
        other = self.make_pool(("203.0.113.52",))
        frappe.get_doc(
            {
                "doctype": "Mail Domain",
                "domain_name": "news.com",
                "tenant": self.tenant.name,
                "egress_pool": other.name,
            }
        ).insert()
        frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": "acme.com", "tenant": self.tenant.name}
        ).insert()
        operations = {op["object"]: op for op in egress.cluster_operations(cluster)}
        self.assertEqual(sorted(operations["MtaRoute"]["value"]), ["egress-p2", "egress-p3"])
        route = operations["MtaOutboundStrategy"]["value"]["route"]
        self.assertEqual(
            egress.expression_rules(route)[1], {"if": "sender_domain == 'news.com'", "then": "'egress-p3'"}
        )
        self.assertEqual(route["else"], "'egress-p2'")

        # Clearing the default hands the fallback back to direct delivery; a foreign else is kept.
        cluster.db_set("default_egress_pool", None)
        push.resync_cluster(cluster)
        live = self.fake.singletons["MtaOutboundStrategy"]["route"]
        self.assertEqual(live["else"], "'mx'")
        self.assertEqual([r["then"] for r in egress.expression_rules(live)], ["'local'", "'egress-p3'"])
        self.fake.singletons["MtaOutboundStrategy"]["route"]["else"] = "'custom'"
        operations = {op["object"]: op for op in egress.cluster_operations(cluster)}
        self.assertEqual(operations["MtaOutboundStrategy"]["value"]["route"]["else"], "'custom'")

    def live_route(self) -> tuple[list[dict], str]:
        route = self.fake.singletons["MtaOutboundStrategy"]["route"]
        return egress.expression_rules(route), route["else"]

    def make_domain(self, domain_name: str, **fields):
        return frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": domain_name, "tenant": self.tenant.name, **fields}
        ).insert()

    def test_relaying_domains_keep_only_the_addresses_they_hold(self) -> None:
        local = {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}
        held = {"if": "is_local_address(rcpt)", "then": "'local'"}
        foreign = {"if": "rcpt_domain == 'partner.com'", "then": "'partner'"}
        self.fake.singletons["MtaOutboundStrategy"]["route"]["match"]["1"] = foreign
        split = self.make_domain("split.com")
        self.make_domain("acme.com")

        # Stalwart accepts every address of a relaying domain; the ones it does not hold must miss
        # the local rules to reach the domain's MX. Saving the flag is enough to reroute them.
        split.allow_relaying = 1
        split.save()
        beside_split = {"if": f"{local['if']} && rcpt_domain != 'split.com'", "then": "'local'"}
        self.assertEqual(self.live_route(), ([held, beside_split, foreign], "'mx'"))

        # They leave like any other mail: through the sender's pool, or the default one.
        pool = self.make_pool()
        self.cluster.db_set("default_egress_pool", pool.name)
        other = self.make_pool(("203.0.113.52",))
        split.reload()
        split.egress_pool = other.name
        split.save()
        by_sender = {"if": "sender_domain == 'split.com'", "then": "'egress-p2'"}
        self.assertEqual(self.live_route(), ([held, beside_split, by_sender, foreign], "'egress-p1'"))

        # A domain created relaying joins them; a disabled one is the cluster's business no more.
        moving = self.make_domain("moving.com", allow_relaying=1)
        beside_both = {
            "if": f"{local['if']} && rcpt_domain != 'moving.com' && rcpt_domain != 'split.com'",
            "then": "'local'",
        }
        self.assertEqual(self.live_route()[0], [held, beside_both, by_sender, foreign])
        split.reload()
        split.enabled = 0
        split.save()
        beside_moving = {"if": f"{local['if']} && rcpt_domain != 'moving.com'", "then": "'local'"}
        self.assertEqual(self.live_route()[0], [held, beside_moving, foreign])

        # Without a relaying domain the route is the plain local rule again, whether the last one
        # stopped relaying or was deleted; the rules written for it are replaced, not left behind.
        moving.reload()
        moving.allow_relaying = 0
        moving.save()
        self.assertEqual(self.live_route()[0], [local, foreign])
        moving.allow_relaying = 1
        moving.save()
        self.assertEqual(self.live_route()[0], [held, beside_moving, foreign])
        moving.delete()
        self.assertEqual(self.live_route(), ([local, foreign], "'egress-p1'"))

    def test_gateway_plan(self) -> None:
        pool = self.make_pool(("203.0.113.51",))
        operations = {
            op["object"]: op
            for op in egress.gateway_plan(frappe.get_doc("Egress Gateway", self.gateway.name))
        }

        listener = operations["NetworkListener"]["value"]["relay-p1"]
        self.assertEqual(
            (listener["protocol"], listener["bind"], listener["useTls"]),
            ("smtp", {"0.0.0.0:2525": True}, True),
        )
        strategy = operations["MtaConnectionStrategy"]["value"]["p1"]
        self.assertEqual(
            strategy["sourceIps"],
            {"0": {"sourceIp": "203.0.113.51", "ehloHostname": f"p1-1.{self.cluster.default_domain}"}},
        )
        self.assertEqual(
            operations["MtaOutboundStrategy"]["value"]["connection"],
            {"match": {"0": {"if": "received_via_port == 2525", "then": "'p1'"}}, "else": "'default'"},
        )
        # The relay login belongs to the egress zone; customer sender addresses must pass on relay ports.
        self.assertEqual(
            operations["MtaStageAuth"]["value"]["mustMatchSender"],
            {"match": {"0": {"if": "local_port == 2525", "then": "false"}}, "else": "true"},
        )
        role = operations["ClusterRole"]["value"]["gateway-role"]
        self.assertEqual(role["name"], "egress")
        self.assertIn("metricsPush", role["tasks"]["taskTypes"])
        self.assertEqual(operations["Metrics"]["value"]["prometheus"], {"@type": "Disabled"})
        self.assertEqual(role["listeners"], {"@type": "EnableAll"})  # the firewall limits exposure
        # The gateway's own domain: its name, a certificate that also covers every pool hostname,
        # DKIM keys of its own (a domain shared between gateways would publish clashing selectors)
        # and an MX at the cluster since a gateway's port 25 is firewalled.
        domain = operations["Domain"]["value"]["gateway"]
        self.assertEqual(domain["name"], self.gateway.hostname)
        self.assertEqual(
            domain["certificateManagement"]["subjectAlternativeNames"],
            {f"*.out.{self.cluster.default_domain}": True},
        )
        self.assertEqual(domain["dkimManagement"]["algorithms"], {"Dkim1RsaSha256": True})
        self.assertEqual(operations["SystemSettings"]["value"]["defaultDomainId"], "#gateway")
        self.assertEqual(
            operations["SystemSettings"]["value"]["mailExchangers"],
            {"0": {"hostname": self.cluster.hostname, "priority": 10}},
        )
        relay = operations["Account"]["value"]["relay"]
        self.assertEqual(relay["credentials"]["0"]["secret"], self.cluster.get_password("relay_password"))
        self.assertEqual(operations["Coordinator"]["value"], {"@type": "Disabled"})
        self.assertEqual(operations["Tracer"]["value"]["log"]["path"], "/var/log/stalwart")
        self.assertEqual(operations["DnsResolver"]["value"]["@type"], "Custom")
        self.assertIn("SpamSettings", operations)

        variables = egress.build_gateway_variables({"gateway": self.gateway.name})
        self.assertEqual(variables["wait_ports"], [443, 2525])
        self.assertIn("STALWART_ROLE=egress", variables["env_normal"])
        self.assertIn(f"SUITE_CLOUD_TELEMETRY_TOKEN={self.gateway.telemetry_key}:", variables["env_normal"])
        self.assertIn("env_normal", variables["__secret_keys__"])
        self.assertIn(plan.get_telemetry_token(self.gateway), variables["__secret_values__"])
        self.assertIn('"object":"SpamSettings"', variables["defaults_ndjson"])
        self.assertIn('"name":"egress"', variables["defaults_ndjson"])  # the role env_normal names
        self.assertNotIn('"name":"full"', variables["defaults_ndjson"])
        self.assertIn('"object":"DnsResolver"', variables["defaults_ndjson"])
        self.assertIn('"@type":"RocksDb"', variables["bootstrap_ndjson"])
        self.assertIn(pool.pool_name, variables["cluster_ndjson"])

    def test_a_failed_ptr_lookup_keeps_the_last_state(self) -> None:
        pool = self.make_pool()
        row = pool.addresses[0]
        row.db_set("ptr_verified", 1, update_modified=False)
        target = "suite_cloud.mail.doctype.egress_ip_pool.egress_ip_pool.verify_ptr_record"
        with patch(target, return_value=None):
            self.assertTrue(pool.verify_ptr(row.name)[row.ip_address])
        self.assertEqual(frappe.db.get_value(row.doctype, row.name, "ptr_verified"), 1)
        with patch(target, return_value=False):
            self.assertFalse(pool.verify_ptr(row.name)[row.ip_address])
        self.assertEqual(frappe.db.get_value(row.doctype, row.name, "ptr_verified"), 0)

    def test_verify_ptr_marks_rows_one_or_all(self) -> None:
        pool = self.make_pool(("203.0.113.51", "203.0.113.52"))
        first, second = pool.addresses
        target = "suite_cloud.mail.doctype.egress_ip_pool.egress_ip_pool.verify_ptr_record"

        with patch(target, side_effect=lambda ip, host: ip == "203.0.113.51") as check:
            self.assertEqual(pool.verify_ptr(first.name), {"203.0.113.51": True})
            check.assert_called_once_with("203.0.113.51", first.ehlo_hostname)
            self.assertEqual(pool.verify_ptr(), {"203.0.113.51": True, "203.0.113.52": False})
        self.assertRaisesRegex(frappe.ValidationError, "not found", pool.verify_ptr, "missing")

        rows = frappe.get_all(
            "Egress IP Pool Address", {"parent": pool.name}, ["ip_address", "ptr_verified"], order_by="idx"
        )
        self.assertEqual(
            [(r.ip_address, r.ptr_verified) for r in rows], [("203.0.113.51", 1), ("203.0.113.52", 0)]
        )
        self.assertEqual(second.ptr_verified, 0)

    def test_pool_must_belong_to_the_cluster(self) -> None:
        pool = self.make_pool()
        other = activate_cluster(make_cluster("blr-2", hostname=f"mail.blr2.{ROOT_DOMAIN}"))
        self.addCleanup(remove_cluster, other.name)

        other.default_egress_pool = pool.name
        self.assertRaisesRegex(frappe.ValidationError, "another cluster", other.save)
        self.assertRaisesRegex(
            frappe.ValidationError,
            "another cluster",
            make_tenant,
            other,
            "other.frappe.test",
            egress_pool=pool.name,
        )

        tenant = make_tenant(other, "other.frappe.test")
        self.addCleanup(frappe.db.delete, "Suite Tenant", {"name": tenant.name})
        domain = frappe.get_doc(
            {
                "doctype": "Mail Domain",
                "domain_name": "other.com",
                "tenant": tenant.name,
                "egress_pool": pool.name,
            }
        )
        self.assertRaisesRegex(frappe.ValidationError, "another cluster", domain.insert)

    def test_a_tenant_without_a_cluster_keeps_no_pool(self) -> None:
        pool = self.make_pool()
        tenant = make_tenant(self.cluster, "other.frappe.test", egress_pool=pool.name)
        self.addCleanup(frappe.db.delete, "Suite Tenant", {"name": tenant.name})
        tenant.stalwart_cluster = None
        tenant.save()
        self.assertIsNone(tenant.egress_pool)

    def test_pool_deletion_is_blocked_while_used(self) -> None:
        pool = self.make_pool()
        self.tenant.db_set("egress_pool", pool.name)
        self.assertRaisesRegex(frappe.ValidationError, "still used", pool.delete)
        self.tenant.db_set("egress_pool", None)
        pool.delete()
        self.assertFalse(frappe.db.exists("DNS Record", {"managed_by": pool.name}))
        self.assertNotIn(
            "ip4:203.0.113.51", frappe.db.get_value("DNS Record", {"host": "spf.mail.blr"}, "value")
        )

    def test_spf_includes_nodes_and_pools(self) -> None:
        node = make_node(self.cluster, "203.0.113.10")
        node.db_set("status", "Active")
        self.make_pool(("203.0.113.51",))
        dns.sync_spf_record(self.cluster)
        self.assertEqual(
            frappe.db.get_value("DNS Record", {"host": "spf.mail.blr"}, "value"),
            "v=spf1 ip4:203.0.113.10 ip4:203.0.113.51 -all",
        )
