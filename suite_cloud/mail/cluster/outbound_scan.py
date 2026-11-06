"""The outbound spam scan on the cluster's side: the MtaMilter that sends each signed-in sender's
message to the node's Rspamd at DATA, and the Memory Lookup Keys that name the trusted accounts it
skips. Mail Account owns which accounts are trusted; the keys are a copy."""

from typing import TYPE_CHECKING

import frappe

from suite_cloud.mail.cluster import rspamd
from suite_cloud.mail.cluster.plan import as_list, as_set
from suite_cloud.mail.cluster.quota import NEVER
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

    from suite_cloud.mail.stalwart.client import StalwartClient

EXEMPT_NAMESPACE = "suite-cloud-outbound-scan-exempt"
LOOKUP_KEY = "MemoryLookupKey"
SCANNED = {
    "match": as_list(
        [
            {
                "if": f"!is_empty(authenticated_as) && !key_exists('{EXEMPT_NAMESPACE}', authenticated_as)",
                "then": "true",
            }
        ]
    ),
    "else": "false",
}


def cluster_operations(cluster: Document) -> list[dict]:
    return [milter_operation(cluster)]


def milter_operation(cluster: Document) -> dict:
    """Stalwart sends the message without the scan when Rspamd fails or is slow: the sending limits
    still cap the account. Off until every running node has its scanner. MtaMilter has no
    description, so the plan matches it on its address."""

    milter = {
        "enable": SCANNED if is_scan_enabled(cluster) else NEVER,
        "hostname": rspamd.MILTER_HOST,
        "port": rspamd.MILTER_PORT,
        "stages": as_set(["data"]),
        "tempFailOnError": False,
        "timeoutConnect": 1000,
        "timeoutCommand": 5000,
        "timeoutData": 6000,  # longer than Rspamd's task_timeout, so it answers with what it has
        "protocolVersion": "v6",
        "useTls": False,
        "maxResponseSize": 52428800,
    }
    return {
        "@type": "upsert",
        "object": "MtaMilter",
        "matchOn": ["hostname", "port"],
        "value": {"outbound-scan": milter},
    }


def is_scan_enabled(cluster: Document) -> bool:
    return get_config("outbound_scan_mode") != "Off" and rspamd.has_every_scanner(cluster)


# --- trusted accounts ------------------------------------------------------------------------


def set_exempt(cluster: Document, email: str, is_exempt: bool) -> None:
    """Adds or removes one account's key. Stalwart reads keys only when it reloads its lookup
    stores, which ``ReloadSettings`` does not do."""

    client = cluster.get_client()
    keys = get_exempt_keys(client)
    service = client.objects(LOOKUP_KEY)
    if is_exempt and email not in keys:
        service.create(lookup_key(email))
    elif not is_exempt and email in keys:
        service.delete(keys[email])
    else:
        return
    client.reload_lookup_stores()


def sync_exempt_accounts(cluster: Document) -> bool:
    """Makes the cluster's keys match the cluster's trusted accounts; True when it changed any."""

    client = cluster.get_client()
    keys = get_exempt_keys(client)
    wanted = set(get_exempt_accounts(cluster))
    missing = sorted(wanted - set(keys))
    extra = [id for email, id in keys.items() if email not in wanted]
    service = client.objects(LOOKUP_KEY)
    for email in missing:
        service.create(lookup_key(email))
    if extra:
        service.delete(extra)
    if not (missing or extra):
        return False
    client.reload_lookup_stores()
    return True


def get_exempt_accounts(cluster: Document) -> list[str]:
    return frappe.get_all("Mail Account", {"cluster": cluster.name, "skip_outbound_scan": 1}, pluck="email")


def get_exempt_keys(client: StalwartClient) -> dict[str, str]:
    """``{address: key id}`` of the namespace's keys."""

    found = client.objects(LOOKUP_KEY).get_all(filter={"namespace": EXEMPT_NAMESPACE})
    return {key["key"]: key["id"] for key in found}


def lookup_key(email: str) -> dict:
    return {"namespace": EXEMPT_NAMESPACE, "key": email, "isGlobPattern": False}
