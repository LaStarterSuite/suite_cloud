import json
import unittest
from pathlib import Path

from suite_cloud.telemetry import EVENT_DATA_COLUMNS

WORKBOOKS = Path(__file__).resolve().parents[1] / "insights_workbook"
MAIL_DOCTYPES = Path(__file__).resolve().parents[2] / "mail" / "doctype"
FRAME_COLUMNS = {"name", "creation", "modified", "owner", "modified_by"}


def shipped_workbook(name: str) -> dict:
    return json.loads((WORKBOOKS / name / f"{name}.json").read_text())


class TestShippedWorkbooks(unittest.TestCase):
    def test_metric_charts_are_plotted_per_hour(self) -> None:
        """A gauge sampled every minute shows one point per day when bucketed by day; the host
        and Stalwart metric lines bucket by hour, with room for a week of points per host."""

        for workbook in ("stalwart",):
            for name, chart in shipped_workbook(workbook)["charts"].items():
                x_axis = chart["config"].get("x_axis", {}).get("dimension", {})
                if x_axis.get("column_name") != "timestamp" or "metrics" not in chart["query"]:
                    continue
                with self.subTest(chart=name):
                    self.assertEqual(x_axis.get("granularity"), "hour")
                    self.assertGreaterEqual(chart["config"].get("limit", 0), 1000)
                    self.assertNotIn("per day", chart.get("info") or "")

    def test_every_dashboard_opens_on_a_bounded_date_range(self) -> None:
        """Without a default every chart reads the whole retention on the first load."""

        for workbook in ("stalwart",):
            for name, dashboard in shipped_workbook(workbook)["dashboards"].items():
                for item in dashboard["items"]:
                    if item["type"] == "filter" and item["filter_type"] == "Date":
                        with self.subTest(dashboard=name):
                            self.assertEqual(item["default_operator"], "within")
                            self.assertTrue(item.get("default_value"))

    def test_the_events_query_reads_the_extracted_columns_instead_of_parsing_data(self) -> None:
        query = shipped_workbook("stalwart")["queries"]["stalwart-events"]
        mutates = [operation for operation in query["operations"] if operation["type"] == "mutate"]
        selected = next(operation for operation in query["operations"] if operation["type"] == "select")

        for mutate in mutates:
            self.assertNotIn("data", mutate["expression"]["expression"])
        self.assertLessEqual({column.name for column in EVENT_DATA_COLUMNS}, set(selected["column_names"]))

    def test_the_sending_usage_query_reads_fields_the_doctypes_have(self) -> None:
        """The query names Mail Send Usage and Mail Account columns; a renamed field fails here,
        not on an operator's dashboard."""

        def fields(doctype: str) -> set[str]:
            path = MAIL_DOCTYPES / doctype / f"{doctype}.json"
            return {field["fieldname"] for field in json.loads(path.read_text())["fields"]} | FRAME_COLUMNS

        operations = shipped_workbook("stalwart")["queries"]["stalwart-sending-usage"]["operations"]
        source, account_join = operations[0], operations[1]
        self.assertEqual(source["table"]["table_name"], "tabMail Send Usage")
        self.assertEqual(account_join["table"]["table_name"], "tabMail Account")
        usage_fields = {"account", "period", "sent", "refused", "send_limit", "reported_at"}
        self.assertLessEqual(usage_fields, fields("mail_send_usage"))
        joined = {column["column_name"] for column in account_join["select_columns"]}
        self.assertLessEqual(joined, fields("mail_account"))

    def test_the_sending_usage_filters_reach_every_chart(self) -> None:
        workbook = shipped_workbook("stalwart")
        dashboard = workbook["dashboards"]["stalwart-sending-usage"]
        charts = {item["chart"] for item in dashboard["items"] if item["type"] == "chart"}
        self.assertTrue(charts <= set(workbook["charts"]))
        for item in dashboard["items"]:
            if item["type"] == "filter":
                with self.subTest(filter=item["filter_name"]):
                    self.assertEqual(set(item["links"]), charts)
