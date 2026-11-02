"""Sending limits: the Agents pull them per cluster, push what they counted, and the cluster plan
carries the hook and its backstop once the Agents can answer it."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from suite_cloud.api.mail import accounts, domains
from suite_cloud.api.telemetry import quota as quota_api
from suite_cloud.api.tenant import InvalidInputError, SendLimitsNotAllowedError
from suite_cloud.mail.cluster import plan, push, quota
from suite_cloud.mail.doctype.mail_send_usage import mail_send_usage
from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import (
    current_periods,
    periods_at,
    prune_usage_job,
    upsert_usage,
    usage_name,
)
from suite_cloud.mail.tests.fixtures import make_cluster, make_node
from suite_cloud.mail.tests.test_tenant_api import TenantApiTestCase
from suite_cloud.patches.v1_0 import (
    fill_quota_hook_tokens,
    fill_send_limit_defaults,
    rename_backstop_to_max_daily_send_limit,
)
from suite_cloud.telemetry import TELEMETRY_USER
from suite_cloud.telemetry.status import CACHE_KEY
from suite_cloud.tests.fixtures import ROOT_DOMAIN

NODE_IP = "203.0.113.10"


def frozen_clock(moment: datetime) -> type[datetime]:
    """``datetime`` with ``now()`` fixed at ``moment``, for the usage module."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz)

    return Frozen


class SendingLimitsTestCase(TenantApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        domains.create_domain("acme.com")
        self.verify("acme.com")
        self.tenant.db_set("allow_custom_send_limits", 1)
        self.act_as(self.tenant)
        for name in ("alice", "bob", "carol"):
            accounts.create_account(f"{name}@acme.com", "secret-pw", daily_send_limit=100)
        frappe.set_user("Administrator")
        self.node = make_node(self.cluster, ipv4=NODE_IP)
        self.node.db_set("status", "Active")
        self.act_as(self.tenant)
        settle = patch.object(quota_api, "SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)
        # One moment for the test and the code, so a run across midnight UTC sees one day.
        self.now = datetime.now(UTC)
        clock = patch.object(mail_send_usage, "datetime", frozen_clock(self.now))
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self) -> None:
        frappe.local.agent_cluster = None
        frappe.db.delete("Mail Send Usage", {"account": ("like", "%@acme.com")})
        super().tearDown()

    def as_agent(self, cluster: str | None = None) -> None:
        """As the auth hook leaves a request its cluster's credentials signed in."""

        frappe.set_user(TELEMETRY_USER)
        frappe.local.agent_cluster = cluster or self.cluster.name
        frappe.utils.set_request(method="POST", headers={"User-Agent": "suite-cloud-agent/0.2.0+abcdef12"})
        frappe.local.form_dict = frappe._dict()

    def pull(self, since: str | None = None) -> dict:
        self.as_agent()
        return quota_api.limits(NODE_IP, since)


class TestLimitsApi(SendingLimitsTestCase):
    def test_pages_lose_no_account_that_shares_a_modified_time(self) -> None:
        moment = now_datetime() - timedelta(minutes=1)
        frappe.db.set_value("Mail Account", {"domain": "acme.com"}, "modified", moment, update_modified=False)
        with patch.object(quota_api, "PAGE_SIZE", 2):
            first = self.pull()
            second = self.pull(first["next_version"])
            third = self.pull(second["next_version"])

        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        names = [entry["account"] for entry in first["limits"] + second["limits"]]
        self.assertEqual(names, ["alice@acme.com", "bob@acme.com", "carol@acme.com"])
        self.assertEqual(first["limits"][0]["daily"], 100)
        self.assertEqual(first["limits"][0]["version"], moment.strftime("%Y-%m-%d %H:%M:%S.%f"))
        self.assertEqual((third["limits"], third["next_version"]), ([], second["next_version"]))

        frappe.set_user("Administrator")
        accounts_doc = frappe.get_doc("Mail Account", "bob@acme.com")
        accounts_doc.monthly_send_limit = 500
        accounts_doc.save()
        changed = self.pull(second["next_version"])["limits"]
        self.assertEqual([(e["account"], e["monthly"]) for e in changed], [("bob@acme.com", 500)])

    def test_a_save_is_served_once_it_settled(self) -> None:
        with patch.object(quota_api, "SETTLE_SECONDS", 60):
            self.assertEqual(self.pull()["limits"], [])
        self.assertEqual(len(self.pull()["limits"]), 3)

    def test_only_one_active_node_of_the_cluster_may_pull(self) -> None:
        self.as_agent()
        for host_ip in ("198.51.100.1", "not-an-ip"):
            self.assertRaises((frappe.PermissionError, InvalidInputError), quota_api.limits, host_ip)
        self.assertRaisesRegex(InvalidInputError, "since_version", quota_api.limits, NODE_IP, "2026")

        frappe.set_user("Administrator")
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        self.as_agent(other.name)  # another cluster's Agent naming this cluster's node
        self.assertRaisesRegex(frappe.PermissionError, "Agent's cluster", quota_api.limits, NODE_IP)

        frappe.set_user("Administrator")
        self.node.db_set("status", "Provisioned")
        self.assertRaises(frappe.PermissionError, self.pull)

    def test_quota_calls_leave_the_telemetry_status_to_pushes(self) -> None:
        frappe.cache.hdel(CACHE_KEY, NODE_IP)  # only this test's node: the site may run real agents
        self.pull()
        self.assertIsNone(frappe.cache.hget(CACHE_KEY, NODE_IP))

    def test_the_pull_reads_an_index_on_cluster_and_modified(self) -> None:
        self.assertTrue(frappe.db.has_index("tabMail Account", "cluster_modified_index"))
        # Each Agent call finds its server by cluster and address.
        for doctype in ("Stalwart Node", "Egress Gateway"):
            self.assertTrue(frappe.db.has_index(f"tab{doctype}", "cluster_ipv4_address_index"))

    def test_only_agents_may_call(self) -> None:
        frappe.set_user("Administrator")
        self.assertRaises(frappe.PermissionError, quota_api.limits, NODE_IP)
        self.assertRaises(frappe.PermissionError, quota_api.usage, NODE_IP, [])


class TestUsageApi(SendingLimitsTestCase):
    def push(self, *records: tuple) -> dict:
        """``(account, period, sent)`` or ``(account, period, sent, refused)`` each."""

        self.as_agent()
        keys = ("account", "period", "sent", "refused")
        return quota_api.usage(NODE_IP, [dict(zip(keys, record, strict=False)) for record in records])

    def stored(self, account: str, period: str, field: str = "sent") -> int | None:
        return frappe.db.get_value("Mail Send Usage", {"account": account, "period": period}, field)

    def test_rows_are_written_in_one_lock_order(self) -> None:
        day, month = current_periods()
        with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
            self.push(("bob@acme.com", day, 1), ("alice@acme.com", month, 1), ("alice@acme.com", day, 1))
        insert = next(call for call in sql.call_args_list if "Mail Send Usage" in str(call.args[0]))
        values = [str(value) for value in insert.args[1].values()]  # in the order of the statement
        written = [(value, values[i + 1]) for i, value in enumerate(values) if value.endswith("@acme.com")]
        self.assertEqual(written, [("alice@acme.com", day), ("alice@acme.com", month), ("bob@acme.com", day)])

    def test_the_higher_count_of_two_agents_is_kept(self) -> None:
        day, month = current_periods()
        self.assertEqual(self.push(("alice@acme.com", day, 5), ("alice@acme.com", month, 40))["accepted"], 2)
        self.push(("alice@acme.com", day, 3))  # another node read the counter earlier
        self.assertEqual(self.stored("alice@acme.com", day), 5)
        self.push(("alice@acme.com", day, 7))
        self.assertEqual((self.stored("alice@acme.com", day), self.stored("alice@acme.com", month)), (7, 40))

        # Refusals are kept the same way, and an Agent that sends none leaves them as they are.
        self.push(("alice@acme.com", day, 7, 2))
        self.push(("alice@acme.com", day, 7, 1), ("alice@acme.com", day, 7))
        self.assertEqual(self.stored("alice@acme.com", day, "refused"), 2)

    def test_each_period_keeps_the_limit_it_was_last_reported_with(self) -> None:
        day, month = current_periods()
        frappe.db.set_value("Mail Account", "alice@acme.com", "monthly_send_limit", 2000)
        self.push(("alice@acme.com", day, 5), ("alice@acme.com", month, 40))
        stored = frappe.get_all("Mail Send Usage", {"account": "alice@acme.com"}, ["period", "send_limit"])
        self.assertEqual({row.period: row.send_limit for row in stored}, {day: 100, month: 2000})

        frappe.db.set_value("Mail Account", "alice@acme.com", "daily_send_limit", 150)
        self.push(("alice@acme.com", day, 6))
        self.assertEqual(self.stored("alice@acme.com", day, "send_limit"), 150)

    def test_a_late_report_keeps_the_limit_of_its_day(self) -> None:
        yesterday, _month = periods_at(self.now - timedelta(days=1))
        self.push(("alice@acme.com", yesterday, 90))
        frappe.db.set_value("Mail Account", "alice@acme.com", "daily_send_limit", 10)  # after midnight
        self.push(("alice@acme.com", yesterday, 95))
        self.assertEqual(self.stored("alice@acme.com", yesterday), 95)
        self.assertEqual(self.stored("alice@acme.com", yesterday, "send_limit"), 100)

    def test_the_row_name_fits_any_account(self) -> None:
        long_account = f"{'a' * 64}@{'b' * 75}.com"  # 140 characters, the longest name
        self.assertEqual(len(usage_name(long_account, "d:20261028")), 40)
        self.assertNotEqual(
            usage_name("alice@acme.com", "d:20261028"), usage_name("alice@acme.com", "m:202610")
        )

    def test_an_account_deleted_meanwhile_does_not_fail_the_batch(self) -> None:
        day, _month = current_periods()
        upsert_usage(
            [
                {"account": "gone@acme.com", "period": day, "sent": 3, "refused": 0},
                {"account": "alice@acme.com", "period": day, "sent": 2, "refused": 0},
            ],
            self.cluster.name,
        )
        self.assertEqual(self.stored("alice@acme.com", day), 2)
        self.assertIsNone(self.stored("gone@acme.com", day))

    def test_another_cluster_s_accounts_are_dropped(self) -> None:
        day, _month = current_periods()
        frappe.set_user("Administrator")
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        frappe.db.set_value("Mail Account", "bob@acme.com", "cluster", other.name)
        answer = self.push(("alice@acme.com", day, 1), ("bob@acme.com", day, 1))
        self.assertEqual(answer["accepted"], 1)
        self.assertIsNone(self.stored("bob@acme.com", day))

    def test_unknown_accounts_and_old_periods_are_dropped(self) -> None:
        day, _month = current_periods()
        old_day, _old_month = periods_at(self.now - timedelta(days=3))
        answer = self.push(
            ("ALICE@acme.com", day, 1), ("ghost@acme.com", day, 1), ("bob@acme.com", old_day, 1)
        )
        self.assertEqual(answer["accepted"], 1)
        self.assertEqual(self.stored("alice@acme.com", day), 1)
        self.assertIsNone(self.stored("bob@acme.com", old_day))
        self.assertRaises(InvalidInputError, self.push, ("bob@acme.com", day, -1))
        self.assertRaises(InvalidInputError, self.push, ("bob@acme.com", day, 1, -1))

    def test_yesterday_and_last_month_are_taken_after_midnight(self) -> None:
        """The Agent's first push after midnight UTC sends the periods that just ended."""

        moment = datetime(2026, 11, 1, 0, 0, 30, tzinfo=UTC)
        with patch.object(mail_send_usage, "datetime", frozen_clock(moment)):
            answer = self.push(
                ("alice@acme.com", "d:20261101", 1),
                ("alice@acme.com", "m:202611", 1),
                ("alice@acme.com", "d:20261031", 7, 2),
                ("alice@acme.com", "m:202610", 40, 2),
                ("alice@acme.com", "d:20261030", 5),
            )
        self.assertEqual(answer["accepted"], 4)
        stored = frappe.get_all(
            "Mail Send Usage", {"account": "alice@acme.com"}, ["period", "sent", "refused"]
        )
        self.assertEqual(
            {row.period: (row.sent, row.refused) for row in stored},
            {"d:20261101": (1, 0), "m:202611": (1, 0), "d:20261031": (7, 2), "m:202610": (40, 2)},
        )

    def test_form_and_api_show_the_usage_and_delete_takes_it(self) -> None:
        day, month = current_periods()
        self.push(("alice@acme.com", day, 80, 3), ("alice@acme.com", month, 900, 5))
        usage = {"day": 80, "month": 900, "refused_day": 3, "refused_month": 5}
        frappe.set_user("Administrator")
        doc = frappe.get_doc("Mail Account", "alice@acme.com")
        doc.run_method("onload")
        self.assertEqual(doc.get_onload().send_usage, usage)

        self.act_as(self.tenant)
        payload = accounts.get_account("alice@acme.com")
        self.assertEqual(
            (payload["daily_send_limit"], payload["monthly_send_limit"], payload["send_usage"]),
            (100, 10000, usage),
        )
        listed = {item["email"]: item for item in accounts.list_accounts()["items"]}
        self.assertEqual(listed["alice@acme.com"]["send_usage"], usage)
        self.assertEqual(listed["bob@acme.com"]["send_usage"], dict.fromkeys(usage, 0))

        accounts.delete_account("alice@acme.com")
        self.assertFalse(frappe.db.exists("Mail Send Usage", {"account": "alice@acme.com"}))

    def test_prune_keeps_the_days_and_months_of_the_retention(self) -> None:
        now = self.now
        inside, outside, old = (now - timedelta(days=days) for days in (29, 31, 70))
        rows = [*current_periods(), *periods_at(inside), *periods_at(outside), *periods_at(old)]
        upsert_usage(
            [
                {"account": "alice@acme.com", "period": period, "sent": 1, "refused": 0}
                for period in set(rows)
            ],
            self.cluster.name,
        )
        with (
            patch(f"{mail_send_usage.__name__}.get_config", return_value=30),
            patch.object(frappe.db, "delete", wraps=frappe.db.delete) as delete,
        ):
            prune_usage_job()
        # One period per statement, so a shortened retention never deletes months of rows at once.
        self.assertTrue(delete.call_args_list)
        self.assertTrue(all(set(call.args[1]) == {"period"} for call in delete.call_args_list))
        kept = set(frappe.get_all("Mail Send Usage", {"account": "alice@acme.com"}, pluck="period"))
        # The month of the 31st day back is kept while the retention still reaches into it.
        month_at_cutoff = periods_at(now - timedelta(days=30))[1]
        self.assertEqual(kept, {*current_periods(), *periods_at(inside), month_at_cutoff})

    def test_a_retention_under_a_day_falls_back_to_the_default(self) -> None:
        for days in (0, -5):
            with patch(f"{mail_send_usage.__name__}.get_config", return_value=days):
                self.assertEqual(mail_send_usage.retention_days(), mail_send_usage.DEFAULT_RETENTION_DAYS)


class TestTenantSendLimits(SendingLimitsTestCase):
    def test_a_new_account_gets_its_tenant_s_default_limits_unless_the_tenant_sets_them(self) -> None:
        created = accounts.create_account("dave@acme.com", "secret-pw")
        self.assertEqual((created["daily_send_limit"], created["monthly_send_limit"]), (2000, 10000))

        self.tenant.db_set({"default_daily_send_limit": 300, "default_monthly_send_limit": 4000})
        created = accounts.create_account("erin@acme.com", "secret-pw", monthly_send_limit=500)
        self.assertEqual((created["daily_send_limit"], created["monthly_send_limit"]), (300, 500))
        created = accounts.create_account("fay@acme.com", "secret-pw", daily_send_limit=0)
        self.assertEqual((created["daily_send_limit"], created["monthly_send_limit"]), (300, 4000))

    def test_tenants_set_the_limits_within_the_cluster_s_max_but_cannot_lift_them(self) -> None:
        updated = accounts.update_account("bob@acme.com", monthly_send_limit=2000)
        self.assertEqual((updated["daily_send_limit"], updated["monthly_send_limit"]), (100, 2000))
        self.assertRaises(frappe.ValidationError, accounts.update_account, "bob@acme.com", daily_send_limit=0)
        self.assertRaisesRegex(
            frappe.ValidationError,
            "Monthly Send Limit must be between 1 and the cluster's max of 10000, not 10001",
            accounts.update_account,
            "bob@acme.com",
            monthly_send_limit=10001,
        )
        # Every account has a limit, so create refuses a negative one too, naming the field and the value.
        for limits in ({"daily_send_limit": -1}, {"monthly_send_limit": -5}):
            with self.subTest(**limits):
                self.assertRaisesRegex(
                    frappe.ValidationError,
                    f"Send Limit must be between 1 and the cluster's max of \\d+, not {next(iter(limits.values()))}\\.",
                    accounts.create_account,
                    "dave@acme.com",
                    "secret-pw",
                    **limits,
                )
        self.assertFalse(frappe.db.exists("Mail Account", "dave@acme.com"))

    def test_a_tenant_passes_a_limit_only_once_an_operator_allowed_it(self) -> None:
        self.tenant.db_set("allow_custom_send_limits", 0)
        self.act_as(self.tenant)
        for call in (
            lambda: accounts.create_account("dave@acme.com", "secret-pw", daily_send_limit=50),
            lambda: accounts.update_account("bob@acme.com", monthly_send_limit=50),
        ):
            self.assertRaisesRegex(SendLimitsNotAllowedError, "may not set send limits", call)
        self.assertFalse(frappe.db.exists("Mail Account", "dave@acme.com"))

        created = accounts.create_account("dave@acme.com", "secret-pw")
        self.assertEqual((created["daily_send_limit"], created["monthly_send_limit"]), (2000, 10000))
        self.assertEqual(accounts.update_account("bob@acme.com", display_name="Bob")["daily_send_limit"], 100)

    def test_a_tenant_s_defaults_stay_within_the_cluster_s_max(self) -> None:
        frappe.set_user("Administrator")
        for value in (10001, 0):
            tenant = frappe.get_doc("Suite Tenant", self.tenant.name)
            tenant.default_monthly_send_limit = value
            self.assertRaisesRegex(
                frappe.ValidationError, f"between 1 and the cluster's max of 10000, not {value}", tenant.save
            )

    def test_the_patch_fills_unset_limits_from_the_tenant_and_the_agents_pull_them(self) -> None:
        cursor = self.pull()["next_version"]
        fields = list(fill_send_limit_defaults.FIELDS)
        frappe.db.set_value("Mail Account", "bob@acme.com", dict.fromkeys(fields, 0), update_modified=False)
        self.tenant.db_set({"default_daily_send_limit": 300, "default_monthly_send_limit": 4000})

        fill_send_limit_defaults.execute()
        fill_send_limit_defaults.execute()  # a second run finds nothing to fill

        self.assertEqual(frappe.db.get_value("Mail Account", "bob@acme.com", fields), (300, 4000))
        self.assertEqual(frappe.db.get_value("Mail Account", "alice@acme.com", fields), (100, 10000))
        pulled = {entry["account"]: entry for entry in self.pull(cursor)["limits"]}
        self.assertEqual(set(pulled), {"bob@acme.com"})
        self.assertEqual((pulled["bob@acme.com"]["daily"], pulled["bob@acme.com"]["monthly"]), (300, 4000))

    def test_tenants_read_an_account_s_daily_history(self) -> None:
        now = self.now
        today, two_days_ago, long_ago = (now - timedelta(days=days) for days in (0, 2, 40))
        # A future row, which the usage API never stores, stays out of the answer too.
        future = now + timedelta(days=40)
        periods = {*periods_at(today), *periods_at(two_days_ago), *periods_at(long_ago), *periods_at(future)}
        upsert_usage(
            [{"account": "alice@acme.com", "period": period, "sent": 9, "refused": 1} for period in periods],
            self.cluster.name,
        )

        history = accounts.get_send_usage("alice@acme.com", days=7)
        self.assertEqual(
            [day["date"] for day in history["days"]], [f"{today:%Y-%m-%d}", f"{two_days_ago:%Y-%m-%d}"]
        )
        self.assertEqual(
            history["days"][0], {"date": f"{today:%Y-%m-%d}", "sent": 9, "refused": 1, "limit": 100}
        )
        months = {f"{moment:%Y-%m}" for moment in (today, now - timedelta(days=6))}
        self.assertEqual({month["month"] for month in history["months"]}, months)

        self.act_as(self.other)
        self.assertRaises(frappe.DoesNotExistError, accounts.get_send_usage, "alice@acme.com")


class TestQuotaPlan(SendingLimitsTestCase):
    def test_the_plan_carries_the_hook_once_the_agents_answer_it(self) -> None:
        objects = lambda operations: {op["object"] for op in operations}  # noqa: E731

        def quota_objects(operations: list[dict]) -> dict:
            values = {op["object"]: op["value"] for op in operations}
            return values["MtaHook"]["quota"], values["MtaInboundThrottle"]["quota-backstop"]

        # Off: both stay in the plan, switched off, so a hook installed before is turned off too.
        with patch.object(quota, "is_quota_enabled", return_value=False):
            hook, backstop = quota_objects(plan.cluster_plan(self.cluster))
        self.assertEqual((hook["enable"], backstop["enable"]), (quota.NEVER, False))

        with patch.object(quota, "is_quota_enabled", return_value=True):
            operations = plan.cluster_plan(self.cluster)
            recovery = plan.recovery_plan(self.cluster)
        hook = next(op for op in operations if op["object"] == "MtaHook")["value"]["quota"]
        self.assertEqual(hook["url"], "http://127.0.0.1:8095/hooks/quota")
        self.assertEqual(
            (hook["stages"], hook["timeout"], hook["tempFailOnError"]), ({"data": True}, 2000, False)
        )
        self.assertEqual(
            hook["httpAuth"]["bearerToken"]["secret"], self.cluster.get_password("quota_hook_token")
        )
        self.assertIn(self.cluster.get_password("quota_hook_token"), plan.secret_strings(operations))

        throttle = next(op for op in operations if op["object"] == "MtaInboundThrottle")["value"][
            "quota-backstop"
        ]
        # Only signed-in sessions: unauthenticated ones would share one bucket otherwise.
        self.assertEqual(throttle["match"], quota.SIGNED_IN)
        self.assertEqual(
            (throttle["enable"], throttle["key"], throttle["rate"]),
            (True, {"authenticatedAs": True}, {"count": 2000, "period": 86_400_000}),
        )

        # The cap is the cluster's and is never off: nothing else caps an account while the Agents are down.
        for field in ("max_daily_send_limit", "max_monthly_send_limit"):
            cluster = frappe.get_doc("Stalwart Cluster", self.cluster.name)
            cluster.set(field, 0)
            self.assertRaisesRegex(
                frappe.ValidationError, "at least 1", cluster.save, ignore_permissions=True
            )
        self.cluster.db_set("max_daily_send_limit", 20000)
        self.assertEqual(
            quota.backstop_operation(self.cluster)["value"]["quota-backstop"]["rate"]["count"], 20000
        )

        # Stalwart adds its default throttles only when none exist, so recovery leaves ours out.
        self.assertIn("MtaHook", objects(recovery))
        self.assertNotIn("MtaInboundThrottle", objects(recovery))

    def test_the_patch_turns_the_backstop_on_without_a_config_push(self) -> None:
        self.cluster.db_set("max_daily_send_limit", 0)
        with patch.object(push, "resync_cluster_after_commit") as resync:
            rename_backstop_to_max_daily_send_limit.execute()
        self.assertEqual(
            frappe.db.get_value("Stalwart Cluster", self.cluster.name, "max_daily_send_limit"), 2000
        )
        resync.assert_not_called()

    def test_the_patch_gives_older_clusters_a_token_and_keeps_existing_ones(self) -> None:
        frappe.set_user("Administrator")
        token = self.cluster.get_password("quota_hook_token")
        self.assertEqual(len(token), 32)
        fill_quota_hook_tokens.execute()
        self.assertEqual(self.cluster.get_password("quota_hook_token"), token)

        frappe.db.delete(
            "__Auth",
            {"doctype": "Stalwart Cluster", "name": self.cluster.name, "fieldname": "quota_hook_token"},
        )
        fill_quota_hook_tokens.execute()
        self.assertNotIn(self.cluster.get_password("quota_hook_token", raise_exception=False), (None, token))
