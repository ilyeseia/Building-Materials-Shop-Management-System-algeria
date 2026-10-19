# Commercial editions, installation and releases

Status: **implemented and locally verified** on 2026-10-19. Items that need a
customer-specific destination or credential are marked **BLOCKED** rather than
presented as complete.

This document describes how the product can be sold, installed, licensed and
updated without hiding anything from the customer. The application has **no
activation server, no telemetry, no remote kill switch and no code path that
disables a feature because a license file is missing or expired**.

---

## 1. What the commercial offer can contain

The software supports a single business per deployment. A commercial offer can
combine, at the vendor's discretion:

* the source release or a built container image;
* installation and first-run configuration;
* staff training and onboarding;
* an update/support period;
* optional hosting and backup management.

Prices, contract terms and support response times are **not hard-coded** in the
repository and must be stated in the signed offer. Do not invent a price in the
product or in this document.

### Honest scope of the license file

`instance/license.json` is **metadata for the administrator**. It records the
licensee, edition, issue/expiry dates and purchased features. It is shown on
**Settings → System status** and by `flask license-status`.

What it does:

* makes the commercial relationship visible to the operator;
* warns about an upcoming or passed renewal date;
* optionally detects accidental edits with an HMAC signature.

What it does **not** do:

* it does not encrypt data or the source;
* it does not block sign-in or any screen;
* it does not delete or degrade data;
* it is not a substitute for a contract, invoice or legal terms.

An expired license produces a **WARN** entry in `flask doctor`, and nothing else.

### License file format

```json
{
  "schema_version": 1,
  "licensee": "Example SARL",
  "edition": "professional",
  "issued_at": "2026-10-01",
  "expires_at": "2027-10-01",
  "features": ["ai", "fleet"],
  "signature": "<hex HMAC-SHA256 of the canonical body>"
}
```

`expires_at` may be `null` for a perpetual license. If `LICENSE_SECRET` is set in
the environment, the `signature` must match; a missing or wrong signature is
reported as `invalid` but still does not disable anything. Generate a signature
with:

```bash
python - <<'PY'
import json
from app.services.licensing import sign_license
payload = {
    "schema_version": 1,
    "licensee": "Example SARL",
    "edition": "professional",
    "issued_at": "2026-10-01",
    "expires_at": "2027-10-01",
    "features": ["ai", "fleet"],
}
print(sign_license(payload, "THE-LICENSE-SECRET"))
PY
```

`LICENSE_FILE` defaults to `instance/license.json` (inside the persistent
`app-instance` volume in Docker). `LICENSE_SECRET` is read from the environment
and is never stored in the database or rendered on a page.

---

## 2. Installation (Docker, recommended)

### Prerequisites

| Item | Minimum |
|---|---|
| Linux host | Ubuntu 22.04/24.04 LTS or equivalent |
| Docker Engine | 24+ with Compose v2 |
| RAM | 2 GB for the host and stack (the app limit is 512 MB) |
| Disk | 10 GB free before the first backup |
| Network | HTTPS via a reverse proxy; the app itself speaks HTTP inside the private network |

### Steps

1. **Obtain and verify the release**

   ```bash
   sha256sum -c bmshop-1.1.0.tar.gz.sha256
   tar -xzf bmshop-1.1.0.tar.gz
   cd bmshop-1.1.0
   ```

2. **Create the environment file**

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

   Fill in at least `SECRET_KEY`, `MYSQL_ROOT_PASSWORD` and `MYSQL_PASSWORD`
   (use `openssl rand -hex 32` / `openssl rand -hex 24`). Keep `BIND_ADDRESS`
   on `127.0.0.1` unless a trusted proxy or VPN needs otherwise.

3. **Validate and build the image with its release identity**

   ```bash
   docker compose config --quiet
   docker compose build \
     --build-arg APP_VERSION="$(cat VERSION)" \
     --build-arg BUILD_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
     --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```

4. **Start the database and application**

   ```bash
   docker compose up -d
   docker compose ps            # both services must become healthy
   ```

5. **Create/upgrade the schema, reference data and first administrator**

   ```bash
   docker compose exec app /app/scripts/docker-init.sh
   ```

   `docker-init.sh` is idempotent and never drops data. If `ADMIN_PASSWORD` is
   not set, create the administrator interactively:

   ```bash
   docker compose exec app flask create-user --username admin --role admin
   ```

6. **Run the installation checks**

   ```bash
   docker compose exec app flask doctor
   docker compose exec app flask version
   docker compose exec app flask license-status
   ```

   Then open **Settings → System status** (`/settings/system`) as an
   administrator. It shows the release commit, schema revision, database size,
   backup freshness, disk space, AI provider configuration, license metadata and
   the same pass/warn/fail checks. It never displays a password, API key or
   database URL.

7. **Configure the business**

   * Settings: business name/legal name, address, phone, currency `DZD`,
     timezone `Africa/Algiers`, VAT `19` (a percentage), invoice footer.
   * Warehouses, units of measure and categories.
   * Products with an explicit stock unit and a selling price separate from any
     market reference price.
   * Users and roles (administrator, manager, sales, cashier, storekeeper,
     accountant, driver, viewer, etc.). Review the least-privilege defaults.
   * Opening stock via the normal stock movement/opening workflow, not by editing
     the database.

8. **Put HTTPS in front**

   The production compose file is designed to sit behind a TLS-terminating
   proxy. Set `SESSION_COOKIE_SECURE=1` when HTTPS is enforced, and set
   `FORWARDED_ALLOW_IPS` to the proxy address (not `*` unless the app port is
   reachable only through the proxy). Verify the response headers:
   `Strict-Transport-Security`, `Content-Security-Policy` without
   `unsafe-inline`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`.

9. **Install and test backups before entering real data**

   ```bash
   docker compose exec app /app/scripts/backup.sh
   ./scripts/verify-backup.sh
   ./scripts/install-backup-schedule.sh --print   # inspect first
   ./scripts/install-backup-schedule.sh           # install daily 02:15 cron
   ```

   Test the notification webhook and the off-site destination separately; see
   section 4. A backup that has never been restored is only a hope.

10. **Record the baseline**

    Save `flask doctor` output, the release commit and the first verified backup
    name in the customer's handover document.

---

## 3. First-day onboarding checklist

- [ ] Administrator can sign in; all other demo accounts removed or disabled.
- [ ] Arabic is the default language and the layout is RTL; French/English work.
- [ ] Currency is DZD, timezone is `Africa/Algiers`, VAT is configured.
- [ ] At least one warehouse, the units actually used and the product catalogue
      with explicit stock units are entered.
- [ ] Opening stock is recorded through stock movements, with a dated reason.
- [ ] Selling prices are confirmed by the owner; market observations are
      recorded separately and never overwrite selling prices.
- [ ] A first quotation → order → delivery → invoice → payment is exercised on a
      test customer, then cancelled/credited according to policy.
- [ ] A purchase order → goods receipt → supplier invoice → payment is exercised.
- [ ] Receivables/payables ageing matches the owner's own figures.
- [ ] A backup is taken, verified in isolation, and the restore procedure is
      explained to the responsible person.
- [ ] Update and rollback procedure is handed over (section 5).
- [ ] Support contacts, license renewal date and data ownership are in writing.

---

## 4. Backup schedule, off-site copy and alerts

### Local schedule

`scripts/backup-cron.sh` runs the in-container backup, then the optional
off-site copy and notification. `scripts/install-backup-schedule.sh` installs a
daily cron entry (default `15 2 * * *`) for the current host user:

```bash
BACKUP_CRON="15 2 * * *" ./scripts/install-backup-schedule.sh
```

Output is appended to `backups-cron.log` in the project directory. The script
never restores, never drops a database and never removes business rows. Local
retention is controlled by `BACKUP_RETENTION_DAYS` (default 30).

### Off-site copy (needs a destination)

`scripts/backup-offsite.sh` exports the newest backup files from the container
with `docker compose cp`, uploads them with `rclone` and runs `rclone check`.
Configure on the host:

```bash
rclone config                                  # one-time credentials
export BACKUP_REMOTE="remote:bmshop-backups"   # e.g. Backblaze/S3/SFTP
./scripts/backup-offsite.sh
```

For the scheduled job, cron does not inherit an interactive shell. Put
`BACKUP_REMOTE` (and optionally `BACKUP_NOTIFY_WEBHOOK`) in a mode-600
`backup.env` in the project directory; `backup-cron.sh` sources it if present.
The file is git-ignored.

If `BACKUP_REMOTE` is empty, the script prints `skipped` and exits 0. The status
page then still shows the **local** backup as fresh; it cannot claim that an
off-site copy exists. **Off-site storage is BLOCKED until the customer provides
a destination and credentials.** No cloud account or credential is invented
here.

### Alerts

Set `BACKUP_NOTIFY_WEBHOOK` to a Slack-compatible JSON webhook. The wrapper sends
one message on success and one on failure. A webhook failure is logged but does
not change the backup result. A failed backup also writes
`/app/backups/latest-failure.json`, which the system status page displays next to
the last successful backup. The marker is removed on the next success.

### Point-in-time recovery

MySQL binary logging/PITR is **not configured** by this repository. The supported
recovery point is the last verified dump. Do not claim minute-level recovery
until binlog retention, off-host log shipping and a tested PITR drill exist.
That work requires a database-level decision and is marked **BLOCKED**.

---

## 5. Releases, upgrades and rollback

### Version identity

`VERSION` is the source of truth (`1.1.0` at the time of writing). The running
version, commit and build date are visible in **System status** and via
`flask version`. Docker image labels also carry them.

### Build a reproducible source release

```bash
RUN_TESTS=1 ./scripts/release.sh
```

The script refuses a dirty working tree, archives exactly `HEAD` with
`git archive`, and writes:

```
dist/bmshop-1.1.0.tar.gz
dist/bmshop-1.1.0.tar.gz.sha256
dist/bmshop-1.1.0.RELEASE.json
```

The archive contains only tracked files: no `.env`, `instance/`, backups,
`__pycache__` or untracked scratch data.

### Upgrade in production (non-destructive)

1. Take and **verify** a pre-update backup:
   ```bash
   docker compose exec app /app/scripts/backup.sh
   ./scripts/verify-backup.sh
   ```
2. Build the new image with the new version/commit labels.
3. Apply migrations explicitly:
   ```bash
   docker compose run --rm app flask db upgrade
   ```
4. Recreate only the application container:
   ```bash
   docker compose up -d --no-deps app
   ```
5. Verify:
   ```bash
   curl -fsS http://127.0.0.1:8080/healthz
   docker compose exec app flask doctor
   ```
   and sign in to the status page.

Never run `docker compose down -v` on a live installation. Never restore over the
live database without the explicit `YES` + `RESTORE` confirmation and a
pre-restore safety backup produced by `scripts/restore.sh`.

### Rollback

* **Application code only:** check out the previous commit or run the previous
  image tag, then `docker compose up -d --no-deps app`. The database stays as-is.
* **Database migration:** migrations are forward-only. If a release must be
  rolled back across a schema change, restore the verified pre-update backup in
  an isolated database first, validate it, and only then perform the confirmed
  restore. Expect downtime; do not improvise.

---

## 6. Data ownership and exit

* Business data lives in MySQL (`db-data`) and generated files in `app-instance`.
  Backups are ordinary SQL dumps plus a tar archive with checksums.
* The customer can request a verified backup at any time. The license file does
  not encrypt or withhold it.
* There is no vendor access built into the application. Remote support requires
  the customer to grant access to the host.
* Deleting the license file does not delete or degrade business data.

---

## 7. Known limitations of the commercial layer

| Item | Status | Note |
|---|---|---|
| Transparent license metadata | PASS | No enforcement anywhere |
| System status / install checks | PASS | `/settings/system`, `flask doctor` |
| Reproducible source archive | PASS | `scripts/release.sh` |
| Daily local backup schedule | PASS (script) | Install on the host with cron |
| Off-site backup | BLOCKED | Needs destination and credentials |
| Backup notifications | PASS (script) | Needs a webhook URL |
| PITR / binlog | BLOCKED | Not configured or tested |
| Multi-tenant SaaS | NOT SUPPORTED | One business per deployment |
| Online activation / DRM | NOT IMPLEMENTED | Deliberately absent |
| Payment gateway integration | NOT IMPLEMENTED | Out of scope |

Anything not listed as PASS must be described as a limitation in the commercial
offer, not implied as included.
