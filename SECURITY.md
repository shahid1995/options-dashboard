# StrikeNova — Security

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-18

---

## 1. Security model at a glance

Two independent identity planes, one secure browser transport:

1. **StrikeNova platform identity** — email/password and Google Sign-In;
   durable hashed `UserSession` records; authenticates platform routes.
2. **Broker authorization (BYOB)** — user-connected broker accounts (Upstox
   first) via OAuth; authorizes broker API calls only; encrypted at rest.

A broker token never authenticates the platform; a platform session never
implies broker authorization ([`DECISIONS.md`](DECISIONS.md) ADR-005/006).

## 2. Browser session transport (Issue #61 — cookie-only)

- The **only** browser platform-session transport is the **HttpOnly
  `strikenova_session`** cookie (`Secure`, `SameSite=None`, backend-set
  max-age).
- Login responses (`/auth/login-email`, `/auth/google`, broker callback)
  return **user info only — never `session_id` or token material**.
- **Retired and must not return:** `session_id` cookie name as a transport;
  `localStorage`/`sessionStorage` session storage; session credentials in URL
  query/fragments; frontend `X-Session-Id` injection; WebSocket subprotocol
  credentials (`Sec-WebSocket-Protocol`); session credentials in WebSocket
  URLs.
- **`X-Session-Id`** remains a **server-side compatibility transport** for
  legacy/test clients; the browser application never sends it. The frontend
  relies on `withCredentials: true` and cookie transport.
- **WebSocket authentication** reads the cookie server-side
  (`ws_session()` in `app/routers/chains.py`); the client passes no
  subprotocol (`chainWsProtocols() → undefined`).

## 3. Auth failure semantics (transient vs explicit)

- `/auth/me` is the authoritative platform-identity check (`AuthGate`).
- **401/403** → server explicitly rejected authentication → client clears
  authenticated UI state (redirect `/` where appropriate).
- **5xx / network failure** → session validity **unknown** → client
  **preserves** authenticated state and surfaces a **retryable error**. A
  transient backend outage never causes client-side logout.
- A subsequent successful `/auth/me` is authoritative and clears the error.
- The HttpOnly cookie is never cleared or touched by transient client logic.

Regression coverage: `backend/tests/test_secure_session_cookies.py`,
`frontend/lib/useAuth.behavior.test.js`, `frontend/lib/session-transport.test.js`,
`frontend/components/AuthGate.test.js`.

## 4. OAuth state and CSRF

- Broker OAuth state is **HMAC-signed** with session binding and a TTL; legacy
  unsigned state is rejected (`token_store.create_oauth_state` /
  `consume_oauth_state`).
- Google id-token flows bind a backend-generated **nonce** through the same
  signed-state mechanism (`POST /auth/google/state`), preventing replay of
  id tokens from unrelated auth attempts.

## 5. Phase 10.2 status (do not over-assume)

Account-security work **completed on this branch** — the final
end-to-end release/security gate passed 2026-09-19 (Issue #69, ADR-013) — see
[`DECISIONS.md`](DECISIONS.md) ADR-011 for the standing record. Completed:
identity/session hardening, token/OAuth-state work, the account-auth route
surface, and the secure browser session transport (PR #62). **Implemented and
verified:** the Brevo transactional-email adapter behind the provider-neutral
`EmailSender` boundary (`EMAIL_PROVIDER=brevo`; Issue #65) — real
mailbox/email-delivery verification completed on staging 2026-09-19 (Issue
#67; ADR-012): registration verification, password reset (with full session
revocation) and email-change messages were delivered by Brevo to real
external mailboxes and consumed end-to-end with single-use replay rejection.
The final end-to-end Phase 10.2 release/security gate — was Pending, now
**Complete** (update note below). Brevo credentials live
only in backend environment configuration (`BREVO_API_KEY`), are sent only in
the provider `api-key` header, and are never logged, persisted, or exposed to
the frontend.

*(Update 2026-09-19, Issue #69: the final end-to-end Phase 10.2
release/security gate PASSED on the merged feature tip `798c6c2` — full-suite,
migration, browser-matrix, email-link and broker-regression evidence recorded
in ADR-013. Phase 10.2 account security is complete on this branch. Brevo
credential handling above is unchanged.)*

## 6. Credential handling

- Broker tokens are encrypted at rest (Fernet; `app/crypto.py`) and stored
  server-side (`BrokerToken` / `BrokerAuthorization` ownership path). The
  browser never sees them.
- Broker authorization follows the **connection-ownership path**:
  `UserSession → user → BrokerConnection → active BrokerAuthorization`; a
  session-scoped legacy `BrokerToken` row exists only as a read fallback for
  pre-migration data.
- `TOKEN_ENCRYPTION_KEY` is server-side configuration; OAuth-state HMAC is
  derived from it.
- Session IDs are cryptographically strong (`secrets.token_urlsafe(32)`) and
  stored **hashed** (`hash_session_id`) in `UserSession`/`BrokerToken` rows.
- Agents never read, print, copy, decode, or expose credential files
  (including `.strikenova_gh_token`) — [`AGENTS.md`](AGENTS.md) §5.
- Anonymous registration never attaches local credentials to an existing
  account. `POST /auth/account/register` fails closed for every pre-existing
  email — no `password_hash`, `display_name`, status, identity-source,
  verification-state or broker-field write, no session, no verification or
  reset token, and no account identifier in the response. Submitting an
  address is never proof of ownership: adding a local password to an
  OAuth-created account requires a separate authenticated +
  recent-authentication (or verified single-use token) flow. The legacy
  `POST /auth/register` is remediated separately (`security/auth-register-no-password-linking`, PR #135 on `main`).

## 7. Server authority

- Paper trading (equity, positions, exits, P&L) is **server-authoritative**;
  the client cannot compute or assert balances.
- Route authorization: platform routes resolve through `CurrentUser()`;
  broker-dependent routes legitimately 403 when no broker credential exists —
  platform-only sessions remain valid for platform routes.

## 8. Operational rules

- No deploy, no production database access, no infrastructure change without
  explicit Founder authorization.
- Database safety: Alembic is the schema authority; migrations run through CI
  compatibility gates (PostgreSQL service container; CockroachDB validated).
- Security-relevant events are logged **without exposing secrets** (session
  prefixes only).

## 9. Reporting

Security issues are filed as **Security** issues on the GitHub Project
([`PROJECT-CONTROL.md`](PROJECT-CONTROL.md)); see
`.github/ISSUE_TEMPLATE/security.md`.
