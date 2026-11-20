# StrikeNova — Testing and Verification

> **Purpose:** Define the verification system for StrikeNova: which checks exist, what each layer proves, when to run them, and what evidence is required before work is considered complete.
>
> **Status:** Active
>
> **Core principle:** A test passing is evidence for the behavior it exercises. It is not evidence for unrelated behavior.

---

## 1. Verification model

StrikeNova uses layered verification:

    Focused regression test
            ↓
    Affected domain/API tests
            ↓
    Security / ownership / migration checks
            ↓
    Full frontend/backend suites as appropriate
            ↓
    Production build
            ↓
    Browser/runtime verification where applicable
            ↓
    CI
            ↓
    Acceptance

The required layer depends on the risk and scope of the change.

---

## 2. Test environments

### Backend

- Python 3.13
- pytest
- pytest-asyncio with asyncio_mode = auto
- SQLAlchemy test databases where appropriate
- SQLite/in-memory test setups for isolated tests
- PostgreSQL/Neon production integration expectations for persistence-sensitive work (CockroachDB is retired as a production target per ADR-019 and is no longer a required target for the migration-lease protocol or its future acceptance tests); SQLite/in-memory setups remain for isolated tests

### Frontend

- Node.js 20 in CI
- Vitest 4.1.10
- JSX parsing for .js/.jsx
- @ alias mapped to the frontend root
- Next.js production build

### CI

GitHub Actions currently verifies:

- backend imports;
- selected backend GEX tests;
- selected backend candle tests;
- full frontend Vitest suite;
- frontend Next.js production build.

The workflow is defined in options-dashboard-project/.github/workflows/ci.yml.

---

## 3. Frontend verification

### 3.1 Unit/component tests

Primary command:

    cd options-dashboard-project/frontend
    npx vitest run

Package-script equivalent:

    npm test

These tests are appropriate for:

- component behavior;
- rendering contracts;
- utility functions;
- calculation helpers;
- state transformations;
- UI interaction behavior.

### 3.2 Coverage

    cd options-dashboard-project/frontend
    npm run test:coverage

Coverage helps identify untested areas. Coverage percentage is not, by itself, a correctness criterion.

### 3.3 Build

    cd options-dashboard-project/frontend
    npx next build

The build verifies route compilation, module resolution, production bundling, and build-time integration.

---

## 4. Backend verification

### 4.1 Full backend suite

    cd options-dashboard-project/backend
    python -m pytest -q

This is the broad regression check for backend work.

### 4.2 Focused backend tests

    python -m pytest tests/<relevant_test_file>.py -q

Use the smallest relevant subset first for fast feedback.

### 4.3 Import/startup smoke test

The current CI performs:

    python -c "from app.main import app; print('App starts OK, routes:', len(list(app.openapi()['paths'].keys())))"

This catches import, startup wiring, and route-registration failures. It does not replace behavioral tests.

---

## 5. Domain-specific verification

### Authentication and identity

Verify, where applicable:

- valid session;
- missing session;
- expired session;
- revoked session;
- inactive/suspended user;
- correct user resolution;
- cross-user rejection.

OAuth changes should additionally verify valid, tampered, expired, replayed, and incorrectly bound state.

### Broker integration

Verify provider registration, adapter creation, capability mapping, provider error mapping, authentication failure behavior, token isolation, and unsupported capability behavior.

### Paper execution

Where applicable, verify:

- market-open execution;
- market-closed rejection;
- unknown market-state rejection;
- authoritative server pricing;
- quantity and exposure validation;
- atomic success;
- failure without partial-write residue;
- idempotent replay;
- cross-user rejection;
- concurrency;
- exit behavior;
- journal/portfolio reconciliation.

An HTTP 200 response alone is insufficient evidence for a financial-state mutation.

### Database and migration

Verify:

- migration applies;
- migration is safe to rerun where expected;
- existing data remains compatible;
- constraints/indexes are present;
- startup schema behavior is correct;
- no request-path DDL is introduced.

Relevant areas include test_alembic_migrations.py and test_db_migration.py.

### GEX and quant calculations

The current GEX contract is:

    raw_gex = gamma × open_interest × spot² × 0.01

Verify formula, sign convention, invalid-input behavior, data-quality exclusions, expiry/instrument identity, and historical consistency where applicable.

Expected values should be independently derived where practical.

### Historical-data pipelines

Verify normalization, validation, retry behavior, idempotent persistence, checkpoints, completeness reporting, raw/derived separation, and source-specific failures.

---

## 6. Security verification

Security-sensitive changes require negative-path testing.

| Area | Positive test | Negative test |
|---|---|---|
| Session | valid session succeeds | expired/revoked session rejected |
| Ownership | owner can access | another user rejected |
| OAuth | valid state accepted | tampered/replayed state rejected |
| Broker credential | correct connection resolves | another user's credential cannot resolve |
| Paper execution | authorized execution succeeds | cross-user execution rejected |
| CORS | configured origin works | unauthorized origin rejected |

Security tests must not rely on frontend hiding or disabling UI controls.

---

## 7. Concurrency verification

Concurrency is a correctness concern for order execution, exits, position updates, idempotency, journal attribution, cash ledger updates, session/token state, and database writes.

When a change touches concurrent state:

1. identify the race;
2. reproduce it where practical;
3. verify locking, constraints, and transactions;
4. test duplicate and competing requests;
5. inspect final persisted state.

---

## 8. Browser and runtime verification

Unit tests cannot prove browser rendering, responsive behavior, routing, focus/keyboard behavior, cross-origin auth handoff, runtime API integration, or actual deployment behavior.

For user-visible or route-sensitive changes, browser verification should check:

- route loads;
- no blocking console/runtime errors;
- primary content renders;
- relevant interaction works;
- responsive behavior remains acceptable when changed;
- public/authenticated boundaries behave correctly.

Use the project's established Playwright/agent-browser workflow where available.

Do not claim browser verification because a build passed.

---

## 9. API verification

For backend endpoint changes, verify request validation, authentication, authorization, response shape, error status, domain side effects, persistence, and idempotency where applicable.

For state-changing endpoints, inspect persisted state after the request.

---

## 10. CI verification

GitHub Actions is the repository's automated verification layer.

### Backend CI

- Python 3.13 setup;
- dependency installation;
- application import verification;
- selected GEX tests;
- selected candle tests.

### Frontend CI

- Node 20 setup;
- npm ci;
- full Vitest suite;
- Next.js production build.

CI passing means the configured checks passed. It does not mean browser behavior, production deployment, live broker behavior, or every backend test was verified.

---

## 11. Test selection by change type

| Change | Minimum appropriate verification |
|---|---|
| Documentation only | Read-back / consistency check |
| UI component | Focused Vitest + affected route/build |
| Public page | Focused tests + Next build + browser verification |
| Auth/session | Focused auth/security tests + backend suite + browser flow where applicable |
| Broker adapter | Adapter/domain + security/error-path tests |
| API endpoint | Focused API/domain + ownership/security tests |
| Paper execution | Focused execution + concurrency/idempotency + broader backend suite |
| Database schema | Migration tests + affected domain tests + broader backend verification |
| GEX formula | Quant tests + historical/data-quality regression tests |
| Historical ingestion | Pipeline + persistence/data-quality checks |
| Deployment/config | Startup/import + build + environment/runtime verification |

This table is guidance. Issue-specific acceptance and risk requirements still control.

---

## 12. Fresh evidence requirement

Verification evidence must be fresh enough to prove the current change.

Do not report an old test run, previous CI result, another agent's unverified claim, or a build as proof of unrelated behavioral correctness.

Completion reports should record:

- exact command;
- relevant scope;
- exit status;
- material result;
- known limitations.

---

## 13. Failure classification

Classify failures before deciding what to do:

- **Product defect** — changed behavior is incorrect.
- **Test defect** — test does not represent the intended contract.
- **Environment failure** — required tooling/service/configuration is unavailable.
- **Flaky/intermittent failure** — result is nondeterministic and needs investigation.
- **Pre-existing unrelated failure** — demonstrably outside the change scope.

Do not relabel product failures as environment noise without evidence.

---

## 14. Regression-test strategy

When fixing a defect:

1. reproduce it;
2. create or identify a regression test;
3. confirm the test fails for the intended reason where practical;
4. implement the smallest correction;
5. confirm the regression test passes;
6. run broader affected verification.

Regression tests should protect the contract rather than incidental implementation details.

---

## 15. Test-data rules

Test data should be deterministic where practical, minimal, synthetic unless real data is necessary, isolated from production data, and free of secrets.

Never copy live broker credentials, tokens, passwords, encryption keys, or production session identifiers into fixtures.

Multi-user security tests should use at least two distinct users.

---

## 16. Database-test rules

Use isolated test databases for service logic and appropriate integration databases for dialect-specific behavior, migrations, constraints, locking, and transaction semantics.

The closer a change is to database correctness or concurrency, the less acceptable it is to rely only on mocks.

---

## 17. Quant-test rules

Quant tests should use independently calculated expected values, reference cases, tolerance-aware comparisons, edge cases, and invalid-data tests.

A test should not derive the expected result from the same implementation branch it is supposed to validate.

---

## 18. Production verification boundary

Source verification and production verification are different.

Production verification may include deployed URL availability, backend health, API behavior, authentication handoff, database connectivity, broker integration behavior, and runtime error monitoring.

Deployment is separately authorized. Do not deploy merely to make a test pass, and do not report production verification unless the production environment was actually exercised.

---

## 19. Completion standard

A task is verification-complete when:

1. acceptance criteria are mapped to evidence;
2. affected code paths have appropriate tests;
3. required builds pass;
4. security/ownership checks pass where relevant;
5. browser/runtime checks pass where relevant;
6. failures are classified and disclosed;
7. the integrated state has been independently inspected.

Final reports should distinguish:

- **Verified** — freshly demonstrated;
- **Not run** — not executed;
- **Blocked** — required evidence could not be obtained;
- **Known failure** — a real failure remains.

Do not collapse these categories into a generic done state.

---

## 20. What should trigger broader verification

Expand verification beyond the narrow test when a change touches:

- authentication or sessions;
- user ownership;
- broker connections/tokens;
- paper execution;
- positions/orders/cash/P&L;
- migrations;
- concurrency;
- GEX methodology;
- historical-data ingestion;
- shared UI primitives;
- public/authenticated route boundaries;
- deployment/runtime configuration.

Shared infrastructure requires broader regression confidence because small local changes can affect many routes.

---

## 21. Verification command reference

### Frontend

    cd options-dashboard-project/frontend
    npm ci
    npm test
    npm run test:coverage
    npx next build

### Backend

    cd options-dashboard-project/backend
    pip install -r requirements.txt
    python -m pytest -q
    python -c "from app.main import app; print('App starts OK, routes:', len(list(app.openapi()['paths'].keys())))"

### Focused backend test

    python -m pytest tests/<test_file>.py -q

Use the repository's current Python/Node versions and environment configuration when reproducing CI locally.

---

## 22. Testing principle

StrikeNova verification should remain:

**Focused first → broad enough for the risk → independent where correctness matters → fresh → evidence-based.**

The objective is not to maximize the number of tests. The objective is to make it difficult for an implementation, especially an AI-generated implementation, to claim correctness without actually demonstrating it.