# Development setup

This guide sets up a development site for Suite Cloud. Each part builds on the parts before it, so stop when you have what your work needs:

| Work on | You need |
| --- | --- |
| The tenant API, DocTypes and the test suite | [A bench and a site](#1-a-bench-and-a-site) and [tests and lint](#2-tests-and-lint) |
| Mail on a real Stalwart cluster | Also [DNS zones](#3-dns-zones) and [a Stalwart cluster](#4-a-stalwart-cluster) |
| Hosting Suite sites on Frappe Cloud | Also [a Cloudflare tunnel](#5-a-cloudflare-tunnel) and [Frappe Cloud](#6-frappe-cloud) |
| The telemetry API and the Insights dashboards | [A bench and a site](#1-a-bench-and-a-site), [tests and lint](#2-tests-and-lint) and [telemetry](#8-telemetry) |

The tests need none of the outside services. They use fakes of Stalwart, Central, Pilot, the hosted site and the DNS providers.

## 1. A bench and a site

Suite Cloud runs on Frappe `develop` with Python 3.14, MariaDB and Redis. It requires [Frappe Insights](https://github.com/frappe/insights) `develop`, which holds the telemetry the nodes push.

1. Create a bench: `bench init --python python3.14 suite-bench`.
2. Get the apps: `bench get-app insights` and `bench get-app https://github.com/frappe/suite_cloud`.
3. Install the development tools: `bench setup requirements --dev`.
4. Create a site: `bench new-site suite-cloud.localhost`.
5. Install the app: `bench --site suite-cloud.localhost install-app suite_cloud`. This installs Insights first.
6. Turn on developer mode: `bench --site suite-cloud.localhost set-config developer_mode 1`.
7. Start the bench: `bench start`.

Open `http://suite-cloud.localhost:8000/desk` and log in as Administrator. All settings are in **Suite Cloud Settings**. The app does not read `site_config.json`.

Run `bench --site suite-cloud.localhost migrate` after you pull changes or edit a DocType's JSON.

## 2. Tests and lint

The tests run on a site that allows them:

```sh
bench --site suite-cloud.localhost set-config allow_tests true
bench --site suite-cloud.localhost run-tests --app suite_cloud --module suite_cloud.hosting.tests.test_signup
```

CI runs each test module on its own, in four jobs: Core (`suite_cloud/tests`), Mail (`suite_cloud/mail/tests`), Hosting (`suite_cloud/hosting/tests`) and Telemetry (`suite_cloud/telemetry/tests`). Run the modules of the folder you changed.

The tests create records under `.frappe.test` and delete only those. So you can run them on the site that holds your real cluster and zones.

`test_delivery` sends mail through a local Stalwart. It needs the `stalwart` binary of the version in **Suite Cloud Settings > Stalwart Version** on your `PATH`. Without it, the test is skipped.

Lint with the Ruff version CI pins:

```sh
uvx ruff@0.14.10 check .
uvx ruff@0.14.10 format --check .
```

Or install the hooks once with `pre-commit install`. They run the same Ruff version on each commit.

## 3. DNS zones

A DNS Zone is a domain whose records Suite Cloud writes through the DNS provider's API. You need one zone for a cluster. For hosting, you also need a zone for the tenants' mail domains.

1. Get a domain for development. Do not use a production domain.
2. Point the domain's name servers at a provider that Suite Cloud supports. Use DigitalOcean or Cloudflare: only these two can publish MX records.
3. Create an API token at the provider with write access to the domain's records.
4. In the desk, create a **DNS Zone** with the domain as **Domain Name**.
5. Select the **DNS Provider** and enter the **Token**. For Cloudflare, also enter the **Zone ID**.
6. Make sure that **Enabled** is checked.

The development site at Frappe uses two DigitalOcean zones:

| Zone | Used for |
| --- | --- |
| `frappe.email` | The cluster (`mail.bom.frappe.email`) and the hosted sites' domains |
| `frappesuite.com` | The tenants' mail domains, for example `acme.frappesuite.com` |

The tenant mail zone must not be a cluster's zone. One zone can hold both the site domains and the tenant mail domains. Then site domains go under `sites`, for example `acme.sites.example.com`.

## 4. A Stalwart cluster

A cluster is one or more Stalwart servers that Suite Cloud installs and configures over SSH. For development, one node with RocksDB is enough.

You need a VPS:

- Debian or Ubuntu, with a public IPv4 address.
- SSH as `root`, or as a user with passwordless `sudo`.
- Inbound ports 25, 465, 587, 143, 993, 110, 995, 443 and 4190 open at the provider. The provisioning job sets the server's own firewall.
- Outbound port 25 open. Many providers block it on new accounts. Ask them to open it.

Then set up the cluster:

1. In **Suite Cloud Settings > Certificates**, enter the **ACME Contact Email**.
2. For a throwaway cluster, set **ACME Directory URL** to the Let's Encrypt staging URL, `https://acme-staging-v02.api.letsencrypt.org/directory`. This keeps you below the production rate limits.
3. With a staging certificate, clear **Suite Cloud Settings > Stalwart > Verify Stalwart TLS Certificates**.
4. Create a **Stalwart Store** with **Kind** Data and **Type** RocksDb.
5. Create a **Mail Region** with the code `bom` and the title Mumbai, then a **Stalwart Cluster** in it with your zone and that store. Its hostname is `mail.bom.<zone>`.
6. Copy the cluster's SSH public key into `~/.ssh/authorized_keys` on the VPS.
7. At the VPS provider, set the reverse DNS (PTR) of the IPv4 address to the node's hostname, for example `n1.mail.bom.example.com`.
8. Create a **Stalwart Node** with that hostname and the IPv4 address.
9. Click **Verify SSH**, then **Provision**. The Server Job shows each Ansible task as it runs.
10. Wait for the certificate. Then **Finish Bootstrap** runs, by hand or from the five-minute job, and the cluster turns **Active**.

The [README](../README.md#setting-up-a-cluster-step-by-step) explains each step and how to add more nodes.

## 5. A Cloudflare tunnel

A hosted site calls Suite Cloud: it checks its credentials and reads the tenant's mail settings. A server on Frappe Cloud cannot reach `localhost`, so you expose your bench through a tunnel.

1. Install `cloudflared`, for example `brew install cloudflared`.
2. Start a quick tunnel to your site:

   ```sh
   cloudflared tunnel --url http://127.0.0.1:8000 --http-host-header suite-cloud.localhost
   ```

3. Copy the `https://<words>.trycloudflare.com` address that it prints.
4. Paste the address into **Suite Cloud Settings > General > Public URL**.

`--http-host-header` makes Frappe find your site. Without it, the bench gets the tunnel's host name and answers with an error.

A quick tunnel gets a new address each time it starts. After a restart, update **Public URL**. Sites that are already configured keep the old address. **Rotate Secret** on a site's Suite Tenant sends the new address to the site, with a new secret. For an address that does not change, use a named Cloudflare tunnel.

Keep the tunnel running while you work on hosting. When Suite Cloud is not reachable, the **Configuring the Suite Site** step waits with "Suite Cloud is unreachable". It tries again every five minutes.

## 6. Frappe Cloud

Suite Cloud creates servers through Central, Frappe Cloud's control plane, and creates sites through Pilot, the agent on each server.

1. On Central (`https://dashboard.frappe.io`), get a team for development and an API key for a user in that team. The user needs the `server:create`, `server:view`, `cluster:view` and `server:console` permissions. The team's spend limit caps what Suite Cloud can create.
2. In **Suite Cloud Settings > Frappe Cloud**, enter the **Central URL**, **Central API Key** and **Central API Secret**.
3. Select the **Central Team**. The list comes from Central with the key on the form.
4. Click **Test Central Connection**.
5. Set **Image Offering** to `pilot`. Leave **Plan** blank to use the cheapest plan.
6. Set **Repository** to `https://github.com/frappe/suite` and **Branch or Tag** to `develop`, or to the branch you test.
7. Set **Site DNS Zone** and **Tenant Mail Zone** to your zones from [part 3](#3-dns-zones).
8. Click **Sync Regions**. Open the region to use, for example `mumbai`, and check **Enabled**.
9. Set **Default Region** to that region.
10. Set **Ready Sites per Region** to 0 or 1. Each ready site waits in a pool on a server, and each server costs money.
11. Create a **Virtual Machine** in the region. It turns **Running** and installs Suite in about ten minutes.

A region's **Mail Region** picks the cluster of its tenants. A region without one goes to the default cluster, so one cluster serves every region in development.

## 7. Create a tenant end to end

1. Create a **Suite Tenant Request** with an email address, a full name and an organization. Leave **Label** blank.
2. Open the request. The progress bar and the **Steps** table show each step with its time.
3. Wait for **Completed**. The owner gets an invite email from the Email Account that **Suite Cloud Settings > Tenant Email** sets for Suite Site tenants.

The steps that wait for DNS take a few minutes. The poll runs every five minutes.

To remove the test, click **Drop Site** on the Suite Site. The tenant's mail domain stays until you archive the tenant with its data.

## Troubleshooting

**Background jobs end with "Work-horse terminated unexpectedly" (signal 6 or 11) on macOS.** RQ runs each job in a forked process. On macOS, the first HTTPS call in a forked process reads the system proxy settings through CoreFoundation, which crashes after a fork. Start the workers with these variables:

```sh
export no_proxy='*'
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
```

Or run the bench in a mode that runs jobs in threads, for example Pilot's lite mode. The process stops before the step commits, so nothing is marked failed. The request keeps the status Processing, and the job runs again every five minutes.

**A hosted site reports "Suite Cloud is temporarily unavailable", and its Error Log shows a Cloudflare 502.** The bench restarted while the site called it. A development bench restarts when a file under `apps/` changes. Do not save files under `apps/` while a request runs. Keep scratch scripts in another directory. The step tries again on the next poll.

**The site domain step is skipped with "The server has no Let's Encrypt email".** Pilot on the server has no email for Let's Encrypt. Verify Host sets it from **Suite Cloud Settings > Certificates > ACME Contact Email**, but only Pilot v0.0.74-pre-alpha and later keep it. Fill in the ACME Contact Email, update Pilot if it is older, and click **Verify Host** on the Virtual Machine. The next poll adds the site domain to the server's Active sites.

**The site domain is attached but "Not Made Primary".** After Pilot attaches the domain, it issues the certificate in a task of its own. Suite Cloud checks `https://<domain>` every 15 seconds for up to 5 minutes and makes the domain primary once it answers. A primary domain gets every visitor of the site, so a domain without HTTPS stays attached only: the site keeps its `suite-<hex>` host name, the request goes on, and an Error Log names the domain. The regional proxy (`proxy.<region>.frappe.cloud`) serves HTTPS for the name only once the certificate was issued; until then port 443 answers without TLS. Look for the failed `setup-letsencrypt` task on the server, for example a missing Let's Encrypt email, and check with `curl -v https://<domain>/api/method/ping`. Once it answers, click **Domains > Make Primary** on the Suite Site.

**A label is "not available".** A site, an open request, a cluster, a mail domain or a DNS record already uses it. Leave **Label** blank to get a free one made from the organization, for example `acme-2`.

**Stalwart rejects requests with a certificate error.** The cluster uses a staging certificate. Clear **Verify Stalwart TLS Certificates**.

## Clean up

Servers and VPSs cost money until you remove them:

1. Drop the Suite Sites that you do not need.
2. Click **Terminate** on each Virtual Machine.
3. Set **Ready Sites per Region** to 0 so that the pool does not create new sites.
4. Delete the Stalwart VPS at its provider when you no longer need the cluster.

## 8. Telemetry

Stalwart on each node and gateway pushes events and metrics to the [telemetry API](api/telemetry.md). Suite Cloud stages each batch as a parquet file under `private/files/telemetry/` and the five-minute job copies the files into the `suite_cloud_telemetry` DuckDB data source in Insights, which `migrate` creates.

1. In **Suite Cloud Settings > Telemetry**, make sure **Enable Telemetry** is on. It is on by default on a new site. Take the **Suite Cloud Key** and **Suite Cloud Secret** from a Stalwart Node (`frappe.get_doc("Stalwart Node", name).get_password("suite_cloud_secret")` in a console).
2. Send a batch as Stalwart's WebHook does:

   ```sh
   curl -X POST "http://suite-cloud.localhost:8000/api/v2/method/suite_cloud.api.telemetry.webhook?stream=events" \
     -H "Authorization: Bearer <key>.<secret>" -H "Content-Type: application/json" \
     -d '{"events": [{"id": "1", "createdAt": "2026-10-20T09:59:00Z", "type": "delivery.delivered", "data": {"domain": "example.com"}}]}'
   ```

3. Load it without waiting for the scheduler: `bench --site suite-cloud.localhost execute suite_cloud.telemetry.warehouse.load_staged_batches`.
4. To see an alert, set **Telemetry Alert Recipients** and run `bench --site suite-cloud.localhost execute suite_cloud.telemetry.alerts.check_telemetry_alerts` once the node from step 1 has been quiet for the configured minutes: a **Telemetry Alert** opens, and resolves on the next check after another batch.
5. Open Insights and the **Stalwart Mail** workbook. Its dashboards read the `events` and `metrics` tables live. The `nodes` and `mail_domains` tables beside them are copies of the Stalwart Node, Egress Gateway and Mail Domain records, refreshed by the same job, which the Tenants and Clusters dashboards join on; a gateway is a node of its cluster with the role `gateway`. A delivery is attributed to the tenant by its sender domain, an incoming message by its recipient domain. The DMARC and TLS dashboards read the DMARC Report and TLS Report records live through the `Site DB` data source Insights ships; they need no telemetry.

The workbook ships in `suite_cloud/telemetry/insights_workbook/stalwart/stalwart.json`. With developer mode on, every change you save in the Insights UI writes that file again, so edit the dashboards there and commit the file. A new workbook becomes a shipped one with `frappe.get_doc("Insights Workbook", name).mark_as_standard(name="<file name>", module="Telemetry")` in a console. The shipped name never changes: a rename deletes the workbook on every site and creates a new one.
