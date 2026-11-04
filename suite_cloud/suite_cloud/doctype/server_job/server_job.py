# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now, time_diff_in_seconds
from frappe.utils.background_jobs import is_job_enqueued

from suite_cloud.provisioning.ansible import PlaybookRun, playbook_path, playbook_task_names
from suite_cloud.utils import assert_test_host, get_config, log_exception

# Every function a job may run to build its variables, and every method it may call back on its
# server. A System Manager can create Server Job rows; a free choice here would be a way to
# run any callable in the bench as the worker.
VARIABLES_BUILDERS = (
    "suite_cloud.mail.cluster.bootstrap.build_node_variables",
    "suite_cloud.mail.cluster.bootstrap.build_command_variables",
    "suite_cloud.mail.cluster.egress.build_gateway_variables",
)
CALLBACKS = ("after_provision", "after_upgrade", "after_install_token")

REDACTED = "***"


class ServerJob(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.suite_cloud.doctype.server_job_task.server_job_task import ServerJobTask

        callback: DF.Data | None
        changed: DF.Int
        context: DF.JSON | None
        duration: DF.Float
        ended_at: DF.Datetime | None
        error_log: DF.Code | None
        failures: DF.Int
        max_retries: DF.Int
        ok: DF.Int
        playbook: DF.Data
        retries: DF.Int
        server: DF.DynamicLink
        server_doctype: DF.Literal["Stalwart Node", "Egress Gateway"]
        skipped: DF.Int
        started_at: DF.Datetime | None
        status: DF.Literal["Pending", "Running", "Success", "Failed"]
        tasks: DF.Table[ServerJobTask]
        title: DF.Data
        unreachable: DF.Int
        variables: DF.JSON | None
        variables_builder: DF.Data | None
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def autoname(self) -> None:
        self.name = str(uuid7())

    def validate(self) -> None:
        self.status = self.status or "Pending"
        playbook_path(self.playbook)  # throws when the playbook does not exist
        # The job row is writable by operators; the code it names must not be their choice.
        if self.variables_builder and self.variables_builder not in VARIABLES_BUILDERS:
            frappe.throw(_("{0} is not a known variables builder.").format(self.variables_builder))
        if self.callback and self.callback not in CALLBACKS:
            frappe.throw(_("{0} is not a known callback.").format(self.callback))
        if self.is_new() and not self.tasks:
            for task in playbook_task_names(self.playbook):
                self.append("tasks", {"task": task, "status": "Pending"})

    def after_insert(self) -> None:
        self.enqueue()

    def enqueue(self) -> None:
        if frappe.flags.do_not_enqueue:
            self.execute()
            return

        frappe.enqueue_doc(
            self.doctype,
            self.name,
            "execute",
            queue="long",
            timeout=cint(get_config("server_job_timeout")) or 1800,
            job_id=f"server-job:{self.name}",
            deduplicate=True,
            enqueue_after_commit=True,
        )

    # --- execution --------------------------------------------------------------

    def execute(self) -> None:
        """Runs the playbook (blocking; meant for the long queue) and fires the callback."""

        if self.status == "Running" and not self.is_stale():
            return
        assert_test_host(frappe.db.get_value(self.server_doctype, self.server, "hostname"))

        self.mark_running()
        run = None
        try:
            variables = self.build_variables()
            run = PlaybookRun(self, variables)
            outcome = run.run()
        except Exception:
            # Never with_context: the locals hold the private key, passwords and provider tokens.
            traceback = frappe.get_traceback()
            self.mark_finished("Failed", error_log=run.mask(traceback) if run else traceback)
            self.fire_callback(success=False)
            return

        self.mark_finished(outcome.status, stats=outcome.stats, error_log=outcome.error_log)
        self.fire_callback(success=outcome.status == "Success")

    def build_variables(self) -> dict:
        """Playbook variables come from a builder function so secrets never touch the database."""

        if not self.variables_builder:
            return {}

        context = json.loads(self.context) if isinstance(self.context, str) else (self.context or {})
        variables = frappe.get_attr(self.variables_builder)(context)
        secret_keys = set(variables.pop("__secret_keys__", ()))
        snapshot = {
            k: (REDACTED if k in secret_keys else v) for k, v in variables.items() if k != "__secret_values__"
        }
        self.db_set("variables", json.dumps(snapshot, indent=2), update_modified=False)
        return variables

    def get_server(self) -> Document:
        return frappe.get_doc(self.server_doctype, self.server)

    def fire_callback(self, success: bool) -> None:
        if not self.callback:
            return

        method = self.callback if success else f"{self.callback}_failed"
        try:
            server = self.get_server()
        except frappe.DoesNotExistError:
            return  # the server was deleted while the job ran; nothing left to update
        if not hasattr(server, method):
            return

        # The callback's partial work is discarded on failure so the job's Failed state is consistent.
        frappe.db.savepoint("server_job_callback")
        try:
            getattr(server, method)(self)
        except Exception:
            try:
                frappe.db.rollback(save_point="server_job_callback")
            except Exception:
                frappe.db.rollback()  # the callback committed and released the savepoint
            log_exception(f"Server Job callback {method} failed", self)
            if success:
                self.mark_finished("Failed", error_log=frappe.get_traceback())
                self.fire_callback(success=False)

    # --- state -----------------------------------------------------------------

    def mark_running(self) -> None:
        self.db_set(
            {"status": "Running", "started_at": now(), "ended_at": None, "error_log": None},
            update_modified=False,
            commit=should_commit(),
            notify=True,
        )
        self.reload()

    def mark_finished(self, status: str, stats: dict | None = None, error_log: str | None = None) -> None:
        ended_at = now()
        values = {
            "status": status,
            "ended_at": ended_at,
            "duration": time_diff_in_seconds(ended_at, self.started_at or ended_at),
            "error_log": error_log,
        }
        if stats:
            values.update(
                {k: cint(stats.get(k)) for k in ("ok", "changed", "failures", "unreachable", "skipped")}
            )
        if status == "Failed":
            values["retries"] = cint(self.retries) + 1
        self.db_set(values, update_modified=False, commit=should_commit(), notify=True)
        self.reload()

    def is_superseded(self) -> bool:
        """A newer job for the same server, or a deleted server, makes an automatic retry harmful."""

        if not frappe.db.exists(self.server_doctype, self.server):
            return True
        return bool(
            frappe.db.exists(
                "Server Job",
                {
                    "server_doctype": self.server_doctype,
                    "server": self.server,
                    "creation": [">", self.creation],
                },
            )
        )

    def is_stale(self) -> bool:
        """A job still marked Running after the worker timeout lost its worker (kill, deploy, OOM)."""

        if self.status != "Running" or not self.started_at:
            return False
        timeout = cint(get_config("server_job_timeout")) or 1800
        return time_diff_in_seconds(now(), self.started_at) > timeout

    @frappe.whitelist()
    def retry(self) -> None:
        frappe.only_for("System Manager")
        if self.status != "Failed" and not self.is_stale():
            frappe.throw(_("Only failed (or stale running) jobs can be retried."))

        for task in self.tasks:
            task.db_set(
                {
                    "status": "Pending",
                    "started_at": None,
                    "ended_at": None,
                    "duration": 0,
                    "stdout": None,
                    "stderr": None,
                    "exception": None,
                    "result": None,
                },
                update_modified=False,
            )
        self.db_set({"status": "Pending", "error_log": None}, update_modified=False)
        self.enqueue()


def should_commit() -> bool:
    """Progress is committed as it happens so the desk can follow along; tests keep their rollback."""

    return not frappe.in_test


def create_server_job(
    server: Document,
    playbook: str,
    title: str,
    context: dict | None = None,
    variables_builder: str | None = None,
    callback: str | None = None,
    max_retries: int = 1,
) -> ServerJob:
    job = frappe.new_doc("Server Job")
    job.title = title
    job.server_doctype = server.doctype
    job.server = server.name
    job.playbook = playbook
    job.context = json.dumps(context or {})
    job.variables_builder = variables_builder
    job.callback = callback
    job.max_retries = max_retries
    job.insert(ignore_permissions=True)
    return job


def retry_failed_jobs() -> None:
    """Cron: retries failed jobs with attempts left, and jobs whose worker vanished."""

    jobs = frappe.get_all(
        "Server Job",
        filters={"status": ["in", ["Failed", "Running", "Pending"]]},
        fields=["name", "status", "retries", "max_retries", "started_at", "creation"],
    )
    timeout = cint(get_config("server_job_timeout")) or 1800
    for row in jobs:
        job = frappe.get_doc("Server Job", row.name)
        if job.is_superseded():
            continue
        if job.status == "Failed" and job.retries <= job.max_retries:
            job.retry()
        elif job.status == "Running" and job.is_stale():
            job.retry()
        elif job.status == "Pending" and time_diff_in_seconds(now(), job.creation) > 2 * timeout:
            if not is_job_enqueued(f"server-job:{job.name}"):
                job.enqueue()
