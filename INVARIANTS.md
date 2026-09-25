# StrikeNova — Invariants

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-18

Invariants are properties that must never regress. Any change that violates one
requires an explicit decision record ([`DECISIONS.md`](DECISIONS.md)) and
Founder acceptance.

---

## Governance

1. **Founder is the final authority** for product direction, scope,
   acceptance, and release. Agents (Hermes/FreeBuff) execute; they never
   decide.
2. **Obsidian is the knowledge/context authority**; GitHub is the
   implementation/project authority. Durable decision records live in
   Obsidian; this repository mirrors their engineering effect in
   [`DECISIONS.md`](DECISIONS.md).
3. **One governance system.** The canonical control-document set at the
   repository root (see [`AI.md`](AI.md)) is the only governance layer; do not
   create competing boards, wikis, or duplicate control documents.
4. **One control document per topic.** Link; do not copy.

## Runtime topology

5. **Production topology is Vercel (frontend) → Render (backend) →
   CockroachDB (production database).** Railway references are historical and
   must not be presented as current production truth.
6. **Database portability is preserved.** Local development on SQLite,
   PostgreSQL-compatible CI, CockroachDB-validated production target. No code
   path may depend on a single vendor dialect where portability exists today.
6a. **Production database configuration is fail-closed.** With
   `STRIKENOVA_ENV=production` (provider-neutral — never Railway-era markers
   alone), the backend refuses to start unless `DATABASE_URL` is set and does
   not point at SQLite. A production deployment can never silently run on
   ephemeral SQLite (ADR-014).
7. **Alembic is the sole schema authority.** Schema changes happen only
   through migrations; no ad-hoc DDL in application code or tests.

## Identity, sessions, and brokers

8. **Broker OAuth and StrikeNova platform identity remain separate systems.**
   A broker token never authenticates the platform; a platform session never
   implies broker authorization.
9. **BYOB (bring your own broker)** is the connectivity model. Broker
   credentials belong to the user's own broker connection and are encrypted at
   rest.
10. **The only browser platform-session transport is the HttpOnly
    `strikenova_session` cookie.** No session credential in `localStorage`,
    `sessionStorage`, React state, URL query/fragments, custom frontend
    headers, WebSocket subprotocols, or WebSocket URLs.
11. **`X-Session-Id` is a server-side compatibility transport only** — it is
    never injected by the browser application.
12. **WebSocket session authentication uses the cookie**; the server never
    accepts the platform session credential from `Sec-WebSocket-Protocol`.
13. **Signed OAuth state.** Broker OAuth state is HMAC-signed with session
    binding; unsigned state is rejected.
14. **A valid platform session does not require a broker token.** Platform-only
    users (email/Google) authenticate to platform routes with
    `access_token = None`; broker authorization is a separate concern.

## Trading and computation

15. **Paper trading is server-authoritative.** Balances, positions, exits, and
    P&L are computed and persisted server-side; the client renders only.
16. **GEX conventions are owned by `docs/GEX_V1_0_SPEC.md`.** Do not redefine
    sign conventions, flip/wall definitions, or aggregation windows in code or
    docs outside that specification.
17. **Timezone handling is standardized** (IST market context; UTC storage
    conventions per `docs/PHASE_7_24_4_TIMEZONE_STANDARDIZATION.md`); no naive
    `datetime.now()` in production paths.

## Process

18. **Verification before completion.** Required tests/CI run and are reported
    with evidence; local passes are not CI evidence.
19. **No production mutation without authorization.** No deploys, no
    production database writes, no infrastructure changes unless the Founder
    explicitly authorizes them.
20. **Credentials are invisible to agents.** Never read, print, copy, decode,
    or expose credential files (including `.strikenova_gh_token`).
