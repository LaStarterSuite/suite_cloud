from contextlib import suppress

import frappe
from frappe import _
from frappe.utils import sbool

from suite_cloud.api.mail import aliases
from suite_cloud.api.tenant import (
    MEMBERSHIP_CAP,
    SendLimitsNotAllowedError,
    as_alias_rows,
    as_list,
    current_tenant,
    mail_api,
    owned,
    owned_page,
    page_size,
    visible,
)
from suite_cloud.mail.doctype.mail_account.mail_account import (
    account_payloads,
    validate_password,
)
from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import usage_history
from suite_cloud.mail.tenancy import account_credentials, sync
from suite_cloud.mail.tenancy import quotas as quota_rows
from suite_cloud.mail.tenancy.account_credentials import API_KEYS, APP_PASSWORDS, Kind
from suite_cloud.mail.tenancy.usage import used_disk_by_name
from suite_cloud.utils import local_datetime

ACCOUNT_PAGE_CAP = 200


@frappe.whitelist(methods=["GET"])
@mail_api
def list_accounts(
    domain: str | None = None, search: str | None = None, start: int = 0, limit: int = 50
) -> dict:
    filters = {"domain": owned("Mail Domain", domain).name} if domain else None
    names, total = owned_page(
        "Mail Account", search, start, limit, ACCOUNT_PAGE_CAP, ("name", "display_name"), filters
    )
    return {"items": account_payloads(names), "total": total}


@frappe.whitelist(methods=["GET"])
@mail_api
def get_account(email: str) -> dict:
    return owned("Mail Account", email).to_api(with_usage=True)


HISTORY_DAYS_CAP = 400


@frappe.whitelist(methods=["GET"])
@mail_api
def get_send_usage(email: str, days: int = 30) -> dict:
    """The account's sent and refused recipients per UTC day for the last ``days`` (at most
    400) and per month, newest first, each with the limit it had then; only as far back as the
    Sending Usage Retention keeps."""

    return usage_history(owned("Mail Account", email).name, page_size(days, HISTORY_DAYS_CAP))


QUOTA_LOOKUP_LIMIT = 500


@frappe.whitelist(methods=["GET"])
@mail_api
def get_quotas(emails: list[str] | str) -> dict:
    """``{email: {disk_quota_gb, used_disk_bytes}}`` for the tenant's accounts among ``emails``;
    a ``disk_quota_gb`` of 0 means no limit.

    The quotas are one query; usage is one cluster call per ``maxObjectsInGet`` accounts, so a
    list page shows both for every row.
    """

    wanted = [e.strip().lower() for e in as_list(emails) if e and e.strip()][:QUOTA_LOOKUP_LIMIT]
    if not wanted:
        return {}
    rows = frappe.get_all(
        "Mail Account",
        filters=visible("Mail Account", {"name": ["in", wanted]}),
        fields=["name", "stalwart_id", "cluster"],
    )
    quotas = quota_rows.disk_quota_gb_by_name("Mail Account", [row.name for row in rows])
    usage = used_disk_by_name(rows)
    return {
        row.name: {"disk_quota_gb": quotas[row.name], "used_disk_bytes": usage.get(row.name)} for row in rows
    }


@frappe.whitelist(methods=["POST"])
@mail_api
def create_account(
    email: str,
    password: str,
    display_name: str | None = None,
    description: str | None = None,
    aliases: list | str | None = None,
    groups: list[str] | str | None = None,
    mailing_lists: list[str] | str | None = None,
    disk_quota_gb: float | None = None,
    quotas: dict | str | None = None,
    locale: str | None = None,
    time_zone: str | None = None,
    disable_receiving: bool = False,
    daily_send_limit: int | None = None,
    monthly_send_limit: int | None = None,
) -> dict:
    """``disable_receiving`` makes the account send-only: mail addressed to it bounces. The send
    limits count recipients per UTC day and month, up to the cluster's max; left out, the
    tenant's defaults apply."""

    validate_password(password)

    tenant = current_tenant()
    assert_may_set_send_limits(tenant, daily_send_limit, monthly_send_limit)
    # Everything the account depends on is resolved first: a refusal after the insert would leave
    # the Stalwart account behind while the database change rolls back.
    lists = [owned("Mailing List", email_) for email_ in as_list(mailing_lists, MEMBERSHIP_CAP)]
    doc = frappe.get_doc(
        {
            "doctype": "Mail Account",
            "email": email,
            "tenant": tenant.name,
            "display_name": display_name,
            "description": description,
            "locale": locale or "en-US",
            "time_zone": time_zone,
            "disable_receiving": int(sbool(disable_receiving)),
            "daily_send_limit": daily_send_limit,
            "monthly_send_limit": monthly_send_limit,
            "aliases": as_alias_rows(aliases),
            "groups": [{"group": owned("Mail Group", g).name} for g in as_list(groups, MEMBERSHIP_CAP)],
        }
    )
    quota_rows.apply(doc, disk_quota_gb, quotas)
    doc.flags.password = password
    doc.flags.secrets_in_response = True
    doc.insert(ignore_permissions=True)

    # The list memberships are separate cluster calls after the insert: if one fails the row
    # rolls back, so the cluster account must go too or a retry meets "already exists".
    joined = []
    try:
        for mailing_list in lists:
            mailing_list.add_recipients([doc.email])
            joined.append(mailing_list)
    except Exception:
        for mailing_list in joined:  # the lists that took the address must let it go again
            with suppress(Exception):
                mailing_list.remove_recipients([doc.email])
        sync.push_destroy(doc, "accounts")
        raise

    frappe.local.response["http_status_code"] = 201
    # Returned once; rotate_app_password issues a fresh one later. Suite Cloud stores neither it
    # nor the account's own password.
    return {**doc.to_api(), "app_password": account_credentials.site_login_secret(doc, APP_PASSWORDS)}


@frappe.whitelist(methods=["PATCH"])
@mail_api
def update_account(
    email: str,
    display_name: str | None = None,
    description: str | None = None,
    disk_quota_gb: float | None = None,
    quotas: dict | str | None = None,
    locale: str | None = None,
    time_zone: str | None = None,
    disable_receiving: bool | None = None,
    daily_send_limit: int | None = None,
    monthly_send_limit: int | None = None,
) -> dict:
    """``quotas`` replaces the other limits (``{}`` lifts them all); the disk quota stays unless
    ``disk_quota_gb`` or a ``maxDiskQuota`` entry changes it, and 0 lifts it. ``disable_receiving``
    makes the account send-only or lets it receive again; left out, it stays as it is. So do the
    send limits."""

    doc = owned("Mail Account", email)
    assert_may_set_send_limits(current_tenant(), daily_send_limit, monthly_send_limit)
    for field, value in {
        "display_name": display_name,
        "description": description,
        "locale": locale,
        "time_zone": time_zone,
        "daily_send_limit": daily_send_limit,
        "monthly_send_limit": monthly_send_limit,
    }.items():
        if value is not None:
            doc.set(field, value)
    if disable_receiving is not None:
        doc.disable_receiving = int(sbool(disable_receiving))
    quota_rows.apply(doc, disk_quota_gb, quotas)
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["PUT"])
@mail_api
def set_account_enabled(email: str, enabled: bool) -> dict:
    doc = owned("Mail Account", email)
    doc.set_enabled(sbool(enabled))
    return doc.to_api()


@frappe.whitelist(methods=["PUT"])
@mail_api
def set_password(email: str, password: str) -> None:
    doc = owned("Mail Account", email)
    doc.set_password(password)


@frappe.whitelist(methods=["POST"])
@mail_api
def rotate_app_password(email: str) -> dict:
    """Mints a new app password for the account, revokes the previous one, and returns it once."""

    return {"app_password": owned("Mail Account", email, for_update=True).mint_app_password()}


@frappe.whitelist(methods=["GET"])
@mail_api
def list_app_passwords(email: str) -> dict:
    return list_credentials(APP_PASSWORDS, email)


@frappe.whitelist(methods=["POST"])
@mail_api
def create_app_password(
    email: str,
    description: str = "Suite",
    expires_at: str | None = None,
    allowed_ips: list[str] | str | None = None,
) -> dict:
    return create_credential(APP_PASSWORDS, email, description, expires_at, allowed_ips)


@frappe.whitelist(methods=["PATCH"])
@mail_api
def update_app_password(
    email: str,
    id: str,
    description: str | None = None,
    expires_at: str | None = None,
    allowed_ips: list[str] | str | None = None,
) -> dict:
    return update_credential(APP_PASSWORDS, email, id, description, expires_at, allowed_ips)


@frappe.whitelist(methods=["DELETE"])
@mail_api
def delete_app_password(email: str, id: str) -> None:
    delete_credential(APP_PASSWORDS, email, id)


@frappe.whitelist(methods=["GET"])
@mail_api
def list_api_keys(email: str) -> dict:
    return list_credentials(API_KEYS, email)


@frappe.whitelist(methods=["POST"])
@mail_api
def create_api_key(
    email: str,
    description: str,
    expires_at: str | None = None,
    allowed_ips: list[str] | str | None = None,
) -> dict:
    return create_credential(API_KEYS, email, description, expires_at, allowed_ips)


@frappe.whitelist(methods=["PATCH"])
@mail_api
def update_api_key(
    email: str,
    id: str,
    description: str | None = None,
    expires_at: str | None = None,
    allowed_ips: list[str] | str | None = None,
) -> dict:
    return update_credential(API_KEYS, email, id, description, expires_at, allowed_ips)


@frappe.whitelist(methods=["DELETE"])
@mail_api
def delete_api_key(email: str, id: str) -> None:
    delete_credential(API_KEYS, email, id)


def list_credentials(kind: Kind, email: str) -> dict:
    """The credentials as the cluster holds them; no secret is part of the answer."""

    doc = owned("Mail Account", email)
    account_credentials.load(doc, kind)
    return {"items": [account_credentials.payload(row) for row in doc.get(kind.field)]}


def create_credential(kind: Kind, email: str, description: str, expires_at, allowed_ips) -> dict:
    """Answers 201 with the new credential and its secret, which Suite Cloud does not keep."""

    doc = owned("Mail Account", email, for_update=True)
    row = doc.append(
        kind.field,
        {
            "description": description,
            "expires_at": local_datetime(expires_at),
            "allowed_ips": "\n".join(as_list(allowed_ips)),
        },
    )
    doc.flags.secrets_in_response = True
    doc.save(ignore_permissions=True)

    frappe.local.response["http_status_code"] = 201
    return {**account_credentials.payload(row), "secret": doc.flags.credential_secrets[row.name]}


def update_credential(kind: Kind, email: str, id: str, description, expires_at, allowed_ips) -> dict:
    """Only the fields passed change; an empty ``expires_at`` or ``allowed_ips`` clears it."""

    doc = owned("Mail Account", email, for_update=True)
    row = account_credentials.row_by_id(doc, kind, id)
    if description is not None:
        row.description = description
    if expires_at is not None:
        row.expires_at = local_datetime(expires_at)
    if allowed_ips is not None:
        row.allowed_ips = "\n".join(as_list(allowed_ips))
    doc.save(ignore_permissions=True)
    return account_credentials.payload(row)


def delete_credential(kind: Kind, email: str, id: str) -> None:
    """Revokes one credential; the tenant's own app password is rotated, never deleted."""

    doc = owned("Mail Account", email, for_update=True)
    row = account_credentials.row_by_id(doc, kind, id)
    if row.description == account_credentials.SITE_LOGIN and kind.always_present:
        frappe.throw(
            _("Use rotate_app_password for the {0} app password.").format(account_credentials.SITE_LOGIN)
        )
    doc.remove(row)
    doc.save(ignore_permissions=True)
    frappe.local.response["http_status_code"] = 204


@frappe.whitelist(methods=["PUT"])
@mail_api
def set_aliases(email: str, aliases: list | str | None = None) -> dict:
    doc = owned("Mail Account", email)
    doc.set("aliases", as_alias_rows(aliases))
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@mail_api
def add_alias(email: str, alias: str, description: str | None = None) -> dict:
    return aliases.add("Mail Account", email, alias, description).to_api()


@frappe.whitelist(methods=["DELETE"])
@mail_api
def remove_alias(email: str, alias: str) -> dict:
    return aliases.remove("Mail Account", email, alias).to_api()


@frappe.whitelist(methods=["PUT"])
@mail_api
def set_alias_enabled(email: str, alias: str, enabled: bool) -> dict:
    return aliases.set_enabled("Mail Account", email, alias, sbool(enabled)).to_api()


@frappe.whitelist(methods=["PUT"])
@mail_api
def set_groups(email: str, groups: list[str] | str | None = None) -> dict:
    doc = owned("Mail Account", email)
    doc.set("groups", [{"group": owned("Mail Group", g).name} for g in as_list(groups, MEMBERSHIP_CAP)])
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["DELETE"])
@mail_api
def delete_account(email: str) -> None:
    doc = owned("Mail Account", email)
    doc.delete(ignore_permissions=True)
    frappe.local.response["http_status_code"] = 204


def assert_may_set_send_limits(tenant, daily: int | None, monthly: int | None) -> None:
    """Refused rather than ignored, so the tenant learns of the setting."""

    if (daily is not None or monthly is not None) and not tenant.allow_custom_send_limits:
        raise SendLimitsNotAllowedError(_("This tenant may not set send limits."))
