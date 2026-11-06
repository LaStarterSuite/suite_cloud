"""The outbound spam scanner on each Stalwart Node: Rspamd in milter mode on loopback. Suite Cloud
renders its ``local.d`` files, ``install-rspamd.yml`` writes them, and a node records the digest of
what it last wrote."""

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import frappe
from frappe import _
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
OVERRIDE_DIRECTORY = "/etc/rspamd/override.d"
SOURCE_DIRECTORY = Path(__file__).resolve().parents[2] / "provisioning" / "rspamd"
RECORDER_PATH = "/etc/rspamd/rspamd.local.lua"
SHIPPER_PATH = "/usr/local/lib/suite-cloud/ship_outbound_scans.py"
SHIPPER_UNIT = "suite-cloud-scans"
MILTER_HOST = "127.0.0.1"
MILTER_PORT = 11332
CPU_COUNT_PLACEHOLDER = "__CPU_COUNT__"  # the playbook puts the node's vCPU count here
REJECT_MESSAGE = "Message refused: it looks like spam. Contact your administrator if this is wrong."
# Off the outbound profile: ASN and MX checks only judge the sender's network, which is ours, and
# neural and statistics need training outbound mail does not give. Fuzzy stays off without the
# cluster's Redis: its stock rule sends hashes of the mail to rspamd.com.
DISABLED_GROUPS = ("neural", "statistics", "asn", "mx_check")
SCAN_FIELDS = ("outbound_scan_mode", "outbound_scan_reject_score")  # what the files depend on
REDIS_TIMEOUT = "1s"  # inside the scan's 4s task timeout
FUZZY_PORT = 11335
# The files that hold the Redis password: readable by Rspamd only.
PRIVATE_FILES = (f"{CONFIG_DIRECTORY}/worker-fuzzy.inc", f"{CONFIG_DIRECTORY}/history_redis.conf")


def get_rspamd_files(cluster: Document) -> dict[str, str]:
    """``{path: content}`` of every file the playbook writes, from the current settings and the
    cluster's outbound scan store: Rspamd's own, the scan recorder, and the shipper that sends its
    records to Suite Cloud."""

    store = cluster.get_store("outbound_scan_store")
    files = {
        f"{CONFIG_DIRECTORY}/worker-proxy.inc": worker_proxy(),
        f"{CONFIG_DIRECTORY}/worker-normal.inc": "enabled = false;\n",
        f"{CONFIG_DIRECTORY}/worker-controller.inc": 'bind_socket = "127.0.0.1:11334";\n',
        f"{CONFIG_DIRECTORY}/options.inc": OPTIONS,
        f"{CONFIG_DIRECTORY}/actions.conf": actions_conf(get_reject_score(*get_config(SCAN_FIELDS))),
        f"{CONFIG_DIRECTORY}/settings.conf": settings_conf(
            DISABLED_GROUPS if store else ("fuzzy", *DISABLED_GROUPS)
        ),
        f"{CONFIG_DIRECTORY}/logging.inc": LOGGING,
        # Stalwart signs after the scan, so Rspamd never adds a DKIM or ARC signature.
        f"{CONFIG_DIRECTORY}/dkim_signing.conf": "enabled = false;\n",
        f"{CONFIG_DIRECTORY}/arc.conf": "enabled = false;\n",
        RECORDER_PATH: (SOURCE_DIRECTORY / "scan_recorder.lua").read_text(),
        SHIPPER_PATH: (SOURCE_DIRECTORY / "ship_outbound_scans.py").read_text(),
        f"/etc/systemd/system/{SHIPPER_UNIT}.service": shipper_service(),
        f"/etc/systemd/system/{SHIPPER_UNIT}.timer": SHIPPER_TIMER,
    }
    if store:
        files.update(get_redis_files(cluster, get_redis_options(store)))
    return files


def get_config_digest(cluster: Document) -> str:
    return get_files_digest(get_rspamd_files(cluster))


def get_files_digest(files: dict[str, str]) -> str:
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


def settings_conf(disabled_groups: tuple[str, ...]) -> str:
    """The profile for signed-in senders, which only the milter's are."""

    return f"""outbound {{
  id = "outbound";
  priority = high;
  authenticated = yes;
  apply {{
    groups_disabled = [{", ".join(f'"{group}"' for group in disabled_groups)}];
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


# --- Redis ----------------------------------------------------------------------------------
# With the cluster's outbound scan store, the nodes share two things through Redis: the fuzzy hashes
# an operator learns in the web UI, which a local fuzzy storage worker keeps, and one scan history.
# Only these two modules get the servers, so no other module starts writing to the store.


def get_redis_options(store: Document) -> dict[str, str | bool]:
    """Rspamd's connection options from a Redis store's URL. Rspamd cannot use Redis Cluster, and
    with Sentinel it needs the master's address too, which a Sentinel store does not hold."""

    if store.kind != "In-Memory" or store.type != "Redis":
        frappe.throw(_("The Outbound Scan Store must be an In-Memory store of type Redis."))
    url = urlsplit(store.url or "")
    database = url.path.strip("/")
    if url.scheme not in ("redis", "rediss") or not url.hostname or (database and not database.isdigit()):
        frappe.throw(_("The Outbound Scan Store URL must look like redis://user:password@host:6379/1."))
    if re.search(r"\{[{%#]", store.url):
        frappe.throw(
            _("The Outbound Scan Store URL cannot hold {{, {% or {#: Ansible reads them as a template.")
        )

    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    options: dict[str, str | bool] = {"servers": f"{host}:{url.port or 6379}", "timeout": REDIS_TIMEOUT}
    if url.username:
        options["username"] = unquote(url.username)
    if url.password:
        options["password"] = unquote(url.password)
    if database:
        options["db"] = database
    if url.scheme == "rediss":
        options["ssl"] = True
        if url.fragment == "insecure":  # Stalwart's way to skip the certificate check
            options["no_ssl_verify"] = True
    return options


def get_redis_secrets(cluster: Document) -> list[str]:
    """The password in the Redis files, so a job can mask it."""

    store = cluster.get_store("outbound_scan_store")
    password = store and get_redis_options(store).get("password")
    return [password] if password else []


def get_redis_files(cluster: Document, options: dict[str, str | bool]) -> dict[str, str]:
    redis = to_ucl(options)
    return {
        f"{CONFIG_DIRECTORY}/worker-fuzzy.inc": f"""bind_socket = "127.0.0.1:{FUZZY_PORT}";
count = 1;
backend = "redis";
{redis}expire = 90d;
allow_update = ["127.0.0.1"];
""",
        # The whole section, so the stock rule that sends hashes to rspamd.com is gone.
        f"{OVERRIDE_DIRECTORY}/fuzzy_check.conf": FUZZY_CHECK,
        f"{CONFIG_DIRECTORY}/fuzzy_group.conf": FUZZY_GROUP,
        # One history for the cluster, so the web UI on any node shows every node's scans.
        f"{CONFIG_DIRECTORY}/history_redis.conf": f"""{redis}key_prefix = {json.dumps(f"rs_history_{cluster.name}")};
nrows = 1000;
compress = true;
""",
    }


def to_ucl(options: dict[str, str | bool]) -> str:
    """``key = value;`` lines; JSON quoting escapes a password's quotes and backslashes."""

    return "".join(
        f"{key} = {str(value).lower() if isinstance(value, bool) else json.dumps(value)};\n"
        for key, value in options.items()
    )


# One learn in the web UI counts in full: a hits limit of 1. The weights are the stock rule's.
FUZZY_CHECK = f"""min_bytes = 1k;
timeout = 1s;
retransmits = 1;
rule "local" {{
  algorithm = "mumhash";
  servers = "127.0.0.1:{FUZZY_PORT}";
  symbol = "LOCAL_FUZZY_UNKNOWN";
  mime_types = ["*"];
  hits_limit = 1.0;
  read_only = false;
  skip_unknown = true;
  short_text_direct_hash = true;
  min_length = 64;
  fuzzy_map = {{
    LOCAL_FUZZY_DENIED {{
      hits_limit = 1.0;
      flag = 11;
    }}
    LOCAL_FUZZY_PROB {{
      hits_limit = 1.0;
      flag = 12;
    }}
    LOCAL_FUZZY_WHITE {{
      hits_limit = 1.0;
      flag = 13;
    }}
  }}
}}
"""

FUZZY_GROUP = """symbols = {
  "LOCAL_FUZZY_UNKNOWN" {
    weight = 0.0;
    description = "Matches a learned hash of an unknown list";
  }
  "LOCAL_FUZZY_DENIED" {
    weight = 12.0;
    description = "Matches a hash learned as spam";
  }
  "LOCAL_FUZZY_PROB" {
    weight = 5.0;
    description = "Matches a hash learned as probable spam";
  }
  "LOCAL_FUZZY_WHITE" {
    weight = -2.1;
    description = "Matches a hash learned as good mail";
  }
}
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
            "outbound_scanner_digest": ("!=", get_config_digest(cluster)),
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
