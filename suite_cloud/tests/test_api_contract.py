"""The REST contract every API function keeps: its verb follows its name, bad input is a 400."""

from datetime import datetime

import frappe
from frappe.tests import UnitTestCase

from suite_cloud.api import email, sending_limits, telemetry, tenant
from suite_cloud.api.mail import accounts, dmarc, domains, groups, mailing_lists, meta, tls
from suite_cloud.api.tenant import InvalidInputError, api_errors, as_datetime, as_list
from suite_cloud.mail.stalwart.errors import (
    StalwartError,
    StalwartRejectedError,
    StalwartUnauthorizedError,
    StalwartUnavailableError,
)

MODULES = (tenant, email, domains, accounts, groups, mailing_lists, meta, dmarc, tls)
SERVER_MODULES = (telemetry, sending_limits)  # called by Stalwart, not by tenants
VERB_BY_PREFIX = {
    "list_": "GET",
    "get_": "GET",
    "check_": "GET",
    "ping": "GET",
    "update_": "PATCH",
    "set_": "PUT",
    "delete_": "DELETE",
    "remove_": "DELETE",
}


def api_functions(modules=MODULES):
    """Every whitelisted function of the given API modules."""

    for module in modules:
        for name, member in vars(module).items():
            if callable(member) and member in frappe.whitelisted and member.__module__ == module.__name__:
                yield f"{module.__name__}.{name}", name, member


def expected_verb(name: str) -> str:
    """Creates and actions are plain POSTs; everything else carries the verb its name says."""

    return next((verb for prefix, verb in VERB_BY_PREFIX.items() if name.startswith(prefix)), "POST")


class TestApiVerbs(UnitTestCase):
    def test_every_site_method_answers_only_the_verb_its_name_says(self) -> None:
        for path, name, function in api_functions(MODULES + SERVER_MODULES):
            methods = frappe.allowed_http_methods_for_whitelisted_func[function]
            verb = expected_verb(name)
            with self.subTest(path):
                self.assertEqual(methods, ("GET", "QUERY") if verb == "GET" else (verb,))

    def test_the_contract_covers_the_whole_package(self) -> None:
        names = {name for _, name, _ in api_functions()}
        self.assertTrue(
            {"ping", "send_email", "create_domain", "list_accounts", "delete_mailing_list"} <= names
        )

    def test_every_endpoint_resolves_the_tenant_first(self) -> None:
        # Tenant credentials sign the owner in; only the tenant_api wrapper checks state and address.
        for path, _, function in api_functions():
            with self.subTest(path):
                self.assertTrue(getattr(function, "is_tenant_api", False))

    def test_every_server_endpoint_checks_the_server_first(self) -> None:
        # A token signs the telemetry user in; only the hook_api and push_api wrappers refuse anyone else.
        names = set()
        for path, name, function in api_functions(SERVER_MODULES):
            names.add(name)
            with self.subTest(path):
                self.assertTrue(getattr(function, "is_telemetry_api", False))
        self.assertEqual(names, {"webhook", "otlp", "check"})


class TestApiInput(UnitTestCase):
    def test_malformed_input_is_a_400(self) -> None:
        for call in (
            lambda: as_list("[not json"),
            lambda: as_list(["a", "b", "c"], cap=2),
            lambda: as_datetime("yesterday-ish"),
        ):
            with self.assertRaises(InvalidInputError) as ctx:
                call()
            self.assertEqual(ctx.exception.http_status_code, 400)

    def test_mail_server_failures_leave_with_a_status_and_a_plain_message(self) -> None:
        cases = (
            (
                StalwartRejectedError({"type": "forbidden", "description": "Quota exceeded."}),
                422,
                "Quota exceeded.",
            ),
            (StalwartRejectedError({"notCreated": {"0": {"type": "alreadyExists"}}}), 422, "alreadyExists"),
            (StalwartUnavailableError(), 503, "not reachable"),
            (StalwartUnauthorizedError("bad key"), 502, "credentials"),
            (StalwartError("boom"), 502, "could not complete"),
        )
        for raised, status, text in cases:
            with self.subTest(type(raised).__name__), self.assertRaises(frappe.ValidationError) as ctx:
                with api_errors():
                    raise raised
            self.assertEqual(ctx.exception.http_status_code, status)
            self.assertIn(text, str(ctx.exception))
            self.assertNotIn("Stalwart", str(ctx.exception))

    def test_moments_parse_or_stay_absent(self) -> None:
        self.assertIsNone(as_datetime(None))
        self.assertIsNone(as_datetime(""))
        self.assertEqual(as_datetime("2026-03-04 05:06:07"), datetime(2026, 3, 4, 5, 6, 7))
