# Suite Cloud

Suite Cloud is the app that runs the mail servers behind [Frappe Suite](https://github.com/frappe/suite).
It sets up [Stalwart](https://stalw.art) mail clusters, keeps them configured, and lets many Suite
tenants share one cluster safely.

## How it works, in short

- **A Suite Tenant** records each Suite tenant: its limits, the key and secret it authenticates
  with, and, once it is set up for Mail, the Stalwart cluster it uses.
- **A Suite tenant** never talks to the mail server's admin API itself. When it wants to add a
  domain, create a mailbox, change a password and so on, it asks Suite Cloud. Suite Cloud checks
  that the tenant owns what it is touching, saves the change in its own records, and pushes it to
  the cluster in the same step.
- **End users** (the people reading and sending mail) connect straight to the cluster with their
  own app passwords. Suite Cloud is not in that path.
- **Operators** use the desk to create clusters, add servers, manage DNS and watch jobs.

```
                             Suite Cloud ◀────────────── Suite tenant (admin changes)
                                  │
                                  │ pushes configuration and directory changes
                                  ▼
                           Stalwart cluster ◀──────────── end users (mail clients)
                                  ▲
Suite Cloud ── SSH + Ansible ─────┘ (installs and updates the servers)
Suite Cloud ── DNS provider API ──▶ publishes records in each DNS Zone
Suite Cloud ── public resolvers ──▶ checks the records a customer domain must carry
```

The app has three modules, so one CODEOWNERS line can cover each product:

| Module | Folder | Holds |
| --- | --- | --- |
| **Suite Cloud** | `suite_cloud/suite_cloud` | What every hosted product shares: DNS Zone, DNS Record, Server Job, Suite Tenant, Suite Cloud Settings, plus `provisioning` (Ansible over SSH) and `dns` (provider client, resolver). |
| **Mail** | `suite_cloud/mail` | The mail product: Stalwart Store, Cluster, Node, Egress Gateway and IP Pool, the mail directory (Mail Domain, Mail Account, Mail Group, Mailing List), and the `stalwart`, `cluster` and `tenancy` packages. |
| **Telemetry** | `suite_cloud/telemetry` | The events and metrics Stalwart pushes: the rows made from its WebHook and OTLP requests, the spool that stages a batch as parquet, the `Warehouse` that loads the files into Frappe Insights' DuckDB and prunes them, the Insights dashboards for Stalwart, and the `agent` package that builds the Suite Cloud Agent (the Go program in `agent/`) and installs it with the telemetry tokens. |
| **Hosting** | `suite_cloud/hosting` | Suite sites on Frappe Cloud: Region, Virtual Machine, Suite Site, Suite Tenant Request, the `central` clients (Central, Pilot, the site's REST API) and the `machines`, `sites`, `domains` and `capacity` steps. |

The tenant-facing API is split the same way: `suite_cloud.api.tenant` for what any product needs
(authentication, `ping`, the tenant profile) and `suite_cloud.api.mail` for the mail directory.
`suite_cloud.api.telemetry` is for Stalwart and the agents on the nodes, not for tenants.

## The pieces

| Record | What it is |
| --- | --- |
| **DNS Zone** | A domain you own (for example `frappemail.com`) plus the login for the DNS provider that manages it. Suite Cloud publishes server records there. You can have several zones; each cluster picks one. |
| **Stalwart Store** | Where a cluster keeps its data: a PostgreSQL or MySQL database, an S3 bucket for message bodies, Redis for shared memory, and optionally a search engine. |
| **Mail Region** | A place with a cluster, for example `bom` (Mumbai). Its code names the cluster's hosts under the DNS zone, and a Region points at one to place its tenants' mail. |
| **Stalwart Cluster** | One mail service per Mail Region, shared by the tenants placed there: its public hostname (`mail.bom.frappemail.com`), its stores, its admin credentials and an SSH key for its servers. |
| **Stalwart Node** | One server (VPS) that runs Stalwart for a cluster. Nodes share the same stores, so any node can serve any user. |
| **Egress Gateway** and **Egress IP Pool** | Optional extra servers used only for sending. A pool is a set of IP addresses; a domain, a tenant or a whole cluster can be told to send through it. |
| **DNS Record** | One record Suite Cloud keeps in a zone (a node's address, the cluster's round-robin entry, the SPF list, and so on). Records are created and removed together with the thing that needs them. |
| **Server Job** | One run of an install or update script on a node or gateway, with the result of every step. Secrets are looked up when the job runs and never stored in it. |
| **Suite Tenant** | One hosted Frappe Suite site: its secret, its title and contact email, its total disk quota, and, once set up for Mail, its Stalwart cluster and mail limits (domains, accounts, groups, lists). Without a cluster every mail call is refused. Its id is the TXT value its domains are verified with. |
| **Mail Domain, Mail Account, Mail Group, Mailing List** | The mail directory of a tenant. Saving one of these pushes it to the cluster in the same step, so the records and the cluster never drift apart. Each carries the id Stalwart gave it. |
| **DMARC Report** | One DMARC aggregate report another receiver sent about a domain, copied hourly from the cluster that intercepted it (Stalwart parses reports mailed to `postmaster@`). Attributed to the tenant that holds the domain, and only when the report was addressed to one of that tenant's domains; kept for the configured retention, and read by tenants through the API. |
| **TLS Report** | One TLS aggregate report (RFC 8460) a sending server made about delivering to a domain: sessions that negotiated TLS as the domain's MTA-STS or DANE policy asks, sessions that failed and why. Copied, attributed, kept and served the same way as DMARC reports, with its own retention; a report whose policies name more than one domain is shown to no tenant. |
| **Region** | A Frappe Cloud region, synced from Central. Sites and servers go only to regions an operator enabled. An optional Mail Region picks the mail cluster of its tenants (else the default cluster); the proxy zone Central serves the region's sites under is learned from the region's first server. |
| **Virtual Machine** | A Frappe Cloud server Suite Cloud created in its Central team. Each one boots Pilot, is checked against its region's proxy zone and gets the Suite app from git; it then takes up to its Max Sites sites. |
| **Suite Site** | A Suite site on a server: waiting in the region's pool, or assigned to a tenant, connected to Suite Cloud, given the domain `<label>.<site zone>` and a mail domain `<label>.<tenant mail zone>`, and then invites the tenant's owner. |
| **Suite Tenant Request** | One request for a hosted workspace: the email, the organization, and a label made from it (Acme Inc. gives acme, or acme-2 when taken) unless one is given. Once the site can send mail, it invites the owner to an account at the tenant's mail domain, and the owner chooses their password when they accept. A signup through the public API confirms its email with a one-time code; one an operator creates in the desk is processed at once. Its Progress table lists every step from the code to the owner's invite, with when it ran and how long it took, and the form shows a progress bar while it runs. |
| **Tenant Email** | One email Suite Cloud sent for a tenant's site: its type (e.g. `password-reset`), recipient, the Email Account that sent it, and whether it was sent. Kept 90 days. |
| **Suite Cloud Settings** | Defaults: DNS time-to-live, Stalwart version and download URLs, certificate settings, the public URL of this Suite Cloud, job timeout, the outgoing Email Account each tenant type's sites send through, and the telemetry switch, retention, request limit, metrics interval, webhook discard time, and alert thresholds and recipients. |
| **Telemetry** | Not a record but a store: the events and metrics Stalwart on each node and gateway pushes to the [telemetry API](docs/api/telemetry.md), kept as the `events` and `metrics` tables of the `suite_cloud_telemetry` data source in Frappe Insights, which Suite Cloud requires, beside `nodes` and `mail_domains` copies of the Stalwart Node, Egress Gateway (a node with the role `gateway`) and Mail Domain records so a dashboard can group by cluster or tenant. Every batch is staged as a parquet file under the site's private files and loaded every five minutes; rows older than the retention are deleted daily. The shipped **Stalwart Mail** Insights workbook holds the dashboards: Delivery, Deliverability, Authentication, Spam, SMTP, Security, Ingest, Capacity, Node Health, Tenants, Clusters, and DMARC and TLS, which read the stored DMARC and TLS reports through Insights' Site DB source. |
| **Telemetry Alert** | One condition the five-minute check found in the telemetry: a Silent Host that reported in the last day but sent nothing for the configured minutes, or an Error Rate of at least the configured number of error events in the last hour. Resolved by the check when the condition clears, or by hand for a node taken out of service; the configured recipients are mailed on both. |

## Install

```sh
bench get-app https://github.com/frappe/suite_cloud
bench --site yoursite install-app suite_cloud
```

Ansible comes with the app: `pyproject.toml` pins it, with the `community.general` collection the
firewall steps use, and Server Jobs run that copy even when the host has its own. The bench host
only needs an SSH client (`apt install openssh-client`; Frappe Cloud installs it from
`pyproject.toml`).

Operators are System Managers. Installing creates one role, **Suite Tenant**, without desk access:
every Suite Tenant names an owner, a User holding that role, and requests signed with the tenant's
secret run as that owner. The role opens the tenant-owned DocTypes (domains, accounts, groups,
lists, reports) and the owner's own Suite Tenant, and the app narrows each of them to the rows of
the tenants the user owns. On the Suite Tenant itself an owner may change only the title and the
contact email; everything else sits at permission level 1, for System Managers.

## Setting up a cluster, step by step

1. **Settings.** Enter the email address that should receive certificate notices. While testing,
   switch the ACME directory to the Let's Encrypt staging URL so you do not hit rate limits.
2. **DNS Zone.** Create a zone for the domain your servers will live under (`frappemail.com`) and
   enter the DNS provider credentials for it. Mark it as the default. Add more zones later if some
   clusters should live under a different domain.
3. **Stores.** Create a PostgreSQL (or MySQL) data store, an S3 blob store and a Redis in-memory
   store. Redis also coordinates the nodes. RocksDB and local files only work for a single-node
   cluster.
4. **Cluster.** Create a Mail Region (`bom`, Mumbai), then a Stalwart Cluster in it. The region
   and the zone make the hostname, `mail.bom.frappemail.com`; a second cluster in the region gets
   `mail.bom-2`. The hostname is also the default domain that nodes, gateways and pools get their
   names under, so one wildcard certificate covers them all. Tenants of a Region that points at the
   mail region are placed on its cluster. Copy the SSH public key.
5. **First node.** Put that public key on a fresh VPS and set the reverse DNS (PTR) of its IP to the
   node's hostname. Create a Stalwart Node (`n1.mail.bom.frappemail.com` with its IPv4), click
   **Verify SSH**, then **Provision**. The first Verify SSH records the server's host keys; every
   later connection must match them, and a changed address or port asks for a fresh verification. The job installs Stalwart and its CLI, sets up the system
   service and firewall, starts Stalwart once to write the store settings, applies the cluster
   configuration (roles, coordinator, certificate provider, wildcard certificate, system settings),
   and restarts it normally with the temporary admin credential removed.
6. **Finish bootstrap.** Once the certificate is issued, **Finish Bootstrap** (also tried every
   five minutes) creates the management API key and marks the cluster **Active**. The cluster
   hostname now points at this node.
7. **More nodes.** Create and provision them the same way. They only receive the database
   connection; everything else is read from the shared database. A node is added to the cluster
   hostname's DNS as soon as Stalwart reports it active.

Day-to-day buttons: **Drain** takes a node out of DNS before maintenance, **Upgrade Nodes** rolls a
new Stalwart version out one node at a time, **Sync Config** pushes the generated configuration
again, **Check Drift** reports what differs from it without changing anything, and **Upgrade Agents**
installs each server's telemetry token and the current [Suite Cloud Agent](docs/agent.md) build; the
cluster, node and gateway forms show when each server last reported.

### Telemetry

Stalwart pushes its own events and metrics to the [telemetry API](docs/api/telemetry.md). Two WebHooks send the events: one lists the events the dashboards read, the other every other event Stalwart logs at error level. OTLP sends the metrics every **Metrics Push Interval (Seconds)**. The cluster plan holds both, and the gateway plan holds its own.

Each node and gateway has its own telemetry token, made with the record. Provisioning and upgrades write it to `/etc/stalwart/stalwart.env` as `SUITE_CLOUD_TELEMETRY_TOKEN`, and each request carries the token of the server that sent it. A config reload that names a variable a running server lacks fails. So the plan switches the push on only once every Provisioned, Active and Draining node of the cluster, or the gateway, has **Telemetry Token Installed**. Until then it writes both WebHooks switched off and OTLP disabled.

OTLP starts only when Stalwart starts. These jobs restart Stalwart:

1. **Upgrade Agents** on a cluster runs a job per Active or Draining node and Active gateway. Each job writes the token to `stalwart.env`, restarts Stalwart when the file changed, and installs the agent that only enforces sending limits. A gateway's agent stops.
2. When the last server of the cluster, or the gateway, has its token, a job pushes the config and restarts Stalwart on each server once more.
3. **Upgrade Agents** on a cluster where every server has its token pushes and restarts again. Run it when step 2 failed. Its error shows on the cluster's form.

The servers restart in parallel jobs, not one at a time. **Rotate Telemetry Token** on a node or gateway form makes a new secret and restarts Stalwart on that server. A change of the metrics interval or of **Public URL** takes effect after Upgrade Agents. A Sync Config alone does not restart Stalwart.

### Adopting a server that already has a directory

A Stalwart that a Suite tenant administered directly, before Suite Cloud, holds domains, accounts,
groups and lists that Suite Cloud never created. Register it as a cluster, create the Suite Tenant
for it, then use **Adopt Directory** on the Suite Tenant (or run
`suite_cloud.mail.tenancy.adopt.adopt_directory` with the tenant name). Every object on the
cluster is recorded with the id it already has, and nothing is pushed back. The cluster must serve
that one tenant, since nothing on it says which tenant an object belongs to; an object whose record
already exists anywhere is skipped, and each object imports on its own, so one that cannot (an
alias on a domain the tenant does not own, say) is reported while the rest go in. Adopted domains
are marked verified; adopted accounts and groups keep the cluster's quotas. Passwords and app
passwords stay on the cluster and keep working.

### Sending through dedicated IPs

1. Create an Egress Gateway (`out1.c1.frappemail.com`) on a VPS that already has the extra public
   IPs configured, and provision it.
2. Create an Egress IP Pool listing those IPs with a hostname for each (`ded1.c1.frappemail.com`,
   with reverse DNS set at the provider).
3. Assign the pool to a Mail Domain, to a Suite Tenant, or as the cluster default.

Mail from the matching sender domains is relayed to the gateway over an authenticated, encrypted
connection and leaves from the pool's addresses. The `spf.c1.frappemail.com` record that customer
domains include always lists every node and pool address, so SPF stays valid whichever path a
message takes.

## Hosting Suite sites on Frappe Cloud

Frappe Cloud's control plane, Central, creates servers; Pilot, the bench agent on each server, creates sites on it. Central has no API for sites, so Suite Cloud asks Central for a bench token (`get_bench_link`) and drives Pilot's Admin API with it.

1. **Central.** Create a user for Suite Cloud with an API key, and a team where that user holds a team-wide role with `server:create`, `server:view`, `cluster:view` and `server:console`. The team's spend limit bounds how many servers Suite Cloud can create. Enter the URL, team, key and secret in the **Frappe Cloud** tab of Suite Cloud Settings and run **Test Central Connection**.
2. **Regions.** Run **Sync Regions**, then enable the regions to host in, optionally with the Mail Region whose cluster takes their tenants' mail (else the default cluster). Pick the default region. The proxy zone, `<region>.<Central wildcard domain>`, is learned from each region's first server.
3. **Zones.** Pick a site DNS zone and a tenant mail zone, DNS Zones with a DNS provider; every hosted site gets a mail domain, so the mail zone is required. They may be one zone: a tenant labelled `acme` then gets the mail domain `acme.<zone>` and the site domain `acme.sites.<zone>`, since the site's CNAME cannot share a name with the mail domain's MX and TXT records. The tenant mail zone must not be a cluster's zone.
4. **Servers.** Create a Virtual Machine in a region, or turn on **Create Servers Automatically**. Each one is created on Central, waits for Pilot, is checked (its wildcard zone must be a zone of the region, and equal the proxy zone the region's first server reported; production mode on), and installs Suite from the repository and branch in Settings. Verify Host gives Pilot the ACME Contact Email as its Let's Encrypt email when it has none (Pilot v0.0.74-pre-alpha and later keep it); a server whose Pilot has no email takes sites but no site or custom domains.
5. **Sites.** Each enabled region keeps **Ready Sites per Region** sites with Suite installed. **Create Hosted Site** on a Suite Tenant, or a signup, claims the oldest one, else creates one. The site is connected to Suite Cloud (Suite Settings, Mail Settings, onboarding) and turns **Active**. The site domain and the mail domain follow and never hold up Active. Once the mail domain is verified, Suite Cloud has the site invite the owner (Suite's own Mail Account Request, Suite Admin, valid 7 days) to `username@<label>.<tenant mail zone>`. The owner chooses their password when they accept; **Resend Invite** sends it again, or a fresh one once it expired.

Suite Cloud sends the emails Suite itself sends, such as the owner's invite, password resets and share notices: the site names the email and its parameters, and Suite Cloud renders it from its own template and sends it immediately through the Email Account that **Tenant Email** in Suite Cloud Settings sets for the tenant's type. Set one before the first site is created, or the owner's invite cannot go out; see [docs/api/email.md](docs/api/email.md). Any other mail of a site, such as Frappe's own notifications, goes through an Email Account the site owner sets up.

**Login as Administrator** on a Suite Site opens a fresh session; **Drop Site** deletes the site and its site domain records; the mail domain and its mailboxes stay with the tenant until it is archived with its data. Signup is off until **Allow Signup** is checked; see [docs/api/signup.md](docs/api/signup.md).

## API

Suite Cloud serves one REST API at `/api/v2/method/suite_cloud.api.<module>.<method>`, documented in [docs/api](docs/api/README.md).

The **tenant API** lets a Suite Tenant manage its own domains, accounts, groups, mailing lists and DMARC and TLS reports. The tenant authenticates with its id and secret in an `Authorization: Tenant <id>:<secret>` header; it only ever sees its own objects, and a request from outside its allowed addresses is refused.

The verb follows the method name (`list_` and `get_` are GETs, `update_` a PATCH, `set_` a PUT, `delete_` and `remove_` a DELETE, the rest POSTs), parameters travel as JSON, the result sits under `data`, and an error answers `{"errors": [{"type", "message"}]}` with a REST status code. Each tenant may make 300 requests per minute.

### How a domain goes live

1. The tenant calls `check_domain`. Nothing is stored yet: the answer is a TXT record the domain
   owner must publish at the apex, `frappe-suite-verification=<tenant id>`. The id is fixed for
   the life of the tenant, so every domain it adds asks for the same record and no other tenant
   can produce it. This is what keeps one tenant from claiming another tenant's domain on a shared
   cluster. Operators adding a domain from the desk vouch for it themselves and skip this step.
   So does a cloud with **Skip Domain Verification** on in Suite Cloud Settings: it takes a
   tenant's word for its domains, which also count as verified without the DNS check below -
   for a development cloud, whose test domains exist in no public DNS, never for a shared one.
   Turning it off re-checks the domains it vouched for, so one without records goes offline.
2. Once the record resolves, `create_domain` creates the Mail Domain and the domain on the
   cluster, disabled.
3. `get_dns_records` lists what the owner must publish next: an MX record pointing at the
   cluster, an SPF record of the form `v=spf1 include:spf.<zone> -all`, the DKIM keys Stalwart
   generated for the domain, and DMARC and TLS reporting records. The rows come from Stalwart's
   `dnsZoneFile`, with SPF rewritten so egress gateways are covered. Stalwart generates and holds
   the keys; they use fixed selectors, `frappemail-rsa` and `frappemail-ed25519`, and never rotate,
   so the records an owner publishes stay valid for the life of the domain. Should a key leak,
   **Replace DKIM Keys** on the Mail Domain (and on a cluster or gateway, for their own domains)
   generates new keys under the same selectors; the owner republishes the records, and the domain
   is offline from the next hourly check until they resolve.
4. The owner publishes them. `verify_dns_records` checks on public resolvers; once SPF, DMARC and
   at least one DKIM selector resolve, the domain is verified. MX is optional: a domain may use the
   cluster for sending only.
5. A domain is **active** when it is enabled and verified; only then does Stalwart accept mail for
   it, and only then may the tenant create accounts, groups and lists on it. Disabling a domain drops
   its verification on purpose, so enabling it again needs a fresh check.
6. Verification is retried every hour, and a DKIM key the cluster was still generating when the
   records were read is picked up hourly. A temporary DNS failure never turns a working domain off.
   **Skip Scheduled Verification** on the Mail Domain keeps a domain out of the hourly check, so
   one a manager verified by hand stays verified; **Verify DNS Records** still runs on demand.

A domain carries three delivery settings a tenant may change: a catch-all address for local parts
that match no account, sub-addressing (`user+tag@`), and relaying, which makes the cluster forward
mail for addresses it does not hold to the domain's MX instead of rejecting it, so a domain can
keep some mailboxes on another server (split delivery). Relayed mail leaves like any other the
cluster sends, through the egress pool of its sender when there is one.

Records for mail client auto-setup (SRV, autoconfig, MTA-STS) are listed only when the domain has
`publish_client_discovery_records` turned on, because the cluster has no certificate for customer
hostnames.

## Configuration

All configuration lives in Suite Cloud Settings; `site_config.json` is not consulted. On a development site that talks to a cluster with a staging certificate, turn off **Verify Stalwart TLS Certificates** in the Stalwart tab.

## Scheduled work

| When | What |
| --- | --- |
| Every 5 minutes | Retry failed Server Jobs; poll nodes that are provisioning or draining; read every Virtual Machine from Central and drive unfinished ones on; drive Suite Sites that are mid-way; top up each region's pool of ready sites. |
| Hourly | Verify unverified domains, except those set to skip it; refresh the DNS records of domains whose DKIM keys are not all stored yet; copy the DMARC and TLS reports each cluster received since the last run; add a server to a region short of free site slots, when servers are created automatically. |
| Daily | Verify every DNS Record in the zones; check every cluster for configuration drift; check the PTR records of nodes and pool addresses; drop DMARC and TLS reports older than their retention; sync regions and every server from Central, marking a server Central no longer lists Terminated; expire stale signups and forget their passwords. |

## Development

[docs/development.md](docs/development.md) sets up a development site step by step: the bench, tests and lint, DNS zones, a Stalwart cluster, a Cloudflare tunnel and Frappe Cloud.

```sh
bench --site yoursite run-tests --app suite_cloud --test-category all
```

Tests live next to their module: `suite_cloud/tests` for the generic app (settings, fixtures)
`suite_cloud/hosting/tests` for hosting, with fakes of Central, Pilot and the site's REST API,
and `suite_cloud/mail/tests` for mail, including the fake Stalwart
(`suite_cloud/mail/tests/fake_stalwart.py`), an in-process JMAP management server the real
client talks to unchanged. Nothing reaches a real server or a real DNS provider. Tests that create
tenants use names under `.frappe.test` and clean up only those, so they can run on a site that also
holds real clusters.

The fake keeps the routes it is sent but cannot follow them, so `test_delivery` starts a Stalwart
of its own to see where they take a message: from a temporary directory, on loopback ports, with
a local sink as its only way out. It needs a `stalwart` on `PATH` of the version new clusters
install and is skipped without one; CI installs it.

CI (`.github/workflows/ci.yml`) runs one job per module folder (Core, Mail, Hosting, Telemetry) plus
Ruff and a Go job for the agent, so a future product adds a matrix entry rather than a workflow.

The bootstrap path has been run end to end against a real single-node cluster on Stalwart
v0.16.20 (DigitalOcean DNS, Let's Encrypt wildcard certificate through DNS-01). Lessons that
are now built in: Stalwart encodes every `Set` as `{"value": true}` and every `List` as an
index-keyed object; secrets are `{"@type": "Value", "secret": ...}` unions; Let's Encrypt
rejects a wildcard order that also names a host it covers; the built-in roles are provisioned
only on a normal start that finds no Role objects, so the recovery-stage plan creates none;
bootstrap's admin password is never disclosed, so the recovery-stage plan sets it; the first
normal start fails without the cluster role it names and imports the spam filter rules once,
from the latest release unless told otherwise, so a short recovery stage before it creates the
roles, points DNS at Unbound and pins the release the server supports; and Stalwart queries
all its DNS servers by measured speed rather than in order, so nodes resolve through a local
Unbound only (a public fallback would answer too, and blocklists refuse public resolvers).

Still unverified on a live server: the egress gateway (relay listeners, connection strategy),
multi-node registry leases with a Redis coordinator, and node upgrades.

## License

AGPL-3.0
