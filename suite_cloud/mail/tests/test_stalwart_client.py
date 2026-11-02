from unittest.mock import patch

import requests
from frappe.tests import UnitTestCase

from suite_cloud.mail.stalwart.client import StalwartClient
from suite_cloud.mail.stalwart.connection import ConnectionInfo, JMAPConnection
from suite_cloud.mail.stalwart.credentials import Credential
from suite_cloud.mail.stalwart.directory import Account, Domain, EmailAlias, Group, MailingList
from suite_cloud.mail.stalwart.errors import (
    StalwartRejectedError,
    StalwartUnauthorizedError,
    StalwartUnavailableError,
)
from suite_cloud.mail.stalwart.service import CORE_CAPABILITY
from suite_cloud.mail.tests.fake_stalwart import FakeStalwart


class TestStalwartClient(UnitTestCase):
    def setUp(self) -> None:
        self.fake = FakeStalwart()
        self._install = self.fake.install()
        self._install.__enter__()
        self.addCleanup(self._install.__exit__, None, None, None)
        self.client = self.admin_client()

    def admin_client(self) -> StalwartClient:
        info = ConnectionInfo(self.fake.base_url, username="admin", password="secret")
        return StalwartClient(JMAPConnection(info))

    def test_session_discovery_scopes_calls_to_the_admin_account(self) -> None:
        self.assertEqual(self.client.domains.account_id, self.fake.admin_id)

    def test_bearer_token_authenticates(self) -> None:
        self.fake.add_token("tok-1")
        client = StalwartClient(JMAPConnection(ConnectionInfo(self.fake.base_url, token="tok-1")))
        self.assertEqual(client.domains.get_all(), [])

    def test_bad_credentials_raise_unauthorized(self) -> None:
        info = ConnectionInfo(self.fake.base_url, username="admin", password="wrong")
        self.assertRaises(StalwartUnauthorizedError, JMAPConnection, info)

    def test_an_unreachable_server_names_the_url_and_the_cause(self) -> None:
        session = requests.Session()
        mismatch = requests.exceptions.SSLError("certificate verify failed: Hostname mismatch")
        with (
            patch("suite_cloud.mail.stalwart.connection.http_session_factory", return_value=session),
            patch.object(session, "request", side_effect=mismatch),
            self.assertRaises(StalwartUnavailableError) as raised,
        ):
            JMAPConnection(ConnectionInfo("https://mail.c1.example.test", token="tok-1"))
        self.assertIn("https://mail.c1.example.test", str(raised.exception))
        self.assertIn("Hostname mismatch", str(raised.exception))
        self.assertTrue(str(raised.exception).startswith("Stalwart unavailable: GET "))

    def test_domain_lifecycle(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com", description="Example"))

        self.assertEqual(self.client.domains.find_by_name("example.com")["id"], domain_id)
        self.assertEqual(len(self.client.dkim_signatures.get_all_by_domain(domain_id)), 2)
        self.assertIn("v=spf1", self.client.domains.get_zone_file(domain_id))

        self.client.domains.delete(domain_id)
        self.assertIsNone(self.client.domains.find_by_name("example.com"))
        self.assertEqual(self.client.dkim_signatures.get_all_by_domain(domain_id), [])

    def test_duplicate_domain_is_rejected_with_stalwart_error_type(self) -> None:
        self.client.domains.create_id(Domain(name="dup.com"))

        with self.assertRaises(StalwartRejectedError) as ctx:
            self.client.domains.create_id(Domain(name="dup.com"))

        self.assertEqual(ctx.exception.error_type, "alreadyExists")
        self.assertEqual(ctx.exception.http_status_code, 422)

    def test_account_wire_format_and_patches(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        group_id = self.client.groups.create_id(Group(name="sales", domain_id=domain_id))
        account = self.client.accounts.create(
            Account(
                name="alice",
                domain_id=domain_id,
                password="pw",
                member_group_ids=[group_id],
                aliases=[EmailAlias("ally", domain_id)],
                disk_quota_bytes=2 * 1024**3,
            )
        )

        self.assertEqual(account["emailAddress"], "alice@example.com")
        self.assertEqual(account["credentials"], {"0": {"@type": "Password", "secret": "pw"}})
        self.assertEqual(account["memberGroupIds"], {group_id: True})
        self.assertEqual(account["quotas"], {"maxDiskQuota": 2 * 1024**3})
        self.assertEqual(account["roles"], {"@type": "User"})
        self.assertEqual(self.client.groups.get_member_ids(group_id), [account["id"]])

        self.client.accounts.set_password(account["id"], "new-pw")
        self.client.accounts.set_member_group_ids(account["id"], [])
        role_id = self.fake._add("Role", {"description": "suite-disabled"})
        self.client.accounts.set_roles(account["id"], [role_id])
        stored = self.fake.get("Account", account["id"])
        self.assertEqual(stored["credentials"]["0"]["secret"], "new-pw")
        self.assertEqual(stored["memberGroupIds"], {})
        self.assertEqual(stored["roles"], {"@type": "Custom", "roleIds": {role_id: True}})
        self.assertRaises(StalwartRejectedError, self.client.accounts.set_roles, account["id"], ["role-x"])

    def test_disabling_one_permission_keeps_the_accounts_other_permissions(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        accounts = self.client.accounts
        live = lambda account_id: self.fake.get("Account", account_id)["permissions"]  # noqa: E731

        def set_disabled(account_id: str, permission: str, disabled: bool) -> None:
            changed = accounts.changed_permissions(account_id, permission, disabled)
            accounts.update(account_id, {"permissions": changed})

        # An account that inherits everything gains the one denial, and loses it again.
        plain = accounts.create_id(Account(name="plain", domain_id=domain_id))
        set_disabled(plain, "emailReceive", True)
        self.assertEqual(
            live(plain),
            {"@type": "Merge", "enabledPermissions": {}, "disabledPermissions": {"emailReceive": True}},
        )
        set_disabled(plain, "emailReceive", False)
        self.assertEqual(live(plain), {"@type": "Inherit"})

        # Grants and denials merged in by hand stay, whichever way the one permission goes.
        merged = accounts.create_id(Account(name="merged", domain_id=domain_id))
        self.fake.get("Account", merged)["permissions"] = {
            "@type": "Merge",
            "enabledPermissions": {"sysAccountGet": True},
            "disabledPermissions": {"authenticate": True},
        }
        set_disabled(merged, "emailReceive", True)
        self.assertEqual(
            live(merged),
            {
                "@type": "Merge",
                "enabledPermissions": {"sysAccountGet": True},
                "disabledPermissions": {"authenticate": True, "emailReceive": True},
            },
        )
        set_disabled(merged, "emailReceive", False)
        self.assertEqual(
            live(merged),
            {
                "@type": "Merge",
                "enabledPermissions": {"sysAccountGet": True},
                "disabledPermissions": {"authenticate": True},
            },
        )

        # A list that replaces the roles' is never turned into inheritance, even once it is empty:
        # that would hand the account every permission it was kept from.
        bare = accounts.create_id(Account(name="bare", domain_id=domain_id))
        self.fake.get("Account", bare)["permissions"] = {
            "@type": "Replace",
            "enabledPermissions": {},
            "disabledPermissions": {"emailReceive": True},
        }
        set_disabled(bare, "emailReceive", False)
        self.assertEqual(
            live(bare), {"@type": "Replace", "enabledPermissions": {}, "disabledPermissions": {}}
        )

    def test_group_delete_clears_membership(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        group_id = self.client.groups.create_id(Group(name="team", domain_id=domain_id))
        account_id = self.client.accounts.create_id(
            Account(name="bob", domain_id=domain_id, member_group_ids=[group_id])
        )

        self.client.groups.delete(group_id)

        self.assertEqual(self.fake.get("Account", account_id)["memberGroupIds"], {})
        self.assertIsNone(self.fake.get("Account", group_id))

    def test_mailing_list_recipients_are_a_set(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        list_id = self.client.mailing_lists.create_id(
            MailingList(name="all", domain_id=domain_id, recipients=["a@example.com"])
        )
        self.client.mailing_lists.set_recipients(list_id, ["b@example.com", "c@x.org"])

        self.assertEqual(
            self.fake.get("MailingList", list_id)["recipients"], {"b@example.com": True, "c@x.org": True}
        )

    def test_app_password_via_master_user_login(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        self.client.accounts.create_id(Account(name="carol", domain_id=domain_id, password="pw"))

        info = ConnectionInfo(self.fake.base_url, username="carol@example.com%admin", password="secret")
        member = StalwartClient(JMAPConnection(info))
        credential_id, secret = member.app_passwords.create_secret(Credential(description="Suite"))

        self.assertTrue(secret.startswith("apppassword-"))
        self.assertNotEqual(member.app_passwords.account_id, self.fake.admin_id)
        self.assertIn(credential_id, self.fake.objects[f"AppPassword:{member.app_passwords.account_id}"])

    def test_api_key_returns_secret_once(self) -> None:
        _, secret = self.client.api_keys.create_secret(Credential(description="suite-cloud"))
        self.assertTrue(secret.startswith("apikey-"))
        self.assertNotIn("secret", self.client.api_keys.get_all()[0])

    def test_plan_apply_is_idempotent_and_resolves_refs(self) -> None:
        plan = [
            {
                "@type": "upsert",
                "object": "ClusterRole",
                "matchOn": ["name"],
                "value": {"full": {"name": "full", "tasks": {"@type": "EnableAll"}}},
            },
            {
                "@type": "upsert",
                "object": "Domain",
                "matchOn": ["name"],
                "value": {"default": {"name": "blr.test"}},
            },
            {"@type": "update", "object": "SystemSettings", "value": {"defaultDomainId": "#default"}},
        ]

        first = self.client.apply(plan)
        second = self.client.apply(plan)

        self.assertEqual(len(self.fake.all("ClusterRole")), 1)
        self.assertEqual(self.fake.singletons["SystemSettings"]["defaultDomainId"], first.ids["default"])
        self.assertEqual(second.unchanged, [first.ids["full"], first.ids["default"]])

        plan[0]["value"]["full"]["description"] = "changed"
        third = self.client.apply(plan)
        self.assertEqual(third.updated, [first.ids["full"], "SystemSettings/singleton"])
        self.assertEqual(self.fake.get("ClusterRole", first.ids["full"])["description"], "changed")

    def test_tracers_are_matched_by_kind(self) -> None:
        from suite_cloud.mail.cluster.plan import tracer_operation

        # What bootstrap leaves behind: one Journal tracer at info, no name to match on.
        journal_id = self.fake._add("Tracer", {"@type": "Journal", "enable": True, "level": "info"})

        first = self.client.apply([tracer_operation()])
        tracers = {t["@type"]: t for t in self.fake.all("Tracer")}
        self.assertEqual(set(tracers), {"Journal", "Log"})
        self.assertEqual((tracers["Journal"]["id"], tracers["Journal"]["level"]), (journal_id, "warn"))
        self.assertEqual((tracers["Log"]["path"], tracers["Log"]["rotate"]), ("/var/log/stalwart", "daily"))
        self.assertEqual(first.updated, [journal_id])

        second = self.client.apply([tracer_operation()])
        self.assertEqual((second.updated, len(second.unchanged)), ([], 2))

    def test_resolver_and_spam_settings_are_updated_as_singletons(self) -> None:
        from suite_cloud.mail.cluster.plan import dns_resolver_operation

        rules = {"spamFilterRulesUrl": "https://rules.example.test/v3.0.1.json.gz"}
        spam = {"@type": "update", "object": "SpamSettings", "value": rules}
        result = self.client.apply([dns_resolver_operation(), spam])

        self.assertEqual(result.updated, ["DnsResolver/singleton", "SpamSettings/singleton"])
        resolver = self.fake.singletons["DnsResolver"]
        self.assertEqual((resolver["@type"], resolver["servers"]["1"]["protocol"]), ("Custom", "tcp"))
        self.assertEqual(self.fake.singletons["SpamSettings"], rules)

    def test_metrics_exporter_is_updated_as_a_singleton(self) -> None:
        from suite_cloud.mail.cluster.plan import _drifted

        operation = {"@type": "update", "object": "Metrics", "value": {"prometheus": {"@type": "Disabled"}}}
        first = self.client.apply([operation])
        self.assertEqual(first.updated, ["Metrics/singleton"])
        self.assertEqual(self.fake.singletons["Metrics"]["prometheus"], {"@type": "Disabled"})

        on = {"prometheus": {"@type": "Enabled", "authUsername": "telemetry"}}
        self.assertEqual(
            _drifted("Metrics", on, operation["value"]), [{"object": "Metrics", "property": "prometheus"}]
        )

    def test_plan_apply_resends_secrets_without_counting_them_as_changes(self) -> None:
        plan = [
            {
                "@type": "upsert",
                "object": "MtaRoute",
                "matchOn": ["name"],
                "value": {
                    "r": {
                        "@type": "Relay",
                        "name": "egress-x",
                        "port": 2525,
                        "authSecret": {"@type": "Value", "secret": "pw"},
                    }
                },
            }
        ]
        self.client.apply(plan)
        result = self.client.apply(plan)
        self.assertEqual(result.updated, [])
        self.assertEqual(len(result.unchanged), 1)
        # A rotated secret still reaches the existing object, which Stalwart cannot report as changed.
        plan[0]["value"]["r"]["authSecret"] = {"@type": "Value", "secret": "rotated"}
        result = self.client.apply(plan)
        self.assertEqual(result.updated, [])
        self.assertEqual(self.fake.find("MtaRoute", name="egress-x")["authSecret"]["secret"], "rotated")

    def test_get_many_respects_max_objects_in_get(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        ids = [self.client.accounts.create_id(Account(name=f"u{i}", domain_id=domain_id)) for i in range(5)]
        self.client.domains.connection.session["capabilities"][CORE_CAPABILITY]["maxObjectsInGet"] = 2

        calls = len(self.fake.calls)
        objects = self.client.accounts.get_many(ids, properties=["id", "usedDiskQuota"])

        self.assertEqual([o["id"] for o in objects], ids)
        gets = [args for name, args in self.fake.calls[calls:] if name == "x:Account/get"]
        self.assertEqual([len(g["ids"]) for g in gets], [2, 2, 1])

    def test_unsupported_query_filters_are_refused_by_the_fake(self) -> None:
        self.assertRaises(StalwartRejectedError, self.client.roles.find, {"description": "x"})
        self.assertIsNone(self.client.roles.find_by_description("missing"))

    def test_patches_need_existing_parents(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        account_id = self.client.accounts.create_id(Account(name="nopw", domain_id=domain_id))
        self.fake.get("Account", account_id)["credentials"] = {}
        self.client.accounts.set_password(account_id, "fresh-pw")  # falls back to replacing the map
        self.assertEqual(self.fake.get("Account", account_id)["credentials"]["0"]["secret"], "fresh-pw")

    def test_reload_settings_runs_an_action(self) -> None:
        self.client.reload_settings()
        self.assertEqual(self.fake.all("Action")[0]["@type"], "ReloadSettings")
