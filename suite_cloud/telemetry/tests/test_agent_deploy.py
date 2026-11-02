import json
from unittest.mock import patch

import frappe

from suite_cloud.mail.cluster import bootstrap, egress
from suite_cloud.mail.tests.fixtures import configure_settings, error_log_count, make_cluster, make_node
from suite_cloud.provisioning.ansible import playbook_task_names
from suite_cloud.telemetry import status
from suite_cloud.telemetry.agent import build, deploy
from suite_cloud.telemetry.agent.build import AgentBuild
from suite_cloud.telemetry.tests.test_agent import AgentBuildFixture
from suite_cloud.utils import clear_config_cache


class TestAgentDeploy(AgentBuildFixture):
    def setUp(self) -> None:
        super().setUp()
        frappe.flags.do_not_enqueue = True
        self.addCleanup(lambda: setattr(frappe.flags, "do_not_enqueue", False))
        configure_settings()
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 1
        settings.public_url = "https://cloud.example.test"
        settings.save()  # queues a roll-out at most, which runs after a commit a test never makes
        clear_config_cache()
        self.cluster = make_cluster()
        self.node = make_node(self.cluster)
        self.gateway = frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "cluster": self.cluster.name,
                "ipv4_address": "203.0.113.50",
                "ssh_host_keys": "203.0.113.50 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTests",
            }
        ).insert()
        self.addCleanup(
            lambda: frappe.delete_doc(
                "Egress Gateway", self.gateway.name, force=True, ignore_permissions=True, ignore_on_trash=True
            )
        )
        fixture = self

        class FixtureBuild(AgentBuild):
            def __init__(self, version: str | None = None) -> None:
                super().__init__(root=fixture.root, source=fixture.source, version=version)

        self.with_build = patch.object(deploy, "AgentBuild", FixtureBuild)
        self.with_build.start()
        self.addCleanup(self.with_build.stop)

    def test_the_agent_config_holds_the_sending_limits_only(self) -> None:
        config = deploy.agent_config(self.node)
        self.assertEqual(
            config["suite_cloud"],
            {
                "url": "https://cloud.example.test",
                "secret": self.cluster.agent_token,
                "requests_per_minute": 300,
            },
        )
        self.assertRegex(config["suite_cloud"]["secret"], r"^[0-9a-f]{20}:[0-9a-f]{40}$")
        self.assertEqual(config["node"], {"host_ip": self.node.ipv4_address, "hostname": self.node.hostname})
        self.assertFalse({"logs", "metrics"} & set(config))
        self.assertNotIn("ReadWritePaths", deploy.systemd_unit())

    def test_agent_variables_follow_the_settings_and_the_build(self) -> None:
        self.assertFalse(deploy.agent_variables(self.node, {})["agent_enabled"])  # no build yet
        version = self.build().run()

        variables = deploy.agent_variables(self.node, {})
        self.assertTrue(variables["agent_enabled"])
        self.assertEqual(variables["agent_version"], version)
        self.assertEqual(
            variables["agent_binaries"],
            {arch: str(self.build().binary_path(arch)) for arch in ("x86_64", "aarch64")},
        )
        self.assertIn('"host_ip": "203.0.113.10"', variables["agent_config_json"])
        self.assertIn("User=stalwart", variables["agent_systemd_unit"])
        self.assertEqual((variables["agent_restart"], variables["restart_stalwart"]), (False, False))
        self.assertTrue(deploy.agent_variables(self.node, {"agent_restart": True})["agent_restart"])
        self.assertEqual(variables["__secret_values__"], [self.cluster.agent_token])

        # A gateway runs no agent: it has no sending limits.
        self.assertFalse(deploy.agent_variables(self.gateway, {})["agent_enabled"])
        self.assertTrue(deploy.agent_variables(self.node, {"restart_stalwart": True})["restart_stalwart"])

        # Sources change before the next build finishes: the nodes keep getting the last build.
        (self.source / "cmd" / "main.go").write_text("package main\n\nfunc main() {}\n")
        self.assertNotEqual(self.build().version, version)
        self.assertEqual(deploy.agent_variables(self.node, {})["agent_version"], version)

        # Off: the Agents stop, and each active cluster gets a push that switches the push off.
        self.cluster.db_set("status", "Active")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        settings.save()
        clear_config_cache()
        self.assertFalse(deploy.agent_variables(self.node, {})["agent_enabled"])
        with (
            patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"),
            patch.object(deploy.push, "resync_cluster_after_commit") as resync,
        ):
            deploy.roll_out_agents()
        resync.assert_any_call(self.cluster.name)

    def test_the_quota_section_waits_for_a_build_that_answers_the_hook(self) -> None:
        self.build().run()  # 0.1.0 refuses a config with a quota section
        self.assertFalse(build.is_quota_enabled())
        self.assertNotIn("quota", deploy.agent_config(self.node))

        (self.source / "VERSION").write_text("0.2.0\n")
        self.build().run()
        self.assertTrue(build.is_quota_enabled())
        section = deploy.agent_config(self.node)["quota"]
        # The intervals are the agent's own defaults, so config.json leaves them out.
        self.assertEqual(set(section), {"hook_port", "hook_token", "store"})
        self.assertEqual(section["hook_port"], 8095)
        self.assertEqual(section["hook_token"], self.cluster.get_password("quota_hook_token"))
        self.assertEqual(section["store"], {"type": "redis", "urls": ["redis://redis.example.test:6379"]})
        self.assertNotIn("quota", deploy.agent_config(self.gateway))  # relays only, like an outbound node
        outbound = make_node(self.cluster, ipv4="203.0.113.11", role="outbound")
        self.assertNotIn("quota", deploy.agent_config(outbound))

        secrets = deploy.agent_variables(self.node, {})["__secret_values__"]
        self.assertIn(section["hook_token"], secrets)
        self.assertIn("redis://redis.example.test:6379", secrets)

    def test_the_quota_store_follows_the_in_memory_store(self) -> None:
        from suite_cloud.mail.cluster import quota
        from suite_cloud.mail.tests.fixtures import make_store

        single = frappe._dict(get_store=lambda field: None)
        self.assertEqual(quota.store_config(single), {"type": "memory"})

        sentinel = make_store(
            "In-Memory",
            "RedisSentinel",
            urls="redis://s1:26379\nredis://s2:26379",
            auth_username="stalwart",
            auth_secret="redis-pw",
            sentinel_secret="sentinel-pw",
        )
        config = quota.store_config(frappe._dict(get_store=lambda field: sentinel))
        self.assertEqual(
            config,
            {
                "type": "redis_sentinel",
                "urls": ["redis://s1:26379", "redis://s2:26379"],
                "username": "stalwart",
                "password": "redis-pw",
                "service_name": "mymaster",
                "sentinel_username": "",
                "sentinel_password": "sentinel-pw",
            },
        )
        self.assertEqual(
            set(quota.store_secrets(config)),
            {"redis://s1:26379", "redis://s2:26379", "redis-pw", "sentinel-pw"},
        )

    def test_nodes_show_their_agent_config_with_every_secret_masked(self) -> None:
        (self.source / "VERSION").write_text("0.2.0\n")
        self.build().run()
        secrets = deploy.agent_secrets(deploy.agent_config(self.node))
        self.assertEqual(len(secrets), 3)  # token, hook token, Redis URL

        shown = self.node.show_agent_config()
        text = json.dumps(shown["config"])
        for secret in secrets:
            self.assertNotIn(secret, text)
        config = shown["config"]
        self.assertEqual(config["suite_cloud"]["secret"], "***")
        self.assertEqual(config["quota"]["hook_token"], "***")
        self.assertEqual(config["node"]["host_ip"], self.node.ipv4_address)
        self.assertEqual(config["quota"]["hook_port"], 8095)
        self.assertTrue(shown["telemetry_enabled"])

        self.assertEqual(
            deploy.mask_config({"url": "redis://:pw@host"}, ["pw"]), {"url": "redis://:***@host"}
        )
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertRaises(frappe.PermissionError, self.node.show_agent_config)

    def test_node_and_gateway_jobs_carry_the_token_and_the_agent_as_secrets(self) -> None:
        self.build().run()
        node = bootstrap.build_node_variables({"node": self.node.name})
        gateway = egress.build_gateway_variables({"gateway": self.gateway.name})
        for server, variables in ((self.node, node), (self.gateway, gateway)):
            self.assertIn("env_normal", variables["__secret_keys__"])
            token = f"{server.telemetry_key}:{server.get_password('telemetry_secret')}"
            self.assertIn(f"SUITE_CLOUD_TELEMETRY_TOKEN={token}", variables["env_normal"])
            self.assertIn(token, variables["__secret_values__"])
        self.assertIn("agent_config_json", node["__secret_keys__"])
        self.assertIn(self.cluster.agent_token, node["__secret_values__"])
        self.assertTrue(node["agent_enabled"])
        self.assertFalse(gateway["agent_enabled"])  # no sending limits on a gateway

        names = playbook_task_names("configure-node.yml")
        self.assertLess(names.index("Install the Stalwart binary"), names.index("Install the agent binary"))
        upgrade = playbook_task_names("upgrade-stalwart.yml")
        self.assertLess(upgrade.index("Write the runtime environment"), upgrade.index("Restart Stalwart"))
        telemetry = playbook_task_names("install-telemetry.yml")
        self.assertLess(telemetry.index("Restart Stalwart"), telemetry.index("Install the agent binary"))

    def test_install_agent_jobs(self) -> None:
        self.assertRaisesRegex(frappe.ValidationError, "Build Agent", deploy.install_agent, self.node)
        version = self.build().run()

        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            job = deploy.install_agent(self.node)
            self.assertEqual(
                (job.playbook, job.callback, job.server),
                ("install-telemetry.yml", "after_install_agent", self.node.name),
            )
            self.assertEqual(job.title, f"Install agent {version} on {self.node.hostname}")
            self.assertEqual(frappe.db.get_value("Stalwart Node", self.node.name, "status"), self.node.status)
            # A reinstall rewrites what differs and restarts the agent whatever its version.
            self.assertTrue(bootstrap.build_node_variables(json.loads(job.context))["agent_restart"])
            self.assertFalse(bootstrap.build_node_variables({"node": self.node.name})["agent_restart"])

    def test_the_last_token_switches_the_push_on(self) -> None:
        self.build().run()
        self.cluster.db_set("status", "Active")
        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        execute = "suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"
        cluster_push = (
            "suite_cloud.mail.doctype.stalwart_cluster.stalwart_cluster.StalwartCluster.push_config"
        )
        gateway_push = "suite_cloud.mail.doctype.egress_gateway.egress_gateway.EgressGateway.push_config"

        def titles(names: list[str]) -> set[str]:
            return {frappe.db.get_value("Server Job", name, "title") for name in names}

        with patch(execute), patch(cluster_push) as pushed, patch(gateway_push) as gateway_pushed:
            # Upgrade Agents: every server gets its token; the gateway's agent stops.
            jobs = deploy.upgrade_agents(self.cluster)
            self.assertEqual(
                titles(jobs),
                {
                    f"Install agent {build.AgentBuild.last_built().version} on {self.node.hostname}",
                    f"Stop agent on {self.gateway.hostname}",
                },
            )
            pushed.assert_not_called()

            before = set(frappe.get_all("Server Job", pluck="name"))
            self.node.after_install_agent(frappe._dict())
            self.assertTrue(frappe.db.get_value("Stalwart Node", self.node.name, "telemetry_token_installed"))
            pushed.assert_called_once()
            started = set(frappe.get_all("Server Job", pluck="name")) - before
            self.assertEqual(titles(list(started)), {f"Start telemetry push on {self.node.hostname}"})
            job = frappe.get_doc("Server Job", started.pop())
            self.assertTrue(bootstrap.build_node_variables(json.loads(job.context))["restart_stalwart"])

            # A second token for the same cluster changes nothing; the gateway's own switches its push.
            self.node.after_install_agent(frappe._dict())
            pushed.assert_called_once()
            self.gateway.after_install_agent(frappe._dict())
            gateway_pushed.assert_called_once()

            # Once all have it, Upgrade Agents pushes and restarts again, which also retries a failure.
            jobs = deploy.upgrade_agents(self.cluster)
            self.assertEqual(
                titles(jobs), {f"Start telemetry push on {s.hostname}" for s in (self.node, self.gateway)}
            )

    def test_a_rotated_telemetry_token_is_written_by_a_job(self) -> None:
        token = f"{self.node.telemetry_key}:{self.node.get_password('telemetry_secret')}"
        self.assertIsNone(self.node.rotate_telemetry_token())  # not running: provisioning writes it
        self.node.db_set("status", "Active")
        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            job = self.node.rotate_telemetry_token()
        node = frappe.get_doc("Stalwart Node", self.node.name)
        self.assertNotEqual(f"{node.telemetry_key}:{node.get_password('telemetry_secret')}", token)
        self.assertEqual(frappe.db.get_value("Server Job", job, "playbook"), "install-telemetry.yml")
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertRaises(frappe.PermissionError, self.gateway.rotate_telemetry_token)

    def test_the_patch_fills_missing_tokens(self) -> None:
        from suite_cloud.patches.v1_0 import fill_telemetry_tokens

        self.node.db_set("telemetry_key", None)
        frappe.db.delete("__Auth", {"doctype": "Stalwart Node", "name": self.node.name})
        fill_telemetry_tokens.execute()
        node = frappe.get_doc("Stalwart Node", self.node.name)
        token = f"{node.telemetry_key}:{node.get_password('telemetry_secret')}"
        self.assertRegex(token, r"^[0-9a-f]{20}:[0-9a-f]{40}$")
        fill_telemetry_tokens.execute()  # a second run keeps it
        self.assertEqual(
            frappe.db.get_value("Stalwart Node", self.node.name, "telemetry_key"), node.telemetry_key
        )

    def test_a_settings_change_is_rolled_out_once(self) -> None:
        self.node.db_set("status", "Active")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        settings.save()
        with (
            patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"),
            patch.object(deploy.push, "resync_cluster_after_commit"),
        ):
            self.assertTrue(deploy.roll_out_agents())
            # A second job queued for the same change, say by a quick second turn, finds it done.
            self.assertEqual(deploy.roll_out_agents(), [])

    def test_the_settings_save_leaves_the_roll_out_to_a_background_job(self) -> None:
        # Even with do_not_enqueue on, as in setUp: the roll-out reaches every server of the site.
        before = frappe.get_all("Server Job", pluck="name")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        with patch("frappe.enqueue") as enqueue:
            settings.save()
        enqueue.assert_called_once_with(deploy.roll_out_agents, queue="long", enqueue_after_commit=True)
        self.assertEqual(frappe.get_all("Server Job", {"name": ["not in", before]}), [])

    def test_turning_telemetry_on_or_off_rolls_the_agents_out(self) -> None:
        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        draining = make_node(self.cluster, ipv4="203.0.113.12")
        draining.db_set("status", "Draining")
        make_node(self.cluster, ipv4="203.0.113.13").db_set("status", "Provisioned")
        servers = (self.node, draining, self.gateway)
        settings = frappe.get_single("Suite Cloud Settings")

        # Only this cluster's servers: the site may hold clusters of its own.
        ours = [
            name
            for doctype in ("Stalwart Node", "Egress Gateway")
            for name in frappe.get_all(doctype, {"cluster": self.cluster.name}, pluck="name")
        ]

        def jobs_after(enabled: bool) -> list[dict]:
            before = frappe.get_all("Server Job", pluck="name")
            settings.reload()
            settings.enable_telemetry = int(enabled)
            with patch("frappe.enqueue") as enqueue:
                settings.save()
                # The save only queues the roll-out; it runs here as a worker would, without playbooks.
                if any(call.args[:1] == (deploy.roll_out_agents,) for call in enqueue.call_args_list):
                    with (
                        patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"),
                        patch.object(deploy.push, "resync_cluster_after_commit"),
                    ):
                        deploy.roll_out_agents()
            filters = {"name": ["not in", before], "server": ["in", ours]}
            return frappe.get_all("Server Job", filters, ["server", "title"])

        # Off: the agents are stopped, build or not.
        jobs = jobs_after(False)
        self.assertEqual({job.title for job in jobs}, {f"Stop agent on {s.hostname}" for s in servers})

        # On without a build: an Error Log instead of jobs the play would refuse.
        errors = error_log_count()
        self.assertEqual(jobs_after(True), [])
        self.assertEqual(error_log_count(), errors + 1)

        version = self.build().run()
        jobs_after(False)
        jobs = jobs_after(True)
        self.assertEqual({job.server for job in jobs}, {s.name for s in servers})
        self.assertEqual(
            {job.title.split(" on ")[0] for job in jobs}, {f"Install agent {version}", "Stop agent"}
        )

        # A save that leaves the switch as it is changes nothing.
        self.assertEqual(jobs_after(True), [])

    def test_each_cluster_has_its_own_agent_credentials(self) -> None:
        from frappe.utils.password import get_decrypted_password, set_encrypted_password

        from suite_cloud.patches.v1_0 import move_agent_auth_to_clusters

        self.assertEqual(len(self.cluster.agent_key), 20)
        self.assertEqual(self.gateway.get_cluster().agent_token, self.cluster.agent_token)

        # Older sites: the shared secret turns telemetry on and goes; clusters get credentials.
        settings = "Suite Cloud Settings"
        frappe.db.set_single_value(settings, "enable_telemetry", 0)
        set_encrypted_password(settings, settings, "shared-secret", "telemetry_secret")
        self.cluster.db_set("agent_key", None)
        frappe.db.delete(
            "__Auth", {"doctype": "Stalwart Cluster", "name": self.cluster.name, "fieldname": "agent_secret"}
        )
        move_agent_auth_to_clusters.execute()

        self.assertEqual(frappe.db.get_single_value(settings, "enable_telemetry"), 1)
        self.assertIsNone(
            get_decrypted_password(settings, settings, "telemetry_secret", raise_exception=False)
        )
        cluster = frappe.get_doc("Stalwart Cluster", self.cluster.name)
        self.assertRegex(cluster.agent_token, r"^[0-9a-f]{20}:[0-9a-f]{40}$")
        token = cluster.agent_token
        move_agent_auth_to_clusters.execute()  # a second run keeps them
        self.assertEqual(frappe.get_doc("Stalwart Cluster", self.cluster.name).agent_token, token)

    def test_the_status_follows_the_last_batch(self) -> None:
        status.forget_contacts()
        self.addCleanup(status.forget_contacts)
        self.assertEqual(status.server_status(self.node)["state"], "unknown")

        status.record_contact(self.node.ipv4_address)
        status.record_contact(self.gateway.ipv4_address)
        self.assertEqual(status.server_status(self.gateway)["state"], "ok")

        stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2).isoformat()
        frappe.cache.hset(status.CACHE_KEY, self.node.ipv4_address, {"seen_at": stale})
        self.assertEqual(status.server_status(self.node)["state"], "silent")

        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        summary = status.cluster_summary(self.cluster)
        self.assertEqual(summary["state"], "silent")
        self.assertEqual(summary["message"], f"Telemetry: 1 reporting, 1 silent ({self.node.hostname}).")
        self.assertEqual(status.get_server_status("Stalwart Node", self.node.name)["state"], "silent")
        self.assertEqual(status.get_cluster_summary(self.cluster.name)["state"], "silent")
        self.assertRaises(
            frappe.ValidationError, status.get_server_status, "Stalwart Cluster", self.cluster.name
        )
