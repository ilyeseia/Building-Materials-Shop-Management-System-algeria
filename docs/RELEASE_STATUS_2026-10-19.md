# Release status - 2026-10-19

This is the authoritative status of the current release work.  It separates
**verified** facts from **not tested** and **blocked** items.  Statuses are
`PASS`, `PARTIAL`, `FAIL`, `NOT TESTED` and `BLOCKED`.

**Source version:** `1.1.0` (`VERSION`)  
**Local suite:** **229 passed, 0 failed, 0 skipped** in 78.64 s  
**Production before this deployment:** revision `7211b7a`, database
`a1b2c3d4e5f6`, 51 tables, existing rows preserved.  
**New migration:** `b2c3d4e5f6a7` (append-only AI interactions + per-business
`enable_ai`).

---

## 1. Status by area

| Area | Status | Verified evidence | Remaining limit |
|---|---|---|---|
| Authentication, RBAC, CSRF, headers | **PASS** | Security tests; production login/session cookie/CSP/HSTS/frame/referrer headers observed | Public HTTPS path has a separate TLS client failure (see below) |
| Stock ledger and explicit units | **PASS** | Unit/UoM tests; over-release now refuses instead of clamping; MySQL invariant checks | — |
| Sales, purchasing, payments, AR/AP | **PASS** | Full chain tests; two-process MySQL payment race drill (one allocation, one refusal); aging tests | No external payment gateway |
| Backup and restore | **PASS** | Verified production backup restored in isolation: 51/51 tables, 179 FKs, `CHECK TABLE` pass, zero stock/payment violations; destructive drill on disposable DB with safety backup and report | Off-site copy not configured; no PITR |
| Backup scheduling and alerts | **PARTIAL** | Host cron wrapper, `backup.env` support, rclone/webhook scripts, failure marker on status page | Schedule not installed on the host yet; no destination or webhook credentials |
| Read-only AI workspace | **PASS** | 13 tests: permissions, server-side tool re-check, no write tool, audit/append-only, provider failure fallback, tool-output boundary, disable switch persisted | External provider is mocked; no real provider account was tested |
| Arabic-first UI and responsiveness | **PASS** | Playwright at 1440x900, 834x1112, 390x844: no horizontal overflow on dashboard/products/market pages; drawer opens/closes; theme persistence; AR/FR/EN switch | New AI/status screens need the same final viewport pass |
| Market reference prices | **PASS** | Separate append-only table; unit-aware comparison; stale flag; selling price unchanged in MySQL scratch test | — |
| System status / doctor / version | **PASS** | Tests; CLI commands; `/settings/system` permission and redaction tests | Production deployment verification pending |
| Licensing | **PASS (transparent, non-enforcing)** | Missing/valid/expired/invalid/tampered license tests; no enforcement path; `flask license-status` | It is metadata, not copy protection; legal terms are contractual |
| Reproducible release | **PASS (script)** | `scripts/release.sh` archives `HEAD`, checks dirty/untracked trees, writes SHA-256 + manifest | Release archive not yet produced/tagged |
| Performance | **PARTIAL** | N+1 removed: dashboard 1,014→16 queries (SQLite 993→101 ms p50; MySQL 130.6 ms p50); receivables 1,003→4 (MySQL 147.3 ms); sales list 139→4; query-count regression tests | Not concurrent and not a write-path load test |
| Production deployment of this release | **PENDING** | Code, tests, MySQL migration and MySQL benchmark are complete; pre-update backup and schema migration required | Not deployed at the time of writing |
| Public TLS access | **BLOCKED** | Local `curl` and older Chromium 1179 succeed; branded Chrome and server-side `curl` fail with TLS/connection errors; certificate chain inspected but root cause unknown | Needs TLS/ALPN/client comparison before any proxy change |
| Off-site backup / PITR | **BLOCKED** | Scripts exist and are guarded | No destination, credentials, binlog retention or restore drill |

---

## 2. What changed in this release

### AI workspace

* `/ai` and rate-limited `/ai/ask`; fixed read-only tool registry.
* Tools filtered by the signed-in user's role and re-checked inside
  `execute_tool`; no write tool exists.
* Local deterministic provider is the default and makes no network calls.
* Optional OpenAI-compatible provider uses environment-only credentials; any
  failure falls back locally and is recorded as `unavailable`.
* Append-only `ai_interactions` table plus normal audit event.
* Per-business switch (`BusinessSettings.enable_ai`) and global `FEATURE_AI`.
* The AI read path inherited the reporting N+1; it is fixed by the same batch.

### Operations and commercial layer

* `/settings/system`, `flask doctor`, `flask version`, `flask license-status`.
* Optional transparent license JSON; no lockout, no telemetry, no remote kill.
* Version/commit/build labels, `VERSION`, `scripts/release.sh`.
* Host backup cron wrapper, `backup.env`, rclone off-site copy, webhook alerts,
  failure marker, backup freshness on the status page.

### Performance

* `reporting.py` now aggregates payment allocations and credit notes once for a
  page instead of calling `invoice.paid_amount` / `credited_amount` per invoice.
* `invoice_settlements()` batches the sales list.
* Two regression tests assert the dashboard stays under 25 statements for 15
  invoices and the sales settlement batch stays at four statements.

---

## 3. Evidence and limitations

* **Test suite:** SQLite in-memory/file database with `db.create_all()`; this is
  not a MySQL test of every code path.
* **MySQL checks:** migrations, catalog pages, market prices, payment locking and
  backup/restore were executed against disposable MySQL databases; the new AI
  migration is validated before deployment.
* **Performance:** the benchmark uses Flask's test client in one process and a
  synthetic dataset on both SQLite and MySQL 8.0.46.  It excludes network, TLS,
  proxy and browser rendering.  Absolute MySQL latency is measured, but
  concurrent capacity is not claimed.
* **AI provider:** no external provider account exists.  The fallback and
  boundary are tested with a fake client; the real provider integration is
  **NOT TESTED**.
* **Backups:** local backup/restore is verified.  Off-site and point-in-time
  recovery are **BLOCKED** until a destination and credentials exist.
* **Licensing:** transparent metadata only.  It cannot and does not disable the
  application.
* **TLS:** the public URL is reachable with local `curl` and older Chromium 1179,
  but not with branded Chrome or server-side `curl`.  No production proxy
  configuration was changed while the cause is unknown.

---

## 4. Release gate

Before calling the release deployed:

- [x] Full suite green (229 local).
- [x] MySQL scratch migration `a1b2c3d4e5f6` → `b2c3d4e5f6a7` validated
      (52 tables, 180 foreign keys, expected AI columns).
- [x] MySQL scratch benchmark recorded (8 iterations; 300/500/1,000 volumes).
- [ ] Pre-update backup taken and verified.
- [ ] New image built with version/commit/date labels.
- [ ] Migration applied; app recreated non-destructively.
- [ ] `/healthz`, login, AI, status page, permissions and preserved row counts
      verified.
- [ ] Arabic/French/English and mobile/tablet viewport pass on the new screens.
- [ ] Final statuses updated with the deployed commit and revision.

Anything left unchecked above is **not** claimed as complete.
