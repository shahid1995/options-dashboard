# AGENTS.md

> **Purpose:** Operating rules for AI coding agents working in StrikeNova.
>
> This document defines **how an agent must work**. Product decisions, architecture decisions, and non-negotiable system properties remain governed by the other control documents.

## 1. Before Work

For substantive work, read:

1. `AI.md`
2. `AGENTS.md`
3. `PROJECT-CONTROL.md`
4. `CONTEXT.md`
5. Relevant `INVARIANTS.md) entries
6. Relevant `DECISIONS.md) entries
7. Relevant specialist documentation

Then inspect the actual repository state, current branch/diff, affected code, tests, and configuration.

Never rely on conversation memory as a substitute for repository inspection.

## 2. Scope Control

- Treat the GitHub Issue as the implementation contract.
- Implement only the requested scope.
- Preserve unrelated work.
- Do not perform broad refactors while solving a narrow issue.
- Record adjacent findings rather than silently expanding scope.
- Do not deploy, publish, rotate credentials, modify production data, or change live infrastructure unless explicitly authorized.

## 3. Architecture Discipline

- Read existing architecture before introducing new components.
- Reuse established boundaries and abstractions.
- Do not invent endpoints, models, services, dependencies, or deployment topology.
- Do not silently replace an accepted architectural decision with a preferred design.
- If a requirement conflicts with an invariant or accepted decision, stop and surface the conflict.

## 4. Security and Data

- Preserve authentication, authorization, tenancy, and user-isolation boundaries.
- Treat broker credentials, session tokens, and other secrets as sensitive.
- Never commit secrets or expose them unnecessarily to frontend code, logs, tests, or user-visible responses.
- Do not turn user-scoped data into shared data for convenience.
- Do not introduce historical collection or persistence merely because it is technically useful.

## 5. Quantitative and Trading Logic

For GEX, Greeks, pricing, execution, portfolio, positions, orders, and other decision-support calculations:

- identify the governing invariant and decision first;
- preserve units, instrument identity, expiry, timestamp, and sign conventions;
- do not change formulas without explicit approval;
- prefer independently derived expected values in tests;
- treat server-side execution state as authoritative.

Current GEX convention:

`raw_gex = gamma × open_interest × spot² × 0.01`

No lot-size multiplier is added under the current convention.

## 6. Implementation Pattern

Use this sequence:

1. Establish the change contract.
2. Inspect current behavior.
3. Identify affected invariants and decisions.
4. Reproduce the gap or establish a baseline.
5. Make the smallest coherent change.
6. Run focused verification.
7. Inspect the diff.
8. Run broader proportionate verification.
9. Report evidence and remaining limitations.

Prefer incremental, test-backed changes.

## 7. Verification

Do not claim completion from:

- reasoning alone;
- a previous test run;
- another agent's assertion;
- a successful build that does not exercise the changed behavior.

Fresh evidence should include the relevant command, result, and scope.

Classify failures honestly as product defects, test defects, environment failures, flaky failures, or unrelated pre-existing failures.

Use `TESTING.md) for the repository's verification strategy.

## 8. Documentation

Update living documentation when a material architectural, security, data, quantitative, or operational contract changes.

Use the existing documents instead of creating duplicates:

- `CONTEXT.md) — current system map
- `ARCHITECTURE.md) — structure and boundaries
- `INVARIANTS.md) — rules that must remain true
- `DECISIONS.md) — accepted rationale
- `SECURITY.md) — security controls
- `DATA.md` — data lifecycle
- `TESTING.md` — verification
- `PROJECT-CONTROL.md` — project workflow
- `CHANGELOG.md` — material history

## 9. Completion Report

Every substantive completion report should state:

- what changed;
- files/components affected;
- tests/build/browser/security verification performed;
- exact material failures or limitations;
- whether deployment was performed.

Do not imply deployment or production verification unless it actually occurred.

## 10. ChatGPT engineering-supervisor boundary (Markdown-only authoring)

ChatGPT's role is planning, brainstorming, research, architecture analysis, code
and diff review, test/CI evidence interpretation, writing coding-agent prompts
and scoped tasks, preparing issues and pull requests, and supervising authorised
coding agents. It may inspect code, run verification, and create or edit
repository Markdown documentation.

**ChatGPT must not create, edit, delete, generate, or apply patches to any
non-Markdown repository file.** This includes application/runtime source,
tests, scripts, dependency manifests, configuration, CI/workflow files,
migrations, SQL/schema artifacts, build files, and generated assets. There are
no exceptions for a small change, a test-only change, or a supposedly necessary
fix. Reading or testing code is allowed; modifying repository code is not.

When implementation work is needed, ChatGPT prepares the plan, acceptance
criteria, issue, PR materials, or coding-agent prompt and lets an authorised
coding agent perform the non-Markdown changes. ChatGPT then independently
reviews the resulting diff and verification evidence. Any branch, commit, or
PR whose repository file changes ChatGPT authors must contain Markdown files
only.

This boundary does not grant authority to merge, deploy, change production data,
or override the Founder's separate approval requirements.

## 11. Final Rule

**Do not guess when the repository can tell you. Do not change what was not authorized. Do not call work complete without evidence.**
