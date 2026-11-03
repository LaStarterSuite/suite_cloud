# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.dns.resolver import verify_ptr_record
from suite_cloud.mail.cluster import bootstrap, dns, naming, telemetry
from suite_cloud.provisioning.ansible import ping
from suite_cloud.provisioning.ssh import SSHTarget, scan_host_keys, validate_ssh_user_field
from suite_cloud.utils import log_exception

REMOVABLE_STATUSES = ("Pending", "Failed", "Disabled")


class StalwartNode(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        cluster: DF.Link
        enabled: DF.Check
        hostname: DF.Data
        in_ingress_dns: DF.Check
        installed_version: DF.Data | None
        ipv4_address: DF.Data
        ipv6_address: DF.Data | None
        is_bootstrap_node: DF.Check
        last_error: DF.SmallText | None
        last_health_at: DF.Datetime | None
        node_id: DF.Int
        provisioned_at: DF.Datetime | None
        ptr_verified: DF.Check
        role: DF.Literal["full", "frontend", "outbound"]
        ssh_port: DF.Int
        ssh_user: DF.Data | None
        ssh_verified: DF.Check
        telemetry_key: DF.Data | None
        telemetry_secret: DF.Password | None
        telemetry_token_installed: DF.Check
        title: DF.Data | None
        status: DF.Literal[
            "Pending", "Provisioning", "Provisioned", "Active", "Draining", "Failed", "Disabled"
        ]
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def autoname(self) -> None:
        self.hostname = naming.next_hostname("Stalwart Node", self.cluster, naming.NODE_PREFIX)
        self.name = self.hostname

    def before_insert(self) -> None:
        telemetry.fill_token(self)

    def validate(self) -> None:
        validate_ssh_user_field(self)
        cluster = self.get_cluster()
        self.title = (self.title or "").strip() or self.hostname
        self.hostname = (self.hostname or "").strip().lower().rstrip(".")
        suffix = f".{cluster.default_domain}"
        if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
            frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

        self.validate_single_node(cluster)
        if self.enabled and (self.is_new() or self.has_value_changed("enabled")):
            cluster.validate_node_count(cluster.node_count() + 1)  # this node joins the count
        self.ipv4_address = validate_ip(self.ipv4_address, 4)
        self.ipv6_address = validate_ip(self.ipv6_address, 6) if self.ipv6_address else None
        self.ssh_user = self.ssh_user or cluster.ssh_user
        self.ssh_port = self.ssh_port or cluster.ssh_port
        if self.is_new():
            self.status = "Pending"

        if not self.is_new() and (
            self.has_value_changed("ipv4_address") or self.has_value_changed("ssh_port")
        ):
            self.ssh_verified = 0
            self.ssh_host_keys = None  # a different box answers there; its key is unknown again
        elif self.has_value_changed("ipv4_address") or self.has_value_changed("ipv6_address"):
            self.ssh_verified = 0

    def validate_single_node(self, cluster: Document) -> None:
        """Embedded stores live on one VPS: no second node, and roles that split work make no sense."""

        if not cluster.single_node:
            return
        other = frappe.db.get_value("Stalwart Node", {"cluster": cluster.name, "name": ["!=", self.name]})
        if other:
            frappe.throw(
                _("Cluster {0} uses an embedded store and can only have one node ({1}).").format(
                    cluster.name, other
                )
            )
        if self.role != "full":
            frappe.throw(_("The only node of a cluster must have the full role."))

    def after_insert(self) -> None:
        dns.sync_node_records(self)
        self.revalidate_cluster()

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if not before:
            return
        if (before.ipv4_address, before.ipv6_address) != (self.ipv4_address, self.ipv6_address):
            dns.sync_node_records(self)
            dns.sync_spf_record(self.get_cluster())

        if before.enabled and not self.enabled and self.status in ("Active", "Provisioned"):
            bootstrap.drain_node(self)

    def on_trash(self) -> None:
        if self.status not in REMOVABLE_STATUSES:
            frappe.throw(_("Drain and disable the node before deleting it."))
        cluster = self.get_cluster()
        if self.is_bootstrap_node and cluster.status == "Active":
            frappe.throw(_("The bootstrap node of an active cluster cannot be deleted."))
        if cluster.bootstrap_node == self.name:
            # A failed first attempt: let another node bootstrap the cluster.
            cluster.db_set({"bootstrap_node": None, "status": "Pending"}, update_modified=False)

        bootstrap.forget_node(self)
        dns.delete_node_records(self)

    def after_delete(self) -> None:
        dns.sync_spf_record(self.get_cluster())

    # --- helpers --------------------------------------------------------------

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def revalidate_cluster(self) -> None:
        """Saves a fresh copy of the cluster, which checks its stores against the new node count."""

        frappe.get_doc("Stalwart Cluster", self.cluster).save(ignore_permissions=True)

    def ssh_target(self) -> SSHTarget:
        cluster = self.get_cluster()
        return SSHTarget(
            host=self.ipv4_address,
            user=self.ssh_user or cluster.ssh_user,
            port=cint(self.ssh_port or cluster.ssh_port),
            private_key=cluster.get_password("ssh_private_key"),
            host_keys=self.ssh_host_keys,
        )

    def set_status(self, status: str, error: str | None = None) -> None:
        values = {"status": status}
        if error is not None:
            values["last_error"] = error[:1000]
        self.db_set(values, update_modified=False, notify=True)

    # --- actions --------------------------------------------------------------

    @frappe.whitelist()
    def verify_ssh(self) -> bool:
        frappe.only_for("System Manager")
        if not self.ssh_host_keys:
            # First contact: the operator has just put the cluster's key on this box, so the key it
            # presents now is the one every later connection must match.
            try:
                self.db_set(
                    "ssh_host_keys",
                    scan_host_keys(self.ipv4_address, cint(self.ssh_port)),
                    update_modified=False,
                )
            except Exception as e:
                self.db_set({"ssh_verified": 0, "last_error": str(e)}, update_modified=False)
                frappe.msgprint(_("SSH connection failed: {0}").format(e), indicator="red")
                return False
        ok, detail = ping(self.ssh_target())
        self.db_set({"ssh_verified": cint(ok), "last_error": None if ok else detail}, update_modified=False)
        if ok:
            frappe.msgprint(_("SSH connection verified."), indicator="green", alert=True)
        else:
            frappe.msgprint(_("SSH connection failed: {0}").format(detail), indicator="red")
        return ok

    @frappe.whitelist()
    def reset_ssh_host_keys(self) -> None:
        """Forgets the pinned host keys after the server was reinstalled; Verify SSH records the new ones."""

        frappe.only_for("System Manager")
        self.db_set({"ssh_host_keys": None, "ssh_verified": 0}, update_modified=False)
        # The next Verify SSH trusts whatever answers, so record who dropped the pin and when.
        self.add_comment("Info", _("Reset the SSH host keys."))

    @frappe.whitelist()
    def provision(self) -> str:
        frappe.only_for("System Manager")
        if not self.ssh_verified:
            frappe.throw(_("Verify the SSH connection first."))
        if not self.enabled:
            frappe.throw(_("Enable the node first."))
        return bootstrap.provision_node(self).name

    @frappe.whitelist()
    def upgrade(self) -> str:
        frappe.only_for("System Manager")
        return bootstrap.upgrade_node(self).name

    @frappe.whitelist()
    def install_telemetry_token(self) -> str:
        frappe.only_for("System Manager")
        from suite_cloud.telemetry.install import install_telemetry

        return install_telemetry(self).name

    @frappe.whitelist()
    def rotate_telemetry_token(self) -> str | None:
        """A new telemetry secret; a job writes it to stalwart.env and restarts Stalwart."""

        frappe.only_for("System Manager")
        from suite_cloud.telemetry.install import rotate_telemetry_token

        return rotate_telemetry_token(self)

    @frappe.whitelist()
    def drain(self) -> None:
        frappe.only_for("System Manager")
        bootstrap.drain_node(self)

    @frappe.whitelist()
    def restore(self) -> None:
        frappe.only_for("System Manager")
        bootstrap.restore_node(self)

    @frappe.whitelist()
    def check_health(self) -> bool:
        frappe.only_for("System Manager")
        return bootstrap.check_node(self)

    @frappe.whitelist()
    def verify_ptr(self) -> bool:
        frappe.only_for("System Manager")
        ok = verify_ptr_record(self.ipv4_address, self.hostname)
        if ok is None:
            return bool(self.ptr_verified)  # the lookup failed; the last known state stands
        self.db_set("ptr_verified", cint(ok), update_modified=False)
        return ok

    # --- Server Job callbacks -----------------------------------------------------

    def after_provision(self, job: Document) -> None:
        bootstrap.after_provision(self, job)

    def after_provision_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)
        cluster = self.get_cluster()
        exhausted = (job.retries or 0) > (job.max_retries or 0)
        if self.is_bootstrap_node and cluster.status == "Bootstrapping" and exhausted:
            cluster.db_set("status", "Failed", update_modified=False)

    def after_upgrade(self, job: Document) -> None:
        bootstrap.after_upgrade(self, job)

    def after_upgrade_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)

    def after_install_telemetry(self, job: Document) -> None:
        from suite_cloud.telemetry.install import mark_token_installed

        mark_token_installed(self)


def on_doctype_update() -> None:
    """Every telemetry batch looks its node up by cluster and address."""

    frappe.db.add_index("Stalwart Node", ["cluster", "ipv4_address"])


def validate_ip(value: str | None, version: int) -> str:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        frappe.throw(_("{0} is not a valid IP address.").format(value))
    if address.version != version:
        frappe.throw(_("{0} is not an IPv{1} address.").format(value, version))
    return str(address)


def poll_pending_nodes() -> None:
    """Cron: moves provisioned nodes to Active once the cluster registry (or TLS) confirms them."""

    for name in frappe.get_all("Stalwart Node", {"status": "Provisioned"}, pluck="name"):
        node = frappe.get_doc("Stalwart Node", name)
        try:
            bootstrap.check_node(node)
        except Exception:
            frappe.db.rollback()
            log_exception(f"Health check failed for {name}", node)
            continue
        if not frappe.in_test:
            frappe.db.commit()


def verify_all_ptr_records() -> None:
    for name in frappe.get_all("Stalwart Node", {"enabled": 1}, pluck="name"):
        node = frappe.get_doc("Stalwart Node", name)
        ok = verify_ptr_record(node.ipv4_address, node.hostname)
        if ok is not None:
            node.db_set("ptr_verified", cint(ok), update_modified=False)
