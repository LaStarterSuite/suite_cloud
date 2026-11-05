"""An in-process stand-in for Stalwart's JMAP management API.

Mounted as a ``requests`` transport adapter, so the real client code runs unchanged against it.
It models what Suite Cloud relies on: session discovery, Basic/Bearer/master-user auth,
``x:<Type>/get|set|query`` with creation and result references, JSON-pointer patches,
server-set fields (ids, emailAddress, dnsZoneFile, credential secrets), singletons and the
uniqueness rules Stalwart enforces on domains and addresses.
"""

import base64
import json
import re
import secrets
from collections import defaultdict
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlparse

import requests
from requests.adapters import BaseAdapter

CORE = "urn:ietf:params:jmap:core"
MANAGEMENT = "urn:stalwart:jmap"
MAIL = "urn:ietf:params:jmap:mail"

SINGLETONS = {
    "Bootstrap",
    "SystemSettings",
    "Coordinator",
    "DataStore",
    "BlobStore",
    "SearchStore",
    "InMemoryStore",
    "MtaOutboundStrategy",
    "MtaStageAuth",
    "DnsResolver",
    "SpamSettings",
    "Enterprise",
    "Metrics",
}
ACCOUNT_SCOPED = {"AppPassword", "ApiKey"}
ADDRESS_TYPES = ("Account", "MailingList")

# Query filters each type documents (stalw.art/docs/ref/object/<type>); anything else is refused.
QUERY_FILTERS = {
    "Account": {"text", "name", "domainId", "memberTenantId", "memberGroupIds"},
    "Domain": {"text", "name", "memberTenantId"},
    "MailingList": {"text", "memberTenantId"},
    "DkimSignature": {"domainId", "memberTenantId"},
    "Role": {"text"},
    "Tenant": {"text"},
    "MemoryLookupKey": {"namespace"},
    "DmarcExternalReport": {
        "domain",
        "totalFailedSessions",
        "totalSuccessfulSessions",
        "expiresAt",
        "memberTenantId",
    },
}
REFERENCES = {"domainId": "Domain", "memberGroupIds": "Account", "roleIds": "Role"}

SCHEMA = {
    "enums": {
        "Locale": [{"id": "en-US", "description": "English (US)"}, {"id": "de-DE", "description": "German"}],
        "TimeZone": [{"id": "UTC"}, {"id": "Asia/Kolkata"}],
        "Permission": [{"id": "authenticate"}, {"id": "emailReceive"}, {"id": "sysAccountGet"}],
        "StorageQuota": [
            {"name": "maxEmails", "label": "Maximum number of emails"},
            {"name": "maxSieveScripts", "label": "Maximum number of Sieve scripts"},
            {"name": "maxDiskQuota", "label": "Maximum disk space allocated (bytes)"},
        ],
    }
}


class FakeError(Exception):
    def __init__(self, type: str, description: str = "") -> None:
        self.type = type
        self.description = description


class FakeStalwart:
    def __init__(
        self, base_url: str = "https://mail.test", admin_user: str = "admin", admin_password: str = "secret"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.objects: dict[str, dict[str, dict]] = defaultdict(dict)
        self.singletons: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.session_state = "s1"
        self._counter = 0
        self.admin_id = self._add(
            "Account", {"@type": "User", "name": admin_user, "roles": {"@type": "Admin"}}
        )

    # --- mounting ---------------------------------------------------------------

    @contextmanager
    def install(self):
        """Routes every requests.Session the client creates to this fake."""

        adapter = FakeAdapter(self)

        def factory() -> requests.Session:
            session = requests.Session()
            session.mount(self.base_url, adapter)
            return session

        with patch("suite_cloud.mail.stalwart.connection.http_session_factory", factory):
            yield self

    # --- helpers for tests --------------------------------------------------------

    def add_cluster_node(self, hostname: str, node_id: int = 1, status: str = "active") -> str:
        return self._add(
            "ClusterNode",
            {
                "nodeId": node_id,
                "hostname": hostname,
                "lastRenewal": "2026-01-01T00:00:00Z",
                "status": status,
            },
        )

    def add_token(self, secret: str, account_id: str | None = None) -> None:
        self.tokens[secret] = account_id or self.admin_id

    def get(self, type: str, id: str) -> dict | None:
        return self.objects[type].get(id)

    def all(self, type: str) -> list[dict]:
        return list(self.objects[type].values())

    def find(self, type: str, **fields) -> dict | None:
        return next((o for o in self.all(type) if all(o.get(k) == v for k, v in fields.items())), None)

    # --- HTTP -------------------------------------------------------------------------

    def handle(self, request: requests.PreparedRequest) -> requests.Response:
        path = urlparse(request.url).path
        account = self._authenticate(request.headers.get("Authorization"))
        if account is None:
            return json_response(401, {"type": "about:blank", "title": "Unauthorized", "status": 401})
        if self._is_receive_only(account):
            return json_response(403, {"type": "about:blank", "title": "Forbidden", "status": 403})

        if path == "/.well-known/jmap":
            return json_response(200, self._session(account))
        if path == "/jmap" and request.method == "POST":
            body = json.loads(request.body or b"{}")
            return json_response(200, self._jmap(body, account))
        if path == "/api/schema":
            return json_response(200, SCHEMA)
        return json_response(404, {"status": 404, "title": "Not Found"})

    def _authenticate(self, header: str | None) -> str | None:
        """Returns the authenticated account id, or None."""

        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer":
            return self.tokens.get(value)
        if scheme.lower() == "basic":
            try:
                username, password = base64.b64decode(value).decode().split(":", 1)
            except Exception:
                return None
            if password != self.admin_password:
                return None
            if username == self.admin_user:
                return self.admin_id
            # master-user login: <account email>%<admin>
            email, _, admin = username.partition("%")
            if admin != self.admin_user:
                return None
            account = next((a for a in self.all("Account") if a.get("emailAddress") == email), None)
            return account["id"] if account else None
        return None

    def _is_receive_only(self, account_id: str) -> bool:
        """Custom roles that grant nothing beyond receiving refuse the session, as a disabled account's do."""

        roles = self.objects["Account"][account_id].get("roles") or {}
        if roles.get("@type") != "Custom":
            return False
        granted = {
            permission
            for role_id in roles.get("roleIds", {})
            for permission in (self.get("Role", role_id) or {}).get("enabledPermissions", {})
        }
        return granted <= {"emailReceive"}

    def _session(self, account_id: str) -> dict:
        return {
            "capabilities": {CORE: {"maxObjectsInGet": 500, "maxObjectsInSet": 500}},
            "accounts": {account_id: {"name": self.objects["Account"][account_id]["name"]}},
            "primaryAccounts": {MANAGEMENT: account_id, MAIL: account_id},
            "apiUrl": f"{self.base_url}/jmap",
            "downloadUrl": f"{self.base_url}/jmap/download/{{accountId}}/{{blobId}}/{{name}}?accept={{type}}",
            "uploadUrl": f"{self.base_url}/jmap/upload/{{accountId}}/",
            "eventSourceUrl": f"{self.base_url}/jmap/eventsource",
            "state": self.session_state,
        }

    # --- JMAP -------------------------------------------------------------------------

    def _jmap(self, body: dict, account_id: str) -> dict:
        responses: list = []
        created_refs: dict[str, str] = {}
        for name, args, call_id in body.get("methodCalls", []):
            self.calls.append((name, args))
            match = re.fullmatch(r"x:(\w+)/(get|set|query)", name)
            try:
                if not match:
                    raise FakeError("unknownMethod", name)
                type, action = match.groups()
                args = self._resolve_result_refs(args, responses)
                result = getattr(self, f"_{action}")(type, args, account_id, created_refs)
                responses.append([name, result, call_id])
            except FakeError as e:
                responses.append(["error", {"type": e.type, "description": e.description}, call_id])
        return {"methodResponses": responses, "sessionState": self.session_state}

    def _resolve_result_refs(self, args: dict, responses: list) -> dict:
        resolved = {}
        for key, value in args.items():
            if key.startswith("#"):
                source = next((r for r in responses if r[2] == value["resultOf"]), None)
                if source is None:
                    raise FakeError("invalidResultReference", value["resultOf"])
                resolved[key[1:]] = source[1].get(value["path"].strip("/"))
            else:
                resolved[key] = value
        return resolved

    def _collection(self, type: str, account_id: str) -> dict[str, dict]:
        if type in ACCOUNT_SCOPED:
            return self.objects[f"{type}:{account_id}"]
        return self.objects[type]

    def _get(self, type: str, args: dict, account_id: str, refs: dict) -> dict:
        if type in SINGLETONS:
            objects = [{"id": "singleton", **self.singletons.get(type, {})}]
        else:
            collection = self._collection(type, account_id)
            ids = args.get("ids")
            objects = (
                list(collection.values()) if ids is None else [collection[i] for i in ids if i in collection]
            )
        if type == "Domain":
            # Stalwart renders the zone on every read, so keys added since creation show up.
            objects = [{**o, "dnsZoneFile": self._zone_file(o)} for o in objects]
        properties = args.get("properties")
        if properties:
            objects = [{"id": o["id"], **{p: o.get(p) for p in properties if p in o}} for o in objects]
        return {"accountId": account_id, "state": "1", "list": objects, "notFound": []}

    def _query(self, type: str, args: dict, account_id: str, refs: dict) -> dict:
        filter = args.get("filter") or {}
        unsupported = set(filter) - QUERY_FILTERS.get(type, set())
        if unsupported:
            raise FakeError("unsupportedFilter", f"{type} cannot filter on {sorted(unsupported)}")
        objects = [o for o in self._collection(type, account_id).values() if matches(o, filter)]
        for comparator in reversed(args.get("sort") or []):
            objects.sort(
                key=lambda o: o.get(comparator["property"]) or "",
                reverse=not comparator.get("isAscending", True),
            )
        objects = objects[int(args.get("position") or 0) :]
        if limit := args.get("limit"):
            objects = objects[:limit]
        return {"accountId": account_id, "ids": [o["id"] for o in objects], "total": len(objects)}

    def _set(self, type: str, args: dict, account_id: str, refs: dict) -> dict:
        result: dict = {"accountId": account_id, "created": {}, "updated": {}, "destroyed": []}
        if type in SINGLETONS:
            for id, patch_ in (args.get("update") or {}).items():
                try:
                    self.singletons[type] = apply_patch(self.singletons.get(type, {}), patch_)
                except FakeError as e:
                    result.setdefault("notUpdated", {})[id] = {"type": e.type, "description": e.description}
                    continue
                result["updated"][id] = None
                if type == "Bootstrap":
                    self.bootstrapped = True
            return result

        collection = self._collection(type, account_id)
        for ref, payload in (args.get("create") or {}).items():
            payload = resolve_creation_refs(payload, refs)
            try:
                self._check_references(payload)
                created = self._create(type, payload, collection, account_id)
            except FakeError as e:
                result.setdefault("notCreated", {})[ref] = {"type": e.type, "description": e.description}
                continue
            refs[ref] = created["id"]
            result["created"][ref] = created

        for id, patch_ in (args.get("update") or {}).items():
            if id not in collection:
                result.setdefault("notUpdated", {})[id] = {"type": "notFound"}
                continue
            try:
                patch_ = resolve_creation_refs(patch_, refs)
                self._check_references(patch_)
                was_manual = dkim_management_type(collection[id]) != "Automatic"
                collection[id] = apply_patch(collection[id], patch_)
            except FakeError as e:
                result.setdefault("notUpdated", {})[id] = {"type": e.type, "description": e.description}
                continue
            if type == "Domain" and was_manual and dkim_management_type(collection[id]) == "Automatic":
                # Stalwart generates keys when a domain comes under automatic management, not when
                # its signatures are deleted or its policy is re-sent (checked on v0.16.20).
                self._generate_dkim_keys(collection[id])
            self._server_set(type, collection[id])
            result["updated"][id] = None

        for id in args.get("destroy") or []:
            if id not in collection:
                result.setdefault("notDestroyed", {})[id] = {"type": "notFound"}
            elif type == "Domain" and any(s["domainId"] == id for s in self.all("DkimSignature")):
                result.setdefault("notDestroyed", {})[id] = {
                    "type": "forbidden",
                    "description": "Domain still has DKIM signatures.",
                }
            else:
                del collection[id]
                result["destroyed"].append(id)
        return result

    # --- object rules --------------------------------------------------------------------

    def _check_references(self, payload: dict) -> None:
        """Ids must point at existing objects, like the real server's invalidProperties checks."""

        for key, target in REFERENCES.items():
            value = payload.get(key)
            ids = list(value) if isinstance(value, dict) else ([value] if isinstance(value, str) else [])
            for ref in ids:
                if ref not in self.objects[target]:
                    raise FakeError("invalidProperties", f"{key} references unknown {target} {ref}")
        roles = payload.get("roles")
        if isinstance(roles, dict) and roles.get("@type") == "Custom":
            for ref in roles.get("roleIds") or {}:
                if ref not in self.objects["Role"]:
                    raise FakeError("invalidProperties", f"roleIds references unknown Role {ref}")

    def _create(self, type: str, payload: dict, collection: dict, account_id: str | None = None) -> dict:
        self._check_unique(type, payload)
        obj = {**payload, "id": self._new_id(type), "createdAt": "2026-01-01T00:00:00Z"}
        if type == "Domain":
            self._generate_dkim_keys(obj)
        self._server_set(type, obj)
        collection[obj["id"]] = obj
        if type == "ApiKey" and account_id:
            self.tokens[obj["secret"]] = account_id  # a minted key authenticates like any token
        return json.loads(json.dumps(obj))

    def _generate_dkim_keys(self, domain: dict) -> None:
        """Automatic DKIM management: one key per algorithm, named by the selector template, both
        kinds when the policy names none. Every key gets a public key of its own."""

        management = domain.get("dkimManagement")
        for algorithm in dkim_key_types(management):
            signature_id = self._add(
                "DkimSignature",
                {
                    "domainId": domain["id"],
                    "selector": dkim_selector(management, algorithm),
                    "stage": "active",
                },
            )
            self.objects["DkimSignature"][signature_id]["publicKey"] = f"MIIBIjANBg{signature_id}"

    def _check_unique(self, type: str, payload: dict) -> None:
        if type == "Domain" and self.find("Domain", name=payload.get("name")):
            raise FakeError("alreadyExists", f"Domain {payload.get('name')} already exists.")
        if type in ADDRESS_TYPES:
            for other in ADDRESS_TYPES:
                if self.find(other, name=payload.get("name"), domainId=payload.get("domainId")):
                    raise FakeError("alreadyExists", f"Address {payload.get('name')} already exists.")

    def _server_set(self, type: str, obj: dict) -> None:
        if type in ADDRESS_TYPES and obj.get("domainId"):
            domain = self.get("Domain", obj["domainId"])
            if domain:
                obj["emailAddress"] = f"{obj['name']}@{domain['name']}"
        if type == "Domain":
            obj["dnsZoneFile"] = self._zone_file(obj)
        if type in ACCOUNT_SCOPED and "secret" not in obj:
            obj["secret"] = f"{type.lower()}-{secrets.token_hex(8)}"

    def _zone_file(self, domain: dict) -> str:
        name = domain["name"]
        settings = self.singletons.get("SystemSettings", {})
        exchangers = settings.get("mailExchangers") or {
            "0": {"hostname": settings.get("defaultHostname") or "mail.test"}
        }
        first = exchangers[sorted(exchangers, key=int)[0]] if isinstance(exchangers, dict) else exchangers[0]
        mx = first["hostname"].rstrip(".")
        lines = [f"{name}. 3600 IN MX 10 {mx}."]
        lines.append(f'{name}. 3600 IN TXT "v=spf1 mx ra=postmaster -all"')
        for signature in self.all("DkimSignature"):
            if signature["domainId"] == domain["id"]:
                owner = f"{signature['selector']}._domainkey.{name}."
                key = signature.get("publicKey") or "MIIBIjANBg"
                if "ed25519" in signature["selector"]:
                    lines.append(f'{owner} 3600 IN TXT "v=DKIM1; k=ed25519; p={key}"')
                else:
                    # Stalwart splits the long RSA key over a parenthesised group of quoted chunks.
                    lines.append(f"{owner} 3600 IN TXT (")
                    lines.append(f'    "v=DKIM1; k=rsa; p={key}"')
                    lines.append('    "kqhkiG9w0BAQEFAAOCAQ8A"')
                    lines.append(")")
        lines.append(f'_dmarc.{name}. 3600 IN TXT "v=DMARC1; p=reject; rua=mailto:postmaster@{name}"')
        lines.append(f'_smtp._tls.{name}. 3600 IN TXT "v=TLSRPTv1; rua=mailto:postmaster@{name}"')
        lines.append(f'{name}. 3600 IN CAA 0 issue "letsencrypt.org"')
        lines.append(f"mta-sts.{name}. 3600 IN CNAME {mx}.")
        lines.append(f'_mta-sts.{name}. 3600 IN TXT "v=STSv1; id=1"')
        lines.append(f"autoconfig.{name}. 3600 IN CNAME {mx}.")
        lines.append(f"autodiscover.{name}. 3600 IN CNAME {mx}.")
        lines.append(f"_imaps._tcp.{name}. 3600 IN SRV 0 1 993 {mx}.")
        lines.append(f"_submissions._tcp.{name}. 3600 IN SRV 0 1 465 {mx}.")
        return "\n".join(lines) + "\n"

    def _add(self, type: str, payload: dict) -> str:
        obj = {**payload, "id": self._new_id(type)}
        self.objects[type][obj["id"]] = obj
        return obj["id"]

    def _new_id(self, type: str) -> str:
        self._counter += 1
        return f"{type[:3].lower()}{self._counter}"


class FakeAdapter(BaseAdapter):
    def __init__(self, server: FakeStalwart) -> None:
        super().__init__()
        self.server = server

    def send(self, request, **kwargs):
        response = self.server.handle(request)
        response.request = request
        return response

    def close(self) -> None:
        pass


def dkim_management_type(domain: dict) -> str:
    return (domain.get("dkimManagement") or {}).get("@type") or "Automatic"


def dkim_selector(management: dict | None, algorithm: str) -> str:
    """The selector template rendered the way Stalwart does for a key generated on 2026-01-01."""

    template = (management or {}).get("selectorTemplate") or "v{version}-{algorithm}-{date-%Y%m%d}"
    rendered = template.replace("{version}", "1").replace("{algorithm}", algorithm)
    return re.sub(r"\{date-[^}]*\}", "20260101", rendered)


def dkim_key_types(management: dict | None) -> list[str]:
    """Key types Stalwart generates for a domain: none under Manual management, otherwise the
    algorithms named, or both when the payload names none (Stalwart's default)."""

    management = management or {}
    if management.get("@type") == "Manual":
        return []
    algorithms = management.get("algorithms") or {"Dkim1Ed25519Sha256": True, "Dkim1RsaSha256": True}
    return ["ed25519" if "Ed25519" in a else "rsa" for a in algorithms]


def json_response(status: int, payload: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    response.headers["Content-Type"] = "application/json"
    response.encoding = "utf-8"
    return response


def matches(obj: dict, filter: dict) -> bool:
    for key, value in filter.items():
        if key == "text":
            haystack = " ".join(str(obj.get(k) or "") for k in ("name", "description", "emailAddress"))
            if str(value).lower() not in haystack.lower():
                return False
        elif key == "domain" and "report" in obj:
            if (obj["report"].get("policyDomain") or "").lower() != str(value).lower():
                return False
        elif isinstance(obj.get(key), dict):
            if value not in obj[key]:
                return False
        elif obj.get(key) != value:
            return False
    return True


def resolve_creation_refs(value, refs: dict):
    if isinstance(value, str) and value.startswith("#") and value[1:] in refs:
        return refs[value[1:]]
    if isinstance(value, dict):
        return {resolve_creation_refs(k, refs): resolve_creation_refs(v, refs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_creation_refs(v, refs) for v in value]
    return value


def apply_patch(obj: dict, patch_: dict) -> dict:
    """JMAP patch semantics: plain keys replace, ``a/b/c`` pointers set or (with None) delete."""

    obj = json.loads(json.dumps(obj))
    for key, value in patch_.items():
        if "/" not in key:
            if value is None:
                obj.pop(key, None)
            else:
                obj[key] = value
            continue

        *path, leaf = key.split("/")
        target = obj
        for part in path:
            if not isinstance(target, dict) or part not in target:
                raise FakeError("invalidPatch", f"{key}: parent does not exist")  # RFC 8620 5.3
            target = target[part]
        if value is None:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return obj
