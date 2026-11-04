# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.mail.cluster import dns, egress, naming, plan, server_token
from suite_cloud.mail.doctype.stalwart_node.stalwart_node import validate_ip
from suite_cloud.mail.stalwart import get_admin_client, get_client
from suite_cloud.provisioning.ansible import ping
from suite_cloud.provisioning.ssh import SSHTarget, scan_host_keys, validate_ssh_user_field
from suite_cloud.telemetry.install import mark_token_installed, rotate_token
from suite_cloud.utils import dkim_algorithms, get_config, log_exception, validate_version


class EgressGateway(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        admin_password: DF.Password | None
        admin_username: DF.Data | None
        api_key: DF.Password | None
        base_url: DF.Data | None
        cluster: DF.Link
        config_plan: DF.Code | None
        config_version: DF.Int
        data_store: DF.Link | None
        enabled: DF.Check
        hostname: DF.Data
        installed_version: DF.Data | None
        ipv4_address: DF.Data
        last_config_sync_at: DF.Datetime | None
        last_error: DF.SmallText | None
        provisioned_at: DF.Datetime | None
        ssh_port: DF.Int
        ssh_user: DF.Data | None
        ssh_verified: DF.Check
        stalwart_version: DF.Data | None
        suite_cloud_key: DF.Data | None
        suite_cloud_secret: DF.Password | None
        suite_cloud_token_installed: DF.Check
        title: DF.Data | None
        status: DF.Literal["Pending", "Provisioning", "Provisioned", "Active", "Failed", "Disabled"]
    # end: auto-generated types

    # --- lifecycle --------------------------------------------------------------

    def autoname(self) -> None:
        self.hostname = naming.next_hostname("Egress Gateway", self.cluster, naming.GATEWAY_PREFIX)
        self.name = self.hostname

    def before_insert(self) -> None:
        self.status = "Pending"
        self.admin_username = self.admin_username or "admin"
        if not self.admin_password:
            self.admin_password = frappe.generate_hash(length=32)
        server_token.fill_token(self)
        if not self.data_store:
            self.data_store = self.create_local_store().name

    def validate(self) -> None:
        validate_ssh_user_field(self)
        cluster = self.get_cluster()
        self.title = (self.title or "").strip() or self.hostname
        self.hostname = (self.hostname or "").strip().lower().rstrip(".")
        suffix = f".{cluster.default_domain}"
        if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
            frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

        self.base_url = f"https://{self.hostname}"
        self.ipv4_address = validate_ip(self.ipv4_address, 4)
        if not self.is_new() and (
            self.has_value_changed("ipv4_address") or self.has_value_changed("ssh_port")
        ):
            self.ssh_verified = 0
            self.ssh_host_keys = None  # a different box answers there; its key is unknown again
        elif self.has_value_changed("ipv4_address"):
            self.ssh_verified = 0
        self.ssh_user = self.ssh_user or cluster.ssh_user
        self.ssh_port = self.ssh_port or cluster.ssh_port
        self.stalwart_version = validate_version(
            self.stalwart_version or cluster.stalwart_version or get_config("stalwart_version"),
            _("Stalwart Version"),
        )

    def after_insert(self) -> None:
        dns.sync_gateway_records(self)

    def on_update(self) -> None:
        server_token.clear_server_after_commit(self)
        before = self.get_doc_before_save()
        if before and before.ipv4_address != self.ipv4_address:
            dns.sync_gateway_records(self)
            for pool in self.pools():
                dns.sync_pool_records(pool)
        if before and before.enabled and not self.enabled and self.status in ("Provisioned", "Active"):
            self.set_status("Disabled")

    def on_trash(self) -> None:
        if self.status in ("Provisioning", "Provisioned", "Active"):
            frappe.throw(_("Disable the gateway before deleting it."))
        if frappe.db.exists("Egress IP Pool Address", {"gateway": self.name}):
            frappe.throw(_("Remove this gateway's addresses from every pool first."))
        dns.delete_gateway_records(self)
        server_token.clear_server_after_commit(self)

    # --- helpers ------------------------------------------------------------------

    def create_local_store(self) -> Document:
        store = frappe.get_doc(
            {
                "doctype": "Stalwart Store",
                "title": f"{self.hostname} local data",
                "kind": "Data",
                "type": "RocksDb",
                "path": "/var/lib/stalwart",
            }
        )
        store.insert(ignore_permissions=True)
        return store

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def get_store(self, field: str = "data_store") -> Document | None:
        return frappe.get_cached_doc("Stalwart Store", self.get(field)) if self.get(field) else None

    def pools(self) -> list[Document]:
        names = frappe.get_all(
            "Egress IP Pool Address", {"gateway": self.name}, pluck="parent", distinct=True
        )
        return [frappe.get_doc("Egress IP Pool", name) for name in sorted(set(names))]

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
        serving_before = self.status == "Active"
        values = {"status": status}
        if error is not None:
            values["last_error"] = error[:1000]
        self.db_set(values, update_modified=False, notify=True)
        if serving_before != (status == "Active"):
            self.resync_pool_records()

    def resync_pool_records(self) -> None:
        """The pool hostnames list only serving gateways, so they follow this one's status."""

        for pool in self.pools():
            try:
                dns.sync_pool_records(pool)
            except Exception:
                log_exception(f"Pool DNS for {pool.name} could not follow {self.name}", self)

    def get_client(self):
        return get_client(self)

    def get_admin_client(self):
        return get_admin_client(self)

    def bump_config_version(self, rendered_plan: list[dict]) -> None:
        self.db_set(
            {"config_version": (self.config_version or 0) + 1, "config_plan": plan.redacted(rendered_plan)},
            update_modified=False,
        )

    # --- actions --------------------------------------------------------------------

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
        return egress.provision_gateway(self).name

    @frappe.whitelist()
    def sync_config(self) -> dict:
        frappe.only_for("System Manager")
        if self.status != "Active":
            frappe.throw(_("Only an active gateway can be synced."))
        return self.push_config()

    def push_config(self) -> dict:
        rendered = egress.gateway_plan(self)
        result = self.get_client().apply(rendered)
        self.get_client().reload_settings()
        self.bump_config_version(rendered)
        self.db_set("last_config_sync_at", now(), update_modified=False)
        return {"created": result.created, "updated": result.updated, "unchanged": result.unchanged}

    @frappe.whitelist()
    def preview_plan(self) -> str:
        frappe.only_for("System Manager")
        return plan.redacted(egress.gateway_plan(self))

    @frappe.whitelist()
    def show_admin_password(self) -> str:
        frappe.only_for("Administrator")
        return self.get_password("admin_password")

    @frappe.whitelist()
    def check_health(self) -> bool:
        frappe.only_for("System Manager")
        return egress.check_gateway(self)

    @frappe.whitelist()
    def upgrade(self) -> str:
        frappe.only_for("System Manager")
        return egress.upgrade_gateway(self).name

    @frappe.whitelist()
    def rotate_suite_cloud_token(self) -> str | None:
        """A new Suite Cloud secret; a job writes it to stalwart.env and restarts Stalwart."""

        frappe.only_for("System Manager")
        return rotate_token(self)

    @frappe.whitelist()
    def replace_dkim_keys(self) -> None:
        """Emergency replacement of the gateway domain's keys after a leak, same selectors."""

        frappe.only_for("System Manager")
        client = self.get_client()
        domain = client.domains.find_by_name(self.hostname)
        if not domain:
            frappe.throw(_("The gateway does not hold its domain {0} yet.").format(self.hostname))
        client.domains.replace_dkim_keys(domain["id"], dkim_algorithms())

    # --- Server Job callbacks ---------------------------------------------------------

    def after_provision(self, job: Document) -> None:
        egress.after_gateway_provision(self, job)

    def after_provision_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)

    def after_upgrade(self, job: Document) -> None:
        self.db_set("installed_version", self.stalwart_version, update_modified=False)
        self.set_status("Provisioned")
        egress.check_gateway(self)
        mark_token_installed(self)  # the upgrade wrote stalwart.env

    def after_install_token(self, job: Document) -> None:
        mark_token_installed(self)

    def after_upgrade_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)


def on_doctype_update() -> None:
    """Every telemetry batch looks its gateway up by cluster and address."""

    frappe.db.add_index("Egress Gateway", ["cluster", "ipv4_address"])


def poll_pending_gateways() -> None:
    for name in frappe.get_all("Egress Gateway", {"status": "Provisioned"}, pluck="name"):
        gateway = frappe.get_doc("Egress Gateway", name)
        try:
            egress.check_gateway(gateway)
        except Exception:
            frappe.db.rollback()
            log_exception(f"Health check failed for {name}", gateway)
            continue
        if not frappe.in_test:
            frappe.db.commit()
