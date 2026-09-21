# Broker Authorization Architecture

**Status:** Implemented (backend) — 2026-09-15
**Scope:** Broker token/authorization lifecycle. Frontend broker cards are explicitly OUT of scope for this phase.
**Supersedes:** the session-scoped `BrokerToken` design (§5.2) as the authoritative token source. Legacy rows remain readable as a fallback during one transition release.

---

## 1. The three concepts (separation of concerns)

| Concept | Table | Lifetime | Owner | Purpose |
|---|---|---|---|---|
| StrikeNova UserSession | `user_sessions` | Temporary (24h TTL, revocable) | Browser/device | Platform login only. Never owns broker tokens. |
| BrokerConnection | `broker_connections` | Durable — survives logout, new sessions, browsers | StrikeNova user (`user_id`) | Authoritative broker-ownership ledger (one user → many connections). |
| BrokerAuthorization | `broker_authorizations` (NEW) | Its own lifecycle: `active → expired/revoked/superseded` | BrokerConnection (`connection_id`) | Current API authorization: encrypted access/refresh material, expiry, method, issuance/usage timestamps. |

### OAuth state binding (unchanged)

The signed OAuth state still carries `sid` (initiating UserSession), `brk` (broker) and `popup`, HMAC-signed with a 10-minute TTL — the exact mechanism proven in the FYERS staging OAuth validation. OAuth initiation remains:

```text
StrikeNova UserSession
  → signed state
  → broker authorization screen
  → callback
  → resolve initiating User (resolve_platform_user)
  → resolve/create the user's BrokerConnection (get_or_create_connection)
  → persist BrokerAuthorization (persist_connection_authorization)
```

The initiating session proves **WHO** connected; it does **not** determine how long the resulting authorization lives.

## 2. Resolution contract (broker-neutral, one path)

```text
user_id + broker
  → BrokerConnection (user_id, broker, status ∈ {connected, pending})
  → active BrokerAuthorization (status='active', not expired)
```

`app/services/broker_authorization.py`:

- `resolve_broker_authorization(db, user_id, broker)` — per-broker resolution; pure ownership path; expired authorizations are lazily marked and fail closed.
- `resolve_default_broker_authorization(db, user_id)` — broker-neutral default-connection resolution for the data plane and background jobs.
- `persist_connection_authorization(...)` — insert new active row + supersede previous active row, on the caller's transaction.
- `authorization_status(authz)` — honest status (`active|expired|revoked|superseded`), no session coupling.
- `revoke_connection_authorizations(...)` — explicit revocation lifecycle hook.

**Never** resolve a broker token via `UserSession.broker_connection_id` alone — that column is at most a hint for multi-connection users, and the connection's `user_id` must match the session's user (fail closed).

## 3. What changed in the codebase

| Component | Before | After |
|---|---|---|
| `get_token()` DB fallback (`token_store._load_token_from_db`) | `BrokerToken ⋈ UserSession` — token died with the initiating session | Ownership path: `UserSession` (validity gate only) → `BrokerConnection` → active `BrokerAuthorization`; legacy fallback retained |
| `_persist_broker_link` (auth callback) | wrote only session-scoped `BrokerToken` | dual-writes (legacy row + **authoritative** `BrokerAuthorization`) in the SAME transaction |
| `_get_oauth_token_for_gex` (background GEX capture, `main.py`) | required a live `UserSession` with `broker_connection_id` | `resolve_default_broker_authorization` — survives logout/browser changes |
| `startup_db_check()` | counted session-scoped rows | counts active authorizations joined to connected connections |
| FYERS callback | — | unchanged `auth_code` selection (fix `07dc9e0` preserved; regression tests re-verified) |
| Frontend | — | **unchanged** (per task constraint J) |

## 4. FYERS renewal capability (verified against provider documentation)

Verified during this task (2026-09-15) against FYERS v3 documentation:

1. The FYERS v3 OAuth flow issues an access token plus a **refresh token valid ~15 days** (FYERS support documentation, "How to refresh token in FYERS API").
2. FYERS's **April-2026 rules** state that **continuous refresh-token sessions are not supported for trading** (FYERS "New rules for trading APIs — April 2026").
3. The refresh flow is **PIN-gated** — it requires the user's TOTP/PIN flow and cannot be performed unattended.

**Decision (implemented in `app/services/renewal_strategy.py`):**

- FYERS renewal = **user-driven re-authorization** through the existing OAuth popup flow.
- The refresh token is persisted encrypted when FYERS returns one (for a future, explicitly-approved, PIN-gated mechanism) but is **never used silently**.
- `can_refresh()` is False for FYERS **and** Upstox; `refresh_authorization()` raises `BrokerNotRefreshableError` for all brokers.
- **No FYERS password, PIN, TOTP secret, OTP is ever collected, and browser credentials are never automated.**
- Data-only (`-100`) vs trading (`-200`): `FyersRenewalStrategy.capabilities(app_type='-100')` reports `trading: False` — no renewal path grants trading capability to a data-only app; `websocket_order_events` is likewise unsupported on `-100`.

Upstox: OAuth v2 tokens have no refresh token at all (daily re-OAuth is the documented model) — same user-driven reauthorization semantics.

## 5. Migration (non-destructive)

`alembic/versions/f1a2b3c4d5e6_broker_authorization_table_and_carry_forward.py`:

1. Creates `broker_authorizations` (idempotent guard; safe under CockroachDB-style DDL).
2. Carry-forward: legacy `broker_tokens` rows with encrypted material are copied **verbatim** (Fernet blobs — no re-encryption, no key dependency) into authorizations with `method='migration'`, preserving connection association and expiry columns.
3. Legacy rows are **NOT deleted** — the old table remains as a read-only fallback for one transition release.
4. Sentinel `connection_id='none'` (platform sessions) rows are ignored; re-runs are idempotent (already-migrated connections skipped).

Verified on scratch SQLite: parent schema → seed legacy rows → upgrade → assert verbatim blobs + preserved rows → downgrade → assert legacy rows still intact.

## 6. Follow-up tasks (explicitly NOT in this change)

1. **SECURITY (next isolated task):** FYERS `/auth/callback` query strings — including the `auth_code` JWT and signed state — are currently present in Uvicorn/Render access logs. Redact callback query strings at the access-log layer. The auth codes are single-use and short-lived, but the log surface should not persist them. (Task rule L: kept separate from this architecture change.)
2. Migrate remaining direct `token_store.get_token(sid)` call sites (candles/live_gex currently resolve via `deps._resolve_user` and keep working through the shared re-wired fallback) to explicit `resolve_broker_authorization` calls with a broker parameter. **DONE for chains (2026-09):** the option-chain router now resolves through the canonical market-data credential resolver (see §7).
3. Remove the legacy `BrokerToken` dual-write and legacy fallback path after one release.
4. Frontend broker cards phase: individual broker cards, logos, Add buttons, broker-specific modals (Upstox Analytics Token form; FYERS App ID + Secret form; FYERS Connect action; persistent Connected state).

## 7. Market-data credential resolution (Analytics Token era)

Read-only market data (option-chain expiries, option chain, WebSocket chain feed) resolves through ONE canonical path — `app/services/market_data_authorization.resolve_market_data_token`:

```
platform session (HttpOnly cookie → users.id)
    → 1. stored Upstox Analytics Token (preferred; requires BrokerConnection
         status='connected' AND data_status='active' — the same authority,
         identity.get_analytics_token, that background GEX capture uses)
    → 2. OAuth fallback: default connection's active BrokerAuthorization
    → 3. legacy session-scoped broker token (compatibility: pre-architecture
         rows and in-memory compatibility sessions)
```

Capability separation is unchanged: the Analytics Token is a DATA credential
(read-only market data only, never trading); the platform session proves WHO
the user is; OAuth broker authorization remains required for trading/account
capabilities. A platform session identifier is never usable as a broker
credential; token material is decrypted server-side only and never logged,
returned by an API, or exposed to the browser.

Error semantics: 401 = no valid platform session; 403 = valid session with NO
active market-data authorization ("Market data is not connected..." — the UI
points at Settings → Analytics Token). A rejected Analytics Token surfaces as
401 "update it in Settings" and the stored token is never cleared server-side
(the user refreshes it deliberately); only legacy session-scoped tokens are
cleared on session-code broker failures.
