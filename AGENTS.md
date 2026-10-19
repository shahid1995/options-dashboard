# StrikeNova — Agent Operating Rules

**Status:** Canonical · **Owner:** Founder · **Last reviewed:** 2026-09-18
**Applies to:** Hermes / FreeBuff and any AI agent working in this repository.

---

## 1. First principles

1. **Agents execute; they do not decide.** Product direction, scope,
   acceptance, and releases are human (Founder) decisions.
2. **The GitHub Issue is the execution contract.** Read the complete issue
   before implementing anything.
3. **The repository is ground truth.** Inspect current state; never assume
   historical context is still current.
4. **Tests are behavior truth.** Comments, docstrings, and old reports are not
   evidence — trace actual behavior and run the tests.

## 2. Anti-context-drift rules

Context drift — acting on stale assumptions about the codebase — is the primary
agent failure mode. Guards:

1. **Re-verify before relying.** Before changing code, confirm the current
   shape of the affected files, fixtures, and configuration on the branch you
   are on. Branches diverge; docs age; memory does not.
2. **No comment-as-proof.** Never accept a comment, docstring, README claim, or
   chat summary as proof of behavior. Reproduce it.
3. **Isolate before concluding.** When diagnosing failures, run the failing
   test in isolation, then in its file, then in the full suite. Interactions
   between tests are real defects too.
4. **Baseline before changing.** Capture the failure set before any edit so
   every later result is comparable ("no new failure signatures").
5. **Minimal reproducer before grouping.** Two failures may only be grouped
   under one root cause when a minimal reproducer demonstrates the shared
   mechanism.
6. **Root cause before correction.** Classification comes before fixes:
   production defect, stale test, fixture/environment defect, or missing
   schema contract. Never alter correct production behavior to satisfy an
   obsolete assertion.
7. **Scope fences are hard.** The issue's "do not modify" lists bind. Record
   tempting-but-out-of-scope findings as comments or follow-up issues.
8. **Documentation drift is a defect.** If reality and a control document
   disagree, fix the document through the normal workflow (issue → PR), not by
   silently editing unrelated work.

## 3. Execution contract (per issue)

1. Read the complete issue before implementation.
2. Inspect the current repository state rather than assuming historical
   context is still current.
3. Preserve accepted architecture and decision constraints
   ([`DECISIONS.md`](DECISIONS.md), [`INVARIANTS.md`](INVARIANTS.md)).
4. Stay inside the stated scope; protect unrelated local changes (use an
   isolated worktree when the working tree is dirty).
5. Record unexpected findings as comments or follow-up issues rather than
   silently expanding scope.
6. Use a branch associated with the issue.
7. Link the issue from the pull request.
8. Run the required verification before claiming completion — including the
   relevant CI gate; local passes are not CI evidence.
9. Report test/build/browser/security results with concrete evidence
   (commands, counts, run URLs).
10. Never treat an agent's implementation as final product authority.

### ChatGPT engineering-supervisor boundary (Markdown-only authoring)

ChatGPT's role is planning, brainstorming, research, architecture analysis, code
and diff review, test/CI evidence interpretation, writing coding-agent prompts
and scoped tasks, and preparing issues and pull requests. It may inspect code,
run verification, supervise authorised coding agents, and create or edit
repository Markdown documentation.

**ChatGPT must not create, edit, delete, generate, or apply patches to any
non-Markdown repository file.** The prohibition includes application/runtime
source, tests, scripts, dependency manifests, configuration, CI/workflow files,
migrations, SQL/schema artifacts, build files, and generated assets. There are
no exceptions for a small change, a test-only change, or a supposedly necessary
fix. Reading or testing code is allowed; modifying it is not.

When implementation work is needed, ChatGPT prepares the plan, acceptance
criteria, issue or agent prompt and lets the authorised coding agent perform
the code changes. ChatGPT then independently reviews the agent's diff and
verification evidence; any follow-up edits made directly by ChatGPT must be
limited to `.md` files. Any branch, commit, or PR whose file changes ChatGPT
authors must contain Markdown files only. This rule does not grant authority
to merge, deploy, change production data, or override the Founder's separate
approval requirements.

## 4. Verification honesty

- Report failures even when they are pre-existing; classify them, never hide
  them.
- Do not claim success based on local execution when the issue requires CI
  evidence.
- Never weaken a security or behavior assertion merely to make a suite pass;
  stale expectations are reconciled explicitly, with justification.
- PRs stay in Draft until review accepts them; agents do not merge their own
  work unless the issue explicitly instructs it.

## 5. Prohibitions

- Never read, print, copy, decode, or expose credential files (including
  `.strikenova_gh_token`).
- Never deploy or touch production databases without explicit authorization.
- Never merge unrelated branches or cherry-pick unrelated commits into an
  issue branch.
- Never create a second governance system, board, or duplicate control
  document. These documents are canonical; link, don't copy.
- Never introduce browser-readable session storage or session credentials in
  URLs, headers, or WebSocket subprotocols
  ([`SECURITY.md`](SECURITY.md)).

## 6. Where things live

- Authority model and workflow: [`PROJECT-CONTROL.md`](PROJECT-CONTROL.md)
- Entry point and topology: [`AI.md`](AI.md)
- Invariants: [`INVARIANTS.md`](INVARIANTS.md) · Decisions: [`DECISIONS.md`](DECISIONS.md)
- Product context: [`CONTEXT.md`](CONTEXT.md) · Testing: [`TESTING.md`](TESTING.md)

Historical engineering record: `options-dashboard-project/docs/` (evidence;
not duplicated here).
