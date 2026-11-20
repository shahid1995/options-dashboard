# StrikeNova — GitHub Project Control System

## Purpose

GitHub is the **Implementation and Project Authority** for StrikeNova.

This file defines how product work is planned, executed, reviewed, verified, and closed inside this repository without introducing an external project-management system.

## Authority model

- **Founder** — Final Authority. Product direction, scope, acceptance, and release decisions remain human-approved.
- **Obsidian Second Brain** — Knowledge and Context Authority. Long-lived reasoning, decisions, architecture context, research, and durable knowledge remain there.
- **GitHub** — Implementation and Project Authority. Issues, project status, code, pull requests, commits, CI, verification evidence, and releases live here.
- **Hermes / FreeBuff** — Execution. Agents implement approved GitHub work; they do not redefine accepted product decisions or architecture without an explicit change record.
- **Primary Independent Engineering Reviewer (currently ChatGPT)** — Independent technical assessment of substantive engineering work, implementation evidence, tests, and material risks. The reviewer advises the Founder; it does not own product decisions, implementation, acceptance, or release authority.

## Primary Independent Engineering Reviewer

The Founder has designated **ChatGPT as StrikeNova's primary independent engineering reviewer**. This is a review and verification role, separate from Hermes / FreeBuff implementation work.

### Review responsibility

For substantive engineering changes, the reviewer independently assesses the actual proposed change and its evidence. Review depth must be proportionate to risk; authentication, authorization, broker credentials, user ownership, paper execution, positions/orders, quantitative calculations, persistence/migrations, concurrency, and deployment/runtime boundaries require especially careful review.

The reviewer must:

1. Establish the exact repository, PR, base commit, head commit, and changed-file scope.
2. Read applicable repository instructions, requirements, invariants, accepted decisions, architecture, security, data, and testing guidance.
3. Inspect the complete relevant diff and trace affected callers, consumers, schemas, configuration, error paths, and state transitions. A PR description or automated-review summary is not a substitute.
4. Evaluate specification compliance separately from engineering quality, including correctness, security, user/data isolation, quantitative integrity, test quality, maintainability, performance, and operability where relevant.
5. Challenge tests for independent expected values, meaningful negative/boundary cases, isolation, and ability to detect the original defect.
6. Run safe, relevant checks when tools and environments permit; otherwise identify exactly what evidence is missing and who or what must provide it.
7. Reconcile conflicting evidence and distinguish confirmed facts, reasoned inferences, historical information, and unverified claims.
8. Report findings before recommendations, ordered by severity, with confidence, exact location, trigger/preconditions, impact, supporting evidence, and the smallest remediation direction.
9. State the review verdict and all material coverage gaps. A zero-finding result must not hide files, behaviors, or verification layers that were not examined.

### Review verdicts

Use these explicit verdicts:

- **Pass** — the scoped requirements and material risks are supported by fresh, proportionate evidence, with no material unresolved finding.
- **Partial** — useful evidence exists, but a material path, environment, requirement, or verification layer remains unverified.
- **Fail** — direct evidence demonstrates a material requirement failure, defect, or unacceptable risk.
- **Inconclusive** — available evidence is conflicting or insufficient for a defensible decision.

Severity and confidence are separate. Use Critical / High / Medium / Low for impact and High / Medium / Low for confidence. Do not elevate style preferences into defects. Do not call a failure pre-existing or flaky without evidence.

### Independence and evidence

- The reviewer must inspect primary repository evidence and must not accept an implementer's summary, another AI agent's assertion, green CI, code coverage, or a clean automated-review result as proof beyond what that evidence actually exercises.
- Automated reviewers and scanners are supplementary signals. They can identify useful leads, but they do not replace this independent review.
- The reviewer must not review only selected files and describe the result as a full-PR review. The report must disclose any selection, truncation, inaccessible file, unavailable environment, or unrun check.
- Review findings are not implementation instructions to silently expand scope. Adjacent issues should be recorded and routed through the issue/decision process.

### Authority and action limits

The Founder remains the final authority for requirements, product and architecture decisions, risk acceptance, PR acceptance, merge, release, and deployment. The independent reviewer:

- does not redefine an accepted requirement or invariant;
- does not approve product scope or accept residual business risk on the Founder's behalf;
- does not implement fixes or modify application source by default;
- does not merge, deploy, change production/staging systems, mutate databases, enable providers, or access/use secrets unless the Founder separately and explicitly authorizes the specific action;
- may update Markdown governance documentation only when the Founder expressly requests or authorizes that documentation change.

Each material finding must receive a visible disposition: corrected with fresh evidence, demonstrated not to be a defect with supporting evidence, deferred to a tracked issue with explicit risk treatment, or explicitly accepted by the Founder. An unresolved Critical or High finding should normally block acceptance; any exception must be an explicit Founder decision recorded in the relevant GitHub issue or PR. A reviewer verdict is technical advice and evidence, not a merge permission.

### Review record

The PR or linked review report should record:

- exact base/head SHAs and scope reviewed;
- requirements and invariants considered;
- verdict and review coverage;
- findings with severity, confidence, location, evidence, impact, and remediation direction;
- commands/checks run, environment, exit status, and limitations;
- finding dispositions and unresolved risks;
- confirmation that no merge or deployment was performed as part of review.

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
Primary independent engineering review
        ↓
Finding disposition + re-verification when needed
        ↓
Founder acceptance
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
- the primary independent engineering review is complete for substantive engineering changes, and material findings have a documented disposition;
- Founder acceptance is recorded;
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
