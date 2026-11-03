import json
import re
import unittest
from pathlib import Path

from suite_cloud.telemetry import EVENT_DATA_COLUMNS, METRIC_NAMES

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

    def test_filters_sit_side_by_side_from_the_left(self) -> None:
        for name, dashboard in shipped_workbook("stalwart")["dashboards"].items():
            layouts = [item["layout"] for item in dashboard["items"] if item["type"] == "filter"]
            with self.subTest(dashboard=name):
                self.assertEqual(
                    sorted(layout["x"] for layout in layouts), [index * 4 for index in range(len(layouts))]
                )

    def test_line_charts_draw_plain_lines(self) -> None:
        for name, chart in shipped_workbook("stalwart")["charts"].items():
            if chart["chart_type"] == "Line":
                with self.subTest(chart=name):
                    self.assertFalse(chart["config"]["y_axis"].get("show_area"))
                    self.assertFalse(chart["config"]["y_axis"].get("stack"))

    def test_every_metric_the_workbook_reads_is_one_stalwart_exports(self) -> None:
        workbook = shipped_workbook("stalwart")
        names = set(re.findall(r"metric == '([^']+)'", json.dumps(workbook)))
        for chart in workbook["charts"].values():
            for rule in chart["config"].get("filters", {}).get("filters", []):
                if rule["column"]["column_name"] == "metric" and rule["operator"] == "in":
                    names.update(rule["value"])

        self.assertEqual(names, set(METRIC_NAMES))

    def test_current_readings_take_the_host_filter_and_leave_the_date_filter_off(self) -> None:
        """A current value is the newest reading, so a period would only hide it."""

        workbook = shipped_workbook("stalwart")
        current = {name for name, chart in workbook["charts"].items() if "current" in chart["query"]}
        for name, dashboard in workbook["dashboards"].items():
            shown = {item["chart"] for item in dashboard["items"] if item["type"] == "chart"} & current
            links = {
                item["filter_name"]: set(item["links"])
                for item in dashboard["items"]
                if item["type"] == "filter"
            }
            if shown:
                with self.subTest(dashboard=name):
                    self.assertFalse(shown & links["Date Range"])
                    self.assertLessEqual(shown, links["Host"])

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
