# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What an account sent: one row per account and UTC period, unique on the two. The sending-limit
counters live in the Redis cache (``suite_cloud.mail.sending_limits``), which flushes them here
every minute; rows are kept for the retention in Suite Cloud Settings."""

import hashlib
from datetime import UTC, datetime, timedelta

import frappe
from frappe.model.document import Document
from frappe.query_builder import Case, CustomFunction
from frappe.utils import cint
from pypika.terms import Values

from suite_cloud.utils import get_config

DEFAULT_RETENTION_DAYS = 90
Greatest = CustomFunction("GREATEST", ["first", "second"])


class MailSendUsage(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        account: DF.Link
        period: DF.Data
        refused: DF.Int
        reported_at: DF.Datetime | None
        send_limit: DF.Int
        sent: DF.Int
    # end: auto-generated types

    pass


def on_doctype_update() -> None:
    """The upsert finds a period's row by this key."""

    frappe.db.add_unique("Mail Send Usage", ["account", "period"], constraint_name="account_period")


def usage_name(account: str, period: str) -> str:
    """A fixed-length name, since an account's name and a period together can pass the name column."""

    return hashlib.sha1(f"{account}:{period}".encode()).hexdigest()


def periods_at(moment: datetime) -> tuple[str, str]:
    """The day and month keys the counters use, in UTC."""

    moment = moment.astimezone(UTC)
    return f"d:{moment:%Y%m%d}", f"m:{moment:%Y%m}"


def current_periods() -> tuple[str, str]:
    return periods_at(datetime.now(UTC))


def reportable_periods() -> set[str]:
    """Today, this month and the ones before: a flush may run just after midnight."""

    now = datetime.now(UTC)
    return {*periods_at(now), *periods_at(now - timedelta(days=1))}


def sent_by_account(accounts: list[str]) -> dict[str, dict[str, int]]:
    """``{account: {day, month, refused_day, refused_month}}`` for the current periods: recipients
    sent and refused, in one query."""

    day, month = current_periods()
    usage = {account: {"day": 0, "month": 0, "refused_day": 0, "refused_month": 0} for account in accounts}
    if not accounts:
        return usage
    rows = frappe.get_all(
        "Mail Send Usage",
        filters={"account": ("in", accounts), "period": ("in", (day, month))},
        fields=["account", "period", "sent", "refused"],
    )
    for row in rows:
        key = "day" if row.period == day else "month"
        usage[row.account][key] = row.sent
        usage[row.account][f"refused_{key}"] = row.refused
    return usage


def usage_history(account: str, days: int) -> dict[str, list[dict]]:
    """The account's totals of the last ``days`` UTC days, today included, and of their months,
    newest first, each with the limit the account had while the period was current."""

    first_day, first_month = periods_at(datetime.now(UTC) - timedelta(days=days - 1))
    today, this_month = current_periods()
    usage = frappe.qb.DocType("Mail Send Usage")
    rows = (
        frappe.qb.from_(usage)
        .select(usage.period, usage.sent, usage.refused, usage.send_limit)
        .where(usage.account == account)
        .where(
            (usage.period.like("d:%") & usage.period[first_day:today])
            | (usage.period.like("m:%") & usage.period[first_month:this_month])
        )
        .orderby(usage.period, order=frappe.qb.desc)
        .run(as_dict=True)
    )
    history = {"days": [], "months": []}
    for row in rows:
        stamp = row.period[2:]
        if row.period.startswith("d:"):
            entry, key = {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"}, "days"
        else:
            entry, key = {"month": f"{stamp[:4]}-{stamp[4:]}"}, "months"
        history[key].append({**entry, "sent": row.sent, "refused": row.refused, "limit": row.send_limit})
    return history


def upsert_usage(rows: list[dict]) -> int:
    """Stores the ``{account, period, sent, refused}`` rows in one statement and returns how many
    it stored. A counter only grows within its period, so the higher count is kept, and a flush
    written twice changes nothing. A current period takes the account's limit as it is now; a past
    one keeps the limit it was stored with, so a late flush after a limit change does not rewrite
    it."""

    if not rows:
        return 0
    limits = limits_by_account(list({row["account"] for row in rows}))
    rows = [row for row in rows if row["account"] in limits]  # deleted meanwhile
    if not rows:
        return 0
    # One lock order for every writer, so two flushes of the same accounts do not deadlock.
    rows.sort(key=lambda row: (row["account"], row["period"]))
    now = frappe.utils.now()
    user = frappe.session.user
    usage = frappe.qb.DocType("Mail Send Usage")
    limit_while_current = (
        Case().when(usage.period.isin(current_periods()), Values(usage.send_limit)).else_(usage.send_limit)
    )
    query = frappe.qb.into(usage).columns(
        usage.name,
        usage.account,
        usage.period,
        usage.sent,
        usage.refused,
        usage.send_limit,
        usage.reported_at,
        usage.creation,
        usage.modified,
        usage.owner,
        usage.modified_by,
    )
    for row in rows:
        limit = limits[row["account"]][row["period"][0]]
        query = query.insert(
            usage_name(row["account"], row["period"]),
            row["account"],
            row["period"],
            row["sent"],
            row["refused"],
            limit,
            now,
            now,
            now,
            user,
            user,
        )
    query = (
        query.on_duplicate_key_update(usage.sent, Greatest(usage.sent, Values(usage.sent)))
        .on_duplicate_key_update(usage.refused, Greatest(usage.refused, Values(usage.refused)))
        .on_duplicate_key_update(usage.send_limit, limit_while_current)
        .on_duplicate_key_update(usage.reported_at, Values(usage.reported_at))
        .on_duplicate_key_update(usage.modified, Values(usage.modified))
    )
    query.run()
    return len(rows)


def limits_by_account(accounts: list[str]) -> dict[str, dict[str, int]]:
    """``{account: {"d": daily, "m": monthly}}``, keyed like the first letter of a period."""

    rows = frappe.get_all(
        "Mail Account",
        filters={"name": ("in", accounts)},
        fields=["name", "daily_send_limit", "monthly_send_limit"],
    )
    return {row.name: {"d": row.daily_send_limit, "m": row.monthly_send_limit} for row in rows}


def delete_usage(account: str) -> None:
    frappe.db.delete("Mail Send Usage", {"account": account})


def prune_usage_job() -> None:
    """Scheduled daily, and the only place that commits: one expired period per transaction, so a
    shortened retention never deletes months of rows at once."""

    for period in get_expired_periods():
        frappe.db.delete("Mail Send Usage", {"period": period})
        if not frappe.in_test:
            frappe.db.commit()


def get_expired_periods() -> list[str]:
    """The days older than the retention and the months that ended before it, oldest first."""

    day, month = periods_at(datetime.now(UTC) - timedelta(days=retention_days()))
    usage = frappe.qb.DocType("Mail Send Usage")
    old_days = usage.period.like("d:%") & (usage.period < day)
    old_months = usage.period.like("m:%") & (usage.period < month)
    return sorted(
        frappe.qb.from_(usage).select(usage.period).distinct().where(old_days | old_months).run(pluck=True)
    )


def retention_days() -> int:
    """The configured retention; anything under a day falls back to the default, since a negative
    number would move the cutoff into the future and delete today's counts."""

    days = cint(get_config("send_usage_retention_days"))
    return days if days > 0 else DEFAULT_RETENTION_DAYS
