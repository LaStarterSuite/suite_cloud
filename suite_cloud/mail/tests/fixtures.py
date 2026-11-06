"""Fixtures for the Mail tests; the generic ones are re-exported from the core tests."""

from unittest.mock import patch

import frappe
from frappe.core.doctype.error_log.error_log import ErrorLog

from suite_cloud.tests.fixtures import (
    ROOT_DOMAIN,
    clear_request_cache,
    configure_settings,
    make_mail_region,
    make_zone,
    no_dns_provider,
)


def make_store(kind: str, type: str, title: str | None = None, **fields):
    doc = frappe.get_doc(
        {
            "doctype": "Mail Store",
            "title": title or f"{kind} {type}",
            "kind": kind,
            "type": type,
            **fields,
        }
    )
    doc.insert()
    return doc


def make_cluster(name: str = "blr-1", hostname: str | None = None, multi_node: bool = True, **fields):
    """``name`` becomes the title; the document is named by its hostname."""
    if multi_node:
        data = make_store("Data", "PostgreSql", host="db.example.test", auth_secret="pg-secret")
        memory = make_store("In-Memory", "Redis", url="redis://redis.example.test:6379")
        blob = make_store("Blob", "S3", region="ap-south-1", bucket="mail", access_key="AK", secret_key="SK")
    else:
        data = make_store("Data", "RocksDb", path="/var/lib/stalwart")
        memory = blob = None

    # ``hostname`` is accepted for readability; the cluster derives it from the region and zone.
    hostname = hostname or f"mail.blr.{ROOT_DOMAIN}"
    region = make_mail_region(hostname.split(".")[1])
    remove_cluster(hostname)
    cluster = frappe.get_doc(
        {
            "doctype": "Stalwart Cluster",
            "title": name,
            "region": region,
            "data_store": data.name,
            "blob_store": blob.name if blob else None,
            "in_memory_store": memory.name if memory else None,
            **fields,
        }
    )
    cluster.insert()
    return cluster


def remove_cluster(name: str) -> None:
    """Tests share one transaction per class, so a cluster left by an earlier test is torn down here."""

    if not frappe.db.exists("Stalwart Cluster", name):
        return

    nodes = frappe.get_all("Stalwart Node", {"cluster": name}, pluck="name")
    jobs = frappe.get_all("Server Job", {"server": ["in", nodes]}, pluck="name") if nodes else []
    if jobs:
        frappe.db.delete("Server Job Task", {"parent": ["in", jobs]})
        frappe.db.delete("Server Job", {"name": ["in", jobs]})
    frappe.db.delete("DNS Record", {"managed_by": ["in", [*nodes, name]]})
    for node in nodes:
        frappe.delete_doc("Stalwart Node", node, force=True, ignore_permissions=True, ignore_on_trash=True)
    frappe.delete_doc("Stalwart Cluster", name, force=True, ignore_permissions=True, ignore_on_trash=True)


def make_node(cluster, ipv4: str = "203.0.113.10", **fields):
    """Nodes name themselves n1, n2, ... in creation order."""

    fields.setdefault("ssh_host_keys", f"{ipv4} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTests")
    node = frappe.get_doc(
        {"doctype": "Stalwart Node", "cluster": cluster.name, "ipv4_address": ipv4, **fields}
    )
    node.insert()
    return node


def verified_ownership():
    """Every domain's ownership record resolves, so tests may add domains without publishing one."""

    return patch("suite_cloud.mail.tenancy.ownership.verify_dns_record", return_value=True)


def activate_cluster(cluster, token: str = "test-token"):
    """Marks a cluster active with a known API key so a FakeStalwart can serve it."""

    cluster.api_key = token
    cluster.save()
    cluster.db_set("status", "Active")
    cluster.reload()
    return cluster


TENANT_OWNER = "owner@frappe.test"


def make_owner(email: str = TENANT_OWNER, with_role: bool = True) -> str:
    """A user holding the Suite Tenant role, as a tenant's owner must; nothing else."""

    roles = [{"role": "Suite Tenant"}] if with_role else []
    if not frappe.db.exists("User", email):
        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "Owner",
                "send_welcome_email": 0,
                "roles": roles,
            }
        ).insert(ignore_permissions=True)
    return email


def make_tenant(cluster, title: str = "acme.frappe.test", **fields):
    frappe.db.delete("Suite Tenant", {"title": title})
    fields.setdefault("user", make_owner())
    tenant = frappe.get_doc(
        {"doctype": "Suite Tenant", "title": title, "stalwart_cluster": cluster.name, **fields}
    )
    tenant.insert()
    return tenant


def error_log_count() -> int:
    """Error Log lives on Frappe's log database as a virtual DocType, so raw SQL cannot see it,
    and its list stops at one page of 20 rows; the controller counts them all."""

    return ErrorLog.get_count("Error Log")


def latest_error_log_method() -> str | None:
    rows = frappe.get_all("Error Log", pluck="method", order_by="creation desc", limit_page_length=1)
    return rows[0] if rows else None
