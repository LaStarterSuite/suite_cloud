from frappe.model.utils.rename_field import rename_field


def execute() -> None:
    """The two limits cover every Agent request, telemetry and sending-limit calls alike."""

    # The field is server_requests_per_minute now; a site this patch has not reached gets that name.
    rename_field("Suite Cloud Settings", "telemetry_requests_per_minute", "server_requests_per_minute")
    rename_field("Suite Cloud Settings", "telemetry_records_per_request", "agent_records_per_request")
