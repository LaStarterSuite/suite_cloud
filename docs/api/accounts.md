# Accounts

Module `suite_cloud.api.mail.accounts`, plus `mail.meta.get_account_options`. The conventions are in the [overview](README.md).

| Method | Verb | Purpose |
| --- | --- | --- |
| `mail.accounts.list_accounts` | GET | A page of accounts |
| `mail.accounts.get_account` | GET | One account with its disk usage |
| `mail.accounts.get_quotas` | GET | Disk quota and usage for up to 500 addresses at once |
| `mail.accounts.get_send_usage` | GET | Sent and refused recipients per day and month |
| `mail.accounts.create_account` | POST | Creates a mailbox and returns its app password once |
| `mail.accounts.update_account` | PATCH | Changes the name, description, quotas, send limits, locale, time zone or receiving |
| `mail.accounts.set_account_enabled` | PUT | Enables or disables login and delivery |
| `mail.accounts.set_password` | PUT | Sets the account's own password |
| `mail.accounts.rotate_app_password` | POST | Replaces the tenant's own app password |
| `mail.accounts.list_app_passwords` | GET | The account's app passwords, without secrets |
| `mail.accounts.create_app_password` | POST | Creates an app password, returned once |
| `mail.accounts.update_app_password` | PATCH | Changes the description, expiry or allowed IPs of one |
| `mail.accounts.delete_app_password` | DELETE | Revokes one app password |
| `mail.accounts.list_api_keys` | GET | The account's API keys, without secrets |
| `mail.accounts.create_api_key` | POST | Creates an API key, returned once |
| `mail.accounts.update_api_key` | PATCH | Changes the description, expiry or allowed IPs of one |
| `mail.accounts.delete_api_key` | DELETE | Revokes one API key |
| `mail.accounts.set_aliases` | PUT | Replaces the alias set |
| `mail.accounts.add_alias` | POST | Adds one alias |
| `mail.accounts.remove_alias` | DELETE | Removes one alias |
| `mail.accounts.set_alias_enabled` | PUT | Turns one alias on or off |
| `mail.accounts.set_groups` | PUT | Replaces the account's group memberships |
| `mail.accounts.delete_account` | DELETE | Removes the account and its mail |
| `mail.meta.get_account_options` | GET | The locales, time zones and optional quota names the cluster accepts |

## The account object

```json
{
  "email": "jane@acme.example",
  "domain": "acme.example",
  "enabled": true,
  "disable_receiving": false,
  "display_name": "Jane Doe",
  "description": null,
  "disk_quota_gb": 5,
  "quotas": { "maxDiskQuota": 5368709120, "maxEmails": 100000 },
  "used_disk_bytes": 734003200,
  "daily_send_limit": 500,
  "monthly_send_limit": 10000,
  "send_usage": { "day": 120, "month": 2340, "refused_day": 0, "refused_month": 3 },
  "locale": "en-US",
  "time_zone": "Europe/Berlin",
  "aliases": [ { "email": "j.doe@acme.example", "enabled": true, "description": null } ],
  "groups": ["sales@acme.example"],
  "mailing_lists": ["all@acme.example"],
  "created_at": "2026-02-01T09:00:00+00:00"
}
```

`used_disk_bytes` comes from the cluster. `get_account` asks for it on every call; a `list_accounts` page fetches it for the whole page in one cluster call; `create_account` and the write methods answer `null` for it. `mailing_lists` names the lists that deliver to any of the account's addresses, primary or alias.

### Quotas

An account's disk quota is whatever the tenant sets: `disk_quota_gb` of 0, or none, means no limit. Suite Cloud applies no default and does not check the quotas against the tenant's `max_disk_gb`.

`quotas` is an object of Stalwart `StorageQuota` names to limits. `maxDiskQuota` (bytes) is the disk quota; the others are counts (messages, mailboxes, Sieve scripts, calendars, contact cards, app passwords and so on). `get_account_options` lists the other names. On a create or update, `quotas` replaces the other rows (`{}` lifts them all) and keeps the disk row unless it names `maxDiskQuota`; `disk_quota_gb` sets the disk row in GB and wins over a `maxDiskQuota` inside `quotas`. A limit of 0 lifts it. A new account gets `maxAppPasswords` and `maxApiKeys` of 10 unless `quotas` names them.

### Send limits

`daily_send_limit` and `monthly_send_limit` cap the recipients the account may send to per UTC day and month; a message to 50 recipients counts as 50. Every account has both limits, each at least 1 and at most its cluster's max. Over a limit, the mail server refuses each recipient that does not fit with `550 5.4.5` and sends the message to the rest. A change applies to the next recipient. `send_usage` is what the account sent today and this month, in recipients, and how many recipients were refused over a limit (`refused_day`, `refused_month`), up to a minute old. `create_account` and `update_account` take both limits. A new account starts at the tenant's default limits, which the [tenant object](tenant.md) shows, unless `create_account` sets them; `update_account` leaves out what it is not given. A tenant passes a limit only when an operator allowed it, `custom_send_limits` in the tenant object; otherwise the call is a 403.

### Aliases

An alias is an extra address that delivers to the account. The input to `create_account` and `set_aliases` is a JSON array of addresses, or of objects `{"email", "enabled", "description"}` with `enabled` defaulting to true. At most 100 aliases per object. An address may be a primary address or an alias exactly once across the whole directory; a taken one is a 409.

`set_aliases` replaces the whole set. `add_alias`, `remove_alias` and `set_alias_enabled` change one alias under a row lock, so two admins editing the same account never drop each other's rows. Adding an alias that is already there, or removing one that is not, changes nothing and returns the account.

## `list_accounts`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `domain` | string | | Only accounts on this domain |
| `search` | string | | Matches the address or display name |
| `start` | integer | 0 | Offset |
| `limit` | integer | 50 | Page length, at most 200 |

Answers `{"items": [account, …], "total": n}` ordered by address.

## `get_account`

Takes `email`. Returns the account object with its usage.

## `get_quotas`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `emails` | array of strings, required | Up to 500 addresses, as a JSON body on a QUERY, or as the array's JSON text on a GET short enough for a request line |

Answers `{"<email>": {"disk_quota_gb": 5, "used_disk_bytes": 734003200}, …}` for the tenant's accounts among them; a `disk_quota_gb` of 0 means no limit. Addresses that are not the tenant's accounts are left out. The quotas are one query and the usage one cluster call per batch, so a list page can show both for every row.

## `get_send_usage`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `email` | string, required | | The account |
| `days` | integer | 30 | UTC days back from today, at most 400 |

Answers the account's history, newest first, as far back as Suite Cloud keeps it (the operator's Sending Usage Retention, 90 days by default):

```json
{
  "days": [{ "date": "2026-10-27", "sent": 120, "refused": 0, "limit": 500 }],
  "months": [{ "month": "2026-10", "sent": 2340, "refused": 3, "limit": 10000 }]
}
```

`sent` counts recipients and `refused` the recipients refused over a limit. `limit` is the daily or monthly limit the account had while the period was current; 0 means no limit. `months` holds the months the days fall in. A day with nothing sent is left out.

## `create_account`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `email` | string, required | | On an active domain of the tenant |
| `password` | string, required | | At least 8 characters; never stored by Suite Cloud |
| `display_name` | string | | |
| `description` | string | | |
| `aliases` | array | | See aliases |
| `groups` | array of strings | | Group addresses of the tenant, at most 500 |
| `mailing_lists` | array of strings | | List addresses of the tenant the account joins as a recipient, at most 500 |
| `disk_quota_gb` | number | no limit | See quotas |
| `quotas` | object | | See quotas |
| `locale` | string | `en-US` | One of `get_account_options` |
| `time_zone` | string | | One of `get_account_options` |
| `disable_receiving` | boolean | `false` | Send-only: the account logs in and sends, and mail addressed to it bounces |
| `daily_send_limit`, `monthly_send_limit` | integer | the tenant's defaults | Recipients per UTC day and month, at most the cluster's max. Needs `custom_send_limits`. See send limits |

Answers 201 with the account object plus `app_password`, the password the tenant uses for the account's mail access. It is returned only here; `rotate_app_password` issues a fresh one. Suite Cloud stores neither the app password nor the account's own password. An inactive domain, a reached limit or a send limit over the cluster's max is a 422; a send limit the tenant may not set is a 403; a taken address is a 409; an unknown group or list is a 404.

## `update_account`

Takes `email` and any of `display_name`, `description`, `disk_quota_gb`, `quotas`, `daily_send_limit`, `monthly_send_limit`, `locale`, `time_zone` and `disable_receiving`. Only the fields passed change. A send limit needs `custom_send_limits`. Returns the account object.

## `set_account_enabled`

Takes `email` and `enabled`. A disabled account cannot log in; mail addressed to it is still delivered unless `disable_receiving` is set. Its data stays. Returns the account object.

## `set_password`

Takes `email` and `password` (at least 8 characters). Answers 200 with `{"data": null}`.

## App passwords and API keys

An app password is a login for the account's mail clients; an API key is a Bearer token for its JMAP access. The cluster holds both; Suite Cloud keeps their descriptions and never a secret. The description `Suite Cloud` is reserved: the app password with it is the tenant's own login, which `create_account` returns once and `rotate_app_password` replaces, and an API key with it exists only when a Suite Cloud operator mints one. Creating or renaming a credential to `Suite Cloud` is a 422. The account's `maxAppPasswords` and `maxApiKeys` quotas, 10 each unless `quotas` set another value, count the tenant's login too; one more is a 422.

```json
{
  "id": "b",
  "description": "Phone",
  "expires_at": "2027-01-01T00:00:00Z",
  "allowed_ips": ["203.0.113.0/24"],
  "created_at": "2026-10-15T09:00:00Z"
}
```

The methods come in two sets, `*_app_password` and `*_api_key`, with the same parameters and answers.

| Method | Parameters | Answer |
| --- | --- | --- |
| `list_app_passwords`, `list_api_keys` | `email` | `{"items": [...]}` as the cluster holds them, the tenant's login first |
| `create_app_password`, `create_api_key` | `email`, `description` (app passwords default to `Suite`), `expires_at` (ISO 8601, optional), `allowed_ips` (JSON array of addresses or networks, optional) | 201 with the object plus `secret`, returned once |
| `update_app_password`, `update_api_key` | `email`, `id` and any of `description`, `expires_at`, `allowed_ips`; an empty value clears it | The object |
| `delete_app_password`, `delete_api_key` | `email`, `id` | 204; the `Suite Cloud` app password is a 422 |
| `rotate_app_password` | `email` | `{"app_password": "…"}`; the previous login stops working |

An unknown `id` is a 404.

## `set_aliases`, `add_alias`, `remove_alias`, `set_alias_enabled`

| Method | Parameters |
| --- | --- |
| `set_aliases` | `email`, `aliases` (array; empty or omitted removes every alias) |
| `add_alias` | `email`, `alias`, `description` |
| `remove_alias` | `email`, `alias`; the primary address cannot be removed (422) |
| `set_alias_enabled` | `email`, `alias`, `enabled`; an alias that is not there is a 404 |

Each returns the account object.

## `set_groups`

Takes `email` and `groups`, an array of the tenant's group addresses (at most 500; empty or omitted leaves every group). Returns the account object.

## `delete_account`

Takes `email`. Answers 204. The mailbox and its mail are removed from the cluster.

## `get_account_options`

Takes no parameters. Answers the values the cluster accepts, cached for an hour:

```json
{
  "locales": [ { "value": "en-US", "label": "English (United States)" } ],
  "time_zones": [ { "value": "Europe/Berlin", "label": "Europe/Berlin" } ],
  "quotas": [ { "value": "maxEmails", "label": "Maximum number of emails" } ]
}
```
