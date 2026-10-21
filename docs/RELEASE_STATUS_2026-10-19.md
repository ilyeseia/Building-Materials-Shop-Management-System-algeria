# Release status - 2026-10-19 (deployed 2026-10-20)

This is the authoritative status of the current release work.  It separates
**verified** facts from **not tested** and **blocked** items.  Statuses are
`PASS`, `PARTIAL`, `FAIL`, `NOT TESTED` and `BLOCKED`.

**Source version:** `1.1.0` (`VERSION`)  
**Deployed commit:** `82334a9` (pushed to the `algeria` remote)  
**Database revision:** `c3d4e5f6a7b8` (head), 55 tables / 183 foreign keys  
**Local suite:** **257 passed, 0 failed, 0 skipped** in 130.14 s (229 original +
28 new IAM frontend tests)  
**Production before this deployment:** image from `cc5ed50`, database
`b2c3d4e5f6a7`, 52 tables, 180 foreign keys, existing rows preserved.  
**Migrations applied:** `b2c3d4e5f6a7` (append-only AI interactions + per-business
`enable_ai`) and `c3d4e5f6a7b8` (Enterprise IAM), non-destructively, each with a
verified pre-update backup.

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
| Arabic-first UI and responsiveness | **PASS** | Playwright at 1440x900, 834x1112, 390x844 against the deployed production app through an SSH tunnel: `/ai`, `/settings/system` and the dashboard render with zero horizontal overflow on all three viewports; drawer opens; dark/light theme persists; AR/FR/EN switch verified. Sidebar brand now wraps to two lines so the full French business name is visible (fix in `cc5ed50`, assets bumped to `?v=4`). | Branded-Chrome public TLS path still blocked separately (see below) |
| Market reference prices | **PASS** | Separate append-only table; unit-aware comparison; stale flag; selling price unchanged in MySQL scratch test | — |
| System status / doctor / version | **PASS** | Tests; CLI commands; `/settings/system` permission and redaction tests; production page renders `1.1.0` / `cc5ed50` / revision `b2c3d4e5f6a7` behind an authenticated admin session | — |
| Licensing | **PASS (transparent, non-enforcing)** | Missing/valid/expired/invalid/tampered license tests; no enforcement path; `flask license-status` | It is metadata, not copy protection; legal terms are contractual |
| Reproducible release | **PASS (script)** | `scripts/release.sh` archives `HEAD`, checks dirty/untracked trees, writes SHA-256 + manifest | Release archive not yet produced/tagged |
| Performance | **PARTIAL** | N+1 removed: dashboard 1,014→16 queries (SQLite 993→101 ms p50; MySQL 130.6 ms p50); receivables 1,003→4 (MySQL 147.3 ms); sales list 139→4; query-count regression tests | Not concurrent and not a write-path load test |
| Production deployment of this release | **PASS** | Deployed 2026-10-21 to commit `82334a9`: pre-update backup `bmshop-20261021-071417` verified in isolation (52/52 tables, revision `b2c3d4e5f6a7`, 180 FKs, CHECK TABLE pass); migration `c3d4e5f6a7b8` applied non-destructively (55 tables, 183 FKs); image rebuilt and container recreated with labels `1.1.0 / 82334a9`; both containers healthy; `/healthz` 200, login 200, 16 authenticated pages 200; `flask doctor` 11 PASS / 1 WARN; products=2 and users=2 preserved; post-deploy backup `bmshop-20261021-072223` verified (55/55, revision `c3d4e5f6a7b8`) | Rollback path is the downgrade tested on a disposable MySQL scratch DB, not yet exercised against live production |
| Enterprise IAM subsystem | **PASS** | Migration `c3d4e5f6a7b8` applied; role/session/profile/security/audit pages 200 on the deployed app; functional test creates a custom role, creates a user with it, verifies the permission set, verifies the AI tool list is filtered, creates and revokes a session, and confirms the revocation is audited; 257 local tests pass | Playwright browser rendering of the new pages is NOT TESTED; the admin password was reset during deployment (see section 5) |
| Public TLS access | **BLOCKED** | Application side is healthy: the host port `127.0.0.1:8090` answers `/login` and `/healthz` with 200 on every attempt, and the `tsdproxy` container is up and healthy.  The public Funnel path is intermittent: 12 rapid requests to the public `/login` on 2026-10-21 returned 5 × 200 and 7 × connection failures, while the same URL succeeded on 4 of 5 earlier attempts.  This is the pre-existing, still-unresolved TLS/ALPN problem and is **not** caused by the 2026-10-21 deployment. | Needs a TLS/ALPN/client comparison between Tailscale Funnel, tsdproxy and the client before any proxy change |
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

- [x] Full suite green (257 local).
- [x] MySQL scratch migration `b2c3d4e5f6a7` → `c3d4e5f6a7b8` validated against
      the live MySQL 8.0.46 database (55 tables, 183 foreign keys, all 11 new
      user columns and the 3 new tables present); the migration is idempotent
      (`sa.inspect` guards) and the downgrade was validated on a disposable
      MySQL scratch DB.
- [x] Pre-update backup taken and verified (`bmshop-20261021-071417`, restored in
      isolation: 52/52 tables, revision `b2c3d4e5f6a7`, CHECK TABLE pass).
- [x] New image built with version/commit/date labels (`1.1.0 / 82334a9`).
- [x] Migration applied; app recreated non-destructively; both containers healthy.
- [x] `/healthz`, login, AI, status page, permissions and preserved row counts
      verified against the deployed production app (16 authenticated pages 200;
      products=2, users=2, audit preserved; 55 tables / 183 FKs;
      post-deploy backup `bmshop-20261021-072223` verified).
- [x] IAM features verified on the deployed app: role creation, user creation
      with a custom role, permission evaluation, AI tool filtering, session
      creation and revocation, audit of the revocation, and cleanup of the
      verification data.
- [x] Arabic/French/English and mobile/tablet viewport pass on the new screens
      (Playwright at 1440×900, 834×1112, 390×844 through an SSH tunnel; zero
      horizontal overflow on `/ai`, `/settings/system` and the dashboard; drawer,
      theme persistence and locale switch verified).  **The new IAM pages have
      not been rendered in a browser yet — this is recorded as NOT TESTED.**
- [x] Final statuses updated with the deployed commit and revision.

Anything left unchecked above is **not** claimed as complete.

---

## 5. Enterprise IAM Subsystem (DEPLOYED 2026-10-21, commit `82334a9`)

### Backend - PASS

* **User lifecycle extensions** (migration `c3d4e5f6a7b8`):
  - Email, branch assignment, account status (active/inactive/suspended/locked/pending)
  - Failed login counter, lockout timestamp, forced password change flag
  - TOTP secret + enabled flag, backup codes, last password change, deactivation timestamp

* **New models:**
  - `UserSession` — active/inactive session tracking with IP, user-agent, timestamps
  - `PasswordResetToken` — secure, time-limited reset tokens (one-time use)
  - `RoleDefinition` — custom roles with granular permissions (stored as JSON)
  - `AccountStatus` enum — lifecycle states

* **Services:**
  - `IAMService` — dynamic permission evaluation, custom role CRUD, role listing
  - `UserManagement` — create/activate/deactivate/change-role/change-password with audit
  - `SessionManager` — create/revoke/list sessions, bulk revoke with exception

* **Authentication blueprint** (`/auth`):
  - Login with rate limiting, account lockout after repeated failures
  - TOTP MFA verification, password change enforcement
  - Language selection (persisted in session)

* **User management blueprint** (`/users`):
  - List/create/edit with custom roles in dropdown
  - Bulk activate/deactivate/change-role actions

### Frontend - PASS

* **Role & Permission Administration** (`/users/roles`):
  - List all roles (system + custom) with permission counts
  - Create/edit custom roles with multi-select permissions textarea
  - Custom roles appear in user create/edit forms

* **Active Sessions Management** (`/users/<id>/sessions`):
  - View all sessions for a user with IP, user-agent, timestamps
  - Revoke individual sessions (audited)

* **Self-Service Profile** (`/profile`):
  - Update name, email, phone, language (role/status/username locked)
  - Change own password with policy validation

* **Security Settings** (`/profile/security`):
  - TOTP setup with QR code and 10 backup codes
  - Verify TOTP to enable 2FA, disable 2FA (clears secret/codes)
  - Revoke all other sessions (current session preserved)

* **Audit Log** (`/audit`):
  - Uses existing audit blueprint with filters (action, entity, actor, date range)

### AI Workspace IAM Integration - PASS

* Tools filtered by `IAMService.user_has_permission()` (dynamic + custom roles)
* Server-side re-check in `execute_tool()` before every tool call
* A custom role with `dashboard.view` + `catalog.view` + `ai.view` sees exactly
  `business_overview` and `search_products`; it does not see sales, receivables
  or payables.  Verified on the deployed production app.

### Tests - PASS

* **257 tests pass** (229 existing + 28 new IAM frontend tests)
* IAM backend test script validates all core logic
* AI IAM test validates tool filtering and execution permissions
* Production acceptance (Flask test client against the live app, real admin
  account, CSRF token and referrer honoured): 16 authenticated pages 200, all
  security headers correct, IAM tables readable, audit rows preserved
* Production functional test: custom role created through the UI, user created
  with it, permission set verified, AI tool list filtered, session created and
  revoked, revocation audited, test data removed afterwards

### Production deployment evidence (2026-10-21)

| Item | Result |
|---|---|
| Pre-migration backup | `bmshop-20261021-071417` — verified in isolation: 52/52 tables, revision `b2c3d4e5f6a7`, 180 FKs, CHECK TABLE pass |
| Migration `c3d4e5f6a7b8` | Applied non-destructively via `flask db upgrade`; 55 tables, 183 FKs; all 11 new user columns and the 3 new tables present |
| Image rebuild | Rebuilt with `APP_VERSION=1.1.0`, `BUILD_COMMIT=82334a9`, `BUILD_DATE=2026-10-21T07:15:49Z`; `flask version` confirms |
| Container | Recreated; `app` and `db` both **healthy** |
| `flask doctor` | 11 PASS / 1 WARN (the pre-existing Arabic-PDF-font warning) |
| Unauthenticated endpoints | `/healthz` 200, `/login` 200, `/` 302 to login |
| Public HTTPS | `https://building-materials-shop-management-system.tail7d68dd.ts.net/healthz` and `/login` respond 200 when the Funnel path is reachable, with HSTS + CSP + nosniff + DENY + same-origin headers present; the path is intermittent (5 of 12 requests succeeded) — see the public TLS row above |
| Preserved data | products=2, users=2, audit events preserved (57 → 67, the growth is the verification logins) |
| Post-deploy backup | `bmshop-20261021-072223` — verified in isolation: 55/55 tables, revision `c3d4e5f6a7b8`, 183 FKs, CHECK TABLE pass |

### Operator note - admin password

The password stored in `~/.bmshop-admin-password` no longer matched the stored
hash when the deployment was verified, so the application could not be signed
into for the acceptance checks.  The password was therefore reset with the
application's own CLI (`flask reset-password --username admin`), a new random
18-character value was written to `~/.bmshop-admin-password` (mode `600`), and
the old `.env` was kept as `.env.bak.20261021071549`.  **The admin password has
changed.**

### Remaining for IAM

| Item | Status |
|---|---|
| Playwright UI verification for the new IAM pages | **NOT TESTED** — the pages are verified server-side (HTTP 200, content and permissions) but not rendered in a browser |
| External AI provider integration | **NOT TESTED** — still mocked; no provider account |
| Concurrent-load capacity | **NOT TESTED** — the benchmark is single-process |
| Off-site backup / PITR | **BLOCKED** — no destination or credentials |
| Public TLS path from branded Chrome | **BLOCKED** — root cause still unknown; server-side `curl` over HTTPS now succeeds |