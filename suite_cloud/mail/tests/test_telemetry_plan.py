"""Stalwart's own push in the cluster and gateway plans: on only once every running server holds
its telemetry token."""

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import egress, plan, telemetry
from suite_cloud.mail.stalwart import forget_sessions
from suite_cloud.mail.tests.fake_stalwart import FakeStalwart
from suite_cloud.mail.tests.fixtures import activate_cluster, clear_request_cache, make_cluster, make_node
from suite_cloud.telemetry import ERROR_EVENTS, WARN_EVENTS, WEBHOOK_EVENTS
from suite_cloud.tests.fixtures import configure_settings

TOKEN = {
    "@type": "Bearer",
    "bearerToken": {"@type": "EnvironmentVariable", "variableName": "SUITE_CLOUD_TOKEN"},
}


def by_object(operations: list[dict]) -> dict:
    return {operation["object"]: operation for operation in operations}


class TestTelemetryPlan(IntegrationTestCase):
    def setUp(self) -> None:
        configure_settings(
            enable_telemetry=1, public_url="https://cloud.example.test", webhook_discard_after_minutes=10
        )
        self.cluster = make_cluster()
        self.node = make_node(self.cluster)
        self.node.db_set("status", "Active")

    def test_the_push_waits_for_every_running_node_to_hold_its_token(self) -> None:
        off = by_object(plan.cluster_plan(self.cluster))
        webhooks = off["WebHook"]["value"]
        self.assertFalse(any(webhook["enable"] for webhook in webhooks.values()))
        self.assertEqual(off["Metrics"]["value"]["openTelemetry"], {"@type": "Disabled"})
        self.assertEqual(off["Metrics"]["value"]["prometheus"], {"@type": "Disabled"})

        self.node.db_set("suite_cloud_token_installed", 1)
        make_node(self.cluster, ipv4="203.0.113.11")  # pending: not running, so it does not count
        on = by_object(plan.cluster_plan(self.cluster))
        # The URL follows the Public URL, so a WebHook is found by its stream header instead.
        self.assertEqual(on["WebHook"]["matchOn"], ["httpHeaders"])
        events, errors, warnings = (
            on["WebHook"]["value"]["telemetry-events"],
            on["WebHook"]["value"]["telemetry-errors"],
            on["WebHook"]["value"]["telemetry-warnings"],
        )
        base = "https://cloud.example.test/api/v2/method/suite_cloud.api.telemetry."
        self.assertEqual(
            (events["url"], events["level"], events["eventsPolicy"], events["enable"]),
            (f"{base}webhook?stream=events", "info", "include", True),
        )
        # Stalwart ignores a WebHook's level, so each names its events; none is sent twice.
        self.assertEqual(
            (errors["url"], errors["level"], errors["eventsPolicy"]),
            (f"{base}webhook?stream=errors", "error", "include"),
        )
        self.assertEqual(
            (warnings["url"], warnings["level"], warnings["eventsPolicy"]),
            (f"{base}webhook?stream=warnings", "warn", "include"),
        )
        self.assertEqual(set(events["events"]), set(WEBHOOK_EVENTS))
        self.assertEqual(set(errors["events"]), set(ERROR_EVENTS))
        self.assertEqual(set(warnings["events"]), set(WARN_EVENTS))
        self.assertEqual(
            (events["httpAuth"], events["signatureKey"], events["throttle"], events["discardAfter"]),
            (TOKEN, {"@type": "None"}, 30_000, 600_000),
        )
        self.assertEqual(
            on["Metrics"]["value"],
            {
                "openTelemetry": {
                    "@type": "Http",
                    "endpoint": f"{base}otlp",
                    "interval": 60_000,
                    "timeout": 10_000,
                    "httpAuth": TOKEN,
                    "httpHeaders": {},
                },
                "prometheus": {"@type": "Disabled"},
            },
        )

        # Another running node without its token turns it off again, as does the setting.
        make_node(self.cluster, ipv4="203.0.113.12").db_set("status", "Draining")
        self.assertFalse(telemetry.is_push_enabled(self.cluster))
        frappe.db.set_value("Stalwart Node", {"cluster": self.cluster.name}, "suite_cloud_token_installed", 1)
        self.assertTrue(telemetry.is_push_enabled(self.cluster))
        configure_settings(enable_telemetry=0)
        self.assertFalse(telemetry.is_push_enabled(self.cluster))

    def test_a_gateway_pushes_once_it_holds_its_token(self) -> None:
        gateway = frappe.get_doc(
            {"doctype": "Egress Gateway", "cluster": self.cluster.name, "ipv4_address": "203.0.113.50"}
        ).insert()
        self.assertTrue(telemetry.is_push_enabled(gateway))  # provisioning starts it with the token
        gateway.db_set("status", "Active")
        self.assertFalse(telemetry.is_push_enabled(gateway))
        self.assertFalse(
            by_object(egress.gateway_plan(gateway))["WebHook"]["value"]["telemetry-events"]["enable"]
        )
        gateway.db_set("suite_cloud_token_installed", 1)
        self.assertTrue(
            by_object(egress.gateway_plan(gateway))["WebHook"]["value"]["telemetry-events"]["enable"]
        )

    def test_every_role_that_delivers_pushes_metrics(self) -> None:
        roles = plan.CLUSTER_ROLES
        self.assertIn("metricsPush", roles["outbound"]["tasks"]["taskTypes"])
        self.assertEqual(roles["full"]["tasks"], {"@type": "EnableAll"})
        self.assertNotIn("metricsPush", roles["frontend"]["tasks"]["taskTypes"])  # a DisableSome list
        role = egress.gateway_defaults_plan()[0]["value"]["gateway-role"]
        self.assertIn("metricsPush", role["tasks"]["taskTypes"])

    def test_the_token_variable_names_no_secret_in_the_plan(self) -> None:
        self.node.db_set("suite_cloud_token_installed", 1)
        rendered = plan.cluster_plan(self.cluster)
        self.assertNotIn(self.node.get_password("suite_cloud_secret"), plan.to_ndjson(rendered))
        self.assertEqual(plan.marker(rendered), plan.marker(plan.cluster_plan(self.cluster)))

    def test_a_new_public_url_moves_the_webhooks_instead_of_adding_more(self) -> None:
        self.cluster.reload()
        cluster = activate_cluster(self.cluster)
        fake = FakeStalwart(base_url=cluster.base_url, admin_password=cluster.get_password("admin_password"))
        fake.add_token("test-token")
        with fake.install():
            forget_sessions(cluster)
            clear_request_cache()
            webhooks = lambda: telemetry.get_operations(cluster)[:1]  # noqa: E731
            cluster.get_client().apply(webhooks())
            configure_settings(public_url="https://moved.example.test")
            cluster.get_client().apply(webhooks())

        base = "https://moved.example.test/api/v2/method/suite_cloud.api.telemetry.webhook"
        self.assertEqual(
            sorted(hook["url"] for hook in fake.objects["WebHook"].values()),
            [f"{base}?stream={stream}" for stream in ("errors", "events", "warnings")],
        )
