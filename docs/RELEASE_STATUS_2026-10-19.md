# Release status - 2026-10-19 (updated 2026-10-22)

This is the authoritative status of the current release work.  It separates
**verified** facts from **not tested** and **blocked** items.  Statuses are
`PASS`, `PARTIAL`, `FAIL`, `NOT TESTED` and `BLOCKED`.

**Source version:** `1.1.0` (`VERSION`)  
**Deployed code commit:** `a971706` (pushed to the `algeria` remote; the image
label `org.opencontainers.image.revision` confirms it)  
**Database revision:** `e5f6a7b8c9d0` (head), 57 tables / 184 foreign keys  
**Local suite:** **322 passed, 0 failed, 0 skipped** in 362.92 s (2 pre-existing
Flask-Login deprecation warnings)  
**Production before this deployment:** image from `ce96e0a`, database
`e5f6a7b8c9d0`, 57 tables, 184 foreign keys, products=2, users=2, existing rows
preserved.  
**Production after this deployment:** image from `a971706`, same database
revision, 57 tables / 184 foreign keys, products=2, users=2, 29 append-only AI
interaction rows, both containers healthy, `flask doctor` 11 PASS / 1 WARN.
The public HTTPS path passed a browser login + in-place-answer check after the
deploy, and a login + `/ai` page check after the funnel reinstall; the earlier
intermittency is recorded as a historical limitation (section 9).

**Migrations applied (historical):** `b2c3d4e5f6a7` (append-only AI interactions
+ per-business `enable_ai`), `c3d4e5f6a7b8` (Enterprise IAM),
`d4e5f6a7b8c9` (AI provider registry) and `e5f6a7b8c9d0` (provider error
detail), non-destructively, each with a verified pre-update backup.  The
in-place-answer change `a971706` adds **no migration**.

---

## 1. Status by area

| Area | Status | Verified evidence | Remaining limit |
|---|---|---|---|
| Authentication, RBAC, CSRF, headers | **PASS** | Security tests; production login/session cookie/CSP/HSTS/frame/referrer headers observed | The public path passes the current checks; the earlier TLS intermittency is historical (section 9) |
| Stock ledger and explicit units | **PASS** | Unit/UoM tests; over-release now refuses instead of clamping; MySQL invariant checks | — |
| Sales, purchasing, payments, AR/AP | **PASS** | Full chain tests; two-process MySQL payment race drill (one allocation, one refusal); aging tests | No external payment gateway |
| Backup and restore | **PASS** | Verified production backup restored in isolation: 51/51 tables, 179 FKs, `CHECK TABLE` pass, zero stock/payment violations; destructive drill on disposable DB with safety backup and report | Off-site copy not configured; no PITR |
| Backup scheduling and alerts | **PARTIAL** | Host cron wrapper, `backup.env` support, rclone/webhook scripts, failure marker on status page | Schedule not installed on the host yet; no destination or webhook credentials |
| AI workspace (local + registered providers) | **PASS** | Per-question target selector, multi-turn history, opt-in general mode, collapsible tool trace; real OpenRouter MiniMax answers in production through the in-place fetch path (interactions 24 and 27, 7.9 s / 7.0 s, status `ok`) and a later real user question (interaction 29, "سلام", 1560 ms); the provider test lists 468 models and the MiniMax chat test returns in 993 ms; local fallback and provider failure recording verified; the async path is browser-tested over both the SSH tunnel and the public HTTPS URL, and the no-JavaScript redirect path is covered by the route tests (section 8) | Nemotron can hit an upstream 502/capacity limit; concurrent load is **NOT TESTED**; public TLS has a historical intermittency (section 9) |
| Arabic-first UI and responsiveness | **PASS** | Production Playwright at 1440×900, 834×1112 and 390×844 through an SSH tunnel: `/ai` renders the in-place answer/history and has zero horizontal overflow at all three widths; the async flow was also exercised over the public HTTPS URL at 1440 and 390 with screenshots; dark/light theme persists and AR/FR/EN parity is enforced by the translation tests.  Current cache-busting assets are `app.css?v=7` and `app.js?v=5`.  The earlier sidebar-brand fix (`cc5ed50`) remains in place. | The public path passes the current checks; the earlier TLS intermittency is a historical limitation (section 9) |
| Market reference prices | **PASS** | Separate append-only table; unit-aware comparison; stale flag; selling price unchanged in MySQL scratch test | — |
| System status / doctor / version | **PASS** | Tests; CLI commands; `/settings/system` permission and redaction tests; deployed `flask version` reports `1.1.0 / a971706 / 2026-10-21T12:48:14Z`, `flask db current` reports `e5f6a7b8c9d0`, and `flask doctor` reports 11 PASS / 1 WARN (pre-existing Arabic-PDF-font warning) | — |
| Licensing | **PASS (transparent, non-enforcing)** | Missing/valid/expired/invalid/tampered license tests; no enforcement path; `flask license-status` | It is metadata, not copy protection; legal terms are contractual |
| Reproducible release | **PASS (script)** | `scripts/release.sh` archives `HEAD`, checks dirty/untracked trees, writes SHA-256 + manifest | Release archive not yet produced/tagged |
| Performance | **PARTIAL** | N+1 removed: dashboard 1,014→16 queries (SQLite 993→101 ms p50; MySQL 130.6 ms p50); receivables 1,003→4 (MySQL 147.3 ms); sales list 139→4; query-count regression tests | Not concurrent and not a write-path load test |
| Production deployment of this release | **PASS** | Deployed 2026-10-21 to commit `a971706` (no migration): pre-deploy backup `pre-async-20261021` (JSON 268 rows / 20 AI interactions; SQL `bmshop-20261021-124255`), post-deploy backup `post-async-20261021` (JSON 290 rows / 27 AI; SQL `bmshop-20261021-125307`) and final post-acceptance backup `final-async-20261021` (JSON 294 rows / 28 AI; SQL `bmshop-20261021-130236`), all checksum- and gzip-verified with the dump-complete marker; after the funnel label verification and force-recreate (section 9), pre-reinstall `pre-funnel-reinstall-20261022` (JSON 307 rows / 29 AI; SQL `bmshop-20261022-071633`) and post-reinstall `post-funnel-reinstall-20261022` (JSON 309 rows / 29 AI; SQL `bmshop-20261022-072012`) were also verified; image labels `1.1.0 / a971706 / 2026-10-21T12:48:14Z`; app recreated non-destructively; both containers healthy; `/healthz` 200; login and browser acceptance passed over both the SSH tunnel and the public HTTPS URL; database unchanged at 57 tables / 184 FKs, products=2, users=2, 29 AI interactions, revision `e5f6a7b8c9d0` | The live rollback path was not exercised in this deployment (no migration, so rollback is image-only); the earlier public TLS intermittency is a historical limitation (section 9) |
| Enterprise IAM subsystem | **PASS** | Migration `c3d4e5f6a7b8` applied; role/session/profile/security/audit pages 200 on the deployed app; functional test creates a custom role, creates a user with it, verifies the permission set, verifies the AI tool list is filtered, creates and revokes a session, and confirms the revocation is audited; the current full suite has 322 passing tests | Playwright browser rendering of the new IAM pages is **NOT TESTED**; the admin password reset note in section 5 is historical |
| Sign-out ends the session | **PASS (fixed)** | Four defects found and fixed (see section 6).  Verified in a real browser against the deployed app with "remember me" ticked: the `remember_token` cookie is deleted, the dashboard redirects to the login page, and signing in again works.  Verified over real HTTP with a cookie jar as well. | `GET /logout` is still accepted for backward compatibility and is therefore a low-severity forced-logout vector; the application's own UI uses `POST` with a CSRF token |
| AI provider administration | **PASS** | Migrations `d4e5f6a7b8c9` and `e5f6a7b8c9d0` applied; provider id=3 OpenRouter is configured, the key is stored encrypted and never rendered; the live provider test listed **468 models** (audit event 177) and the MiniMax chat test returned in **993 ms** (audit event 178); real answers arrived through both the server-rendered form and the in-place fetch path (interactions 24, 27 and 29) | Nemotron can fail upstream with a capacity 502 (recorded fallback); free-tier latency/availability has no SLA; the public Funnel path was re-registered on 2026-10-22 (section 9) |
| Public TLS access | **PASS (current verification)** | Application side is healthy: the host port `127.0.0.1:8090` answers `/login` and `/healthz` with 200, and the `tsdproxy` container is up and healthy.  Earlier on 2026-10-21 the public path was intermittent: 12 rapid requests to `/login` returned 5 × 200 and 7 × connection failures.  After the `a971706` deploy, 12/12 `/healthz` and a public Chromium login + in-place local answer passed.  After the 2026-10-22 funnel reinstall (section 9), `tsdproxy.funnel: "true"` was verified on the recreated container, tsdproxy re-registered the proxy, **6/6** `/healthz` and **3/3** `/login` returned 200, and a public Chromium login + `/ai` page passed in a secure context with **0 console errors and 0 failed responses**. | The earlier 5/12 failure was not root-caused and no long soak was run; this is a current PASS, not a proof that the old TLS/ALPN fault cannot recur.  The SSH tunnel remains the deterministic acceptance path. |
| Off-site backup / PITR | **BLOCKED** | Scripts exist and are guarded | No destination, credentials, binlog retention or restore drill |

---

## 2. What changed in this release

### AI workspace

* `/ai` is a flexible workspace: the user chooses the target per question
  (automatic/default, any active registered model, or the deterministic local
  assistant), switches to opt-in general mode, sees a collapsible tool trace,
  and keeps a multi-turn history (the last three exchanges are sent as context).
* In-place answers: the composer posts to the rate-limited `/ai/ask` with
  `fetch`; the server returns JSON when `X-Requested-With: fetch` or `Accept`
  prefers JSON, and the page replaces the latest card and prepends the exchange
  to the history without a reload.  The existing redirect/flash path is kept for
  clients without JavaScript.
* The JSON response carries the translated status/provider/model/latency, tool
  calls, fallback notices and manager-only scrubbed provider detail; the
  browser clones inert `<template>` markup and fills all user/provider text
  with `textContent`, never `innerHTML`.
* The same CSRF, rate-limit, permission and append-only audit checks apply to
  both paths.  A network failure, HTTP error or provider fallback is shown in
  place and the composer is restored; only success clears the question box.
* The registered default is tried first for `auto`; if every provider fails, the
  local deterministic assistant answers and the interaction is recorded as
  `unavailable` with the scrubbed error.
* Provider tool turns are sent as user-role `TOOL RESULT` messages, which
  OpenRouter accepts (fix `ce96e0a`); an empty completion is treated as a
  failure instead of a blank answer.  Local deterministic tools remain
  permission-filtered and re-checked server-side; no write tool exists.
* Append-only `ai_interactions` table (provider, model, latency, tool calls,
  error detail) plus normal audit events, the per-business switch
  (`BusinessSettings.enable_ai`) and global `FEATURE_AI`.
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

* **Test suite:** SQLite in-memory/file database with `db.create_all()`; the
  current suite is **322 passed, 0 failed, 0 skipped** in 362.92 s.  This is not
  a MySQL test of every code path.
* **MySQL checks:** migrations, catalog pages, market prices, payment locking and
  backup/restore were executed against disposable MySQL databases.  The current
  `a971706` change adds no migration; production remains at
  `e5f6a7b8c9d0`.
* **Performance:** the benchmark uses Flask's test client in one process and a
  synthetic dataset on both SQLite and MySQL 8.0.46.  It excludes network, TLS,
  proxy and browser rendering.  Absolute MySQL latency is measured, but
  concurrent capacity is not claimed.
* **AI provider:** the real OpenRouter MiniMax path is exercised in production
  (interactions 24 and 27; 7.9 s and 7.0 s; status `ok`; plus the later real
  user question 29 in 1560 ms), the live provider test listed 468 models and
  the MiniMax chat test returned in 993 ms.  The free
  Nemotron model can still be rejected upstream with a capacity 502; auto/local
  fallback records the scrubbed error (interaction 17).  Concurrent load is
  **NOT TESTED**.
* **AI browser flow:** local and production Playwright runs exercised the
  no-reload answer, history update/10-turn cap, localized network and HTTP
  errors, Arabic RTL layout and 1440/834/390 widths.  The tunnel run wrote
  interactions 24-27 and provider/model audit events 177-178; the public HTTPS
  run wrote interaction 28.  Interaction 29 (`"سلام"`, OpenRouter MiniMax,
  1560 ms) was a later real user question, preserved across the reinstall.
* **Backups:** the pre-, post- and final backups for `a971706` were checksum-
  and gzip-verified (`pre-async-20261021`, `post-async-20261021`,
  `final-async-20261021`, plus the 2026-10-22 `pre-funnel-reinstall-20261022`
  and `post-funnel-reinstall-20261022`); none was restored into a disposable
  database in this code-only deployment.  Off-site and point-in-time recovery
  remain **BLOCKED**.
* **Licensing:** transparent metadata only.  It cannot and does not disable the
  application.
* **TLS:** after the 2026-10-22 funnel reinstall, the public URL answered
  **6/6** `/healthz` and **3/3** `/login` requests with 200, and a public
  Playwright login + `/ai` page passed in a secure context with 0 console
  errors and 0 failed responses.  An earlier 5/12 burst failed at the TLS layer;
  its root cause was not established, no long soak was run and branded Chrome
  was not separately re-exercised.

---

## 4. Release gate

Before calling `a971706` deployed:

- [x] Full suite green: **322 passed, 0 failed, 0 skipped** (362.92 s).
- [x] No migration in this change; production stayed at `e5f6a7b8c9d0`
      (57 tables, 184 foreign keys).  The earlier migration chain and backups
      are recorded in the header and sections 5/7.
- [x] Pre-deploy backup `pre-async-20261021` (JSON 268 rows / 20 AI
      interactions, 56 exported tables; SQL `bmshop-20261021-124255`) copied to
      `/home/sysuser/backups/` and verified: SHA-256 OK, gzip OK,
      `-- Dump completed` present.
- [x] Image built with labels `1.1.0 / a971706 / 2026-10-21T12:48:14Z`.
- [x] App recreated non-destructively (`docker compose up -d --no-deps app`);
      app and db containers healthy; `/healthz` 200.
- [x] Database preserved: 57 tables, 184 FKs, products=2, users=2,
      AI interactions append-only (29, including the real user question 29
      preserved across the reinstall), revision `e5f6a7b8c9d0`.
- [x] Production acceptance: admin login, `/ai` async answer, local target,
      real OpenRouter MiniMax answer through the JSON path, tool trace,
      network/HTTP error handling in place, 10-turn cap, 1440/834/390 no
      overflow, provider `/models` test (468 models, audit 177) and MiniMax
      chat test (993 ms, audit 178).
- [x] Post-deploy backup `post-async-20261021` (JSON 290 rows / 27 AI
      interactions; SQL `bmshop-20261021-125307`) copied to
      `/home/sysuser/backups/` and verified: SHA-256 OK, gzip OK,
      `-- Dump completed` present.
- [x] Final acceptance backup `final-async-20261021` (JSON 294 rows / 28 AI;
      SQL `bmshop-20261021-130236`), then the 2026-10-22 funnel reinstall
      backups `pre-funnel-reinstall-20261022` (JSON 307 rows / 29 AI; SQL
      `bmshop-20261022-071633`) and `post-funnel-reinstall-20261022` (JSON
      309 rows / 29 AI; SQL `bmshop-20261022-072012`) copied and verified:
      SHA-256 OK, gzip OK, `-- Dump completed` present.
- [x] `tsdproxy.funnel: "true"` present in the compose file and on the running
      container; the app was force-recreated and tsdproxy re-registered the
      proxy; public checks were 6/6 `/healthz`, 3/3 `/login` and a public
      Chromium login + `/ai` page (section 9).
- [x] Arabic/French/English parity and the no-JavaScript fallback are covered
      by tests.  **The new IAM pages are still not rendered in a browser —
      recorded as NOT TESTED.**
- [x] Final statuses updated with `a971706` and the production evidence.

Anything left unchecked above is **not** claimed as complete.  The outstanding
release-level items are a long TLS soak, off-site backup/PITR and
concurrent-load capacity, recorded below.  The public path itself passes the
current checks.

---

## 5. Enterprise IAM Subsystem (deployed 2026-10-21, commit `82334a9`; historical)

This section records the IAM deployment.  The current deployment is `a971706`;
see sections 1, 4 and 8 for its status.

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

* **The IAM work added 28 tests at the time; the current full suite is 322
  passing tests** (229 original + 28 IAM frontend tests + 10 logout tests + 39
  provider tests + later workspace/async tests)
* IAM backend test script validates all core logic
* AI IAM test validates tool filtering and execution permissions
* Production acceptance (Flask test client against the live app, real admin
  account, CSRF token and referrer honoured): 16 authenticated pages 200, all
  security headers correct, IAM tables readable, audit rows preserved
* Production functional test: custom role created through the UI, user created
  with it, permission set verified, AI tool list filtered, session created and
  revoked, revocation audited, test data removed afterwards

### Production deployment evidence (2026-10-21, historical for `82334a9`)

This table is the evidence for the IAM deployment.  It is kept for the audit
trail; the current deployment is recorded in sections 1 and 8.

| Item | Result |
|---|---|
| Pre-migration backup | `bmshop-20261021-071417` — verified in isolation: 52/52 tables, revision `b2c3d4e5f6a7`, 180 FKs, CHECK TABLE pass |
| Migration `c3d4e5f6a7b8` | Applied non-destructively via `flask db upgrade`; 55 tables, 183 FKs; all 11 new user columns and the 3 new tables present |
| Image rebuild | Rebuilt with `APP_VERSION=1.1.0`, `BUILD_COMMIT=82334a9`, `BUILD_DATE=2026-10-21T07:15:49Z`; `flask version` confirms |
| Container | Recreated; `app` and `db` both **healthy** |
| `flask doctor` | 11 PASS / 1 WARN (the pre-existing Arabic-PDF-font warning) |
| Unauthenticated endpoints | `/healthz` 200, `/login` 200, `/` 302 to login |
| Public HTTPS | `https://building-materials-shop-management-system.tail7d68dd.ts.net/healthz` and `/login` respond 200 when the Funnel path is reachable, with HSTS + CSP + nosniff + DENY + same-origin headers present.  At the time of this IAM deployment the path was intermittent (5 of 12 requests succeeded); the current public path passes the checks in sections 1 and 9 |
| Preserved data | products=2, users=2, audit events preserved (57 → 67, the growth is the verification logins) |
| Post-deploy backup | `bmshop-20261021-072223` — verified in isolation: 55/55 tables, revision `c3d4e5f6a7b8`, 183 FKs, CHECK TABLE pass |

### Operator note - admin password

The password stored in `~/.bmshop-admin-password` no longer matched the stored
hash when the deployment was verified, so the application could not be signed
into for the acceptance checks.  The password was therefore reset with the
application's own CLI (`flask reset-password --username admin`), a new random
18-character value was written to `~/.bmshop-admin-password` (mode `600`), and
the old `.env` was kept as `.env.bak.20261021071549`.  **The admin password
changed at that deployment.**  The `a971706` deployment used the same
credentials and did **not** reset the password.

### Remaining for IAM

| Item | Status |
|---|---|
| Playwright UI verification for the new IAM pages | **NOT TESTED** — the pages are verified server-side (HTTP 200, content and permissions) but not rendered in a browser |
| External AI provider integration | **PASS** (later deployment `a971706`) — real OpenRouter MiniMax answers (interactions 24, 27 and the real user question 29), provider `/models` test 468 models, MiniMax chat test 993 ms |
| Concurrent-load capacity | **NOT TESTED** — the benchmark is single-process |
| Off-site backup / PITR | **BLOCKED** — no destination or credentials |
| Public TLS path from branded Chrome | **NOT TESTED** (branded Chrome) — the public path passes Chromium/Playwright and curl checks (sections 1 and 9); the earlier intermittency was not root-caused |

---

## 6. Sign-out fix (deployed 2026-10-21, commits `ce49c2d` and `54e54f5`)

The operator reported that signing out did not sign out: the account stayed
signed in and the tracked session kept running.  Four separate defects were
behind it.  All four are fixed and verified on the deployed application.

### 6.1 The defects

| # | Defect | Effect |
|---|---|---|
| 1 | Flask-Login marks the persistent "remember me" cookie for deletion with `session["_remember"] = "clear"`.  The logout view called `session.clear()` straight afterwards, which **wiped that marker**, so the cookie was never deleted.  On the next request Flask-Login found the cookie, `session.get("_remember")` was no longer `"clear"`, and it **silently signed the user back in**. | Clicking sign-out left the browser authenticated.  This is the reported symptom. |
| 2 | `SessionManager.revoke_session()` raises `BusinessError("session_not_found")` when the tracked row is missing.  The logout view called it unguarded, so the exception aborted the route with a **500 error** before `logout_user()` and `session.clear()` were reached. | The sign-out failed with an error page and the user stayed signed in. |
| 3 | `change_password()` used `session.sid` to keep the current session alive.  Flask's `SecureCookieSession` has no `sid` attribute, so this raised `AttributeError` on **every** password change. | Changing a password always ended in a 500 error. |
| 4 | Revoking a session from the sessions page only greyed out a row in a table.  **Nothing checked the tracked session on a request**, so a revoked session kept working. | The session management screen had no effect. |

Defect 1 also existed in the "deactivated account must lose access immediately"
path in `app/__init__.py`, where a disabled account was signed out but its
remember cookie survived - the account was immediately signed back in.

### 6.2 The fixes

* `logout()` re-applies `session["_remember"] = "clear"` after clearing the
  session, so Flask-Login's `after_request` hook really deletes the cookie.
  The same correction is applied to the deactivated-account path.
* `SessionManager.revoke_session_if_present()` never raises; a missing row
  already means the desired end state.
* `change_password()` uses the tracked session key, which also keeps the
  session that made the change alive.
* A `before_request` hook checks the tracked session on every request and
  signs the user out when the session has been revoked.
* `SessionManager.create_session()` is now an upsert, so a re-used session key
  can no longer violate the unique index and break a second sign-in.
* `SessionManager.check_and_touch()` performs the check and the "last seen"
  stamp in **one** query, and only writes the stamp once it is a minute old.

### 6.3 Verification

| Check | Result |
|---|---|
| New automated tests | 10 in `tests/test_logout.py`; **266 local tests passed at the time** (the current suite is 322) |
| Query cost | A dashboard request stays at 17-20 queries; a regression guard asserts it stays under 30 |
| Real HTTP against the deployed server (cookie jar) | `remember_token` set on sign-in with "remember me" ticked, **removed** after sign-out, dashboard then redirects to `/login`, tracked session row `is_active = 0` |
| Real browser against the deployed app (Playwright, 1440×900) | Signed in with "remember me" ticked, sign-out button pressed, redirected to `/login`, `remember_token` cookie **deleted**, dashboard not reachable, signing in again works, **0 console/page errors** |
| Deployment | Pre-deploy backup `bmshop-20261021-075443`, post-deploy backup `bmshop-20261021-080148` (55/55 tables, revision `c3d4e5f6a7b8`, CHECK TABLE pass) |
| Release identity | `flask version` reports `1.1.0 / 54e54f5`; `flask doctor` 11 PASS / 1 WARN |

### 6.4 Known remaining item

`GET /logout` is still accepted for backward compatibility, so a third-party
page could force a sign-out (a nuisance, not a data leak).  The application's
own UI uses `POST` with a CSRF token, and `POST` is protected.  Restricting the
route to `POST` alone was not done because it would change an existing,
documented behaviour.

---

## 7. AI provider and model administration (deployed 2026-10-21; commits `56bf190` → `a971706`)

The assistant could only talk to one endpoint, fixed by environment variables.
This release adds a registry an administrator drives from the interface.  The
provider registry was introduced in `56bf190`/`670781b`; the flexible workspace
and in-place answers that use it are in section 8.

### 7.1 The interface

| Page | What it does |
|---|---|
| `/ai/providers` | The registered providers, the endpoint currently in use, and a catalogue of known OpenAI-compatible gateways |
| `/ai/providers/new` | Add a provider: pick a catalogue entry, paste the key, choose the default |
| `/ai/providers/<id>/edit` | Change the name, the base URL, the key (an empty box keeps the stored one), the active and default flags |
| `/ai/providers/<id>/test` | Reach the provider, list the models it offers, and report the latency |
| `/ai/models` | The models of each provider, with the default one per provider |
| `/ai/models/new`, `/ai/models/<id>/edit` | Register a model identifier, its label, context window, output limit, tool support and free-quota flag |

The catalogue covers the gateways that give a usable quota without a paid
subscription - OpenRouter (including the `:free` models), Groq, Google AI
Studio, Mistral, Cerebras, Together AI, SiliconFlow - the paid ones, and the
runtimes that need no key at all (Ollama, LM Studio).  Each entry lists the
model identifiers that are known to work and a link to the sign-up page.

### 7.2 How the credential is protected

* The API key is encrypted with **Fernet** (AES-CBC + HMAC-SHA256) using a
  key derived from `SECRET_KEY` with PBKDF2-HMAC-SHA256 and 200 000 rounds.
* The plaintext key is never rendered, never logged, never written to the
  audit trail and never included in an exception message.  Only a four
  character hint is kept in clear so two keys can be told apart.
* A rotated `SECRET_KEY` makes a stored key unreadable.  That is reported as
  "please enter it again" instead of crashing the page.
* The new `ai.manage` permission is administrator-only, and every create,
  update, delete, default and test is audited.

### 7.3 How the assistant uses the registry

* The registered providers are tried in order, the default one first; the
  first that answers wins.  If every one fails, the deterministic local
  assistant answers and the failure is recorded.
* The legacy `AI_PROVIDER` / `AI_API_KEY` configuration keeps working and is
  shown in the interface as the environment provider.
* The tool list is still filtered by the user's permissions, and the
  permission is re-checked server-side before every tool call.

### 7.4 Two defects the browser check found

| Defect | Effect |
|---|---|
| The delete forms used `{{ csrf_token() }}` on its own.  Flask-WTF renders the token as *text* in that case, so the form carried no `csrf_token` field and every delete was rejected with a **400**. | The delete buttons did nothing.  The four security-page forms (enable, verify and disable 2FA, revoke all sessions) had no CSRF field at all. |
| The templates used inline event handlers (`onsubmit="return confirm(...)"`, `onchange="this.form.submit()"`).  The strict `script-src 'self'` policy blocks them. | The delete confirmation never appeared and the provider filter did not work; the browser logged CSP violations. |

Both are fixed: the forms send a hidden `csrf_token` input and use the
`data-confirm` / `data-autosubmit` attributes that `app.js` already handles.
Two new tests fail the build if either pattern comes back.

### 7.5 Verification

| Check | Result |
|---|---|
| New automated tests | 39 provider tests in `tests/test_ai_providers.py` and 25 AI workspace tests in `tests/test_ai.py` (including 4 async/JSON/template tests); **322 local tests pass** |
| MySQL scratch migration | The whole provider chain applied to a disposable MySQL 8 database: 57 tables, 184 foreign keys, CHECK TABLE pass, insert/read works, the delete cascades, and the downgrade and re-upgrade both succeed.  The current `a971706` change adds no migration |
| Authenticated acceptance on the deployed app | The historical provider-registry acceptance (all five pages 200, encrypted key, catalogue, delete) passed.  On the current deployment the live provider test listed **468 models** (audit event 177) and the MiniMax chat test returned in **993 ms** (audit event 178) |
| Real browser (Playwright, deployed app) | The earlier provider-admin browser checks passed (catalogue, pre-filled form, encrypted key not in the page, delete buttons, zero overflow at 1440/834/390, 0 console errors/failed responses).  On `a971706` the browser exercised the async composer, a real OpenRouter answer, the tool trace, in-place errors and the provider/model test buttons, with screenshots at 1440/834/390; the same flow passed over public HTTPS, and the public post-reinstall login + `/ai` page passed after the funnel re-registration |
| Backups | `a971706`: pre `pre-async-20261021` (JSON 268 rows / 20 AI; SQL `bmshop-20261021-124255`), post `post-async-20261021` (JSON 290 rows / 27 AI; SQL `bmshop-20261021-125307`) and final `final-async-20261021` (JSON 294 rows / 28 AI; SQL `bmshop-20261021-130236`); reinstall pair `pre-funnel-reinstall-20261022` (307 rows / 29 AI; SQL `bmshop-20261022-071633`) and `post-funnel-reinstall-20261022` (309 rows / 29 AI; SQL `bmshop-20261022-072012`); SHA-256 and gzip verified.  Earlier provider-admin backups `bmshop-20261021-095621` / `-100132` remain historical |
| Release identity | `flask version` reports `1.1.0 / a971706 / 2026-10-21T12:48:14Z`; `flask db current` reports `e5f6a7b8c9d0`; `flask doctor` 11 PASS / 1 WARN |

### 7.6 An operational note about the one-off migration container

`docker compose run --rm app flask db upgrade` inherits the service labels,
including the `tsdproxy.*` ones.  tsdproxy then re-points the public proxy at
that throw-away container, and the public path breaks with a 502 when the
container is removed.  The migration was applied that way during the
validation, and the proxy was restored by recreating the application
container so that tsdproxy registered it again.

**Use `docker compose exec app flask db upgrade` on a container that already
runs the new image, or recreate the application container right after a
one-off run.** This is a host-level tsdproxy behaviour, not an application
defect.

### 7.7 Remaining for the AI feature

| Item | Status |
|---|---|
| A real external provider call | **PASS** — OpenRouter MiniMax answered in production through the server-rendered form and the in-place fetch path (interactions 24 and 27; 7901 ms and 6968 ms; status `ok`) and a later real user question (interaction 29, 1560 ms); the MiniMax chat test returned in **993 ms** (audit 178) |
| The `/models` listing against a real gateway | **PASS** — the provider test reached OpenRouter and listed **468 models** (audit 177) |
| The in-place async workspace | **PASS** — local, SSH-tunnel and public-HTTPS Playwright runs; see section 8 |
| Concurrent load with an external provider | **NOT TESTED** |
| Nemotron free-model reliability | **PARTIAL** — the free Nemotron model can return an upstream 502/capacity error; auto/local fallback works and records the scrubbed error (interaction 17).  MiniMax is the working default |

---

## 8. Flexible workspace and in-place answers (deployed 2026-10-21, commit `a971706`)

### 8.1 What changed

The workspace is no longer a single fixed provider and a single question.  On
top of the provider registry (`56bf190`, `670781b`) and the flexible workspace
(`aa3c66c`), `ce96e0a` made provider tool turns OpenRouter-compatible and
`a971706` made the answer appear on the same page.

* **Per-question choice:** automatic/default, any active registered model, or
  the deterministic local assistant.  A conversation keeps the last three
  exchanges as context; the interface lists the last ten exchanges and trims
  the oldest after ten.
* **General mode:** an explicit opt-in lets the assistant answer a general
  question without business tools.  It is off by default and labelled in the
  interface.
* **In-place answer:** the form is enhanced with `fetch`; `/ai/ask` returns
  `200 application/json` with `ok`, `interaction`, `notices` and a manager-only
  `error_detail` when the request asks for JSON.  The latest card and the first
  history turn are replaced/prepended from inert `<template>` elements; no page
  reload occurs.  The URL is updated with `history.replaceState`
  (`?interaction=<id>`), so a manual reload still shows the same interaction.
* **Progressive enhancement:** without JavaScript, the same form posts
  normally and keeps the redirect/flash behaviour.  The async path applies the
  same CSRF token, rate limit, permissions and append-only interaction write.
* **Errors and state:** pending spinner, disabled submit, `aria-busy`,
  `aria-live`, localized network/HTTP messages, full control restoration, and
  clearing the question box only after success.
* **Tool protocol:** provider tool results are sent as user-role messages with
  a `TOOL RESULT` prefix; this avoids the OpenRouter rejection of a `tool` role
  without a matching tool call.  Empty completions are treated as failures.
* **Safety:** all server/LLM text is inserted with `textContent`; the templates
  contain no user data.  Provider error detail is scrubbed of secrets and only
  rendered for users with `ai.manage`.

### 8.2 Verification

| Check | Evidence |
|---|---|
| Full suite | **322 passed, 0 failed, 0 skipped** in 362.92 s |
| AI tests | 25 in `tests/test_ai.py` (including 4 async/JSON/template tests) + 39 in `tests/test_ai_providers.py` |
| Local browser | Playwright at 1440 and 390/834: login, template markers, pending state, first and second answers without reload, URL interaction, history update, 10-turn cap, clearing only on success, network abort and HTTP 400 shown in place, no horizontal overflow, and no console errors or failed responses during successful asks |
| Production browser | Playwright through the SSH tunnel: admin login, 10 initial history turns, local target, two in-place answers, real OpenRouter answer via the JSON path, tool trace, network/HTTP errors in place, screenshots `prod-async-ai-1440.png`, `prod-async-ai-834.png`, `prod-async-ai-390.png`; no overflow and no console/failed responses during successful asks |
| Public HTTPS browser | Playwright at `https://building-materials-shop-management-system.tail7d68dd.ts.net`: login, secure context, in-place local answer (interaction 28), no reload, no overflow at 390, **0 console errors and 0 failed responses** during the successful ask; screenshots `prod-public-async-ai-1440.png` / `-390.png`.  After the 2026-10-22 funnel reinstall, a second public run logged in, opened `/ai`, confirmed the composer and inert templates and the hidden pending state, with 0 console/failed responses (section 9) |
| Production interactions | `22` local 5 ms low-stock and `23` local 21 ms overview (first run); `24` OpenRouter MiniMax 7901 ms "يوجد منتجان نشطان في الكتالوج." (first provider run; the answer was correct, the test assertion was too literal); `25` local 1 ms low-stock and `26` local 190 ms overview (rerun); `27` OpenRouter MiniMax **6968 ms** "يوجد منتجان نشطان." (final provider run); `28` public local **2 ms** low-stock; `29` a later real user question "سلام" answered by OpenRouter MiniMax in **1560 ms**; all `ok` |
| Provider/model tests | Provider 3 test: **468 models** (audit 177, 2026-10-21 12:51:09 UTC); MiniMax model 9: **993 ms** (audit 178, 12:51:12 UTC) |
| Backups | `pre-async-20261021` JSON 268 rows / 20 AI, SQL `bmshop-20261021-124255`; `post-async-20261021` JSON 290 rows / 27 AI, SQL `bmshop-20261021-125307`; `final-async-20261021` JSON 294 rows / 28 AI, SQL `bmshop-20261021-130236`; `pre-funnel-reinstall-20261022` JSON 307 rows / 29 AI, SQL `bmshop-20261022-071633`; `post-funnel-reinstall-20261022` JSON 309 rows / 29 AI, SQL `bmshop-20261022-072012`; all SHA-256 and gzip verified, dump-complete marker present |
| Release identity | `1.1.0 / a971706 / 2026-10-21T12:48:14Z`; revision `e5f6a7b8c9d0`; 57 tables / 184 FKs; products=2, users=2; labels re-verified after the 2026-10-22 force-recreate |

The production browser run is the code deployed in `a971706`; its first
provider assertion was too literal (`"2"` inside an Arabic sentence) and was
corrected to accept the Arabic dual "منتجان".  The answer itself was correct on
the first run (interaction 24).  The corrected run is interaction 27.  The
interaction table is append-only, so these verification rows are retained.

### 8.3 Remaining

| Item | Status |
|---|---|
| Concurrent-load capacity with a real provider | **NOT TESTED** |
| Nemotron free-model reliability | **PARTIAL** — upstream 502/capacity is possible; fallback records the error; MiniMax is the working default |
| Public Funnel/TLS path | **PASS (current verification)** — `tsdproxy.funnel: "true"` verified on the recreated container, proxy re-registered, 6/6 `/healthz` + 3/3 `/login` 200, and a public Chromium login + `/ai` page passed in a secure context with 0 console/failed responses (section 9) |
| Public TLS long soak / branded Chrome | **NOT TESTED** — the earlier 5/12 failure was not root-caused; no 24-hour or multi-client soak was run and branded Chrome was not separately re-exercised |
| Off-site backup / PITR | **BLOCKED** — no destination, credentials, binlog retention or restore drill |
| Restore of the `a971706` backups in isolation | **NOT TESTED** — the backups were checksum/gzip-verified, but this code-only deploy did not repeat a full restore drill; the earlier restore drill remains the evidence for the mechanism |
| Browser rendering of the new IAM pages | **NOT TESTED** (unchanged) |

---

## 9. Funnel label and force-reinstall verification (2026-10-22)

The requested `tsdproxy.funnel: "true"` label was **already present** in both the
repository `docker-compose.yml` and the labels of the running app container.
No code change was required.  The requested reinstall was performed as a
non-destructive force-recreate so tsdproxy would re-read the labels and
re-register the service.

| Check | Result |
|---|---|
| Container labels | `tsdproxy.enable=true`, `tsdproxy.name=Building-Materials-Shop-Management-System`, `tsdproxy.container_port=8000` and `tsdproxy.funnel=true` present in compose and on the recreated container |
| Reinstall | `docker compose up -d --no-deps --force-recreate app`; the app reached **healthy** |
| tsdproxy | Server logs at 07:17 show the previous proxy stopped, the target auto-detected on gateway port **8090**, the proxy set up again and the tsnet `AuthLoop` in state `Running`; public requests followed at 07:18 |
| Release identity | `1.1.0 / a971706 / 2026-10-21T12:48:14Z`; database revision `e5f6a7b8c9d0` |
| Public checks | **6/6** `/healthz` 200; **3/3** `/login` 200; public Chromium login + `/ai` page passed in a secure context with **0 console errors and 0 failed responses** (screenshot `public-funnel-ai.png`) |
| Data | 57 tables / 184 FKs; products=2, users=2; **29** AI interactions; the real user question id 29 (`"سلام"`, OpenRouter MiniMax, 1560 ms) was preserved |
| Backups | Pre `pre-funnel-reinstall-20261022` (JSON 307 rows / 29 AI; SQL `bmshop-20261022-071633`) and post `post-funnel-reinstall-20261022` (JSON 309 rows / 29 AI; SQL `bmshop-20261022-072012`); SHA-256, gzip and dump-complete marker verified |
| Status | **PASS (current verification)**.  The earlier 5/12 TLS failure was not root-caused, no long soak was run, and branded Chrome was not separately re-exercised; this PASS covers the current public path, not a proof that the old TLS/ALPN fault cannot recur |
