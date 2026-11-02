# Suite Cloud API

Suite Cloud exposes one REST API over Frappe's v2 method endpoint. The **tenant API** lets a Suite Tenant manage its own slice of the mail directory: domains, accounts, groups, mailing lists and the DMARC and TLS reports of its domains.

| Page | Covers |
| --- | --- |
| [Tenant](tenant.md) | `ping`, `update_tenant_profile`, `get_disk_usage` |
| [Email](email.md) | `send_email`: Suite's emails, such as invites and password resets, from Suite Cloud's templates |
| [Domains](domains.md) | Adding a domain, its DNS records and verification |
| [Accounts](accounts.md) | Mailboxes, passwords, quotas, aliases, group membership, account options |
| [Groups](groups.md) | Groups, their members and aliases |
| [Mailing lists](mailing-lists.md) | Lists, their aliases and recipients |
| [Reports](reports.md) | DMARC and TLS reports and summaries |
| [Signup](signup.md) | The public signup that creates a tenant and its hosted site |
| [Telemetry](telemetry.md) | `telemetry.webhook`, `telemetry.otlp`: the events and metrics Stalwart pushes, for the Insights dashboards |

## Base URL

Every method lives at `https://<suite-cloud>/api/v2/method/suite_cloud.api.<module>.<method>`. The pages name each method by its module path, for example `mail.accounts.create_account`.

## Authentication

A tenant authenticates with its id, the name of its Suite Tenant record, and the secret an operator issued with **Rotate Secret**. It sends one header on every request:

```http
Authorization: Tenant <id>:<secret>
```

Suite Cloud checks the secret against the Suite Tenant record, so the request runs as the tenant's owner and every method acts only on that tenant's documents. The credentials are accepted only on the tenant API's own URLs; on any other URL they are refused with a 401, so the owner's own permissions are never reachable with the secret. Anything that belongs to another tenant is reported as not found (404), never as forbidden.

When the Suite Tenant lists allowed IP addresses, only requests from those addresses may use its secret. A request from elsewhere gets a 403 with the type `TenantAddressError`, and Suite Cloud logs it for the operators. A secret copied out of a tenant's configuration is worthless anywhere else.

An operator with the System Manager role may call the tenant API from the desk or a script on behalf of a tenant. The tenant is named by the `tenant` parameter or the `X-Suite-Tenant` header.

## Verbs

The HTTP verb follows the method name. A request with another verb gets a 403.

| Name starts with | Verb | Meaning |
| --- | --- | --- |
| `list_`, `get_`, `check_`, `ping` | GET | Reads; nothing changes |
| `create_` and other actions | POST | Creates an object or runs an action |
| `update_` | PATCH | Changes the fields passed; omitted fields stay as they are |
| `set_` | PUT | Replaces one value or one whole set |
| `delete_`, `remove_` | DELETE | Removes an object, an alias or recipients |

## Parameters

Parameters travel as JSON. A write sends a JSON body with `Content-Type: application/json`. A GET sends its parameters in the query string; a list or object among them is the JSON text of that value.

```http
GET /api/v2/method/suite_cloud.api.mail.accounts.get_quotas?emails=["a@example.com","b@example.com"]
```

A list parameter is a JSON array in both places. Comma or newline separated text is refused with a 400. A read whose parameters hold a list or an object may also be sent as a QUERY request with a JSON body, as a write is: a long list, such as 500 addresses for `get_quotas`, does not fit in a request line.

Booleans accept `true`, `false`, `1` and `0`. Email addresses and domain names are lowercased, and the domain part is IDNA encoded, before they are matched or stored.

## Responses

A successful call answers `{"data": <result>}`. A create answers 201, a delete 204 with no body, everything else 200.

Listings answer `{"items": [...], "total": <count of everything that matches>}`. They take `start` (offset, default 0) and `limit` (page length, capped per listing); `search` matches the address and the display name or description.

Timestamps are ISO 8601 in UTC, for example `2026-03-04T05:06:07+00:00`.

## Errors

An error answers `{"errors": [{"type": "<exception>", "message": "<text>"}]}` with the HTTP status below. The entry carries nothing else, except the traceback as `exception` on a developer bench. The type tells a client what happened. The message is a plain sentence for the admin who made the change: it names the object in the words of this reference (domain, account, group, mailing list, alias) and says what to do next.

| Status | Type | Meaning |
| --- | --- | --- |
| 400 | `InvalidInputError` | A required parameter left out, or a value the API could not parse: a list that is not a JSON array, a date that is not a date, a list over its cap |
| 401 | `AuthenticationError` | The id or secret is wrong, or the tenant is disabled; the request is rejected before the tenant API runs |
| 401 | `TenantAuthError` | The caller is logged in but is no tenant: a session without credentials, or an operator naming an unknown tenant |
| 403 | `PermissionError` | No credentials at all, a verb that does not match the method, or a caller without the role |
| 403 | `TenantSuspendedError` | The tenant is suspended or archived |
| 403 | `TenantAddressError` | The request did not come from one of the tenant's allowed addresses |
| 404 | `DoesNotExistError` | The object does not exist, or belongs to another tenant |
| 409 | `DuplicateEntryError` | The address or domain is already taken |
| 422 | `ValidationError` | Suite Cloud refused the change: a limit, a rule, an inactive domain |
| 422 | `MailNotConfiguredError` | The tenant is not set up for Mail yet: it has no Stalwart cluster, so there is nothing to act on |
| 422 | `ClusterRejectedError` | The mail server refused the change; the message carries its reason |
| 429 | `TooManyRequestsError` | The tenant made more than 300 requests in the current minute |
| 502 | `ClusterFailedError` | The mail server failed in a way the caller cannot fix; Suite Cloud logged it for the operators |
| 502 | `ClusterMisconfiguredError` | Suite Cloud's own credentials for the mail server were rejected; an operator problem |
| 503 | `ClusterUnavailableError` | The mail server could not be reached; the same request can be sent again in a few minutes |

## Rate limit

Each tenant may make 300 requests per minute, counted in fixed one-minute windows. Refusals count too. Failed authentication attempts are paced per source address the same way.
