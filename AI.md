# StrikeNova — Project Control

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-18

---

## 1. Purpose

This is the **single entry point** for AI agents and engineers working on
StrikeNova. It defines who decides what, how work flows, and which documents are
authoritative. It does not restate the other control documents — each owns its
own scope.

If any other document disagrees with this one on **authority**, **scope**,
**workflow**, or **runtime topology**, this document wins and the other document
must be corrected. If it disagrees on **technical detail**, the code and its
tests win.

## 2. Authority hierarchy

| Role | Authority | Scope |
|---|---|---|
| **Founder** | Final Authority | Product direction, scope, acceptance, releases. Human-approved; never delegated to an agent. |
| **Obsidian Second Brain** | Knowledge/Context Authority | Durable reasoning, decision records, architecture context, research. |
| **GitHub** | Implementation/Project Authority | Issues, board status, code, PRs, commits, CI, verification evidence, releases. |
| **ChatGPT** | Engineering Supervisor | Plans, researches, reviews, verifies and supervises coding agents; writes scoped prompts/tasks and may prepare issues/PRs. Direct repository file changes are Markdown-only; implementation code is delegated. Details: [`AGENTS.md`](AGENTS.md). |
| **Hermes / FreeBuff** | Execution | Agents implement approved GitHub work. They never redefine accepted decisions or architecture without an explicit change record. |

Details: [`PROJECT-CONTROL.md`](PROJECT-CONTROL.md).

## 3. Runtime topology (current truth)

```text
Browser
   ↓  HttpOnly strikenova_session cookie (secure session transport)
Vercel  (frontend — Next.js)
   ↓  HTTPS REST + cookie-authenticated WebSocket
Render  (backend — FastAPI/uvicorn)
   ↓
CockroachDB  (production database; Alembic is the schema authority)
```

- **Vercel** hosts the frontend; **Render** hosts the backend; **CockroachDB
  Cloud** is the production database. Staging deployments follow the same shape
  (see `options-dashboard-project/docs/architecture/VERCEL_STAGING_DEPLOYMENT.md`
  and `RENDER_STAGING_DEPLOYMENT.md`).
- **Railway was a historical/staging experiment and is not current production
  truth.** Do not document Railway as the production platform.
- The backend remains portable (SQLite for local dev, PostgreSQL-compatible,
  CockroachDB-validated), but the **production** database is CockroachDB.
- **Broker OAuth (BYOB) is separate from StrikeNova platform identity.**
  Broker credentials never authenticate the platform; platform sessions are
  carried by the HttpOnly cookie only.

## 4. Control documents (canonical set)

| Document | Owns |
|---|---|
| [`PROJECT-CONTROL.md`](PROJECT-CONTROL.md) | Authority model, workflow, board, issue/PR contracts, DoD |
| [`AGENTS.md`](AGENTS.md) | Agent operating rules, anti-context-drift, execution contract |
| [`CONTEXT.md`](CONTEXT.md) | Product context, glossary, directory map |
| [`INVARIANTS.md`](INVARIANTS.md) | Invariants that must never regress |
| [`DECISIONS.md`](DECISIONS.md) | Accepted decision records (ADR-style) |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System architecture and module map |
| [`SECURITY.md`](SECURITY.md) | Security model, auth/session, broker separation |
| [`DATA.md`](DATA.md) | Data domains, schema authority, migrations |
| [`TESTING.md`](TESTING.md) | Test strategy, suites, CI gates |
| [`CHANGELOG.md`](CHANGELOG.md) | Chronological control-document history |
| [`.specify/memory/constitution.md`](.specify/memory/constitution.md) | Spec Kit principles, subordinate to Founder authority and the root control documents |

Single source of truth per topic. Do not duplicate governance elsewhere; link
instead. Durable decision records live in Obsidian; `DECISIONS.md` mirrors only
their engineering-effect.

## 5. Workflow (summary)

The canonical flow and its contracts live in
[`PROJECT-CONTROL.md`](PROJECT-CONTROL.md):

```text
Founder-approved decision → GitHub Issue → Project Board → implementation
→ branch → commits → pull request → automated verification → review/acceptance
→ merge → issue closed → release only when separately authorized
```

## 6. Non-negotiables (quick index)

1. Founder = final authority; agents execute, never decide.
2. Alembic is the sole schema authority; no ad-hoc DDL.
3. Broker OAuth (BYOB) stays separate from platform identity.
4. Platform session transport = HttpOnly `strikenova_session` cookie only.
5. Server-authoritative paper trading; the client never computes balances.
6. Preserve existing SQLite/PostgreSQL/CockroachDB compatibility.
7. Never modify production databases or deploy without explicit authorization.
8. Never read, print, or copy credential files (e.g. `.strikenova_gh_token`).
9. ChatGPT authors or modifies repository files only when they are Markdown; code implementation is delegated to an authorised coding agent.

Full lists: [`INVARIANTS.md`](INVARIANTS.md), [`SECURITY.md`](SECURITY.md).

## 7. If something is missing or contradictory

1. Check the code and its tests (they are ground truth for behavior).
2. Check [`DECISIONS.md`](DECISIONS.md) for the accepted decision.
3. If still unresolved, file a GitHub issue; the Founder decides.
4. Agents record unexpected findings as issue comments or follow-up issues —
   never silent scope expansion.
