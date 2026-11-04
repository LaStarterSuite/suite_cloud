"""Each Stalwart Node and Egress Gateway has its own Suite Cloud token, ``<key>.<secret>``, which
its ``stalwart.env`` holds as ``SUITE_CLOUD_TOKEN``. Stalwart sends it on every request it makes to
Suite Cloud: the WebHooks, the OTLP push and the sending-limit hook.

A reload that names a variable a running server lacks fails. So a plan switches the objects that
send the token on only once every running server of the cluster, or the gateway, has it installed.
"""

import hashlib
import secrets
from typing import TYPE_CHECKING

import frappe
from frappe.utils.password import get_decrypted_password

if TYPE_CHECKING:
    from frappe.model.document import Document

TOKEN_VARIABLE = "SUITE_CLOUD_TOKEN"
RUNNING_STATUSES = ("Provisioned", "Active", "Draining")  # Stalwart runs normally, env file and all
SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")
SERVER_TTL = 300  # seconds; a save clears it sooner, this only covers a missed clear
SEPARATOR = "."  # no ":", so the token also passes as Basic userinfo that Frappe's API key check skips


def fill_token(server: Document) -> None:
    """A Stalwart Node or Egress Gateway gets its own key and secret when it is created."""

    server.suite_cloud_key = server.suite_cloud_key or frappe.generate_hash(length=20)
    if not server.suite_cloud_secret:
        server.suite_cloud_secret = frappe.generate_hash(length=40)


def get_token(server: Document) -> str:
    return f"{server.suite_cloud_key}{SEPARATOR}{server.get_password('suite_cloud_secret')}"


def bearer_auth() -> dict:
    """Each server sends its own token: the variable is read on the server that sends."""

    return {
        "@type": "Bearer",
        "bearerToken": {"@type": "EnvironmentVariable", "variableName": TOKEN_VARIABLE},
    }


def get_owner(server: Document) -> Document:
    """Whose plan names the server's token: its cluster's for a node, its own for a gateway."""

    return server if server.doctype == "Egress Gateway" else server.get_cluster()


def has_every_token(owner: Document) -> bool:
    """Every server running the cluster's or gateway's plan has its token installed."""

    if owner.doctype == "Egress Gateway":
        return owner.status not in RUNNING_STATUSES or bool(owner.suite_cloud_token_installed)
    return not frappe.db.exists(
        "Stalwart Node",
        {"cluster": owner.name, "status": ("in", RUNNING_STATUSES), "suite_cloud_token_installed": 0},
    )


def get_server(token: str) -> frappe._dict | None:
    """The Stalwart Node or Egress Gateway whose key and secret make up ``token``; None for
    anything else."""

    key, separator, secret = token.partition(SEPARATOR)
    if not (separator and key and secret):
        return None
    server = get_server_record(key)
    given = hashlib.sha256(secret.encode()).hexdigest()
    if not server or not secrets.compare_digest(server["secret_sha256"], given):
        return None
    return frappe._dict(server)


def get_server_record(key: str) -> dict:
    """The server of ``key`` with the SHA-256 of its secret, cached: the sending-limit hook asks
    once per recipient. Empty for an unknown key, so a guessed one reads the database only once."""

    cache_key = get_cache_key(key)
    server = frappe.cache.get_value(cache_key)
    if server is None:
        server = find_server(key)
        frappe.cache.set_value(cache_key, server, expires_in_sec=SERVER_TTL)
    return server


def find_server(key: str) -> dict:
    fields = ["name", "cluster", "hostname", "ipv4_address"]
    for doctype in SERVER_DOCTYPES:
        if server := frappe.db.get_value(doctype, {"suite_cloud_key": key}, fields, as_dict=True):
            secret = get_decrypted_password(doctype, server.name, "suite_cloud_secret", raise_exception=False)
            if not secret:
                return {}
            return {
                **server,
                "doctype": doctype,
                "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            }
    return {}


def clear_server_after_commit(server: Document) -> None:
    """After commit: a clear before it would let another worker cache the old secret again."""

    if server.suite_cloud_key:
        cache_key = get_cache_key(server.suite_cloud_key)
        frappe.db.after_commit.add(lambda: frappe.cache.delete_value(cache_key))


def get_cache_key(key: str) -> str:
    return f"suite_cloud:server_token:{key}"
