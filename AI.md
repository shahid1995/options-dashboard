# AI.md

> **Purpose:** Entry point for AI agents working on StrikeNova.
>
> This file tells an AI agent **where to look, what has authority, and how to avoid context drift**. It is a navigation and operating document, not a duplicate of the project's architecture or implementation details.

## 1. Mission

StrikeNova is a serious full-stack options-market analytics and paper-trading application.

AI agents working in this repository must optimize for:

- correctness over speed;
- explicit requirements over assumptions;
- preservation of established architecture;
- security and user isolation;
- quantitative correctness;
- minimal, scoped changes;
- fresh verification evidence.

The agent is an implementation assistant, not the owner of product or architectural decisions.

## 2. Mandatory Reading Order

Before substantive implementation work, read the documents in this order:

1. **AI.md** — this entry point and navigation rules.
2. **AGENTS.md** — repository/agent operating instructions.
3. **PROJECT-CONTROL.md** — current project control contract and task workflow.
4. **CONTEXT.md** — current project state and implementation context.
5. **INVARIANTS.md** — non-negotiable rules.
6. **DECISIONS.md** — accepted architectural decisions and their rationale.
7. **Relevant specialist document(s):**
   - ARCHITECTURE.md — system structure and boundaries.
   - SECURITY.md — security model and trust boundaries.
   - DATA.md — data ownership, lifecycle, and persistence.
   - TESTING.md — verification strategy and required evidence.
   - CHANGELOG.md — curated history of material architectural/product changes.

Do not assume that a previous conversation, agent memory, or stale generated summary is more authoritative than the current repository documents.

## 3. Authority Hierarchy

When sources disagree, use this hierarchy:

1. **Founder-approved current requirements**
2. **INVARIANTS.md** for non-negotiable system rules
3. **DECISIONS.md** for accepted architectural decisions
4. **PROJECT-CONTROL.md** for current operating/task control
5. **CONTEXT.md / ARCHITECTURE.md** for current implementation context
6. **Source code and tests** as implementation evidence
7. **CHANGELOG.md** and historical commits as historical evidence

If the apparent conflict cannot be resolved from these sources, stop and surface the conflict rather than silently choosing a design.

## 4. Source-of-Truth Boundaries

- **GitHub:** implementation authority.
- **Obsidian:** knowledge/context authority.
- **Founder:** final authority for product and architectural decisions.
- **Primary Independent Engineering Reviewer (currently ChatGPT):** independently evaluates substantive engineering changes and verification evidence; this role advises the Founder and does not own implementation, product decisions, risk acceptance, merge, or release authority. See `PROJECT-CONTROL.md`.
- **Code:** evidence of what is implemented; it does not automatically redefine accepted decisions.
- **Tests:** evidence of verified behavior; passing tests do not authorize violating an invariant.
- **Historical documents/commits:** evidence of what existed previously, not automatically the current specification.

## 5. Current Deployment Facts

The current production topology is:

    User Browser
         |
         v
       Vercel
         |
         v
       Render
         |
         v
    CockroachDB

Do not reintroduce stale assumptions that the current backend is hosted on Railway or that the production database is PostgreSQL.

For authoritative details, consult ARCHITECTURE.md.

## 6. Core Non-Negotiable Areas

Before touching code in these areas, read the corresponding rules:

| Area | Required reference |
|---|---|
| Authentication / sessions / OAuth | SECURITY.md, INVARIANTS.md, DECISIONS.md |
| Broker integrations / credentials | SECURITY.md, DATA.md, INVARIANTS.md, DECISIONS.md |
| Paper execution / positions / orders | INVARIANTS.md, ARCHITECTURE.md, TESTING.md |
| GEX / Greeks / pricing / quantitative logic | INVARIANTS.md, DECISIONS.md, TESTING.md |
| Database / migrations | DATA.md, ARCHITECTURE.md, TESTING.md, INVARIANTS.md |
| Historical data collection | DATA.md, INVARIANTS.md, DECISIONS.md |
| Frontend UI / routing / accessibility | ARCHITECTURE.md, TESTING.md, INVARIANTS.md |
| Deployment / runtime | ARCHITECTURE.md, CONTEXT.md |
| AI-agent workflow | AGENTS.md, PROJECT-CONTROL.md, INVARIANTS.md |

## 7. Anti-Drift Rules

An AI agent must not:

- invent missing architecture from memory;
- assume a dependency, endpoint, route, model, service, or deployment target exists without checking;
- treat old documentation as current merely because it is detailed;
- make broad refactors for a narrowly scoped task;
- silently change a security, data-ownership, execution, or quantitative rule;
- introduce a new persistent data pipeline merely because it is technically convenient;
- expose broker credentials or sensitive tokens to the client unnecessarily;
- make client-side state authoritative for protected server state;
- claim a change is complete without fresh verification;
- deploy or modify live infrastructure unless explicitly authorized.

## 8. Required Work Pattern

For substantive implementation:

1. Read the mandatory documents relevant to the task.
2. Inspect the current repository state and existing implementation.
3. State the concrete change contract and affected boundaries.
4. Identify relevant invariants and accepted decisions.
5. Trace the current behavior before modifying it.
6. Make the smallest coherent change.
7. Run focused verification.
8. Inspect the resulting diff.
9. Run broader proportionate verification.
10. Report exactly what changed, what was verified, and what remains unverified.

If a proposed change conflicts with an invariant or accepted decision, do not silently override it.

## 9. Documentation Discipline

Do not create another governance document merely to solve information that belongs in an existing document.

Use:

- CONTEXT.md for **what the system is now**.
- ARCHITECTURE.md for **how the system is structured**.
- INVARIANTS.md for **what must remain true**.
- DECISIONS.md for **why accepted architectural choices were made**.
- SECURITY.md for **security boundaries and controls**.
- DATA.md for **data ownership and lifecycle**.
- TESTING.md for **how correctness is verified**.
- AGENTS.md for **agent behavior and repository operating rules**.
- PROJECT-CONTROL.md for **current task/control workflow**.
- CHANGELOG.md for **curated material history**.

Avoid copying the same rule into many documents. Cross-reference the authoritative location instead.

## 10. Change-Control Rule

If implementation requires changing an established invariant or accepted architectural decision:

1. Identify the conflict.
2. Explain why the current requirement cannot be satisfied without changing it.
3. Obtain explicit approval for the architectural change.
4. Record the new decision in DECISIONS.md.
5. Update affected invariants/documentation.
6. Add verification for the new behavior.

An AI agent must not resolve an architectural conflict by silently choosing its preferred implementation.

## 11. Final Principle

> **Read the repository. Respect the decisions. Preserve the invariants. Change only what is authorized. Prove the result.**

This document should remain short. Its job is to keep AI agents oriented and prevent context drift; detailed knowledge belongs in the specialized documents it points to.
