app_name = "suite_cloud"
app_title = "Suite Cloud"
app_publisher = "Frappe"
app_description = "Deploys and manages Stalwart mail clusters for Frappe Suite sites"
app_email = "developers@frappe.io"
app_license = "agpl-3.0"
required_apps = ["insights"]  # holds the telemetry the nodes push (suite_cloud.telemetry)

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# ============================================================================
# Lifecycle
# ============================================================================
after_install = "suite_cloud.install.after_install"
after_migrate = "suite_cloud.install.after_migrate"
after_request = ["suite_cloud.api.tenant.trim_errors"]
auth_hooks = ["suite_cloud.api.tenant.authenticate", "suite_cloud.api.telemetry.authenticate"]

# ============================================================================
# Permissions: tenant owners reach only the rows of the tenants they own
# ============================================================================
PERMISSIONS = "suite_cloud.mail.tenancy.permissions"
TENANT_OWNED_DOCTYPES = (
    "Mail Domain",
    "Mail Account",
    "Mail Group",
    "Mailing List",
    "Mailing List Recipient",
    "DMARC Report",
    "TLS Report",
    "Suite Site",
)
permission_query_conditions = {
    "Suite Tenant": f"{PERMISSIONS}.tenant_query_conditions",
    **{doctype: f"{PERMISSIONS}.tenant_document_query_conditions" for doctype in TENANT_OWNED_DOCTYPES},
}
has_permission = {
    "Suite Tenant": f"{PERMISSIONS}.has_tenant_permission",
    **{doctype: f"{PERMISSIONS}.has_tenant_document_permission" for doctype in TENANT_OWNED_DOCTYPES},
}
doc_events = {"User": {"validate": f"{PERMISSIONS}.keep_owner_role"}}

# ============================================================================
# Scheduled tasks
# ============================================================================
scheduler_events = {
    "cron": {
        "* * * * *": ["suite_cloud.mail.sending_limits.flush_usage"],
        "*/5 * * * *": [
            "suite_cloud.suite_cloud.doctype.server_job.server_job.retry_failed_jobs",
            "suite_cloud.mail.doctype.stalwart_node.stalwart_node.poll_pending_nodes",
            "suite_cloud.mail.doctype.egress_gateway.egress_gateway.poll_pending_gateways",
            "suite_cloud.hosting.machines.refresh_fleet",
            "suite_cloud.hosting.sites.poll_pending_sites",
            "suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request.resume_requests",
            "suite_cloud.hosting.capacity.ensure_site_pool",
            "suite_cloud.telemetry.warehouse.load_staged_batches",
            "suite_cloud.telemetry.alerts.check_telemetry_alerts",
        ],
    },
    "hourly": [
        "suite_cloud.mail.doctype.mail_domain.mail_domain.refresh_rotating_domains",
        "suite_cloud.mail.doctype.mail_domain.mail_domain.verify_unverified_domains",
        "suite_cloud.mail.doctype.dmarc_report.dmarc_report.fetch_all_clusters",
        "suite_cloud.mail.doctype.tls_report.tls_report.fetch_all_clusters",
        "suite_cloud.hosting.capacity.ensure_capacity",
    ],
    "daily": [
        "suite_cloud.suite_cloud.doctype.dns_record.dns_record.verify_all_dns_records",
        "suite_cloud.mail.doctype.stalwart_cluster.stalwart_cluster.check_all_clusters",
        "suite_cloud.mail.doctype.stalwart_node.stalwart_node.verify_all_ptr_records",
        "suite_cloud.mail.doctype.egress_ip_pool.egress_ip_pool.verify_all_ptr_records",
        "suite_cloud.mail.doctype.dmarc_report.dmarc_report.prune_expired_reports",
        "suite_cloud.mail.doctype.tls_report.tls_report.prune_expired_reports",
        "suite_cloud.hosting.machines.sync_fleet",
        "suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request.expire_requests",
        "suite_cloud.telemetry.warehouse.prune_expired_rows",
        "suite_cloud.mail.doctype.mail_send_usage.mail_send_usage.prune_usage_job",
    ],
}

default_log_clearing_doctypes = {"Tenant Email": 90}

# Execution history points back at its server; it must not block deleting the server.
ignore_links_on_delete = ["Server Job"]

export_python_type_annotations = True
require_type_annotated_api_methods = True
