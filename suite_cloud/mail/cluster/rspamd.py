"""The outbound spam scanner on each Stalwart Node: Rspamd in milter mode on loopback. Suite Cloud
renders its ``local.d`` files, ``install-rspamd.yml`` writes them, and a node records the digest of
what it last wrote."""

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import frappe
from frappe.utils import flt

from suite_cloud.mail.cluster import push, server_token, telemetry
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

PLAYBOOK = "install-rspamd.yml"
CALLBACK = "after_install_outbound_scanner"
NODE_BUILDER = "suite_cloud.mail.cluster.bootstrap.build_node_variables"
CONFIG_DIRECTORY = "/etc/rspamd/local.d"
SOURCE_DIRECTORY = Path(__file__).resolve().parents[2] / "provisioning" / "rspamd"
RECORDER_PATH = "/etc/rspamd/rspamd.local.lua"
SHIPPER_PATH = "/usr/local/lib/suite-cloud/ship_outbound_scans.py"
SHIPPER_UNIT = "suite-cloud-scans"
MILTER_HOST = "127.0.0.1"
MILTER_PORT = 11332
CPU_COUNT_PLACEHOLDER = "__CPU_COUNT__"  # the playbook puts the node's vCPU count here
REJECT_MESSAGE = "Message refused: it looks like spam. Contact your administrator if this is wrong."
# Off the outbound profile: no Redis for fuzzy, neural and statistics, and ASN and MX checks only
# judge the sender's network, which is ours.
DISABLED_GROUPS = ("fuzzy", "neural", "statistics", "asn", "mx_check")
SCAN_FIELDS = ("outbound_scan_mode", "outbound_scan_reject_score")  # what the files depend on


def get_rspamd_files() -> dict[str, str]:
    """``{path: content}`` of every file the playbook writes, from the current settings: Rspamd's
    own, the scan recorder, and the shipper that sends its records to Suite Cloud."""

    files = {
        "worker-proxy.inc": worker_proxy(),
        "worker-normal.inc": "enabled = false;\n",
        "worker-controller.inc": 'bind_socket = "127.0.0.1:11334";\n',
        "options.inc": OPTIONS,
        "actions.conf": actions_conf(get_reject_score(*get_config(SCAN_FIELDS))),
        "settings.conf": SETTINGS,
        "logging.inc": LOGGING,
        # Stalwart signs after the scan, so Rspamd never adds a DKIM or ARC signature.
        "dkim_signing.conf": "enabled = false;\n",
        "arc.conf": "enabled = false;\n",
    }
    return {
        **{f"{CONFIG_DIRECTORY}/{name}": content for name, content in files.items()},
        RECORDER_PATH: (SOURCE_DIRECTORY / "scan_recorder.lua").read_text(),
        SHIPPER_PATH: (SOURCE_DIRECTORY / "ship_outbound_scans.py").read_text(),
        f"/etc/systemd/system/{SHIPPER_UNIT}.service": shipper_service(),
        f"/etc/systemd/system/{SHIPPER_UNIT}.timer": SHIPPER_TIMER,
    }


def get_config_digest(files: dict[str, str] | None = None) -> str:
    files = get_rspamd_files() if files is None else files
    return hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()


def get_reject_score(mode: str, score: float) -> float | None:
    """The score Enforce refuses at; None in Observe and Off, where Rspamd refuses nothing."""

    return flt(score) if mode == "Enforce" else None


def worker_proxy() -> str:
    return f"""bind_socket = "{MILTER_HOST}:{MILTER_PORT}";
milter = yes;
timeout = 10s;
count = {CPU_COUNT_PLACEHOLDER};
reject_message = "{REJECT_MESSAGE}";
upstream "local" {{
  default = yes;
  self_scan = yes;
}}
"""


def actions_conf(reject_score: float | None) -> str:
    """The thresholds for every message the milter sends, which only signed-in senders' are.
    Without a reject score Rspamd accepts every message and only logs its score."""

    reject = "null" if reject_score is None else f"{reject_score:g}"
    return f"""reject = {reject};
add_header = null;
greylist = null;
"""


# The profile for signed-in senders: the checks that need Redis, or that judge our own network.
SETTINGS = f"""outbound {{
  id = "outbound";
  priority = high;
  authenticated = yes;
  apply {{
    groups_disabled = [{", ".join(f'"{group}"' for group in DISABLED_GROUPS)}];
  }}
}}
"""


OPTIONS = """dns {
  nameserver = ["127.0.0.1"];
  timeout = 1s;
  retransmits = 2;
}
task_timeout = 4s;
"""


def shipper_service() -> str:
    """The node's Suite Cloud token comes from Stalwart's environment file."""

    return f"""[Unit]
Description=Send the outbound scan records to Suite Cloud
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-/etc/stalwart/stalwart.env
Environment=SUITE_CLOUD_SCANS_URL={telemetry.api_url("outbound_scans")}
ExecStart=/usr/bin/python3 {SHIPPER_PATH}
"""


SHIPPER_TIMER = """[Unit]
Description=Send the outbound scan records to Suite Cloud each minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
"""

LOGGING = """type = "file";
filename = "/var/log/rspamd/rspamd.log";
level = "notice";
"""


# --- node state ------------------------------------------------------------------------------


def has_every_scanner(cluster: Document) -> bool:
    """Every node running the cluster's plan answers on the milter port."""

    return not frappe.db.exists(
        "Stalwart Node",
        {
            "cluster": cluster.name,
            "status": ("in", server_token.RUNNING_STATUSES),
            "outbound_scanner_installed": 0,
        },
    )


def get_outdated_nodes(cluster: Document) -> list[str]:
    """Running nodes whose Rspamd files are not the ones the current settings render."""

    return frappe.get_all(
        "Stalwart Node",
        {
            "cluster": cluster.name,
            "status": ("in", server_token.RUNNING_STATUSES),
            "outbound_scanner_digest": ("!=", get_config_digest()),
        },
        pluck="name",
        order_by="name asc",
    )


def install_scanner(node: Document) -> Document:
    return create_server_job(
        node,
        PLAYBOOK,
        title=f"Install the outbound scanner on {node.hostname}",
        context={"node": node.name},
        variables_builder=NODE_BUILDER,
        callback=CALLBACK,
    )


def install_scanners(cluster: Document) -> list[str]:
    return [install_scanner(node).name for node in cluster.get_nodes(server_token.RUNNING_STATUSES)]


def mark_scanner_installed(node: Document, job: Document) -> None:
    """A job wrote the files of its variables. When the node was the last running one without a
    scanner, the cluster plan is pushed again, which switches the milter on."""

    cluster = node.get_cluster()
    had_every_scanner = has_every_scanner(cluster)
    node.db_set(
        {"outbound_scanner_installed": 1, "outbound_scanner_digest": get_job_digest(job)},
        update_modified=False,
    )
    if not had_every_scanner and has_every_scanner(cluster):
        push.resync_cluster_after_commit(cluster.name)


def get_job_digest(job: Document) -> str:
    """The digest of the files the job wrote, not of the settings now: they may have changed while
    the job ran."""

    return json.loads(job.variables or "{}").get("rspamd_digest") or ""
