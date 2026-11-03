"""The indexes the hot queries and the uniqueness rules rely on, and none that repeat the primary key."""

import frappe
from frappe.tests import IntegrationTestCase

COMPOSITE_INDEXES = {
    "DNS Record": [("dns_zone", "host")],
    "DMARC Report": [("tenant", "date_range_end")],
    "TLS Report": [("tenant", "date_range_end")],
    "Stalwart Node": [("cluster", "ipv4_address")],
    "Egress Gateway": [("cluster", "ipv4_address")],
    "Mailing List Recipient": [("email",), ("tenant", "mailing_list", "email")],
    "Suite Site": [("region", "status"), ("status",)],
    "Suite Tenant Request": [("status",), ("tenant",)],
    "Telemetry Alert": [("host_ip", "alert_type"), ("status",)],
    "Tenant Email": [("tenant", "sent_at")],
}

UNIQUE_INDEXES = {
    "DMARC Report": [("cluster", "stalwart_id")],
    "TLS Report": [("cluster", "stalwart_id")],
    "Egress IP Pool Address": [("ip_address",)],
    "Mail Address Alias": [("alias_email",)],
    "Mail Send Usage": [("account", "period")],
    "Mailing List Recipient": [("mailing_list", "email")],
    "Stalwart Cluster": [("dns_zone", "label")],
}

NAME_FIELDS = {
    "DNS Zone": "domain_name",
    "Egress Gateway": "hostname",
    "Mail Account": "email",
    "Mail Domain": "domain_name",
    "Mail Group": "email",
    "Mail Region": "code",
    "Mailing List": "email",
    "Region": "region",
    "Stalwart Cluster": "hostname",
    "Stalwart Node": "hostname",
}


def get_indexes(doctype: str, unique: bool) -> set[tuple[str, ...]]:
    columns: dict[str, list[str]] = {}
    for row in frappe.db.sql(f"SHOW INDEX FROM `tab{doctype}`", as_dict=True):
        if row.Key_name != "PRIMARY" and bool(row.Non_unique) != unique:
            columns.setdefault(row.Key_name, []).append(row.Column_name)
    return {tuple(found) for found in columns.values()}


class TestIndexes(IntegrationTestCase):
    def test_query_indexes_exist(self):
        for doctype, expected in COMPOSITE_INDEXES.items():
            for columns in expected:
                self.assertIn(columns, get_indexes(doctype, unique=False), doctype)

    def test_unique_indexes_exist(self):
        for doctype, expected in UNIQUE_INDEXES.items():
            for columns in expected:
                self.assertIn(columns, get_indexes(doctype, unique=True), doctype)

    def test_name_fields_have_no_second_index(self):
        for doctype, fieldname in NAME_FIELDS.items():
            indexed = get_indexes(doctype, unique=True) | get_indexes(doctype, unique=False)
            self.assertNotIn((fieldname,), indexed, doctype)
            self.assertFalse(frappe.get_meta(doctype).get_field(fieldname).unique, doctype)
