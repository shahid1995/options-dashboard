# StrikeNova — Changelog

> **Purpose:** Record meaningful product, architecture, security, data, and operational changes that future developers and AI agents need to understand.
>
> **Status:** Active
>
> This is a curated architectural/product changelog, not a replacement for Git history. Commit-level history remains in GitHub.

---

## How to use this file

Add an entry when a change materially affects:

- architecture or deployment topology;
- authentication, authorization, or security boundaries;
- database/data ownership or lifecycle;
- paper-trading behavior or financial-state invariants;
- broker integration/capabilities;
- GEX or other quantitative methodology;
- historical-data collection;
- major public product behavior;
- AI-agent operating contracts;
- a decision that future maintainers need to remember.

Do not add entries for:

- routine formatting;
- trivial refactors with no behavior or architectural effect;
- every individual commit;
- temporary debugging;
- generated files;
- ordinary dependency patch updates unless they materially change behavior or risk.

---

## 2026-10-19 — ChatGPT Markdown-only engineering-supervisor boundary

- Made the accepted ChatGPT authoring boundary explicit in the `main` branch
  control documents: planning, research, review, verification interpretation,
  coding-agent supervision, issue/PR preparation, and Markdown documentation
  are allowed; direct creation or modification of non-Markdown repository
  files is prohibited.
- Added the rule to `AGENTS.md`, linked it as `INV-035` in `INVARIANTS.md`,
  and surfaced it in `AI.md` and `PROJECT-CONTROL.md`.
- The Spec Kit constitution path used on the Day-35 feature branch does not
  exist on `main`; this change deliberately does not import unrelated
  feature-branch scaffolding or create a duplicate governance document.
- Documentation-only. No code, tests, scripts, configuration, dependencies,
  workflows, migrations, SQL/schema files, deployment, or production data were
  changed.

---

## Current baseline

### 2026-09 — Runtime topology

- Frontend runtime/deployment: Vercel.
- Backend runtime/deployment: Render.
- Production database: CockroachDB.
- The application should not be described as currently hosted on Railway.

### 2026-09 — AI workspace documentation baseline

Established a root-level documentation system for AI-assisted engineering:

- AI.md — entry point and document map.
- AGENTS.md — AI coding/operating rules.
- CONTEXT.md — current repository context.
- ARCHITECTURE.md — system relationships and topology.
- INVARIANTS.md — non-negotiable correctness/security properties.
- DECISIONS.md — durable architectural decisions.
- SECURITY.md — security baseline.
- DATA.md — data ownership and lifecycle.
- TESTING.md — verification architecture.
- PROJECT-CONTROL.md — project governance and control.

These documents are complementary. They should not be treated as interchangeable copies of the same context.

### 2026-09 — Identity and broker boundary

- StrikeNova identity is distinct from broker identity.
- Broker connections and tokens are user-scoped.
- Authentication and authorization are backend responsibilities.
- Broker credentials/tokens require protected server-side handling.

### 2026-09 — Paper execution authority

- Paper execution is server-authoritative.
- Client input does not by itself establish fill success, financial state, or ownership.
- Market-status gating and transactional consistency are part of the execution boundary.
- Idempotency and concurrency behavior are treated as correctness requirements.

### 2026-09 — GEX methodology

The repository's current GEX convention is:

    raw_gex = gamma × open_interest × spot² × 0.01

Open interest is treated as contracts under this convention, and no lot-size multiplier is added to the formula.

Changes to this convention require an explicit decision and corresponding regression/quant verification.

### 2026-09 — Historical-data collection posture

Historical/capture behavior is configuration-controlled.

Current defaults observed in the application include:

- IV_HISTORY_ENABLED=false
- GEX_HISTORY_ENABLED=false
- GEX_CAPTURE_ENABLED=false
- CANDLE_BACKFILL_ENABLED=false

Historical collection should therefore not be enabled implicitly merely because the supporting code exists.

### 2026-09 — Database schema authority

- SQLAlchemy defines the ORM model.
- Alembic is the authoritative production schema migration mechanism.
- Production persistence is the configured CockroachDB environment.
- Local development may use SQLite when DATABASE_URL is absent.

Do not treat startup table creation or local SQLite behavior as the production schema authority.

---

## Change-entry template

Use this structure for future material changes:

### YYYY-MM-DD — Short change title

**Category:** Architecture | Security | Data | Quant | Product | Operations | AI Governance

**Change**

Describe what changed in one or two precise paragraphs.

**Why**

State the problem, requirement, or decision that caused the change.

**Impact**

Identify affected components, data, APIs, users, or operational behavior.

**Verification**

Record the relevant tests, builds, browser/runtime checks, migrations, or deployment verification.

**Related decision/invariant**

Link or name the relevant DECISIONS.md / INVARIANTS.md entry when applicable.

**Commit/PR**

Record the Git commit or pull request when the change has one.

---

## Maintenance rules

1. Keep entries factual and concise.
2. Describe the resulting contract, not an agent's internal reasoning.
3. Preserve historical entries; correct factual errors explicitly rather than silently rewriting history.
4. If an architectural decision is reversed, record the reversal as a new entry and update DECISIONS.md.
5. If an invariant changes, update INVARIANTS.md and record the change here.
6. Do not copy secrets, credentials, private user data, or production connection strings into this file.
7. Do not use this file as a substitute for tests, issue acceptance criteria, or Git history.
8. Every material entry should be traceable to a repository change, decision, or verified operational event.

---

## Historical record boundary

Older project notes, audits, screenshots, and issue comments may contain useful evidence about how StrikeNova evolved.

They are not automatically current truth.

When historical documentation conflicts with the current repository:

1. inspect the current implementation;
2. inspect current authoritative decisions/invariants;
3. determine the actual current behavior;
4. update the relevant living document if necessary;
5. preserve historical evidence rather than presenting it as current state.

---

## Changelog principle

**Record what future maintainers need to know — not everything that happened.**