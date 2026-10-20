# StrikeNova — Security Baseline

> **Purpose:** Security model and implementation baseline for StrikeNova's identity, sessions, broker credentials, API boundaries, data ownership, and operational controls.
>
> **Status:** Active
>
> **Principle:** Security boundaries are architectural boundaries. They must not be weakened for convenience, UI simplicity, or agent implementation speed.

---

## 1. Security objectives

StrikeNova security is primarily concerned with preserving:

1. **Identity integrity** — a session resolves to the correct StrikeNova user.
2. **Tenant isolation** — one user's resources cannot be read or mutated through another user's identity.
3. **Credential confidentiality** — broker credentials, access tokens, encryption keys, and authentication secrets remain secret.
4. **Execution integrity** — paper-trading state cannot be manipulated by bypassing server-side rules.
5. **Session integrity** — expired, revoked, forged, replayed, or mismatched sessions/OAuth states are rejected.
6. **Data integrity** — orders, positions, transactions, journal records, and analytical records remain internally consistent.
7. **Configuration safety** — production security settings are not accidentally weakened by development defaults or uncontrolled environment changes.
8. **Change traceability** — material security-boundary changes are explicit, reviewed, and verified.

---

## 2. Security authority and related documents

Security behavior is governed together with:

- INVARIANTS.md — non-negotiable security and ownership properties;
- DECISIONS.md — identity, BYOB, broker, execution, and architecture decisions;
- ARCHITECTURE.md — system and trust boundaries;
- AGENTS.md — required AI-agent behavior;
- PROJECT-CONTROL.md — change/review/verification process.

A security-sensitive implementation must satisfy all of these documents.

---

## 3. Trust boundaries

The principal trust boundaries are:

    Browser
      |
      | session header / cookie
      v
    FastAPI API
      |
      +-- authentication / authorization
      |
      +-- application services
      |
      +-- broker gateway
      |       |
      |       +-- Upstream broker API
      |
      +-- database
              |
              +-- user identity
              +-- session records
              +-- encrypted broker credentials/tokens
              +-- paper-trading state
              +-- analytics/history

The frontend is an untrusted client from the backend's authorization perspective.

Client input must therefore never be treated as proof of:

- identity;
- ownership;
- authorization;
- fill price;
- position state;
- cash;
- P&L;
- execution outcome.

---

## 4. Identity model

### 4.1 Canonical identity

The canonical application identity is User.id.

The session transport identifier is not the long-term application identity.

The identity chain is:

    session identifier
          |
          v
    UserSession
          |
          v
    User.id
          |
          v
    user-owned resources

The current backend models include:

- User;
- UserSession;
- BrokerConnection;
- BrokerToken.

### 4.2 Identity separation

Broker identity and StrikeNova identity remain separate.

A broker account is a connection belonging to a StrikeNova user. It is not the user's platform identity.

Removing or changing a broker connection must not silently change the owner of existing StrikeNova-owned data.

### 4.3 Account status

The application checks the canonical user's status before allowing authenticated access.

A broker login must not silently reactivate a platform account that has been disabled/suspended by the platform.

---

## 5. Session security

### 5.1 Session generation

Broker session identifiers are generated using cryptographically strong random values.

The active token store uses session identifiers as the lookup key, while durable database records store a hash of the session identifier.

The database therefore does not need to persist the plaintext session identifier to recover a broker token.

### 5.2 Session lifetime

The current platform session TTL is 24 hours.

The authenticated cookie is also configured with a 24-hour max age in the broker callback flow.

Provider-specific broker token lifetimes are separate from the StrikeNova platform session lifetime and must not be conflated.

### 5.3 Session validation

Authenticated requests must validate:

- session presence;
- session existence;
- session expiry;
- session revocation;
- associated user existence;
- user active status.

An invalid or expired session must fail closed.

### 5.4 Session transport

The current backend accepts the session identifier through:

- X-Session-Id header;
- session_id cookie as fallback.

The explicit header is the primary cross-site transport used by the frontend/backend deployment arrangement.

Security-sensitive code must not assume that frontend route protection replaces backend session validation.

---

## 6. Broker credential security

Broker credentials are high-sensitivity secrets.

Examples include:

- broker API keys;
- broker API secrets;
- Analytics Tokens;
- OAuth access tokens;
- refresh tokens;
- provider-specific authentication material.

### 6.1 Ownership

Broker credentials are stored against the user's BrokerConnection.

A credential lookup must be scoped to:

1. the authenticated StrikeNova user;
2. the intended broker/provider;
3. the intended connection.

An unrelated user's credential must never be selected as a fallback.

### 6.2 Encryption at rest

Broker credentials/tokens stored in the database are encrypted through app/crypto.py.

The current implementation derives a Fernet key from TOKEN_ENCRYPTION_KEY using PBKDF2-HMAC-SHA256 and uses the resulting Fernet instance for encryption/decryption.

The encryption key itself must not be stored in the database or source repository.

### 6.3 Secret handling rules

Secrets must never be:

- committed to Git;
- placed in Markdown documentation;
- printed in normal logs;
- embedded in API responses unless explicitly required;
- copied into screenshots or test fixtures;
- included in exception messages;
- exposed to client-side JavaScript unnecessarily.

Logs may contain safe operational identifiers such as short session prefixes where already implemented, but never full session identifiers or credential values.

### 6.4 Key rotation

The current encryption implementation does not perform automatic key rotation.

Changing TOKEN_ENCRYPTION_KEY invalidates existing encrypted ciphertext until all affected records are re-encrypted with the new key.

Therefore a key rotation is an operational security change requiring:

1. controlled maintenance;
2. re-encryption planning;
3. complete coverage of broker credential/token records;
4. verification before the old key is retired.

Do not rotate the production encryption key casually.

---

## 7. OAuth security

StrikeNova has security-sensitive OAuth flows for broker and Google authentication.

### 7.1 Broker OAuth state

Broker OAuth state is HMAC-signed when it carries session binding information.

The signed payload can include:

- initiating session;
- broker identifier;
- timestamp.

The server validates the signature, expiry, and pending-state presence before accepting the callback.

Consumed state is removed so it cannot be replayed through the same pending-state path.

### 7.2 Callback identity binding

The authenticated-first broker flow binds OAuth initiation to the initiating session.

The callback must resolve the same user/connection context rather than guessing the owner from an arbitrary active session.

### 7.3 Google OAuth nonce binding

The current authentication design includes backend-generated, HMAC-signed Google OAuth state containing a nonce.

The nonce returned to the frontend is later checked against the Google identity token's nonce so an ID token from an unrelated authentication attempt cannot silently satisfy the current login flow.

### 7.4 OAuth failure behavior

Invalid, expired, tampered, missing, or already-consumed authentication state must fail closed.

Do not downgrade a malformed signed state into a weaker unsigned flow.

---

## 8. Authorization and user isolation

Authentication answers **who is calling**.

Authorization answers **what that caller may access**.

Every protected data operation must enforce both.

### 8.1 Required ownership checks

User-scoped operations must constrain database queries by the canonical authenticated user_id.

This applies to:

- paper accounts;
- trades;
- executions;
- orders;
- positions;
- transactions;
- strategy-related state;
- GEX snapshots;
- broker connections;
- session records;
- tokens.

### 8.2 Cross-user access must fail

A request must not succeed merely because the caller knows another record's numeric/string ID.

An object identifier is not an authorization credential.

The correct security pattern is:

    authenticated user
           +
    requested resource ID
           |
           v
    ownership-constrained lookup
           |
           v
       allow or reject

### 8.3 No unsafe fallback

When the preferred user-owned resource is missing, the backend must not:

- select the first global record;
- select another user's connection;
- reuse an arbitrary broker token;
- fall back to another session;
- infer ownership from the client.

---

## 9. API security

### 9.1 Server-side validation

All externally supplied inputs must be validated on the server.

Pydantic/request validation is not a substitute for business-rule enforcement.

Security-sensitive business rules belong at or below the service/domain boundary so alternate API routes cannot bypass them.

### 9.2 Execution authorization

Paper execution endpoints must preserve:

- authentication;
- user ownership;
- market-status gating;
- authoritative server pricing;
- quantity/exposure validation;
- idempotency;
- transaction consistency.

Adding a new endpoint that writes execution state without these controls is a security defect.

### 9.3 Error responses

Error messages should reveal enough information for safe client behavior but should not disclose:

- credentials;
- access tokens;
- encryption material;
- internal authentication secrets;
- another user's protected data.

Provider errors should be sanitized where necessary before being exposed to clients.

---

## 10. CORS and browser boundary

The backend uses explicit CORS origins derived from configured frontend origins.

The current middleware:

- allows configured origins;
- allows credentials;
- allows the application HTTP methods required by the product;
- allows Content-Type and X-Session-Id headers.

Production origins should remain explicit.

ALLOW_LOCALHOST_CORS is intended for development and must not become an accidental production-wide trust setting.

Never replace the explicit-origin model with unrestricted wildcard origins while credentials are enabled.

---

## 11. Authentication documentation and API docs

FastAPI interactive API documentation is disabled unless the configured debug mode enables it.

This reduces accidental exposure of the interactive API surface in production.

When debugging or enabling API documentation in a non-local environment, treat the exposure as an operational security decision.

---

## 12. Password security

Where email/password authentication is used, passwords are not stored as plaintext.

The current implementation:

- generates a random salt;
- uses PBKDF2-HMAC-SHA256;
- uses 480,000 iterations;
- stores the iteration count, salt, and derived digest;
- compares derived digests using constant-time comparison.

Password verification failures must not leak whether an account or credential component exists beyond what the application's authentication contract intentionally exposes.

### 12.1 Anonymous registration cannot link credentials

An unauthenticated registration request is not proof of ownership of an existing account. `POST /auth/register` therefore fails closed for every pre-existing email address with the same generic `409` response, regardless of the account's identity source (local email/password, Google, or broker OAuth):

- it does not create or replace `password_hash`;
- it does not overwrite `display_name`, status, identity source, verification state, or broker associations;
- it does not create a session;
- it does not reveal whether the address belongs to a local or OAuth-created account.

Linking a local password to an OAuth-created account requires a separate, explicitly designed flow that proves ownership — authenticated plus recent authentication, or a verified single-use, expiry-bound token. Supplying an existing address is never sufficient proof on its own.

---

## 13. Paper-trading security boundary

Paper trading is not merely a UI feature; it is a stateful financial simulation domain.

The security model therefore treats:

- order acceptance;
- fill price;
- fill quantity;
- position changes;
- cash changes;
- P&L;
- journal updates

as protected server-side state.

### Required properties

1. The frontend cannot declare a fill successful by itself.
2. Client-provided prices cannot override the authoritative execution price unless an explicitly supported, validated path permits it.
3. Closed or unknown market state must reject execution.
4. Replayed requests must not produce duplicate financial effects.
5. Cross-user position/exposure references must be rejected.
6. Concurrent state changes must preserve transaction and locking guarantees.

---

## 14. Broker capability separation

Broker connections track distinct capability concerns such as:

- authentication;
- market-data access;
- trading access.

The existence of a valid broker connection does not automatically mean every capability is active.

Code must check the capability needed for the operation.

For example, a read-only data credential must not be assumed to authorize trading.

---

## 15. GEX and analytics security

GEX is an analytics domain, but the same ownership and authentication rules apply.

### User-scoped GEX

Authenticated GEX snapshots must be scoped to the authenticated user.

### Background capture

Background GEX capture must require explicit:

- user identity;
- broker connection;
- usable authorization;
- configured capture enablement.

The capture loop must not choose an arbitrary broker connection belonging to another user.

### Data handling

Market analytics must preserve provenance and ownership distinctions.

A derived analytical record must not accidentally become an authorization bypass merely because its contents are non-transactional.

---

## 16. Logging and observability

Logging is required for security-relevant operational events, but logs are not a credential store.

Safe examples include:

- event type;
- outcome;
- sanitized provider error code;
- timestamp;
- short non-sensitive identifier/prefix;
- request/correlation identifier where available.

Never log:

- passwords;
- broker API secrets;
- access tokens;
- refresh tokens;
- encryption keys;
- full session IDs;
- authorization codes;
- signed OAuth state when it contains reusable authentication material.

Review new logging code specifically for accidental secret leakage.

---

## 17. Background-task security

Background tasks operate without a browser request and therefore must not invent user context.

A background process must explicitly know:

- which user it is operating for;
- which broker connection it may use;
- what capability is required;
- what authorization artifact is valid;
- what data may be persisted.

The current GEX capture loop is intentionally configured with a specific GEX_USER_ID and resolves an explicit default connection.

Future background jobs must follow the same principle rather than scanning all users or choosing arbitrary credentials.

---

## 18. Database security

### 18.1 Schema authority

Alembic is the authoritative production schema mechanism.

Security-sensitive constraints, indexes, ownership fields, uniqueness rules, and foreign keys must be migrated deliberately.

### 18.2 Data integrity

Security includes integrity, not only confidentiality.

Changes involving identity, broker connection, execution, positions, or transaction records must preserve transaction boundaries and concurrency controls.

### 18.3 Sensitive columns

Sensitive database fields must be classified deliberately.

Examples include:

- password hashes;
- encrypted broker credentials;
- encrypted broker tokens;
- refresh tokens;
- session hashes.

Encrypted-at-rest does not mean the plaintext may be freely exposed elsewhere in the application.

---

## 19. Frontend security rules

The frontend must be treated as an untrusted execution environment.

Do not put into browser-visible code:

- broker API secrets;
- encryption keys;
- backend-only credentials;
- server authorization decisions;
- private provider tokens that do not need to reach the browser.

The browser may hold the session identifier necessary for the application's authentication transport, but backend authorization remains mandatory.

Do not treat hidden UI controls as access control.

---

## 20. Configuration security baseline

The following configuration categories are security-sensitive:

- TOKEN_ENCRYPTION_KEY;
- broker API keys/secrets;
- broker tokens;
- database credentials/URLs;
- frontend/backend origins;
- OAuth configuration;
- Google authentication configuration;
- debug flags;
- CORS development switches.

Operational rules:

- secrets belong in protected environment configuration;
- do not commit .env or secret values;
- production configuration should be explicitly reviewed;
- development-only flags must not be silently enabled in production;
- changing security-critical configuration requires verification.

---

## 21. Threat categories

The architecture should explicitly defend against at least these classes of failure:

| Threat | Primary control |
|---|---|
| Session theft/reuse | Strong random session IDs, expiry, revocation, ownership checks |
| Session mix-up | Canonical UserSession → User.id resolution |
| OAuth CSRF/state tampering | HMAC-signed state and pending-state validation |
| OAuth replay | Expiry + consume-on-use state handling |
| Google ID-token replay across auth attempts | Nonce binding |
| Cross-user resource access | User-scoped authorization queries |
| Broker credential disclosure | Encryption at rest + secret-safe logging |
| Duplicate paper execution | client_order_id idempotency |
| Client-side price manipulation | Server-authoritative market price resolution |
| Execution outside market session | Backend execution-time market gate |
| Concurrent state corruption | Transactions, constraints, locks, and concurrency tests |
| CORS over-permission | Explicit configured origins |
| Production debug/API-doc exposure | Debug-gated API documentation |
| Historical/analytics data leakage | Explicit ownership and data-source boundaries |

This table is a baseline, not a complete threat model.

---

## 22. Security-sensitive changes require additional review

Treat a change as security-sensitive when it touches:

- backend/app/identity.py;
- backend/app/routers/auth.py;
- backend/app/routers/deps.py;
- backend/app/services/token_store.py;
- backend/app/crypto.py;
- broker connection/token models;
- authentication dependencies;
- CORS settings;
- session cookies/headers;
- OAuth state or nonce handling;
- user-scoped query logic;
- execution authorization;
- encryption configuration;
- secret handling/logging;
- database constraints protecting ownership;
- production security configuration.

For these changes, normal functional tests are not enough. Verify both the positive path and the relevant rejection/isolation path.

---

## 23. Security verification expectations

A security-sensitive change should, where applicable, verify:

### Authentication

- valid session succeeds;
- missing session is rejected;
- expired session is rejected;
- revoked session is rejected;
- inactive/suspended user is rejected.

### Authorization

- user can access owned data;
- user cannot access another user's data;
- object IDs alone cannot bypass ownership;
- missing resources do not trigger unsafe fallback.

### OAuth

- valid signed state succeeds;
- tampered state fails;
- expired state fails;
- replayed state fails;
- incorrect nonce fails.

### Secrets

- encrypted values decrypt correctly with the configured key;
- plaintext secrets do not appear in logs or API responses;
- key-mismatch behavior fails safely.

### Trading

- market-closed execution is rejected;
- unknown market state is rejected;
- duplicate idempotency requests do not double-apply state;
- cross-user execution references are rejected;
- concurrent updates preserve consistency.

### Browser boundary

- allowed origins work;
- unauthorized origins are rejected;
- production does not accidentally enable development CORS behavior.

---

## 24. Incident-response baseline

When a secret or authentication artifact is suspected to be compromised:

1. stop further exposure;
2. identify the affected secret class and scope;
3. revoke/rotate the affected credential where supported;
4. rotate TOKEN_ENCRYPTION_KEY only through the controlled re-encryption procedure when applicable;
5. invalidate affected sessions/tokens;
6. inspect logs and recent changes for exposure paths;
7. record the incident and remediation;
8. add a regression check where a code defect caused the exposure.

Do not hide a security incident by deleting only the visible artifact from Git history or logs while leaving the credential active.

---

## 25. Security change-control rule

Security architecture must not drift through small unreviewed changes.

When a proposed implementation weakens a security invariant:

- do not silently implement the weaker behavior;
- record the conflict;
- inspect the relevant decision/invariant;
- obtain the required architectural/security approval;
- update the decision/invariant record if the change is intentional;
- add verification covering the new boundary.

Security is part of correctness.

---

## 26. Security principle

The secure default for StrikeNova is:

**Authenticate explicitly → authorize by canonical user identity → keep secrets server-side and encrypted → enforce domain rules at the backend boundary → fail closed → verify both success and rejection paths.**
