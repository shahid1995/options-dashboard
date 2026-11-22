# StrikeNova — Invariants

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-26

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

5. **Production topology is Vercel (frontend) → Render (backend) → Neon
   PostgreSQL (accepted strategic production database, ADR-019).** Neon
   provisioning, live role/grant configuration, Render changes and production
   cutover are separate founder-authorized work and are **not** complete;
   nothing in this repository may assume they have happened. CockroachDB is
   retired (ADR-019); CockroachDB evidence in historical documents is
   historical record, never current target configuration, and Railway
   references are historical too.
6. **Database portability is preserved.** Local development on SQLite,
   PostgreSQL-compatible CI, and a PostgreSQL 16 production target (Neon).
   No code path may depend on a single vendor dialect where portability exists
   today; the ADR-017 lease is deliberately PostgreSQL-only and a CockroachDB
   migration path is refused rather than run unguarded.
6a. **Production database configuration is fail-closed.** The
   provider-neutral production signal is `STRIKENOVA_ENV=production`. The
   legacy Railway-era markers (`RAILWAY_ENVIRONMENT`,
   `RAILWAY_SERVICE_NAME`, `PRODUCTION`) remain active for backward
   compatibility but are never the sole supported way to declare production
   on a new deployment. While any production signal is active, the backend
   refuses to start unless `DATABASE_URL` is set and does not point at
   SQLite (scheme match is case-insensitive). A production deployment can
   never silently run on ephemeral SQLite (ADR-014).
6b. **Production frontend API configuration is fail-closed.** Production
   builds of the frontend require an explicit `NEXT_PUBLIC_API_URL` https
   origin; missing/blank/invalid values (including the historical Railway
   URL, localhost/loopback targets, and plain http) abort the build. No
   default or fallback backend URL is ever embedded in the build output
   (ADR-015).
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
6c. **Runtime and migration database identities are distinct and both are
   mandatory on PostgreSQL.** Alembic and the startup backfill run under the
   identity from `STRIKENOVA_MIGRATION_DATABASE_URL`; there is **no fallback**
   to the runtime credential. On PostgreSQL a missing migration URL, or one
   that resolves to the same database identity as `DATABASE_URL`, fails closed
   (`45004`). The runtime identity must never require cluster administration,
   and the migration identity must never be the normal serving credential
   (ADR-016/ADR-017/ADR-018). SQLite keeps its single-identity local path.
6d. **Migrations are serialized.** Only one process may execute the Alembic
   chain at a time; concurrent application startups must wait, take over an
   expired lease, or fail closed — never execute the same DDL concurrently.
   The lock must work on an empty database and under the migration identity,
   and must never require runtime DDL/owner privileges (ADR-017/018). The
   implemented lease has no renewal thread: ownership is the lease row lock
   itself, so a live owner cannot lose the lease while its transaction is
   open, and a dead owner's transaction is aborted by the server. The lease
   TTL only bounds how long another process waits before it may take over an
   abandoned row. `MIGRATION_LEASE_TTL_SECONDS`, `MIGRATION_LEASE_WAIT_SECONDS`
   and `MIGRATION_LEASE_LOCK_TIMEOUT_MS` are validated at the configuration
   boundary, and the TTL is derived from measured backfill timing
   (runbook §7), not guessed.
6e. **The migration path is guarded, and the guard is verified.** On
   PostgreSQL the lease and the Alembic DDL share one physical connection and
   one transaction; `alembic/env.py` refuses a PostgreSQL connection that did
   not arrive through the lease. A bare `alembic upgrade head` fails closed,
   and `python -m app.migrate upgrade head` is the only supported path. The
   migration URL must exist and must differ from the runtime URL; equal or
   missing identities fail closed (`45004`). Verification
   (`python -m app.migrate bootstrap-lease --verify`) reports state and never
   changes installed protocol versions; upgrades use
   `--upgrade-protocol N`, never a direct `UPDATE installed_version`.
6f. **Future application tables carry runtime DML by default.** Default
   privileges are bound to the role that actually creates application objects
   and are **schema-scoped** (`... IN SCHEMA public`), never global: in
   Topology B that creator is `strikenova_prod_owner`, in Topology A it is the
   migrator itself. They grant the runtime role
   `strikenova_production_app` SELECT / INSERT / UPDATE / DELETE on future
   tables and USAGE on future sequences, so a migration that creates
   objects never requires a follow-up manual grant for the serving
   credential to work. Through this mechanism the runtime identity never
   receives CREATE, ALTER, DROP, TRUNCATE, ownership, admin, or
   role-management privileges. The mechanism is scoped to the named creator
   role in this database only — it does not apply to objects created by
   arbitrary roles, other schemas, or other databases. **Operational
   dependency:** the contract is coupled to the creator role; if migrations
   ever run under a different creator/owner role, the default-privilege
   configuration MUST be re-applied for that role as part of the role
   change — it does not follow automatically (ADR-018).

21. **ChatGPT is Markdown-only for repository file changes.** ChatGPT may
    plan, research, review, verify, supervise coding agents, write prompts, and
    author Markdown documentation, but it must not create or modify non-`.md`
    repository files. The enforceable boundary and delegation workflow live in
    [`AGENTS.md`](AGENTS.md) under the ChatGPT engineering-supervisor boundary.
