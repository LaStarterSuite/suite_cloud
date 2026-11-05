"""The sending limits on the cluster's side: the MTA Hook that asks Suite Cloud about each recipient
of a signed-in sender, and a native throttle that caps every account while Suite Cloud does not
answer."""

from typing import TYPE_CHECKING

from frappe.utils import cint

from suite_cloud.mail.cluster import server_token
from suite_cloud.mail.cluster.plan import as_list, as_set
from suite_cloud.mail.cluster.telemetry import public_url

if TYPE_CHECKING:
    from frappe.model.document import Document

HOOK_METHOD = "/api/v2/method/suite_cloud.api.sending_limits.check"
HOOK_TIMEOUT_MS = 1000  # covers the connect too; Stalwart accepts the recipient after it
# MtaHook has no description, and its URL follows the Public URL, so this header names the hook.
HOOK_HEADER = "X-Suite-Cloud-Hook"
BACKSTOP_DESCRIPTION = "suite-cloud-quota-backstop"
DAY_MS = 86_400_000
SIGNED_IN = {"match": as_list([{"if": "!is_empty(authenticated_as)", "then": "true"}]), "else": "false"}
NEVER = {"match": as_list([]), "else": "false"}


def cluster_operations(cluster: Document) -> list[dict]:
    return [hook_operation(cluster), backstop_operation(cluster)]


def hook_operation(cluster: Document) -> dict:
    """One call per recipient at the RCPT stage, which JMAP sends pass through too, with the
    calling node's Suite Cloud token. Stalwart accepts the recipient when Suite Cloud fails or
    times out. Off until every running node has its token, as the reload needs it."""

    hook = {
        "enable": SIGNED_IN if server_token.has_every_token(cluster) else NEVER,
        "url": f"{public_url()}{HOOK_METHOD}",
        "stages": as_set(["rcpt"]),
        "timeout": HOOK_TIMEOUT_MS,
        "tempFailOnError": False,
        "httpAuth": server_token.bearer_auth(),
        "httpHeaders": {HOOK_HEADER: "sending-limits"},
    }
    return {
        "@type": "upsert",
        "object": "MtaHook",
        "matchOn": ["httpHeaders"],
        "value": {"sending-limits": hook},
    }


def backstop_operation(cluster: Document) -> dict:
    """The cluster's cap per signed-in account and UTC day, checked at MAIL FROM. Without
    ``match`` every unauthenticated session would share one bucket, since Stalwart hashes an
    empty login too."""

    throttle = {
        "enable": True,
        "description": BACKSTOP_DESCRIPTION,
        "key": as_set(["authenticatedAs"]),
        "match": SIGNED_IN,
        "rate": {"count": cint(cluster.max_daily_send_limit), "period": DAY_MS},
    }
    return {
        "@type": "upsert",
        "object": "MtaInboundThrottle",
        "matchOn": ["description"],
        "value": {"quota-backstop": throttle},
    }
