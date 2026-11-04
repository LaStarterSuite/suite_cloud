"""Sending limits, enforced in Suite Cloud: Stalwart's RCPT hook asks for each recipient of a
signed-in sender, and ``count_recipient`` checks and counts it in one atomic step.

Limits and counters live in the Redis cache under ``sending_limits:``, through raw Redis calls:
Frappe's own helpers pickle values and keep a copy per request. Counter keys carry their UTC day or
month, so nothing resets them. ``flush_usage`` copies the counters to Mail Send Usage every minute,
and a counter Redis lost is seeded again from there.
"""

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import frappe
import redis
from frappe.utils import cint

from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import (
    periods_at,
    reportable_periods,
    upsert_usage,
)

LIMIT_TTL = 3600  # seconds; a cleared limit is read again sooner, this only covers a missed clear
DAY_TTL = 3 * 86400
MONTH_TTL = 35 * 86400
FLUSH_BATCH = 5000
MESSAGE_LIMIT = 199  # characters; Stalwart sends the message as one SMTP reply line
TOUCHED = "sending_limits:touched"
FLUSHING = "sending_limits:touched:flushing"
SEED = b"seed"

# KEYS: day counter, month counter, touched set. ARGV: daily limit, monthly limit, day TTL, month
# TTL, day member, month member, then optionally the seed: day sent, day refused, month sent, month
# refused. Returns the verdict ("", "daily" or "monthly"), the day and the month sent counts.
CHECK_SCRIPT = """
local day, month, touched = KEYS[1], KEYS[2], KEYS[3]
if #ARGV >= 10 then
  redis.call('HSETNX', day, 'sent', ARGV[7])
  redis.call('HSETNX', day, 'refused', ARGV[8])
  redis.call('HSETNX', month, 'sent', ARGV[9])
  redis.call('HSETNX', month, 'refused', ARGV[10])
elseif redis.call('EXISTS', day) == 0 or redis.call('EXISTS', month) == 0 then
  return {'seed'}
end
local day_sent = tonumber(redis.call('HGET', day, 'sent') or '0')
local month_sent = tonumber(redis.call('HGET', month, 'sent') or '0')
local verdict = ''
if day_sent + 1 > tonumber(ARGV[1]) then
  verdict = 'daily'
elseif month_sent + 1 > tonumber(ARGV[2]) then
  verdict = 'monthly'
end
if verdict == '' then
  day_sent = redis.call('HINCRBY', day, 'sent', 1)
  month_sent = redis.call('HINCRBY', month, 'sent', 1)
else
  redis.call('HINCRBY', day, 'refused', 1)
  redis.call('HINCRBY', month, 'refused', 1)
end
redis.call('EXPIRE', day, ARGV[3])
redis.call('EXPIRE', month, ARGV[4])
redis.call('SADD', touched, ARGV[5], ARGV[6])
return {verdict, day_sent, month_sent}
"""


class Limits(NamedTuple):
    cluster: str  # empty for an address that is no Mail Account
    daily: int
    monthly: int


def get_client() -> redis.Redis:
    """The cache's connections, without Frappe's pickling wrapper."""

    return redis.Redis(connection_pool=frappe.cache.connection_pool)


def make_key(key: str) -> bytes:
    return frappe.cache.make_key(key)


def count_recipient(cluster: str, account: str) -> str | None:
    """Counts one recipient of ``account`` as sent or refused, and returns the refusal message, or
    None when the recipient may be sent. An account of another cluster, or an address that is no
    Mail Account, is not counted."""

    limits = get_limits(account)
    if limits.cluster != cluster:
        return None
    now = datetime.now(UTC)
    day, month = periods_at(now)
    verdict = run_check(account, day, month, limits)
    if verdict == SEED:
        verdict = run_check(account, day, month, limits, get_seed(account, day, month))
    return get_refusal_message(verdict.decode(), account, limits, now) if verdict else None


def get_limits(account: str) -> Limits:
    """The account's cluster and limits, from the cache or else from Mail Account."""

    client = get_client()
    key = make_key(f"sending_limits:limit:{account}")
    if cached := client.hgetall(key):
        return Limits(cached[b"cluster"].decode(), int(cached[b"daily"]), int(cached[b"monthly"]))
    row = frappe.db.get_value(
        "Mail Account", account, ["cluster", "daily_send_limit", "monthly_send_limit"], as_dict=True
    )
    limits = (
        Limits(row.cluster or "", cint(row.daily_send_limit), cint(row.monthly_send_limit))
        if row
        else Limits("", 0, 0)
    )
    with client.pipeline() as pipe:
        pipe.hset(key, mapping=limits._asdict())
        pipe.expire(key, LIMIT_TTL)
        pipe.execute()
    return limits


def run_check(account: str, day: str, month: str, limits: Limits, seed: list[int] | None = None) -> bytes:
    client = get_client()
    keys = [counter_key(account, day), counter_key(account, month), make_key(TOUCHED)]
    args = [limits.daily, limits.monthly, DAY_TTL, MONTH_TTL, f"{account}|{day}", f"{account}|{month}"]
    return client.register_script(CHECK_SCRIPT)(keys=keys, args=[*args, *(seed or [])], client=client)[0]


def counter_key(account: str, period: str) -> bytes:
    return make_key(f"sending_limits:count:{account}:{period}")


def get_seed(account: str, day: str, month: str) -> list[int]:
    """Day sent, day refused, month sent and month refused as Mail Send Usage last stored them."""

    rows = frappe.get_all(
        "Mail Send Usage",
        filters={"account": account, "period": ("in", (day, month))},
        fields=["period", "sent", "refused"],
    )
    counts = {row.period: [row.sent, row.refused] for row in rows}
    return [*counts.get(day, [0, 0]), *counts.get(month, [0, 0])]


def get_refusal_message(verdict: str, account: str, limits: Limits, now: datetime) -> str:
    if verdict == "daily":
        label, limit, resets = "Daily", limits.daily, now.date() + timedelta(days=1)
    else:
        label, limit = "Monthly", limits.monthly
        resets = (now.date().replace(day=1) + timedelta(days=32)).replace(day=1)
    message = (
        f"{label} sending limit of {limit:,} recipients reached for {account}. "
        f"It resets at {resets:%Y-%m-%d} 00:00 UTC."
    )
    return message[:MESSAGE_LIMIT]


def clear_limits_after_commit(account: str) -> None:
    """After commit: a clear before it would let another worker cache the old row again."""

    key = make_key(f"sending_limits:limit:{account}")
    frappe.db.after_commit.add(lambda: get_client().delete(key))


def flush_usage() -> None:
    """Every minute: copies the counters written since the last run to Mail Send Usage. A failed
    run leaves its set in place for the next run, which writes it again; the upsert keeps the
    higher count, so a second write changes nothing."""

    client = get_client()
    touched, flushing = make_key(TOUCHED), make_key(FLUSHING)
    if not client.exists(flushing):
        if not client.exists(touched):
            return
        client.rename(touched, flushing)
    rows = get_usage_rows(client, client.smembers(flushing))
    for start in range(0, len(rows), FLUSH_BATCH):
        upsert_usage(rows[start : start + FLUSH_BATCH])
    if not frappe.in_test:
        frappe.db.commit()
    client.delete(flushing)


def get_usage_rows(client: redis.Redis, members: set[bytes]) -> list[dict]:
    """``{account, period, sent, refused}`` of each ``<account>|<period>`` member whose period may
    still be stored and whose counter has not expired."""

    periods = reportable_periods()
    pairs = [member.decode().rpartition("|")[::2] for member in members]
    pairs = [(account, period) for account, period in pairs if period in periods]
    with client.pipeline(transaction=False) as pipe:
        for account, period in pairs:
            pipe.hgetall(counter_key(account, period))
        counters = pipe.execute()
    return [
        {
            "account": account,
            "period": period,
            "sent": int(counts[b"sent"]),
            "refused": int(counts[b"refused"]),
        }
        for (account, period), counts in zip(pairs, counters, strict=True)
        if counts
    ]
