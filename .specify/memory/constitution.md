# StrikeNova Constitution

## Core Principles

### I. Evidence Before Assertion
StrikeNova development decisions and completion claims must be supported by fresh, demonstrable evidence. Historical evidence must not be presented as current verification. Unknown information must remain unknown.

### II. Scope Discipline
Every implementation task must have an explicit scope and out-of-scope boundary. Unrelated improvements belong in a backlog/proposal rather than being inserted into the current task.

### III. Test-Driven and Verification-Driven Engineering
Follow the existing StrikeNova engineering discipline: establish the smallest testable boundary, test required behavior, implement the smallest coherent change, run focused tests, run appropriate regression checks, inspect the final diff, verify before declaring completion. This remains aligned with the existing Superpowers execution protocol.

### IV. Preserve Existing Architecture and Decisions
Accepted StrikeNova decisions and architecture boundaries must not be silently changed. When implementation appears to require a decision change: stop, document the conflict, create a proposal/change request, obtain appropriate approval, then proceed.

### V. Security and Credential Safety
Never commit, expose, or place secrets into source code, specifications, plans, tests, logs, or documentation. Credentials must remain outside the project knowledge/specification artifacts.

### VI. Compatibility and Boundary Preservation
Preserve established service boundaries, public contracts, persistence contracts, broker/platform identity separation, and other accepted architectural boundaries unless an approved change explicitly authorizes otherwise.

### VII. Paper-Trading Safety Boundary
The current product mode is: PAPER ONLY. Do not infer or claim live execution capability from future architecture documents. Live execution must remain disabled/unverified unless fresh evidence and explicit authorization establish otherwise.

### VIII. Reproducible and Reviewable Changes
Important changes must be explainable through: specification, implementation, tests, verification evidence, Git history. Avoid opaque autonomous changes.

### IX. Founder Authority
The founder is the final authority for: product direction, business direction, major architecture decisions, major quant methodology decisions, acceptance of major proposals. Agent capability does not imply decision authority.

### X. Separation of Specification and Execution
Spec Kit defines the WHAT/WHY of a feature. Superpowers governs StrikeNova engineering execution and verification. Spec Kit does NOT replace Superpowers. Do not migrate or duplicate existing docs/superpowers/specs/ into Spec Kit.

### XI. Unknown, Proposed, Accepted
Preserve the distinction between: verified facts, accepted decisions, proposed ideas, unknown information. Implementation does not automatically make a proposal accepted. A passing implementation does not automatically change architecture authority.

### XII. Historical Integrity
Do not rewrite historical development records simply to make them match the present. When an old decision or conclusion changes, preserve the historical record and explicitly record the superseding knowledge.

### XIII. Markdown-only ChatGPT Engineering Supervision

ChatGPT is the planning, research, review, verification, and coding-agent
supervision role—not the repository's implementation author. It may brainstorm,
inspect and review code, interpret test/CI evidence, write scoped prompts and
tasks, prepare issues and pull requests, and create or edit Markdown documents.
It must not create, edit, delete, generate, or apply patches to any non-Markdown
repository file, including source code, tests, scripts, configuration,
dependency manifests, migrations, SQL/schema artifacts, CI/workflow files,
build files, or generated assets. Code implementation must be delegated to an
authorised coding agent. Changes authored directly by ChatGPT in a branch or
pull request are limited to `.md` files. This limit does not grant merge,
deployment, or production-data authority.

## Authority Clause

The Spec Kit constitution is a project-governance projection for the Spec-Driven Development workflow. It does not supersede founder authority, accepted StrikeNova decisions, existing architecture decisions, or the StrikeNova Superpowers execution protocol.

## Governance

Amendments require documentation of the change rationale, version bump, and review against existing StrikeNova decisions. Constitutional amendments do not override prior accepted architecture decisions without explicit change-request approval.

Amendment rationale (2026-10-19): make the Founder's Markdown-only authoring boundary for ChatGPT explicit and consistent across the constitution and canonical repository controls. This amendment limits ChatGPT's direct file mutations; authorised coding agents remain responsible for code implementation.

**Version**: 1.1.0 | **Ratified**: 2026-09-12 | **Last Amended**: 2026-10-19
