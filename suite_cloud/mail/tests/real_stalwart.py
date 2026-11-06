"""A real Stalwart of the pinned release, for what the fake cannot answer: where mail ends up.

It runs from a directory of its own and listens on loopback ports. Its one route off the server
is a relay to ``MailSink``, and tests address mail to names under example.org and example.net,
so a message that did get past the sink would have nowhere to go.
"""

import os
import shutil
import smtplib
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from functools import cached_property
from urllib.parse import urljoin

import frappe

from suite_cloud.mail.cluster import plan
from suite_cloud.mail.stalwart.client import StalwartClient
from suite_cloud.mail.stalwart.connection import ConnectionInfo, JMAPConnection
from suite_cloud.mail.stalwart.directory import Domain
from suite_cloud.mail.tests.mail_sink import MailSink

HOSTNAME = "mail.example.org"
ADMIN = "admin"
SINK_ROUTE = "sink"
# Relays, so it takes any address; held nowhere, its mail is local and bounces unless routed off.
PROBE_DOMAIN = "route-probe.example.org"
PROBE_SENDER = "probe@outside.example.net"
# Every start fetches the web interface from GitHub before it listens: a second, or half a minute.
START_TIMEOUT = 90
SETTLE_TIMEOUT = 60


def pinned_stalwart() -> tuple[str | None, str]:
    """The ``stalwart`` on PATH when it is the release new clusters install, else why it is not.

    Another release may route differently, which is the one thing these tests are about.
    """

    wanted = frappe.get_meta("Suite Cloud Settings").get_field("stalwart_version").default
    binary = shutil.which("stalwart")
    if not binary:
        return None, f"needs Stalwart {wanted} on PATH"
    found = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    if found != wanted.removeprefix("v"):
        return None, f"needs Stalwart {wanted} on PATH, found {found}"
    return binary, ""


class RealStalwart:
    def __init__(self, binary: str) -> None:
        self.binary = binary
        self.directory = tempfile.mkdtemp(prefix="suite-cloud-stalwart-")
        self.password = frappe.generate_hash(length=32)
        self.recovery_port, self.http_port, self.smtp_port = free_ports(3)
        self.sink = MailSink()
        self.process: subprocess.Popen | None = None
        self.routes = 0

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        """The stages a node goes through: bootstrap, a recovery stage for what the first normal
        start needs, then that start."""

        self.launch("bootstrap")
        self.client(self.recovery_port).singleton("Bootstrap").write(self.bootstrap_value())
        wait_for(lambda: os.path.exists(self.path("config.json")), "config.json", START_TIMEOUT)
        self.launch("recovery")
        self.prepare_first_start()
        self.launch("normal")
        self.take_test_mail()
        self.management.domains.create_id(Domain(name=PROBE_DOMAIN, dkim_algorithms=(), allow_relaying=True))

    def stop(self) -> None:
        try:
            self.terminate()
        finally:
            self.sink.close()
            shutil.rmtree(self.directory, ignore_errors=True)

    def launch(self, mode: str) -> None:
        self.terminate()
        # The recovery login also works on a normal start, which saves giving the admin a password.
        # The recovery listener binds every interface; the random password is what guards it.
        env = {
            **os.environ,
            "STALWART_HOSTNAME": HOSTNAME,
            "STALWART_PUBLIC_URL": self.url(self.http_port),
            "STALWART_RECOVERY_ADMIN": f"{ADMIN}:{self.password}",
            "STALWART_RECOVERY_MODE_PORT": str(self.recovery_port),
        }
        if mode == "recovery":
            env["STALWART_RECOVERY_MODE"] = "1"
        with open(self.path("stalwart.out"), "ab") as output:
            self.process = subprocess.Popen(
                [self.binary, "--config", self.path("config.json")],
                cwd=self.directory,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        port = self.http_port if mode == "normal" else self.recovery_port
        try:
            wait_for(lambda: self.is_listening(port), f"Stalwart to start in {mode} mode", START_TIMEOUT)
        except TimeoutError as e:
            raise TimeoutError(f"{e}: {self.output()[-2000:]}") from None

    def terminate(self) -> None:
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()  # never left behind, whatever it was busy with
            self.process.wait()
        self.process = None

    def is_listening(self, port: int) -> bool:
        if self.process.poll() is not None:
            raise RuntimeError(f"Stalwart exited: {self.output()[-2000:]}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    # --- configuration ----------------------------------------------------------------

    def bootstrap_value(self) -> dict:
        store = frappe.get_doc(
            {"doctype": "Mail Store", "kind": "Data", "type": "RocksDb", "path": self.path("data")}
        )
        return {
            "serverHostname": HOSTNAME,
            "defaultDomain": HOSTNAME.partition(".")[2],
            "requestTlsCertificate": False,
            "generateDkimKeys": False,
            "dataStore": store.config,
            "blobStore": {"@type": "Default"},
            "searchStore": {"@type": "Default"},
            "inMemoryStore": {"@type": "Default"},
            "directory": {"@type": "Internal"},
            "tracer": {**plan.log_tracer(), "path": self.path("logs")},
            "dnsServer": {"@type": "Manual"},
        }

    def prepare_first_start(self) -> None:
        """The first normal start imports the spam rules, once, and opens Stalwart's public
        listeners unless it finds some: the pin and loopback listeners go in before it."""

        client = self.client(self.recovery_port)
        client.apply(plan.spam_settings_operations())
        for protocol, port in (("http", self.http_port), ("smtp", self.smtp_port)):
            client.objects("NetworkListener").create(
                {
                    "name": protocol,
                    "protocol": protocol,
                    "bind": plan.as_set([f"127.0.0.1:{port}"]),
                    "useTls": False,
                    "tlsImplicit": False,
                }
            )

    def take_test_mail(self) -> None:
        """Lets an unauthenticated sender in as port 25 would, as fast as a test sends, and adds
        the route to the sink."""

        never = {"match": {}, "else": "false"}
        self.management.singleton("MtaStageAuth").write({"require": never})
        # The spam filter asks DNS blocklists about every message, and waits when they are slow.
        self.management.singleton("MtaStageData").write({"enableSpamFilter": never})
        throttles = self.management.objects("MtaInboundThrottle")
        throttles.delete(throttles.query_ids())  # five commands a second from one address
        self.management.objects("MtaRoute").create(
            {
                "@type": "Relay",
                "name": SINK_ROUTE,
                "address": "127.0.0.1",
                "port": self.sink.port,
                "protocol": "smtp",
                "implicitTls": False,
                "allowInvalidCerts": True,
            }
        )
        self.management.reload_settings()

    # --- what tests use ------------------------------------------------------------------

    @cached_property
    def management(self) -> StalwartClient:
        return self.client(self.http_port)

    def route(self, rules: list[dict]) -> None:
        """Routes by ``rules``; whatever they let through leaves by the sink.

        Returns once mail takes the new route: a reload can answer before the queue uses it. A
        probe address only this route sends to the sink proves it; under an older one it bounces.
        """

        self.routes += 1
        probe = f"route-{self.routes}@{PROBE_DOMAIN}"
        canary = {"if": f"rcpt == '{probe}'", "then": f"'{SINK_ROUTE}'"}
        route = {"match": plan.as_list([canary, *rules]), "else": f"'{SINK_ROUTE}'"}
        self.management.singleton("MtaOutboundStrategy").write({"route": route})
        self.management.reload_settings()
        wait_for(lambda: self.is_relayed(probe), "the new route to apply", SETTLE_TIMEOUT)
        self.sink.clear()

    def is_relayed(self, recipient: str) -> bool:
        self.send(PROBE_SENDER, recipient)
        self.settle()
        return recipient in self.sink.relayed()

    def send(self, sender: str, recipient: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = sender, recipient, "Delivery test"
        message["Date"], message["Message-ID"] = formatdate(), make_msgid(domain=sender.partition("@")[2])
        message.set_content(f"For {recipient}.")
        with smtplib.SMTP("127.0.0.1", self.smtp_port, timeout=30) as smtp:
            smtp.send_message(message, sender, [recipient])

    def settle(self) -> None:
        """Waits for the queue to empty: every message was then delivered here, handed to the
        sink, or bounced with a notice that went the same ways."""

        queue = self.management.objects("QueuedMessage")
        wait_for(lambda: not queue.query_ids(), "the queue to empty", SETTLE_TIMEOUT)

    # --- helpers -----------------------------------------------------------------------------

    def client(self, port: int) -> StalwartClient:
        info = ConnectionInfo(self.url(port), username=ADMIN, password=self.password)
        return StalwartClient(LoopbackConnection(info))

    def url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}"

    def path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def output(self) -> str:
        with open(self.path("stalwart.out"), errors="replace") as f:
            return f.read()


class LoopbackConnection(JMAPConnection):
    """The recovery listener hands out its API URL as a path."""

    @property
    def api_url(self) -> str:
        return urljoin(self.info.url, super().api_url)


def free_ports(count: int) -> list[int]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


def wait_for(condition: Callable[[], bool], what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for {what}")
        time.sleep(0.2)
