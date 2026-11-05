"""Sending limits: Stalwart's RCPT hook asks Suite Cloud for each recipient, the counters live in
Redis and are flushed to Mail Send Usage, and the cluster plan carries the hook and its backstop."""

import contextvars
import json
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
import redis
from frappe.utils import get_test_client

from suite_cloud.api import sending_limits as sending_limits_api
from suite_cloud.api.mail import accounts, domains
from suite_cloud.api.telemetry import authenticate
from suite_cloud.api.tenant import SendLimitsNotAllowedError
from suite_cloud.mail import sending_limits
from suite_cloud.mail.cluster import plan, push, quota, server_token
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
    fill_send_limit_defaults,
    rename_backstop_to_max_daily_send_limit,
)
from suite_cloud.telemetry import TELEMETRY_USER
from suite_cloud.tests.fixtures import ROOT_DOMAIN, configure_settings, production_logging

HOOK = "/api/v2/method/suite_cloud.api.sending_limits.check"


def frozen_clock(moment: datetime) -> type[datetime]:
    """``datetime`` with ``now()`` fixed at ``moment``, for the usage and counter modules."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz)

    return Frozen


def clear_keys() -> None:
    """The test accounts' keys and the touched sets: the dev site may count real mail under the
    same prefix."""

    client = sending_limits.get_client()
    for pattern in ("sending_limits:*@acme.com*", "sending_limits:touched*"):
        for key in client.scan_iter(match=sending_limits.make_key(pattern)):
            client.delete(key)


class SendingLimitsTestCase(TenantApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        domains.create_domain("acme.com")
        self.verify("acme.com")
        self.tenant.db_set("allow_custom_send_limits", 1)
        self.act_as(self.tenant)
        for name in ("alice", "bob", "carol"):
            accounts.create_account(f"{name}@acme.com", "secret-pw", daily_send_limit=100)
        # One moment for the test and the code, so a run across midnight UTC sees one day.
        self.now = datetime.now(UTC)
        for module in (mail_send_usage, sending_limits):
            clock = patch.object(module, "datetime", frozen_clock(self.now))
            clock.start()
            self.addCleanup(clock.stop)
        clear_keys()
        self.addCleanup(clear_keys)
        frappe.set_user("Administrator")
        self.node = make_node(self.cluster)
        self.token = server_token.get_token(self.node)

    def tearDown(self) -> None:
        frappe.local.stalwart_server = None
        frappe.db.delete("Mail Send Usage", {"account": ("like", "%@acme.com")})
        super().tearDown()

    def sign_in(self, token: str | None = None) -> None:
        """A hook request as the auth hook sees it."""

        token = self.token if token is None else token
        frappe.local.stalwart_server = None
        frappe.set_user("Guest")
        frappe.utils.set_request(method="POST", path=HOOK, headers={"Authorization": f"Bearer {token}"})
        frappe.local.form_dict = frappe._dict()
        authenticate()

    def check(self, login: str | None = "alice@acme.com") -> dict:
        self.sign_in()
        context = {"stage": "rcpt", "sasl": {"login": login} if login else None}
        envelope = {"from": {"address": login or ""}, "to": [{"address": "someone@example.com"}]}
        return json.loads(sending_limits_api.check(context=context, envelope=envelope).get_data())

    def counter(self, account: str, period: str) -> dict:
        counts = sending_limits.get_client().hgetall(sending_limits.counter_key(account, period))
        return {key.decode(): int(value) for key, value in counts.items()}

    def stored(self, account: str, period: str, field: str = "sent") -> int | None:
        return frappe.db.get_value("Mail Send Usage", {"account": account, "period": period}, field)

    def is_limit_cached(self, account: str) -> bool:
        return bool(
            sending_limits.get_client().exists(sending_limits.make_key(f"sending_limits:limit:{account}"))
        )


class TestCheck(SendingLimitsTestCase):
    def test_a_recipient_under_the_limits_is_accepted_and_counted(self) -> None:
        day, month = current_periods()
        self.assertEqual(self.check(), {"action": "accept"})
        self.assertEqual(self.check(" Alice@ACME.com "), {"action": "accept"})
        self.assertEqual(self.counter("alice@acme.com", day), {"sent": 2, "refused": 0})
        self.assertEqual(self.counter("alice@acme.com", month), {"sent": 2, "refused": 0})
        members = sending_limits.get_client().smembers(sending_limits.make_key(sending_limits.TOUCHED))
        self.assertEqual(members, {f"alice@acme.com|{day}".encode(), f"alice@acme.com|{month}".encode()})

    def test_the_daily_limit_refuses_and_counts_the_refusal(self) -> None:
        day, month = current_periods()
        upsert_usage([{"account": "alice@acme.com", "period": day, "sent": 99, "refused": 0}])
        self.assertEqual(self.check(), {"action": "accept"})
        tomorrow = self.now.date() + timedelta(days=1)
        message = (
            "Daily sending limit of 100 recipients reached for alice@acme.com. "
            f"It resets at {tomorrow:%Y-%m-%d} 00:00 UTC."
        )
        reject = {
            "action": "reject",
            "response": {"status": 550, "enhanced_status": "5.4.5", "message": message},
        }
        self.assertEqual(self.check(), reject)
        self.assertEqual(self.counter("alice@acme.com", day), {"sent": 100, "refused": 1})
        self.assertEqual(self.counter("alice@acme.com", month), {"sent": 1, "refused": 1})

    def test_the_monthly_limit_refuses_until_the_first_of_next_month(self) -> None:
        _day, month = current_periods()
        upsert_usage([{"account": "alice@acme.com", "period": month, "sent": 10000, "refused": 0}])
        answer = self.check()
        next_month = (self.now.date().replace(day=1) + timedelta(days=32)).replace(day=1)
        self.assertEqual(
            answer["response"]["message"],
            "Monthly sending limit of 10,000 recipients reached for alice@acme.com. "
            f"It resets at {next_month:%Y-%m-%d} 00:00 UTC.",
        )
        self.assertEqual(self.counter("alice@acme.com", month), {"sent": 10000, "refused": 1})

    def test_a_long_address_keeps_the_message_on_one_short_line(self) -> None:
        limits = sending_limits.Limits("c", 2000, 10000)
        message = sending_limits.get_refusal_message("daily", f"{'a' * 64}@{'b' * 180}.com", limits, self.now)
        self.assertLess(len(message), 200)
        self.assertNotIn("\n", message)

    def test_stalwart_gets_its_own_json_over_http(self) -> None:
        db = frappe.local.db
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Frappe-Site-Name": frappe.local.site,
            "Content-Type": "application/json",
        }
        body = {"context": {"stage": "rcpt", "sasl": {"login": "alice@acme.com"}}, "envelope": {"to": [{}]}}
        with (
            patch("frappe.database.get_db", return_value=db),
            patch.object(db, "commit"),
            patch.object(db, "rollback"),
            patch("frappe.destroy"),
            production_logging(),
        ):
            response = get_test_client().open(
                HOOK,
                method="POST",
                headers=headers,
                base_url="http://localhost/",
                data=json.dumps(body),
            )
        self.assertEqual((response.status_code, response.get_json()), (200, {"action": "accept"}))
        self.assertEqual(response.mimetype, "application/json")

    def test_other_clusters_unknown_addresses_and_anonymous_senders_pass_uncounted(self) -> None:
        day, _month = current_periods()
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        frappe.db.set_value("Mail Account", "bob@acme.com", "cluster", other.name)
        for login in ("bob@acme.com", "ghost@acme.com", None):
            with self.subTest(login=login):
                self.assertEqual(self.check(login), {"action": "accept"})
        self.sign_in()
        no_recipient = sending_limits_api.check(
            context={"sasl": {"login": "carol@acme.com"}}, envelope={"to": []}
        )
        self.assertEqual(json.loads(no_recipient.get_data()), {"action": "accept"})
        for account in ("bob@acme.com", "ghost@acme.com", "carol@acme.com"):
            self.assertEqual(self.counter(account, day), {})

    def test_only_a_node_s_own_token_signs_in(self) -> None:
        gateway = frappe.get_doc(
            {"doctype": "Egress Gateway", "cluster": self.cluster.name, "ipv4_address": "203.0.113.50"}
        ).insert()
        key, secret = self.node.suite_cloud_key, self.node.get_password("suite_cloud_secret")
        cases = (
            "wrong",
            f"{key}.wrong",
            f"{key}:{secret}",  # the old telemetry token's form
            server_token.get_token(gateway),  # a gateway sends no recipients to check
        )
        for token in cases:
            with self.subTest(token=token[:24]), self.assertRaises(frappe.AuthenticationError) as raised:
                self.sign_in(token)
            self.assertEqual(raised.exception.http_status_code, 401)

        # The caller's cluster is its node's. Telemetry being off does not matter, and only a
        # signed-in node may call.
        frappe.set_user("Administrator")
        other = make_node(make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}"), ipv4="203.0.113.20")
        configure_settings(enable_telemetry=0)
        self.sign_in()
        self.assertEqual(
            (frappe.session.user, frappe.local.stalwart_server.cluster), (TELEMETRY_USER, self.cluster.name)
        )
        self.sign_in(server_token.get_token(other))
        self.assertEqual(frappe.local.stalwart_server.cluster, other.cluster)
        frappe.set_user("Administrator")
        frappe.local.stalwart_server = None
        self.assertRaises(frappe.PermissionError, sending_limits_api.check)

    def test_a_rotated_token_is_forgotten_after_commit(self) -> None:
        self.sign_in()
        frappe.set_user("Administrator")
        node = frappe.get_doc("Stalwart Node", self.node.name)
        node.suite_cloud_secret = frappe.generate_hash(length=40)
        node.save()
        self.sign_in()  # the cached secret still opens until the save commits
        frappe.db.after_commit.run()
        self.assertRaises(frappe.AuthenticationError, self.sign_in)
        self.sign_in(server_token.get_token(node))

    def test_a_warm_cache_reads_no_database_row(self) -> None:
        self.sign_in()
        with patch.object(server_token, "find_server") as find:
            self.sign_in()
        find.assert_not_called()

    def test_a_missing_counter_is_seeded_from_mail_send_usage_once(self) -> None:
        day, month = current_periods()
        upsert_usage(
            [
                {"account": "alice@acme.com", "period": day, "sent": 40, "refused": 2},
                {"account": "alice@acme.com", "period": month, "sent": 900, "refused": 5},
            ]
        )
        with patch.object(sending_limits, "get_seed", wraps=sending_limits.get_seed) as seed:
            self.check()
            self.check()
        seed.assert_called_once_with("alice@acme.com", day, month)
        self.assertEqual(self.counter("alice@acme.com", day), {"sent": 42, "refused": 2})
        self.assertEqual(self.counter("alice@acme.com", month), {"sent": 902, "refused": 5})

        # A counter Redis lost is seeded again; the other keeps its count.
        sending_limits.get_client().delete(sending_limits.counter_key("alice@acme.com", month))
        self.check()
        self.assertEqual(self.counter("alice@acme.com", day), {"sent": 43, "refused": 2})
        self.assertEqual(self.counter("alice@acme.com", month), {"sent": 901, "refused": 5})

    def test_two_checks_of_the_last_free_recipient_get_one_accept(self) -> None:
        day, _month = current_periods()
        self.check()  # caches the limits and creates the counters, so the threads read no row
        sending_limits.get_client().hset(sending_limits.counter_key("alice@acme.com", day), "sent", 99)
        barrier = threading.Barrier(2)
        answers = []

        def count() -> None:
            barrier.wait()
            answers.append(sending_limits.count_recipient(self.cluster.name, "alice@acme.com"))

        threads = [threading.Thread(target=contextvars.copy_context().run, args=(count,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(answer is None for answer in answers), [False, True])
        self.assertEqual(self.counter("alice@acme.com", day), {"sent": 100, "refused": 1})

    def test_a_redis_fault_answers_500(self) -> None:
        self.sign_in()
        with patch.object(sending_limits, "get_client", side_effect=redis.ConnectionError("down")):
            response = sending_limits_api.check(
                context={"sasl": {"login": "alice@acme.com"}}, envelope={"to": [{"address": "x@example.com"}]}
            )
        self.assertEqual(response.status_code, 500)


class TestLimitsCache(SendingLimitsTestCase):
    def test_a_limit_change_clears_the_cache_after_commit_only(self) -> None:
        self.assertEqual(sending_limits.get_limits("bob@acme.com").daily, 100)
        bob = frappe.get_doc("Mail Account", "bob@acme.com")
        bob.daily_send_limit = 50
        bob.save()
        self.assertTrue(self.is_limit_cached("bob@acme.com"))  # another worker could cache the old row
        frappe.db.after_commit.run()
        self.assertFalse(self.is_limit_cached("bob@acme.com"))
        self.assertEqual(sending_limits.get_limits("bob@acme.com").daily, 50)

        bob.daily_send_limit = 60
        bob.save()
        frappe.db.after_commit.reset()  # what a rollback does
        self.assertEqual(sending_limits.get_limits("bob@acme.com").daily, 50)

        bob.display_name = "Bob"
        bob.save()  # nothing the cache holds
        frappe.db.after_commit.run()
        self.assertTrue(self.is_limit_cached("bob@acme.com"))

    def test_a_new_account_clears_a_cached_unknown_address(self) -> None:
        self.assertEqual(sending_limits.get_limits("dave@acme.com"), sending_limits.Limits("", 0, 0))
        self.act_as(self.tenant)
        accounts.create_account("dave@acme.com", "secret-pw")
        self.assertEqual(sending_limits.get_limits("dave@acme.com").cluster, "")
        frappe.db.after_commit.run()
        self.assertEqual(
            sending_limits.get_limits("dave@acme.com"), sending_limits.Limits(self.cluster.name, 2000, 10000)
        )

        accounts.delete_account("dave@acme.com")
        frappe.db.after_commit.run()
        self.assertEqual(sending_limits.get_limits("dave@acme.com").cluster, "")


class TestFlush(SendingLimitsTestCase):
    def test_the_touched_counters_reach_mail_send_usage_once(self) -> None:
        day, month = current_periods()
        self.check()
        self.check()
        self.check("bob@acme.com")
        sending_limits.flush_usage()
        self.assertEqual((self.stored("alice@acme.com", day), self.stored("alice@acme.com", month)), (2, 2))
        self.assertEqual(self.stored("bob@acme.com", day), 1)
        client = sending_limits.get_client()
        self.assertFalse(client.exists(sending_limits.make_key(sending_limits.TOUCHED)))
        self.assertFalse(client.exists(sending_limits.make_key(sending_limits.FLUSHING)))

        with patch.object(sending_limits, "upsert_usage") as upsert:
            sending_limits.flush_usage()  # nothing touched since
        upsert.assert_not_called()

        # Written again, as after a failed run, nothing changes.
        client.sadd(sending_limits.make_key(sending_limits.FLUSHING), f"alice@acme.com|{day}")
        sending_limits.flush_usage()
        self.assertEqual(self.stored("alice@acme.com", day), 2)

    def test_a_failed_run_is_written_again_and_keeps_the_new_entries(self) -> None:
        day, _month = current_periods()
        self.check()
        with patch.object(sending_limits, "upsert_usage", side_effect=RuntimeError("database down")):
            self.assertRaises(RuntimeError, sending_limits.flush_usage)
        self.check("bob@acme.com")  # touched while the failed set waits

        client = sending_limits.get_client()
        sending_limits.flush_usage()
        self.assertEqual(self.stored("alice@acme.com", day), 1)
        self.assertIsNone(self.stored("bob@acme.com", day))  # the next run's
        self.assertEqual(
            {
                member.decode().partition("|")[0]
                for member in client.smembers(sending_limits.make_key(sending_limits.TOUCHED))
            },
            {"bob@acme.com"},
        )
        sending_limits.flush_usage()
        self.assertEqual(self.stored("bob@acme.com", day), 1)

    def test_old_periods_and_expired_counters_are_skipped(self) -> None:
        day, _month = current_periods()
        old_day, _old_month = periods_at(self.now - timedelta(days=3))
        client = sending_limits.get_client()
        client.hset(sending_limits.counter_key("alice@acme.com", old_day), mapping={"sent": 5, "refused": 0})
        client.sadd(
            sending_limits.make_key(sending_limits.TOUCHED),
            f"alice@acme.com|{old_day}",
            f"bob@acme.com|{day}",  # its counter expired
        )
        sending_limits.flush_usage()
        self.assertIsNone(self.stored("alice@acme.com", old_day))
        self.assertIsNone(self.stored("bob@acme.com", day))

    def test_yesterday_and_last_month_are_taken_after_midnight(self) -> None:
        """The first flush after midnight UTC writes the periods that just ended."""

        moment = datetime(2026, 11, 1, 0, 0, 30, tzinfo=UTC)
        client = sending_limits.get_client()
        periods = {"d:20261101": 1, "m:202611": 1, "d:20261031": 7, "m:202610": 40, "d:20261030": 5}
        for period, sent in periods.items():
            client.hset(
                sending_limits.counter_key("alice@acme.com", period), mapping={"sent": sent, "refused": 2}
            )
            client.sadd(sending_limits.make_key(sending_limits.TOUCHED), f"alice@acme.com|{period}")
        with patch.object(mail_send_usage, "datetime", frozen_clock(moment)):
            sending_limits.flush_usage()
        stored = frappe.get_all("Mail Send Usage", {"account": "alice@acme.com"}, ["period", "sent"])
        self.assertEqual(
            {row.period: row.sent for row in stored},
            {"d:20261101": 1, "m:202611": 1, "d:20261031": 7, "m:202610": 40},
        )


class TestUsage(SendingLimitsTestCase):
    def test_rows_are_written_in_one_lock_order(self) -> None:
        day, month = current_periods()
        rows = [("bob@acme.com", day), ("alice@acme.com", month), ("alice@acme.com", day)]
        with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
            upsert_usage([{"account": a, "period": p, "sent": 1, "refused": 0} for a, p in rows])
        insert = next(call for call in sql.call_args_list if "Mail Send Usage" in str(call.args[0]))
        values = [str(value) for value in insert.args[1].values()]  # in the order of the statement
        written = [(value, values[i + 1]) for i, value in enumerate(values) if value.endswith("@acme.com")]
        self.assertEqual(written, [("alice@acme.com", day), ("alice@acme.com", month), ("bob@acme.com", day)])

    def test_the_higher_count_is_kept(self) -> None:
        day, _month = current_periods()

        def write(sent: int, refused: int = 0) -> None:
            upsert_usage([{"account": "alice@acme.com", "period": day, "sent": sent, "refused": refused}])

        write(5, 2)
        write(3, 1)  # a late flush of an older count
        self.assertEqual(
            (self.stored("alice@acme.com", day), self.stored("alice@acme.com", day, "refused")), (5, 2)
        )
        write(7)
        self.assertEqual(self.stored("alice@acme.com", day), 7)

    def test_each_period_keeps_the_limit_it_was_last_written_with(self) -> None:
        day, month = current_periods()
        frappe.db.set_value("Mail Account", "alice@acme.com", "monthly_send_limit", 2000)
        upsert_usage(
            [
                {"account": "alice@acme.com", "period": day, "sent": 5, "refused": 0},
                {"account": "alice@acme.com", "period": month, "sent": 40, "refused": 0},
            ]
        )
        stored = frappe.get_all("Mail Send Usage", {"account": "alice@acme.com"}, ["period", "send_limit"])
        self.assertEqual({row.period: row.send_limit for row in stored}, {day: 100, month: 2000})

        frappe.db.set_value("Mail Account", "alice@acme.com", "daily_send_limit", 150)
        upsert_usage([{"account": "alice@acme.com", "period": day, "sent": 6, "refused": 0}])
        self.assertEqual(self.stored("alice@acme.com", day, "send_limit"), 150)

    def test_a_late_flush_keeps_the_limit_of_its_day(self) -> None:
        yesterday, _month = periods_at(self.now - timedelta(days=1))
        upsert_usage([{"account": "alice@acme.com", "period": yesterday, "sent": 90, "refused": 0}])
        frappe.db.set_value("Mail Account", "alice@acme.com", "daily_send_limit", 10)  # after midnight
        upsert_usage([{"account": "alice@acme.com", "period": yesterday, "sent": 95, "refused": 0}])
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
            ]
        )
        self.assertEqual(self.stored("alice@acme.com", day), 2)
        self.assertIsNone(self.stored("gone@acme.com", day))

    def test_form_and_api_show_the_usage_and_delete_takes_it(self) -> None:
        day, month = current_periods()
        upsert_usage(
            [
                {"account": "alice@acme.com", "period": day, "sent": 80, "refused": 3},
                {"account": "alice@acme.com", "period": month, "sent": 900, "refused": 5},
            ]
        )
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
            [{"account": "alice@acme.com", "period": period, "sent": 1, "refused": 0} for period in set(rows)]
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
    def setUp(self) -> None:
        super().setUp()
        self.act_as(self.tenant)

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

    def test_the_patch_fills_unset_limits_from_the_tenant(self) -> None:
        fields = list(fill_send_limit_defaults.FIELDS)
        frappe.db.set_value("Mail Account", "bob@acme.com", dict.fromkeys(fields, 0), update_modified=False)
        self.tenant.db_set({"default_daily_send_limit": 300, "default_monthly_send_limit": 4000})

        fill_send_limit_defaults.execute()
        fill_send_limit_defaults.execute()  # a second run finds nothing to fill

        self.assertEqual(frappe.db.get_value("Mail Account", "bob@acme.com", fields), (300, 4000))
        self.assertEqual(frappe.db.get_value("Mail Account", "alice@acme.com", fields), (100, 10000))

    def test_tenants_read_an_account_s_daily_history(self) -> None:
        now = self.now
        # The window's first day is six days back, which some dates put in the previous month.
        today, two_days_ago, first_day, long_ago = (now - timedelta(days=days) for days in (0, 2, 6, 40))
        # A future row, which no flush stores, stays out of the answer too.
        future = now + timedelta(days=40)
        moments = (today, two_days_ago, first_day, long_ago, future)
        periods = {period for moment in moments for period in periods_at(moment)}
        upsert_usage(
            [{"account": "alice@acme.com", "period": period, "sent": 9, "refused": 1} for period in periods]
        )

        history = accounts.get_send_usage("alice@acme.com", days=7)
        self.assertEqual(
            [day["date"] for day in history["days"]],
            [f"{moment:%Y-%m-%d}" for moment in (today, two_days_ago, first_day)],
        )
        self.assertEqual(
            history["days"][0], {"date": f"{today:%Y-%m-%d}", "sent": 9, "refused": 1, "limit": 100}
        )
        months = {f"{moment:%Y-%m}" for moment in (today, first_day)}
        self.assertEqual({month["month"] for month in history["months"]}, months)

        self.act_as(self.other)
        self.assertRaises(frappe.DoesNotExistError, accounts.get_send_usage, "alice@acme.com")


class TestQuotaPlan(SendingLimitsTestCase):
    def test_the_plan_carries_suite_cloud_s_hook(self) -> None:
        configure_settings(enable_telemetry=0, public_url="https://cloud.example.test")
        operations = plan.cluster_plan(self.cluster)
        hooks = [op for op in operations if op["object"] == "MtaHook"]
        self.assertEqual(
            [(op["@type"], op.get("matchOn")) for op in hooks],
            [("upsert", ["httpHeaders"])],
        )

        hook = hooks[0]["value"]["sending-limits"]
        self.assertEqual(
            hook["url"], "https://cloud.example.test/api/v2/method/suite_cloud.api.sending_limits.check"
        )
        self.assertEqual(
            (hook["enable"], hook["stages"], hook["timeout"], hook["tempFailOnError"]),
            (quota.SIGNED_IN, {"rcpt": True}, 1000, False),
        )
        self.assertEqual(hook["httpHeaders"], {"X-Suite-Cloud-Hook": "sending-limits"})
        self.assertEqual(
            hook["httpAuth"]["bearerToken"],
            {"@type": "EnvironmentVariable", "variableName": "SUITE_CLOUD_TOKEN"},
        )
        self.assertNotIn(self.token, plan.to_ndjson(operations))

        # A running node without its token keeps the hook off: the reload would fail on it.
        self.node.db_set("status", "Active")
        self.assertEqual(quota.hook_operation(self.cluster)["value"]["sending-limits"]["enable"], quota.NEVER)
        self.node.db_set("suite_cloud_token_installed", 1)
        self.assertEqual(
            quota.hook_operation(self.cluster)["value"]["sending-limits"]["enable"], quota.SIGNED_IN
        )

        throttle = next(op for op in operations if op["object"] == "MtaInboundThrottle")["value"][
            "quota-backstop"
        ]
        # Only signed-in sessions: unauthenticated ones would share one bucket otherwise. Telemetry off
        # does not switch it off.
        self.assertEqual(throttle["match"], quota.SIGNED_IN)
        self.assertEqual(
            (throttle["enable"], throttle["key"], throttle["rate"]),
            (True, {"authenticatedAs": True}, {"count": 2000, "period": 86_400_000}),
        )

        # The cap is the cluster's and is never off: nothing else caps an account while Suite Cloud is down.
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
        recovery = {op["object"] for op in plan.recovery_plan(self.cluster)}
        self.assertIn("MtaHook", recovery)
        self.assertNotIn("MtaInboundThrottle", recovery)

    def test_a_push_keeps_one_suite_cloud_hook(self) -> None:
        self.cluster.push_config()
        self.cluster.push_config()  # a second push finds the hook by its header
        hooks = self.fake.all("MtaHook")
        self.assertEqual([hook["httpHeaders"] for hook in hooks], [{"X-Suite-Cloud-Hook": "sending-limits"}])

    def test_the_patch_turns_the_backstop_on_without_a_config_push(self) -> None:
        self.cluster.db_set("max_daily_send_limit", 0)
        with patch.object(push, "resync_cluster_after_commit") as resync:
            rename_backstop_to_max_daily_send_limit.execute()
        self.assertEqual(
            frappe.db.get_value("Stalwart Cluster", self.cluster.name, "max_daily_send_limit"), 2000
        )
        resync.assert_not_called()
