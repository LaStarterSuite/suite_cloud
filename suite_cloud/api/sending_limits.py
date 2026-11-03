"""Stalwart's sending-limit hook: one call per recipient of a signed-in sender, at the RCPT stage.

The URL names the cluster and the bearer is its Quota Hook Token, which the ``authenticate`` auth
hook checks. The answer is Stalwart's own JSON, without Frappe's envelope.
"""

import json

import frappe
from werkzeug.wrappers import Response

from suite_cloud.api.telemetry import hook_api
from suite_cloud.mail import sending_limits

ACCEPT = {"action": "accept"}


@frappe.whitelist(methods=["POST"])
@hook_api
def check(context: dict | None = None, envelope: dict | None = None) -> Response:
    """``{context, envelope}``: the sender is ``context.sasl.login``, the current recipient the
    last of ``envelope.to``. A fault answers 500, and Stalwart accepts the recipient."""

    account = str(((context or {}).get("sasl") or {}).get("login") or "").strip().lower()
    if not account or not (envelope or {}).get("to"):
        return answer(ACCEPT)
    try:
        refusal = sending_limits.count_recipient(frappe.local.hook_cluster, account)
    except Exception:
        # One line per recipient while Redis is down, so a log rather than an Error Log each.
        frappe.logger("sending_limits").exception(f"Sending limit check failed for {account}")
        return Response(status=500)
    if not refusal:
        return answer(ACCEPT)
    response = {"status": 550, "enhanced_status": "5.4.5", "message": refusal}
    return answer({"action": "reject", "response": response})


def answer(body: dict) -> Response:
    return Response(json.dumps(body), status=200, mimetype="application/json")
