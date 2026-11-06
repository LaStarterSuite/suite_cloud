# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""One condition the alert checker (``suite_cloud.telemetry.alerts``) found on a server: its metrics
or its outbound scan shipper went silent, or it logs too many errors."""

from uuid import uuid7

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class ServerAlert(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        alert_type: DF.Literal["Metrics Silent", "Outbound Scan Silent", "Error Rate"]
        details: DF.SmallText | None
        dismissed: DF.Check
        host_ip: DF.Data
        hostname: DF.Data | None
        opened_at: DF.Datetime | None
        resolved_at: DF.Datetime | None
        status: DF.Literal["Open", "Resolved"]
        title: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    def validate(self) -> None:
        """Resolved by anyone but the checker is a dismissal, which the checker honors until the
        server reports again."""

        self.title = f"{self.alert_type}: {self.hostname or self.host_ip}"
        if self.status == "Open":
            self.resolved_at = None
            self.dismissed = 0
        elif not self.resolved_at:
            self.resolved_at = now_datetime()
            self.dismissed = 0 if self.flags.resolved_by_check else 1


def on_doctype_update() -> None:
    """A dismissed alert is looked up by host and type."""

    frappe.db.add_index("Server Alert", ["host_ip", "alert_type"])
