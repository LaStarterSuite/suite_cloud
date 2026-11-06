from uuid import UUID

import frappe
from frappe.model.naming import set_new_name
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.tests.fixtures import configure_settings, make_cluster
from suite_cloud.patches.v1_0 import name_records_with_uuid7


class TestRecordNames(IntegrationTestCase):
    def test_new_records_are_named_with_uuid7(self) -> None:
        for doctype in name_records_with_uuid7.DOCTYPES:
            doc = frappe.new_doc(doctype)
            set_new_name(doc)
            self.assertEqual(UUID(doc.name).version, 7, doctype)

    def test_the_patch_renames_hash_named_records_and_their_links_follow(self) -> None:
        configure_settings()
        cluster = make_cluster(multi_node=False)
        store = frappe.rename_doc(
            "Mail Store", cluster.data_store, frappe.generate_hash(length=10), force=True
        )
        legacy = {
            "DNS Record": self.insert_hash_named(
                "DNS Record",
                dns_zone="example.test",
                host="mx",
                type="A",
                category="Server",
                value="203.0.113.1",
            ),
            "Mailing List Recipient": self.insert_hash_named(
                "Mailing List Recipient", mailing_list="team@example.test", email="ann@example.test"
            ),
            "Server Job": self.insert_hash_named("Server Job", title="Done", status="Success"),
            "Server Alert": self.insert_hash_named(
                "Server Alert", alert_type="Metrics Silent", status="Open", host_ip="203.0.113.1"
            ),
        }
        pending_job = self.insert_hash_named("Server Job", title="Queued", status="Pending")

        name_records_with_uuid7.execute()

        for doctype, name in {**legacy, "Mail Store": store}.items():
            self.assertFalse(frappe.db.exists(doctype, name), doctype)
        data_store = frappe.db.get_value("Stalwart Cluster", cluster.name, "data_store")
        self.assertEqual(UUID(data_store).version, 7)
        self.assertTrue(frappe.db.exists("Mail Store", data_store))
        self.assertTrue(frappe.db.exists("Server Job", pending_job))

        renamed = frappe.get_all("Server Alert", pluck="name")
        name_records_with_uuid7.execute()  # a second run leaves UUID names alone
        self.assertCountEqual(frappe.get_all("Server Alert", pluck="name"), renamed)

    def insert_hash_named(self, doctype: str, **fields) -> str:
        name = frappe.generate_hash(length=10)
        frappe.get_doc({"doctype": doctype, "name": name, **fields}).db_insert()
        return name
