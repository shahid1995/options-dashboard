# StrikeNova — Decision Records

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-18

Durable reasoning and full context live in the **Obsidian Second Brain**
(knowledge authority). This file mirrors the **engineering effect** of accepted
decisions so agents can honor them from inside the repository. When a decision
changes, it changes through the governance process — not by silent edits.

Format: **ADR-NNN · Title · Status · Evidence**.

---

## ADR-001 · Authority hierarchy and single governance system · Accepted

Founder (final) · Obsidian (knowledge/context) · GitHub (implementation/
project) · Hermes/FreeBuff (execution). Exactly one governance layer: the
canonical control-document set at the repository root. Evidence:
[`PROJECT-CONTROL.md`](PROJECT-CONTROL.md).

## ADR-002 · Alembic is the sole schema authority · Accepted

All schema changes flow through Alembic migrations. Application code and tests
never issue ad-hoc DDL. Existing migrations are never edited to satisfy tests;
new migrations require independent evidence of a schema-contract gap. Evidence:
`docs/PHASE_10_1A_DATABASE_MIGRATIONS.md`, `backend/alembic/`,
`backend/tests/test_day5_alembic_authority.py`.

## ADR-003 · Database portability with CockroachDB production target · Accepted

The backend stays portable: SQLite for local development, PostgreSQL-compatible
CI (service container), CockroachDB validated as the production runtime
(dialect `cockroachdb+psycopg`). Production deploys target CockroachDB Cloud.
Evidence: `docs/architecture/COCKROACH_RUNTIME_VALIDATION.md`,
`docs/architecture/COCKROACH_LIVE_COMPATIBILITY_VALIDATION.md`,
`backend/tests/test_cockroachdb_compat.py`, CI `PostgreSQL compatibility`.

## ADR-004 · Railway superseded; production topology is Vercel/Render/CockroachDB · Accepted

Railway was an early staging experiment (see `docs/RAILWAY_INFRASTRUCTURE_AUDIT.md`,
2026-08-31 — historical). Current topology: frontend on **Vercel**, backend on
**Render**, production database on **CockroachDB Cloud**. Evidence:
`docs/architecture/VERCEL_STAGING_DEPLOYMENT.md`,
`docs/architecture/RENDER_STAGING_DEPLOYMENT.md`, `frontend/vercel.json`.

## ADR-005 · BYOB broker architecture · Accepted

Users connect their own broker accounts (Upstox first) through OAuth. Broker
tokens authorize broker API calls only; they never authenticate the StrikeNova
platform. Broker credentials are encrypted at rest and scoped per connection.
Evidence: `docs/BROKER_AUTHORIZATION_ARCHITECTURE.md`,
`docs/PHASE_10_2B_CONNECTION_ARCHITECTURE.md`.

## ADR-006 · Platform identity separate from broker authorization · Accepted

StrikeNova platform identity (email/Google) issues its own durable
`UserSession` records. A valid platform session needs no broker token;
platform-only users authenticate with `access_token = None`. Evidence:
`docs/PHASE_10_2_IDENTITY_HARDENING.md`, `docs/superpowers/specs/2026-09-16-strikenova-auth-account-security-design.md`.

## ADR-007 · Secure browser session transport (cookie-only) · Accepted

The only browser platform-session transport is the HttpOnly `strikenova_session`
cookie. Retired: `session_id` cookie name, `localStorage`/`sessionStorage`
session storage, URL-fragment/query session capture, frontend `X-Session-Id`
injection, WebSocket subprotocol credentials. `X-Session-Id` remains a
server-side compatibility transport for legacy/test clients. Transient (5xx/
network) `/auth/me` failures preserve authenticated UI state with a retryable
error; only 401/403 clear it. Evidence: PR #62 (merged at `aa70629`),
`backend/app/routers/deps.py`, `frontend/lib/session.js`,
`backend/tests/test_secure_session_cookies.py`,
`frontend/lib/session-transport.test.js`, `frontend/lib/useAuth.behavior.test.js`.

## ADR-008 · Signed broker OAuth state · Accepted

Broker OAuth state is HMAC-signed with session binding and a TTL; legacy
unsigned state is rejected. Google id-token flows bind a nonce through the same
signing mechanism. Evidence: `docs/PHASE_10_2B_3` line of work,
`backend/app/services/token_store.py` (`create_oauth_state`,
`consume_oauth_state`, Google nonce binding), `backend/tests/test_day3_security.py`.

## ADR-009 · Server-authoritative paper trading · Accepted

Paper-trading equity, positions, exits, and P&L are computed and persisted
server-side; the client is a renderer. GEX conventions (sign, flip/wall,
aggregation) are owned by `docs/GEX_V1_0_SPEC.md`. Evidence:
`backend/app/services/paper_execution.py`, `docs/GEX_V1_0_SPEC.md`.

## ADR-010 · Historical engineering record is evidence, not open work · Accepted

`options-dashboard-project/docs/` documents completed phases. They are not
recreated as open issues unless an issue explicitly reopens or supersedes the
work. The current status snapshot is
`docs/superpowers/STRIKENOVA_IMPLEMENTATION_STATUS.md`. Evidence:
[`PROJECT-CONTROL.md`](PROJECT-CONTROL.md) §Existing project history.

## ADR-011 · Phase 10.2 account security — standing status record · Accepted

The approved Phase 10.2 design and execution plan
(`docs/superpowers/specs/2026-09-16-strikenova-auth-account-security-design.md`,
`docs/superpowers/plans/2026-09-16-strikenova-auth-account-security-execution-plan.md`)
was **not uniformly complete**. Standing status (all workstreams complete as
of 2026-09-19 — Issue #69/ADR-013):

| Workstream | Status |
|---|---|
| Identity/session hardening | Completed |
| Token/OAuth-state work | Completed |
| Account-auth implementation | Completed (email-token landing routes added by PR #70, Issue #69) |
| Secure browser session transport | Completed by PR #62 (Issue #61) |
| Transactional email provider integration | **Implemented and live (Brevo adapter, Issue #65); real delivery verified (Issue #67) — see ADR-012** |
| Real mailbox/email-delivery verification | **Verified 2026-09-19 (staging, Brevo delivery; Issue #67)** — see ADR-012 |
| Final end-to-end Phase 10.2 release/security gate | **Complete (2026-09-19, Issue #69)** — see ADR-013 |

Evidence: account-auth route surface (`/auth/account/*` in
`backend/app/routers/auth.py`), security record models and services
(`backend/app/services/account_security.py`, `app/identity.py`), provider-neutral
`EmailSender` boundary with the explicit `EMAIL_PROVIDER` selection and the
Brevo adapter behind it (`backend/app/services/email.py` —
`BrevoEmailSender`, `api-key` header, Brevo `smtp/email` payload; default
remains the deterministic in-memory test sender), secure-transport regressions
(`backend/tests/test_secure_session_cookies.py`,
`frontend/lib/useAuth.behavior.test.js`). Real mailbox/email-delivery
verification and the final Phase 10.2 release/security gate remained pending —
until both pass, no document may describe Phase 10.2 account security as
fully complete. *(Update 2026-09-19, Issue #67: real mailbox/email-delivery
verification is now COMPLETE on staging — registration verification,
resend-verification, password reset (with full session revocation), and
e-mail change all delivered by Brevo to real external mailboxes and consumed
end-to-end with single-use replay rejection; see ADR-012. The final
end-to-end Phase 10.2 release/security gate remains PENDING.)* *(Update
2026-09-19, Issue #69: the final end-to-end Phase 10.2 release/security gate
is now COMPLETE on the merged feature tip `798c6c2` — see ADR-013.)*

## ADR-013 · Phase 10.2 final release/security gate PASSED on the merged tip · Accepted

Date: 2026-09-19 · Reference: Issue #69

**Decision/record:** the final end-to-end Phase 10.2 release/security gate was
executed fresh against the integrated feature tip
`798c6c295a3fcdce58de5a78816ffa7c8049494c` (merge of PR #70, which added the
missing email-token landing routes `/verify-email`, `/reset-password`,
`/verify-email-change` — the last gate blocker). All criteria passed with
fresh evidence:

- Focused backend suites (account-security, account flows, secure session
  cookies, security gaps, Brevo provider, rate limiter, session/token
  separation, platform-session-no-broker): **228 passed**.
- Full backend suite: **5895 passed / 5 failed / 102 skipped** — the 5 are the
  documented TESTING.md baseline failures (market-data/timestamp/upstox
  adapter), no auth/email signatures.
- Frontend: **85 files / 1878 tests passed**; production build succeeds.
- Alembic chain from a clean disposable PostgreSQL 17 database reaches single
  head `c1d2e3f4a5b6`; identity/account-security tables coexist with all
  broker/BYOB and GEX tables.
- Security-material audit: tokens never rendered/stored/logged; URL tokens
  stripped on open; HttpOnly `strikenova_session` remains the only browser
  session transport; no retired transports or secrets in source.
- Browser matrix on the integrated tip (11/11): unauth redirect, login,
  refresh persistence, protected navigation, logout, Back-after-logout,
  direct protected URL, revoked session, `/auth/me` 401 → `/`, network
  failure → retryable without forced logout, broker-disconnected ≠ platform
  logout.
- Email flows user-facing through the integrated routes: registration →
  clicked verification link → verified (replay 400); password reset → clicked
  link → new password set on-page → prior sessions revoked, old password 401,
  new password 200 (replay 400); email change → confirmation delivered to the
  new address only → clicked → account email changed (replay 400). Link-free
  password-changed and email-changed notifications delivered.
- Broker OAuth/BYOB regression: **141 passed**.

**Effect on ADR-011:** "Final end-to-end Phase 10.2 release/security gate"
moves from Pending to **Complete (2026-09-19, Issue #69)**. Phase 10.2
account security is complete on this branch.

## ADR-012 · Real transactional-email delivery verified on staging (Brevo) · Accepted

Date: 2026-09-19 · References: Issue #65 (implementation), Issue #67 (verification)

**Decision/record:** the Brevo transactional provider was configured on the Render
staging service (`strikenova-api-staging`, `srv-daj4vetg1s2s739ecvfg`) and real
mailbox delivery was verified end-to-end at deploy
`dep-damommnf3r2c73ap40jg` (commit `546307d576b9775250be85c5561e3fdea29196ae`).

Configuration (secrets never printed): `EMAIL_PROVIDER=brevo`,
`BREVO_API_KEY` present (Render secret store only),
`BREVO_API_URL=https://api.brevo.com/v3/smtp/email`,
`EMAIL_FROM_ADDRESS=business.ikon@gmail.com` (delivered by Brevo via its
authenticated relay `business.ikon@12187463.brevosend.com`),
`EMAIL_BASE_URL=https://strikenova-frontend-staging.vercel.app`.

Verified with real external mailboxes (GuerrillaMail-controlled):
1. Registration verification email — DELIVERED; tracking-redirect target proven to
   be `…/verify-email?token=…` on `EMAIL_BASE_URL`; token consumption 200; replay
   rejected 400.
2. Resend-verification — generic-success contract honored (no email after the
   account was already verified; enumeration resistance intact).
3. Password reset — DELIVERED; link `…/reset-password?token=…`; reset 200; replay
   400; ALL existing sessions revoked (old session 401); old password rejected;
   "Your StrikeNova password was changed" notification DELIVERED (link-free).
4. Email change — confirmation DELIVERED to the NEW address only
   (`…/verify-email-change?token=…`); consumption 200 changed the account email;
   replay 400; "Your StrikeNova email address was changed" notification DELIVERED
   (link-free).

Evidence limitation (recorded, not blocking): the Brevo credential is
send-scoped — `/senders`, statistics and account read endpoints are not
accessible (HTTP 403/404) and the events feed returned no rows for this key, so
provider-side per-message ids could not be collected. Delivery evidence is the
actual receipt in the controlled mailboxes, which is the stronger standard.

**Effect on ADR-011:** "Real mailbox/email-delivery verification" moves from
Pending to **Verified (2026-09-19, staging)**. *(Update 2026-09-19, Issue #69:
the final gate subsequently passed — ADR-013; Phase 10.2 is complete on this
branch.)*
## ADR-014 · Production database configuration is fail-closed · Accepted

Context: the production SQLite refusal (Day 4) relied on Railway-era
markers (`RAILWAY_ENVIRONMENT`, `RAILWAY_SERVICE_NAME`, `PRODUCTION`) and
only logged a warning, so a Render production deployment without a valid
`DATABASE_URL` would silently boot onto ephemeral in-container SQLite.

Decision (post-merge hardening on the Day 46 release tree, `b960ca2f`):

* `STRIKENOVA_ENV=production` (case-insensitive) is the provider-neutral
  production signal; the Railway-era indicators remain only for backward
  compatibility.
* When production mode is active, startup **fails closed**: missing
  `DATABASE_URL` or a SQLite `DATABASE_URL` raises `RuntimeError`
  ("production database configuration is required"); PostgreSQL/CockroachDB
  URLs proceed unchanged through the existing psycopg normalization.
* Failure messages never embed the connection string (no credential leak).
* Non-production environments keep intentional SQLite behavior; Alembic
  remains the sole schema authority and startup still runs
  `init_db()` → `alembic upgrade head`.

Evidence: `tests/test_production_db_guard.py` (fail-closed, provider-neutral,
redaction cases); Day 4 contract tests updated to the fail-closed semantics.
