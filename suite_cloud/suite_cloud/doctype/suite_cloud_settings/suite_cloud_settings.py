# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from suite_cloud.utils import clear_config_cache, validate_version

# Set all together or not at all; the form marks them with the same rule (mandatory_depends_on).
CENTRAL_CONNECTION_FIELDS = ("central_url", "central_team", "central_api_key", "central_api_secret")
TELEMETRY_LIMITS = (
    "server_requests_per_minute",
    "telemetry_silence_minutes",
    "telemetry_errors_per_hour",
    "metrics_push_interval_seconds",
    "webhook_discard_after_minutes",
)
# What Stalwart is given for its pushes, and how many of them a server may make.
TELEMETRY_RANGES = {
    "server_requests_per_minute": (1, 100000),
    "metrics_push_interval_seconds": (5, 3600),
    "webhook_discard_after_minutes": (1, 10080),
}


class SuiteCloudSettings(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.suite_cloud.doctype.tenant_email_account.tenant_email_account import (
            TenantEmailAccount,
        )

        acme_contact_email: DF.Data
        acme_directory_url: DF.Data
        allow_signup: DF.Check
        allow_subaddressed_signups: DF.Check
        auto_create_vms: DF.Check
        central_api_key: DF.Data | None
        central_api_secret: DF.Password | None
        central_image_offering: DF.Data
        central_plan: DF.Data | None
        central_team: DF.Autocomplete | None
        central_url: DF.Data | None
        default_dns_ttl: DF.Int
        default_region: DF.Link | None
        disk_per_site_gb: DF.Int
        dmarc_report_retention_days: DF.Int
        enable_telemetry: DF.Check
        max_sites_per_vm: DF.Int
        memory_per_site_mb: DF.Int
        min_free_site_slots: DF.Int
        outbound_scan_mode: DF.Literal["Off", "Observe", "Enforce"]
        outbound_scan_reject_score: DF.Float
        public_url: DF.Data | None
        ready_sites_per_region: DF.Int
        reserved_disk_gb: DF.Int
        reserved_memory_mb: DF.Int
        rspamd_version: DF.Data | None
        send_usage_retention_days: DF.Int
        server_job_timeout: DF.Int
        sign_with_ed25519: DF.Check
        site_dns_zone: DF.Link | None
        skip_domain_verification: DF.Check
        spam_filter_rules_version: DF.Data | None
        stalwart_cli_download_url_template: DF.Data
        stalwart_cli_version: DF.Data
        stalwart_download_url_template: DF.Data
        stalwart_version: DF.Data
        suite_app_ref: DF.Data
        suite_app_repo: DF.Data
        telemetry_alert_recipients: DF.SmallText | None
        telemetry_errors_per_hour: DF.Int
        telemetry_retention_days: DF.Int
        telemetry_silence_minutes: DF.Int
        tenant_email_accounts: DF.Table[TenantEmailAccount]
        tenant_mail_zone: DF.Link | None
        tls_report_retention_days: DF.Int
        use_tenant_title_as_sender_name: DF.Check
        verify_stalwart_tls: DF.Check
    # end: auto-generated types

    def validate(self) -> None:
        if self.public_url:
            self.public_url = self.public_url.strip().rstrip("/")
        self.stalwart_version = validate_version(self.stalwart_version, _("Stalwart Version"))
        self.stalwart_cli_version = validate_version(self.stalwart_cli_version, _("Stalwart CLI Version"))
        self.spam_filter_rules_version = validate_version(
            self.spam_filter_rules_version, _("Spam Filter Rules Version")
        )
        self.validate_report_retention()
        self.validate_outbound_scan()
        self.validate_telemetry()
        self.validate_tenant_email_accounts()
        self.validate_hosting()

    def on_update(self) -> None:
        clear_config_cache()
        before = self.get_doc_before_save()
        if before and cint(before.skip_domain_verification) and not cint(self.skip_domain_verification):
            from suite_cloud.mail.doctype.mail_domain.mail_domain import (
                enqueue_recheck_of_vouched_domains,
            )

            enqueue_recheck_of_vouched_domains()
        if before and cint(before.enable_telemetry) != cint(self.enable_telemetry):
            from suite_cloud.telemetry.install import enqueue_roll_out_telemetry

            enqueue_roll_out_telemetry()
        if before:
            self.apply_outbound_scan_change(before)

    def validate_outbound_scan(self) -> None:
        if self.outbound_scan_mode == "Enforce" and flt(self.outbound_scan_reject_score) <= 0:
            frappe.throw(_("The Outbound Spam Scan Reject Score must be above 0."))

    def apply_outbound_scan_change(self, before: Document) -> None:
        """Off is in the cluster plan, so each active cluster is pushed again. Observe, Enforce and
        the reject score are in the nodes' Rspamd files, which only a scanner install writes."""

        from suite_cloud.mail.cluster import push, rspamd

        if (before.outbound_scan_mode == "Off") != (self.outbound_scan_mode == "Off"):
            for name in frappe.get_all("Stalwart Cluster", {"status": "Active"}, pluck="name"):
                push.resync_cluster_after_commit(name)
        before_score, score = (
            rspamd.get_reject_score(*(doc.get(field) for field in rspamd.SCAN_FIELDS))
            for doc in (before, self)
        )
        if before_score != score:
            frappe.msgprint(
                _("Run Install Outbound Scanners on each cluster to apply the new outbound scan settings."),
                indicator="orange",
                alert=True,
            )

    def validate_report_retention(self) -> None:
        for field in (
            "dmarc_report_retention_days",
            "tls_report_retention_days",
            "send_usage_retention_days",
            "telemetry_retention_days",
        ):
            if cint(self.get(field)) < 1:
                frappe.throw(_("{0} must be at least one day.").format(_(self.meta.get_label(field))))

    def validate_telemetry(self) -> None:
        for field in TELEMETRY_LIMITS:
            if cint(self.get(field)) < 1:
                frappe.throw(_("{0} must be at least 1.").format(_(self.meta.get_label(field))))
        for field, (low, high) in TELEMETRY_RANGES.items():
            if not low <= cint(self.get(field)) <= high:
                frappe.throw(
                    _("{0} must be between {1} and {2}.").format(_(self.meta.get_label(field)), low, high)
                )

    def validate_tenant_email_accounts(self) -> None:
        seen = set()
        for row in self.tenant_email_accounts:
            if row.tenant_type in seen:
                frappe.throw(
                    _("Row {0}: {1} tenants already have an Email Account.").format(row.idx, row.tenant_type)
                )
            seen.add(row.tenant_type)
            if not frappe.db.get_value("Email Account", row.email_account, "enable_outgoing"):
                frappe.throw(
                    _("Row {0}: Email Account {1} does not send.").format(row.idx, row.email_account)
                )

    def get_tenant_email_account(self, tenant_type: str) -> str | None:
        return next(
            (row.email_account for row in self.tenant_email_accounts if row.tenant_type == tenant_type), None
        )

    def validate_hosting(self) -> None:
        if self.central_url:
            self.central_url = self.central_url.strip().rstrip("/")
        self.validate_central_connection()
        if self.central_url and not self.tenant_mail_zone:
            frappe.throw(
                _("Set the Tenant Mail Zone: every hosted site gets a mail domain."), frappe.MandatoryError
            )
        if self.default_region and not frappe.db.get_value("Region", self.default_region, "enabled"):
            frappe.throw(
                _("Region {0} is not enabled; enable it before making it the default.").format(
                    self.default_region
                )
            )
        for field in ("memory_per_site_mb", "disk_per_site_gb"):
            if cint(self.get(field)) < 1:
                frappe.throw(_("{0} must be at least 1.").format(_(self.meta.get_label(field))))
        for field in (
            "max_sites_per_vm",
            "ready_sites_per_region",
            "min_free_site_slots",
            "reserved_memory_mb",
            "reserved_disk_gb",
        ):
            if cint(self.get(field)) < 0:
                frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(field))))
        if cint(self.max_sites_per_vm) < 1:
            frappe.throw(_("A server must take at least one site."))
        self.validate_hosting_zones()

    def validate_central_connection(self) -> None:
        if self.flags.ignore_mandatory:  # migrate backfilling defaults must not stop on it
            return
        missing = [f for f in CENTRAL_CONNECTION_FIELDS if not self.get(f)]
        if missing and len(missing) < len(CENTRAL_CONNECTION_FIELDS):
            labels = ", ".join(_(self.meta.get_label(f)) for f in missing)
            frappe.throw(
                _("Set {0} too, or clear the whole Central connection.").format(labels), frappe.MandatoryError
            )

    def validate_hosting_zones(self) -> None:
        """Both zones may be one: site domains then go one level down (``labels.site_domain``).
        Tenant mail domains must not sit in a cluster's own zone."""

        for field in ("site_dns_zone", "tenant_mail_zone"):
            zone = self.get(field)
            if zone and not frappe.db.get_value("DNS Zone", zone, "dns_provider"):
                frappe.throw(_("DNS Zone {0} needs a DNS provider to publish records.").format(zone))
        if self.tenant_mail_zone and (
            cluster := frappe.db.get_value("Stalwart Cluster", {"dns_zone": self.tenant_mail_zone}, "name")
        ):
            frappe.throw(
                _("DNS Zone {0} is the zone of cluster {1}; tenant mail domains need another zone.").format(
                    self.tenant_mail_zone, cluster
                )
            )

    @property
    def is_central_configured(self) -> bool:
        return all(self.get(f) for f in CENTRAL_CONNECTION_FIELDS)

    @frappe.whitelist()
    def test_central_connection(self) -> dict:
        """What the API key may do in the team, as Central reports it."""

        frappe.only_for("System Manager")
        from suite_cloud.hosting import central

        return central.get_central_client().my_capabilities()

    @frappe.whitelist()
    def get_central_teams(self) -> list[dict]:
        """``[{value: team id, label}]`` for the team picker, from the credentials on the form,
        saved or not: the connection cannot be saved without a team."""

        frappe.only_for("System Manager")
        from suite_cloud.hosting import central

        secret = self.central_api_secret
        if not secret or self.is_dummy_password(secret):
            secret = self.get_password("central_api_secret", raise_exception=False)
        if not (self.central_url and self.central_api_key and secret):
            frappe.throw(_("Enter the Central URL, API key and secret first."))
        credentials = {"url": self.central_url.strip(), "api_key": self.central_api_key, "api_secret": secret}
        try:
            teams = central.get_central_client(credentials).my_teams()
        except central.CentralError as e:
            frappe.throw(_("Central did not list the teams: {0}").format(e))
        return [{"value": t["name"], "label": team_label(t)} for t in teams]

    @frappe.whitelist()
    def sync_regions(self) -> int:
        frappe.only_for("System Manager")
        from suite_cloud.hosting.doctype.region.region import sync_regions

        return sync_regions()


def team_label(team: dict) -> str:
    """Central's ``label`` is the team name, else the owner's email, else the id."""

    label = team.get("label")
    return f"{label} ({team['name']})" if label and label != team["name"] else team["name"]
