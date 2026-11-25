# StrikeNova — GitHub Project Control System

## Purpose

GitHub is the **Implementation and Project Authority** for StrikeNova.

This file defines how product work is planned, executed, reviewed, verified, and closed inside this repository without introducing an external project-management system.

## Authority model

- **Founder** — Final Authority. Product direction, scope, acceptance, and release decisions remain human-approved.
- **Obsidian Second Brain** — Knowledge and Context Authority. Long-lived reasoning, decisions, architecture context, research, and durable knowledge remain there.
- **GitHub** — Implementation and Project Authority. Issues, project status, code, pull requests, commits, CI, verification evidence, and releases live here.
- **ChatGPT** — Engineering Supervisor. Plans, researches, reviews code and verification evidence, writes coding-agent prompts/tasks, and prepares issues/PRs. Direct repository file changes are restricted to Markdown; non-Markdown implementation is delegated to an authorised coding agent. See [AGENTS.md](AGENTS.md).
- **Hermes / FreeBuff** — Execution. Agents implement approved GitHub work; they do not redefine accepted product decisions or architecture without an explicit change record.

## Canonical workflow

```text
Founder / approved decision
        ↓
GitHub Issue
        ↓
Project Board
        ↓
Agent / developer implementation
        ↓
Branch
        ↓
Commit(s)
        ↓
Pull Request
        ↓
Automated verification
        ↓
Review / acceptance
        ↓
Merge
        ↓
Issue closed
        ↓
Release / deployment when separately authorized
```

## Project board

Create one canonical GitHub Project for StrikeNova:

**Name:** `StrikeNova — Product & Engineering`

Primary board status flow:

1. **Backlog** — approved or captured work not yet selected.
2. **Ready** — sufficiently specified and ready for execution.
3. **In Progress** — active implementation/research.
4. **Review** — implementation complete and awaiting review.
5. **Verification** — review accepted; tests/browser/security/evidence are being completed.
6. **Done** — acceptance criteria are satisfied and the work is closed.
7. **Blocked** — cannot proceed without a decision, dependency, access, or external condition.

Recommended Project views:

- **Board** — operational flow using the statuses above.
- **Table** — canonical sortable backlog.
- **Roadmap** — milestones/releases and larger workstreams.

Recommended custom fields:

- `Work Type`: Feature / Bug / Task / Research / Security / Technical Debt / Documentation
- `Area`: Frontend / Backend / Quant / Trading / Auth / Infrastructure / AI / Business / Documentation
- `Priority`: Critical / High / Medium / Low
- `Effort`: XS / S / M / L / XL
- `Target`: Release or milestone name

Do not create multiple competing Projects for the same product unless there is a specific cross-repository need.

## Issue types

### Feature
A user-visible or product-capability change.

### Bug
A confirmed defect or regression.

### Task
A bounded implementation or maintenance unit that is not itself a product feature.

### Research
An investigation whose output is evidence, a decision, or a follow-up work item.

### Security
Security design, hardening, audit, threat, or compliance-related work.

### Technical Debt
A deliberate cleanup or structural improvement that does not immediately add product capability.

### Documentation
Project, architecture, operational, or user-facing documentation work.

## Issue rules

Every substantive work item should have:

- a clear objective;
- explicit scope;
- explicit out-of-scope boundaries;
- acceptance criteria;
- verification requirements;
- relevant architectural/decision references;
- a definition of done.

A vague issue should not move to **Ready**.

## Agent execution contract

Hermes / FreeBuff should treat the GitHub Issue as the execution contract.

Agents must:

1. Read the complete issue before implementation.
2. Inspect the current repository state rather than assuming historical context is still current.
3. Preserve accepted architecture and decision constraints.
4. Stay inside the stated scope.
5. Record unexpected findings as comments or follow-up issues rather than silently expanding scope.
6. Use a branch associated with the issue.
7. Link the issue from the pull request.
8. Run the required verification before claiming completion.
9. Report test/build/browser/security results with concrete evidence.
10. Never treat an agent's implementation as final product authority.

## Pull request contract

Every PR should explain:

- what changed;
- why it changed;
- scope and non-scope;
- linked issue(s);
- tests and verification performed;
- known limitations or follow-up work;
- deployment impact, if any.

A PR is not complete merely because code compiles. The acceptance criteria and required verification must be satisfied.

## Definition of Done

A work item reaches **Done** only when all applicable conditions are true:

- acceptance criteria satisfied;
- implementation is scoped to the issue;
- automated tests pass or known failures are explicitly classified;
- production build passes when applicable;
- browser/runtime verification passes when applicable;
- security checks pass when applicable;
- documentation/decision records are updated when required;
- PR review is complete;
- merged commit is identified;
- remaining follow-up work is captured as separate issues.

## Obsidian ↔ GitHub boundary

Use **Obsidian** for durable reasoning, context, research, and accepted decision records.

Use **GitHub** for executable project state and implementation evidence.

Do not copy the entire Second Brain into GitHub. Link to the relevant decision/document when context matters.

GitHub implementation does not automatically rewrite an accepted decision. A decision changes only through the established decision-governance process.

## Existing project history

The repository already contains a substantial historical engineering record under:

`options-dashboard-project/docs/`

The current status snapshot is:

`options-dashboard-project/docs/PROJECT_STATUS_CURRENT.md`

Historical phase plans, audits, specifications, and acceptance records remain evidence. They should not be recreated as duplicate open work unless a future issue explicitly reopens or supersedes the work.

## Migration rule

When this control system is introduced:

- **Completed historical work** → represent as closed issues only where traceability is useful.
- **Currently active work** → create/open issues and place them in the appropriate Project status.
- **Future work** → create issues only after the requirement is sufficiently understood.
- **Research questions** → use Research issues and capture findings before converting them into implementation work.

Do not bulk-create dozens of speculative issues.

## Change-control principle

The Project board answers **what is happening now**.

Issues answer **what exactly must happen**.

PRs and commits answer **what changed**.

CI and verification evidence answer **whether it works**.

Obsidian decision records answer **why the system is designed this way**.

This separation is intentional and should be preserved.
