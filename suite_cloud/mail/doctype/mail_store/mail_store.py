# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

# Which backend variants, as Stalwart names them, may back each store kind. Embedded backends
# (RocksDb, FileSystem) only work for a single node; the cluster validates that.
KIND_TYPES = {
    "Data": ("PostgreSql", "MySql", "RocksDb"),
    "Blob": ("Default", "S3", "FileSystem", "PostgreSql", "MySql"),
    "Search": ("Default", "ElasticSearch", "Meilisearch", "PostgreSql", "MySql"),
    "In-Memory": ("Default", "Redis", "RedisCluster", "RedisSentinel"),
}
EMBEDDED_TYPES = ("RocksDb", "FileSystem")
# The cluster fields that link a store: Stalwart's four and the outbound scanner's.
STORE_FIELDS = ("data_store", "blob_store", "search_store", "in_memory_store", "outbound_scan_store")
# The AWS regions Stalwart names (S3StoreRegion) and so knows the endpoint of.
S3_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "af-south-1",
    "ap-east-1",
    "ap-south-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-southeast-1",
    "ap-southeast-2",
    "cn-north-1",
    "cn-northwest-1",
    "eu-north-1",
    "eu-central-1",
    "eu-central-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "il-central-1",
    "me-south-1",
    "sa-east-1",
)


class MailStore(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        access_key: DF.Data | None
        allow_invalid_certs: DF.Check
        auth_secret: DF.Password | None
        auth_username: DF.Data | None
        blob_size: DF.Int
        bucket: DF.Data | None
        buffer_size: DF.Int
        database: DF.Data | None
        depth: DF.Int
        endpoint: DF.Data | None
        fail_on_timeout: DF.Check
        host: DF.Data | None
        http_auth_type: DF.Literal["Unauthenticated", "Basic", "Bearer"]
        http_bearer_token: DF.Password | None
        http_secret: DF.Password | None
        http_username: DF.Data | None
        include_source: DF.Check
        key_prefix: DF.Data | None
        kind: DF.Literal["Data", "Blob", "Search", "In-Memory"]
        max_allowed_packet: DF.Int
        max_retries: DF.Int
        num_replicas: DF.Int
        num_shards: DF.Int
        options: DF.Data | None
        path: DF.Data | None
        poll_interval: DF.Float
        pool_max_connections: DF.Int
        pool_min_connections: DF.Int
        pool_recycling_method: DF.Literal["fast", "verified", "clean"]
        pool_timeout_create: DF.Float
        pool_timeout_recycle: DF.Float
        pool_timeout_wait: DF.Float
        pool_workers: DF.Int
        port: DF.Int
        protocol_version: DF.Literal["resp2", "resp3"]
        read_from_replicas: DF.Check
        region: DF.Data | None
        secret_key: DF.Password | None
        sentinel_secret: DF.Password | None
        sentinel_username: DF.Data | None
        service_name: DF.Data | None
        timeout: DF.Float
        title: DF.Data
        type: DF.Literal[
            "",
            "Default",
            "PostgreSql",
            "MySql",
            "RocksDb",
            "S3",
            "FileSystem",
            "ElasticSearch",
            "Meilisearch",
            "Redis",
            "RedisCluster",
            "RedisSentinel",
        ]
        url: DF.Data | None
        urls: DF.SmallText | None
        use_tls: DF.Check
        verify_after_write: DF.Check
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    @property
    def is_embedded(self) -> bool:
        return self.type in EMBEDDED_TYPES

    def validate(self) -> None:
        if self.type not in KIND_TYPES.get(self.kind, ()):
            frappe.throw(
                _("{0} cannot be used as a {1} store. Allowed: {2}").format(
                    self.type, self.kind, ", ".join(KIND_TYPES.get(self.kind, ()))
                )
            )
        if self.type in ("PostgreSql", "MySql") and not self.port:
            self.port = 5432 if self.type == "PostgreSql" else 3306
        if self.type == "S3":
            self.validate_s3_region()

    def validate_s3_region(self) -> None:
        """A region Stalwart does not name can only be reached through an endpoint."""

        if not self.region:
            frappe.throw(_("Region is required for an S3 store."))
        if not self.endpoint and self.region not in S3_REGIONS:
            frappe.throw(
                _(
                    "Stalwart has no built-in endpoint for region {0}. Set the Endpoint, e.g. {1} for AWS."
                ).format(self.region, f"https://s3.{self.region}.amazonaws.com")
            )

    def on_trash(self) -> None:
        for field in STORE_FIELDS:
            if cluster := frappe.db.exists("Stalwart Cluster", {field: self.name}):
                frappe.throw(_("Mail Store is used by cluster {0}.").format(cluster))
        if frappe.db.exists("Egress Gateway", {"data_store": self.name}):
            frappe.throw(_("Mail Store is used by an egress gateway."))

    @property
    def config(self) -> dict:
        """The Stalwart store object (DataStore/BlobStore/SearchStore/InMemoryStore variant)."""

        builder = getattr(self, f"_config_{self.type.lower()}", None)
        payload = {"@type": self.type}
        if builder:
            payload.update(builder())
        return {k: v for k, v in payload.items() if v is not None}

    # --- variants -------------------------------------------------------------

    def _database(self) -> dict:
        return {
            "host": self.host,
            "port": cint(self.port),
            "database": self.database or "stalwart",
            "authUsername": self.auth_username or "stalwart",
            "authSecret": secret(self, "auth_secret"),
            "timeout": ms(self.timeout, 15),
            "useTls": bool(self.use_tls),
            "allowInvalidCerts": bool(self.allow_invalid_certs),
            "poolMaxConnections": cint(self.pool_max_connections) or 10,
        }

    def _config_postgresql(self) -> dict:
        return {
            **self._database(),
            "poolRecyclingMethod": self.pool_recycling_method or "fast",
            "options": self.options or None,
        }

    def _config_mysql(self) -> dict:
        return {
            **self._database(),
            "poolMinConnections": cint(self.pool_min_connections) or 5,
            "maxAllowedPacket": cint(self.max_allowed_packet) or None,
        }

    def _config_rocksdb(self) -> dict:
        return {
            "path": self.path,
            "blobSize": cint(self.blob_size) or 16834,
            "bufferSize": cint(self.buffer_size) or 134217728,
            "poolWorkers": cint(self.pool_workers) or None,
        }

    def _config_filesystem(self) -> dict:
        return {"path": self.path, "depth": cint(self.depth) or 2}

    def _config_s3(self) -> dict:
        return {
            "region": self._s3_region(),
            "bucket": self.bucket,
            # Stalwart's PublicString union: shaped like a secret, but shown in the clear.
            "accessKey": {"@type": "Value", "value": self.access_key} if self.access_key else None,
            "secretKey": secret(self, "secret_key"),
            "keyPrefix": self.key_prefix or None,
            "timeout": ms(self.timeout, 30),
            "maxRetries": cint(self.max_retries) or 3,
            "verifyAfterWrite": bool(self.verify_after_write),
        }

    def _s3_region(self) -> dict:
        """Stalwart's S3StoreRegion union: a region it names, or any region with its endpoint.

        The endpoint has no field of its own on the store; S3-compatible services are a Custom region.
        """

        self.validate_s3_region()  # stores saved before the check existed
        if self.endpoint:
            return {"@type": "Custom", "customEndpoint": self.endpoint, "customRegion": self.region}
        return {"@type": "".join(part.capitalize() for part in self.region.split("-"))}

    def _http_auth(self) -> dict:
        if self.http_auth_type == "Basic":
            return {"@type": "Basic", "username": self.http_username, "secret": secret(self, "http_secret")}
        if self.http_auth_type == "Bearer":
            return {"@type": "Bearer", "bearerToken": secret(self, "http_bearer_token")}
        return {"@type": "Unauthenticated"}

    def _config_elasticsearch(self) -> dict:
        return {
            "url": self.url,
            "numReplicas": cint(self.num_replicas),
            "numShards": cint(self.num_shards) or 3,
            "includeSource": bool(self.include_source),
            "timeout": ms(self.timeout, 30),
            "allowInvalidCerts": bool(self.allow_invalid_certs),
            "httpAuth": self._http_auth(),
        }

    def _config_meilisearch(self) -> dict:
        return {
            "url": self.url,
            "pollInterval": ms(self.poll_interval, 0.5),
            "maxRetries": cint(self.max_retries) or 120,
            "failOnTimeout": bool(self.fail_on_timeout),
            "timeout": ms(self.timeout, 30),
            "allowInvalidCerts": bool(self.allow_invalid_certs),
            "httpAuth": self._http_auth(),
        }

    def _pool(self) -> dict:
        return {
            "timeout": ms(self.timeout, 10),
            "poolMaxConnections": cint(self.pool_max_connections) or 10,
            "poolTimeoutCreate": ms(self.pool_timeout_create, 30),
            "poolTimeoutWait": ms(self.pool_timeout_wait, 30),
            "poolTimeoutRecycle": ms(self.pool_timeout_recycle, 30),
        }

    def _config_redis(self) -> dict:
        return {"url": self.url, **self._pool()}

    def _config_rediscluster(self) -> dict:
        return {
            "urls": {u: True for u in lines(self.urls)},
            "authUsername": self.auth_username or None,
            "authSecret": secret(self, "auth_secret"),
            "readFromReplicas": bool(self.read_from_replicas),
            "protocolVersion": self.protocol_version or "resp2",
            **self._pool(),
        }

    def _config_redissentinel(self) -> dict:
        return {
            "urls": {u: True for u in lines(self.urls)},
            "serviceName": self.service_name or "mymaster",
            "authUsername": self.auth_username or None,
            "authSecret": secret(self, "auth_secret"),
            "sentinelUsername": self.sentinel_username or None,
            "sentinelSecret": secret(self, "sentinel_secret"),
            "protocolVersion": self.protocol_version or "resp2",
            **self._pool(),
        }


def secret(doc: Document, field: str) -> dict | None:
    """Stalwart's SecretKey union: a literal value (env/file variants are not used here)."""

    if not doc.get(field):
        return None
    return {"@type": "Value", "secret": doc.get_password(field)}


def ms(seconds: float | None, default: float) -> int:
    return cint(flt(seconds if seconds else default) * 1000)


def lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
