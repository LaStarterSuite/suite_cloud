from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from suite_cloud.mail.stalwart.config import (
    SINGLETON_TYPES,
    ActionService,
    ClusterNodeService,
    ClusterRoleService,
)
from suite_cloud.mail.stalwart.connection import JMAPConnection
from suite_cloud.mail.stalwart.credentials import ApiKeyService, AppPasswordService
from suite_cloud.mail.stalwart.directory import (
    AccountService,
    DkimSignatureService,
    DmarcReportService,
    DomainService,
    GroupService,
    MailingListService,
    RoleService,
    TlsReportService,
)
from suite_cloud.mail.stalwart.service import ManagementService, SingletonService


class StalwartClient:
    """One authenticated Stalwart server, exposed as typed services plus a plan applier."""

    def __init__(self, connection: JMAPConnection) -> None:
        self.connection = connection

    @cached_property
    def accounts(self) -> AccountService:
        return AccountService(self.connection)

    @cached_property
    def groups(self) -> GroupService:
        return GroupService(self.connection)

    @cached_property
    def domains(self) -> DomainService:
        return DomainService(self.connection)

    @cached_property
    def dkim_signatures(self) -> DkimSignatureService:
        return DkimSignatureService(self.connection)

    @cached_property
    def dmarc_reports(self) -> DmarcReportService:
        return DmarcReportService(self.connection)

    @cached_property
    def mailing_lists(self) -> MailingListService:
        return MailingListService(self.connection)

    @cached_property
    def roles(self) -> RoleService:
        return RoleService(self.connection)

    @cached_property
    def tls_reports(self) -> TlsReportService:
        return TlsReportService(self.connection)

    @cached_property
    def app_passwords(self) -> AppPasswordService:
        return AppPasswordService(self.connection)

    @cached_property
    def api_keys(self) -> ApiKeyService:
        return ApiKeyService(self.connection)

    @cached_property
    def actions(self) -> ActionService:
        return ActionService(self.connection)

    @cached_property
    def cluster_nodes(self) -> ClusterNodeService:
        return ClusterNodeService(self.connection)

    @cached_property
    def cluster_roles(self) -> ClusterRoleService:
        return ClusterRoleService(self.connection)

    def objects(self, type: str) -> ManagementService:
        """A generic service for any object type (MtaRoute, AcmeProvider, NetworkListener...)."""

        return ManagementService(self.connection, type=type)

    def singleton(self, type: str) -> SingletonService:
        return SingletonService(self.connection, type=type)

    def reload_settings(self) -> None:
        """Core server configuration only applies after a ReloadSettings action."""

        self.actions.run("ReloadSettings")

    def apply(self, plan: list[dict]) -> ApplyResult:
        return PlanApplier(self).apply(plan)


# Stalwart never echoes secrets back, so they would look changed on every apply.
WRITE_ONLY_KEYS = frozenset(
    {"credentials", "secret", "authSecret", "secretKey", "secretAccessKey", "apiKey", "licenseKey"}
)


def is_write_only(key: str, value: Any) -> bool:
    if key in WRITE_ONLY_KEYS:
        return True
    return isinstance(value, dict) and value.get("@type") == "Value" and "secret" in value


@dataclass
class ApplyResult:
    created: dict[str, str] = field(default_factory=dict)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)

    @property
    def ids(self) -> dict[str, str]:
        return self.created


class PlanApplier:
    """Applies a stalwart-cli style plan (list of operations) over JMAP.

    Mirrors the CLI's ``upsert``/``update``/``destroy`` semantics for the handful of object
    types Suite Cloud owns: objects are matched by ``matchOn`` properties (default ``name``),
    ``#ref`` strings resolve to the ids of earlier operations, and singleton updates omit the id.
    Only differing properties are sent on a match, so re-applying a plan is a no-op.
    """

    def __init__(self, client: StalwartClient) -> None:
        self.client = client
        self.ids: dict[str, str] = {}
        self.result = ApplyResult()

    def apply(self, plan: list[dict]) -> ApplyResult:
        for operation in plan:
            kind = operation["@type"]
            if kind in ("upsert", "create"):
                self._upsert(operation)
            elif kind == "update":
                self._update(operation)
            elif kind == "destroy":
                self._destroy(operation)
            else:
                raise ValueError(f"Unsupported plan operation: {kind}")

        self.result.created = dict(self.ids)
        return self.result

    # --- operations -------------------------------------------------------

    def _upsert(self, operation: dict) -> None:
        object_type = operation["object"]
        match_on = operation.get("matchOn") or ["name"]
        service = self.client.objects(object_type)
        existing = service.get_all()

        for ref, value in operation["value"].items():
            value = self._resolve(value)
            match = next((obj for obj in existing if all(obj.get(k) == value.get(k) for k in match_on)), None)
            if match is None:
                created = service.create(value)
                existing.append(created)
                self.ids[ref] = created["id"]
                continue

            self.ids[ref] = match["id"]
            # Stalwart never echoes secrets, so a rotated one cannot be told from the old one:
            # write-only values are always sent, but only a visible difference counts as an update.
            # Account credentials stay out: replacing the map would wipe app passwords and API keys.
            visible = {
                k: v
                for k, v in value.items()
                if k not in match_on and k != "credentials" and not is_write_only(k, v) and match.get(k) != v
            }
            secrets = {k: v for k, v in value.items() if k != "credentials" and is_write_only(k, v)}
            patch = {**visible, **secrets}
            if patch:
                service.update(match["id"], patch)
                match.update(patch)
            if visible:
                self.result.updated.append(match["id"])
            else:
                self.result.unchanged.append(match["id"])

    def _update(self, operation: dict) -> None:
        object_type = operation["object"]
        value = self._resolve(operation["value"])
        if operation.get("id"):
            id = self._resolve(operation["id"])
            self.client.objects(object_type).update(id, value)
            self.result.updated.append(id)
        elif object_type in SINGLETON_TYPES:
            self.client.singleton(object_type).write(value)
            self.result.updated.append(f"{object_type}/singleton")
        else:
            raise ValueError(f"update of {object_type} needs an id")

    def _destroy(self, operation: dict) -> None:
        """Like the CLI, sends ``value`` as the query filter, so the type must support it."""

        service = self.client.objects(operation["object"])
        for id in service.query_ids(filter=self._resolve(operation.get("value") or {})):
            service.delete(id)
            self.result.destroyed.append(id)

    # --- references -------------------------------------------------------

    def _resolve(self, value: Any) -> Any:
        """Replaces ``#ref`` strings (and dict keys) with the ids of earlier operations."""

        if isinstance(value, str) and value.startswith("#"):
            ref = value[1:]
            if ref not in self.ids:
                raise ValueError(f"Plan references #{ref} before it is created")
            return self.ids[ref]
        if isinstance(value, dict):
            return {self._resolve(k): self._resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        return value
