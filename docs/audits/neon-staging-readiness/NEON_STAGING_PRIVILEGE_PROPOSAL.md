# Neon Staging Privilege Proposal

**Purpose:** Propose a staging-scoped least-privilege identity pair for the Neon `strikenova_staging` database, consistent with the *accepted* ADR-016 / ADR-017 / ADR-018, and document the PostgreSQL 16 role-membership conditions that make the proposal correct.

**Scope:** Documentation only. No role created, altered, granted, or dropped. No database contacted. **All SQL below is review-only and unexecuted.** A disposable PostgreSQL 16.15 rehearsal ([run `41813028902`](https://github.com/shahid1995/options-dashboard/actions/runs/41813028902)) executed the *earlier* revision of §6 — table and sequence defaults only — and validated arrangements A/B/C, 206 assertions, 0 failures. It did **not** execute the corrected statements below, and it did not contact Neon. Evidence: [`POSTGRESQL_16_PRIVILEGE_REHEARSAL.md`](POSTGRESQL_16_PRIVILEGE_REHEARSAL.md) §17–§41.

**Founder-approved application target** (application version approval: **COMPLETE**):
- Repository: `shahid1995/options-dashboard`
- Branch: `feat/strikenova-day35-portfolio-intelligence`
- Application commit: `9dc6e15c7b7c112ccb81b3be9c1609be66c92414`
- Expected Alembic head: `d49aa0000001`

**Authorization gates (all separate):**
| # | Gate | Status |
|---|---|---|
| 1 | Application version | **COMPLETE** |
| 2 | Staging privilege implementation (this document) | **PENDING** |
| 3 | Neon migration execution | **PENDING** |
| 4 | Render–Neon integration & deployment | **PENDING** |

Gate 1 authorizes *which application version*. It does not authorize creating roles, granting privileges, or applying migrations.

---

## 1. Accepted Architecture (ADR-016 / ADR-017 / ADR-018)

These are **Accepted** decisions in [`DECISIONS.md`](../../../DECISIONS.md), not proposals. This document links to them rather than restating them.

| Record | What it settles |
|---|---|
| **ADR-016** — Runtime/migration database identity separation (opt-in) | `STRIKENOVA_MIGRATION_DATABASE_URL` runs Alembic under a dedicated migration identity; the runtime credential can then be limited to DML with no schema ownership. Unset ⇒ historical single-identity behaviour. |
| **ADR-017** — Migration serialization via transactional lease lock | A single-row lease (`_migration_lock`) is acquired around Alembic execution using the migration identity. Bootstraps on an empty database. Requires `MIGRATION_LOCK_TTL_SECONDS >= 2`. |
| **ADR-018** — Production future-table privilege defaults (migrator-creator scope) | Default privileges are bound to the migration/creator role so future tables and sequences carry runtime DML (+ sequence `USAGE`). Runtime never receives CREATE/ALTER/DROP/ownership/admin/role-management. |

**Accepted production contract:** [`DATA.md` §5](../../../DATA.md) — "Production privilege model (database `strikenova`)" — is the accepted database-role contract. It documents `strikenova_production_app` (DML only), `strikenova_prod_migrator` (DDL + migration lock), `strikenova_prod_runtime` (NOLOGIN, owns the application tables), `strikenova_prod_admin` (break-glass), the two `pg_default_acl` rows bound to `strikenova_prod_migrator`, and the warning that **defaults do not follow a creator-role change**.

The production contract has **two** rows because it covers tables and sequences only. The staging design in §6.1 adds a **third**, global function-scoped row; that is a staging-scoped extension of ADR-018's accepted scope, not a restatement of the production contract, and it requires the Founder decision in §10 (item F).

It is *not* paper-trading data and it is not a staging document. This proposal mirrors its shape for staging; it does not create a second governance system (see `AGENTS.md` §5).

**Application support already exists (no source change required):** `app/config.py` defines `STRIKENOVA_MIGRATION_DATABASE_URL`; `app/db.py` resolves the migration URL via `resolve_migration_database_url()` (precedence: explicit/CLI → `STRIKENOVA_MIGRATION_DATABASE_URL` → `DATABASE_URL`) and uses it for the migration engine, the lease, and `_run_alembic_migrations()`; `alembic/env.py` delegates to the same resolver; `app/_migration_lock.py` implements ADR-017.

---

## 2. Observed Staging Role State (earlier read-only inspection)

**Source and date qualification.** The table below records an *earlier* read-only `pg_roles` inspection of Neon project `gentle-field-42671929`, database `strikenova_staging`. That inspection has no separately recorded timestamp in these artifacts; the only dated evidence surviving from the same pass is the Render startup-log inspection dated **2026-11-02**. **The catalog was not re-read during this documentation-only pass.**

| Role | LOGIN | SUPERUSER | CREATEDB | CREATEROLE | REPLICATION | BYPASSRLS | Origin |
|---|---|---|---|---|---|---|---|
| `cloud_admin` | yes | yes | yes | yes | yes | yes | Neon internal |
| `neon_service` | yes | no | yes | yes | yes | yes | Neon internal |
| `neon_superuser` | no | no | yes | yes | yes | no | Neon internal |
| `strikenova_staging_owner` | yes | no | yes | yes | **yes** | **yes** | Neon-created (provider) |

**Reading this correctly:**

- `strikenova_staging_owner` is the only application-present role. Its `replication`, `bypass_rls`, `createdb`, and `createrole` attributes exceed what a staging application connection needs. It is also the identity that *can* create roles (`CREATEROLE`), so gate 2 is executable through it if authorized.
- The `strikenova_prod_*` identities from `DATA.md` §5 belong to the **production** database `strikenova`. Their absence from a staging database is expected and is **not** an implementation failure.
- No broad break-glass *database login* is proposed for staging. Break-glass administration for staging stays with the authorized Neon provider account. (Production's `strikenova_prod_admin` is a production-only artifact.)

---

## 3. Proposed Staging Identities

| Identity | Type | Proposed role |
|---|---|---|
| `strikenova_staging_migrator` | Migration / creator | Owns and creates schema objects; runs Alembic and the ADR-017 lease bootstrap; is the role that `ALTER DEFAULT PRIVILEGES` must be bound to (ADR-018) |
| `strikenova_staging_app` | Runtime DML | `CONNECT`, schema `USAGE`, `SELECT/INSERT/UPDATE/DELETE` on tables, `USAGE` on sequences. No DDL, no ownership, no role management, no cluster attributes. **Not yet specified:** database `TEMPORARY` (held today through `PUBLIC`) and the fact that `CONNECT` is also a `PUBLIC` default, so an explicit `GRANT` is not connection control — both are open decisions, §10 |
| `strikenova_staging_owner` | Provider/setup | Neon-created. Left as-is unless a documented technical requirement justifies altering it |

Staging names are staging-scoped on purpose. Do **not** reuse `strikenova_prod_*` names for staging, and do not import production identities into `strikenova_staging`.

A read-only login is not proposed by default: no current application requirement establishes one, and an unused role is standing attack surface.

---

## 4. PostgreSQL 16 Role-Membership Semantics

This section is the part previous revisions of this document got wrong or omitted. Statements follow the PostgreSQL 16 documentation:
[`ALTER DEFAULT PRIVILEGES`](https://www.postgresql.org/docs/16/sql-alterdefaultprivileges.html) ·
[Role Membership](https://www.postgresql.org/docs/16/role-membership.html) ·
[`SET ROLE`](https://www.postgresql.org/docs/16/sql-set-role.html) ·
[`GRANT`](https://www.postgresql.org/docs/16/sql-grant.html)

### 4.1 Who may alter default privileges

- A role can alter **its own** default privileges.
- It can also alter the default privileges **of roles of which it is a member**. Per the reference page: *"While you can change your own default privileges and the defaults of roles that you are a member of…"*.
- Therefore, to execute `ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator …`, the executing role must **be** `strikenova_staging_migrator` **or be a member of it**.

### 4.2 Defaults apply per creating role — membership does not transfer them

The same sentence continues: *"…at object creation time, new object permissions are only affected by the default privileges of the current role, and are not inherited from any roles in which the current role is a member."*

Consequences that matter here:

- If migrations connect as `strikenova_staging_owner` and create tables, then **`strikenova_staging_migrator`'s defaults do not apply** to those tables. The objects are created by the owner, so the owner's (empty) defaults apply.
- Being a *member* of the migrator does not fix this. Membership changes which privileges are available; it does not change `current_user`, which is what selects the default ACL.
- **Migrations must actually create objects as `strikenova_staging_migrator`.** Two supported ways:
  1. Connect as `strikenova_staging_migrator` directly (simplest; one connection string in `STRIKENOVA_MIGRATION_DATABASE_URL`), or
  2. Connect as an administrative role and `SET ROLE strikenova_staging_migrator` before running the migration, for the duration of the session.

### 4.3 `SET ROLE` needs the `SET` membership option

PostgreSQL 16 membership carries independent options — `INHERIT`, `SET`, and `ADMIN`. Per the Role Membership page, a member may `SET ROLE` to a role only when there is a chain of membership grants **each with `SET TRUE`** (the default when unspecified):

```sql
GRANT strikenova_staging_migrator TO <admin_role> WITH INHERIT TRUE, SET TRUE;
```

After `SET ROLE`, *"any database objects created are considered owned by the group role not the login role"* — which is exactly the behaviour ADR-018 requires.

`INHERIT` alone is **not** sufficient for this path: inheriting a role's privileges does not change the current role, so the migrator's default privileges still would not be applied to objects you create as yourself.

### 4.4 Ownership follows the creating role

Objects are normally owned by the role that executed the creation statement (i.e. `current_user`, which `SET ROLE` changes). So:

- Tables and indexes created by `strikenova_staging_migrator` are owned by it, matching ADR-018's "migrator-creator" scope.
- `_migration_lock` bootstrapped by the migrator is owned by the migrator, matching ADR-017 and `DATA.md` §5.
- Ownership carries implicit `ALTER`/`DROP`/index rights. Never hand ownership to the runtime identity.

### 4.5 Existing objects are not covered by defaults

`ALTER DEFAULT PRIVILEGES` *"allows you to set the privileges that will be applied to objects created in the future"* and *"does not affect privileges assigned to already-existing objects."*

- **Future objects:** handled by the three `ALTER DEFAULT PRIVILEGES` statements in §6 — two schema-scoped grants (tables, sequences) and one global function revoke (§6.1).
- **Existing objects:** require explicit `GRANT … ON ALL TABLES IN SCHEMA` / `ON ALL SEQUENCES IN SCHEMA` at the moment they exist. On a brand-new empty database this set is empty, so the explicit grants are a no-op — but they must be re-run if any object is ever created before the defaults are installed.
- **Existing functions:** the function default in §6.1 is future-only as well. Any function that already exists keeps its own `proacl`; a default `REVOKE` never changes it. Gate 2 must therefore start with an inventory (`SELECT p.oid::regprocedure, p.proacl FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema');`) and an explicit privilege review for anything it finds. The application tree contains no `CREATE FUNCTION` or `CREATE TRIGGER`, so the expected inventory is empty — but that is a property of the pinned tree, not a guarantee.

### 4.6 Who should run the default-privilege statements

Three arrangements work; the first is recommended.

| Arrangement | How | Membership condition |
|---|---|---|
| **A. Run as the migrator** (recommended) | Connect as `strikenova_staging_migrator` and execute `ALTER DEFAULT PRIVILEGES … GRANT …` without a `FOR ROLE` clause | None — the role is acting for itself |
| **B. Run as an administrator that is a member** | Connect as the administrative role, execute `ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator …` | Must have `INHERIT TRUE` membership path to `strikenova_staging_migrator`; bare membership or `INHERIT FALSE` is insufficient under PG16 |
| **C. Run as an administrator, `SET ROLE` first** | `SET ROLE strikenova_staging_migrator;` then arrangement A | Must have `SET TRUE` on every membership edge in the path to `strikenova_staging_migrator` |

Note that arrangement B sets the migrator's defaults for future objects via inherited role privileges, while arrangement C first changes the current role via `SET ROLE` so that subsequent object creation counts as the migrator. Arrangement B does **not** make subsequent object creation in that session count as the migrator — to create objects as the migrator you need A or C.

**PG16 strictness caution.** PostgreSQL 16 introduced separate `INHERIT`/`SET`/`ADMIN` membership options. The `FOR ROLE` form of `ALTER DEFAULT PRIVILEGES` requires a membership path with `INHERIT TRUE` (arrangement B) or a `SET ROLE` path with `SET TRUE` on every edge (arrangement C). Granting `WITH INHERIT TRUE, SET TRUE` satisfies both paths and keeps arrangement C available without ambiguity. Verify the behaviour on a disposable PostgreSQL 16 instance before relying on any arrangement in production.

- `INHERIT TRUE`: permits the role to inherit privileges and satisfies the PG16 inherited-role precondition for arrangement B.
- `SET TRUE`: permits `SET ROLE` to the target role; it is the separate condition required for arrangement C.
- `ADMIN TRUE`: permits membership administration via `GRANT role TO member WITH ADMIN TRUE`; it is distinct from both `INHERIT` and `SET`. The example `WITH INHERIT TRUE, SET TRUE` supports both arrangement B and arrangement C, but the two options serve different purposes.

### 4.7 One more PG16 detail: schema `public`

From PostgreSQL 15 onward, `CREATE` on schema `public` is **no longer granted to `PUBLIC`**; the schema is owned by `pg_database_owner` and `USAGE` is still granted to `PUBLIC`. A fresh Neon PostgreSQL 16 database therefore does **not** let a new login role create tables in `public` by default. `strikenova_staging_migrator` must be given `USAGE, CREATE` on schema `public` explicitly (§6), and `strikenova_staging_app` must be given `USAGE` explicitly rather than relying on the `PUBLIC` grant.

### 4.8 Membership checklist before running anything

1. `strikenova_staging_migrator` and `strikenova_staging_app` exist and are `LOGIN`.
2. The migration-executing identity is exactly one of:
   - **Arrangement A:** `strikenova_staging_migrator` itself (no membership needed).
   - **Arrangement B:** an administrative role with an `INHERIT TRUE` membership path to `strikenova_staging_migrator`; this is what permits the `ALTER DEFAULT PRIVILEGES FOR ROLE` inherited-privilege check. A membership edge with `INHERIT FALSE`, or no membership at all, is insufficient.
   - **Arrangement C:** an administrative role with `SET TRUE` on **every** membership edge in the path to `strikenova_staging_migrator`, which then executes `SET ROLE strikenova_staging_migrator` before the default-privilege statements.

   `INHERIT TRUE` and `SET TRUE` are independent and are **not** interchangeable: `INHERIT TRUE` conveys the role's privileges (and satisfies the `FOR ROLE` check) but does **not** permit `SET ROLE`; `SET TRUE` permits `SET ROLE` but by itself does not satisfy the inherited-privilege check for the `FOR ROLE` form. A grant with `WITH INHERIT TRUE, SET TRUE` satisfies both.
3. `strikenova_staging_migrator` holds `USAGE, CREATE` on schema `public`.
4. `strikenova_staging_app` holds `USAGE` on schema `public` and `CONNECT` on the database.
5. The default privileges in §6 are installed **for** `strikenova_staging_migrator` **before** the first migration creates tables.
6. `pg_default_acl` shows the intended rows for the migrator **and only** the migrator — **three** rows under the corrected design, with the scopes in §8: two schema-scoped (`public`, object types `r` and `S`) and one global (namespace `-`, object type `f`).
7. Runtime identity holds no `CREATE`, no ownership, no `CREATEROLE`/`CREATEDB`/`SUPERUSER`/`REPLICATION`/`BYPASSRLS`.

---

## 5. PostgreSQL 16 SQL Correctness Constraints

- `TEMPORARY` is a **database**-level privilege. Do not write `GRANT CREATE TEMPORARY TABLES ON SCHEMA public …`; that is not valid schema-privilege syntax.
- `CREATE` on a schema does not confer ownership of existing objects.
- Ownership governs `ALTER`, `DROP`, and index management; there is no standalone `CREATE INDEX` privilege to grant.
- Default privileges must be bound to the **actual creating role** via `FOR ROLE`, in the schema where objects are created.
- Cover both future **tables** and future **sequences**. ADR-018's accepted grant is `SELECT, INSERT, UPDATE, DELETE` on tables and `USAGE` on sequences; `USAGE` already permits `nextval` and `currval`. Add sequence `SELECT`/`UPDATE` only if a concrete need for `setval` is demonstrated.
- Per-schema defaults are **added to** global defaults; a per-schema `REVOKE` cannot remove a global grant.
- **Functions:** PostgreSQL grants `EXECUTE` on every new function to `PUBLIC` by default. `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` is a **no-op** — it stores no row when none exists, and can only undo a matching per-schema `GRANT` — so removing the `PUBLIC` grant requires the **global** form, with no `IN SCHEMA` clause (§6.1). Measured on PostgreSQL 16.15; see the rehearsal report §33 and the [PostgreSQL 16 `ALTER DEFAULT PRIVILEGES`](https://www.postgresql.org/docs/16/sql-alterdefaultprivileges.html) reference, which states the case explicitly.
- **`TEMPORARY` and `CONNECT` are database-level and both belong to `PUBLIC` by default** ([§5.7](https://www.postgresql.org/docs/16/ddl-priv.html), Table 5.2: default `PUBLIC` privileges on a database are `Tc`). An explicit `GRANT CONNECT` therefore restricts nothing, and revoking either from `PUBLIC` affects every non-superuser identity that uses the database — provider-managed roles included. Both are open decisions (§10), not settled parts of this proposal.
- Treat existing objects and future objects separately (§4.5).

---

## 6. Review-Only Proposed SQL — CORRECTED, NOT EXECUTED

> Templates for review. Role names, ownership, and grants must be validated against the target database before use. Executing any of this requires **separate Founder authorization** (gate 2). Nothing in this revision has been run against Neon, CockroachDB, or any PostgreSQL instance.
>
> **Revision note (2026-11-04).** The table and sequence default statements below are the ones a disposable PostgreSQL 16.15 rehearsal executed in arrangements A, B and C. The **function** statement (§6.1) is new in this revision: it exists because the rehearsal measured that the earlier SQL left new functions `PUBLIC`-executable, and that the schema-scoped remedy is inert. The function statement was rehearsed **in isolation** (the global form, in a separate disposable database); these three parts as a whole have never been executed.

```sql
-- ------------------------------------------------------------------
-- Parts 1-5 are SETUP. Parts 6, 7, and 8 are ALTERNATIVES: they are
-- arrangements, not a sequence. Execute exactly ONE of them.
-- ------------------------------------------------------------------

-- Part 1 — Role creation. Passwords are provisioned out of band (§7);
-- never place a password literal in a committed file.
CREATE ROLE strikenova_staging_migrator LOGIN;
CREATE ROLE strikenova_staging_app     LOGIN;

-- Part 2 — Database connection privileges.
GRANT CONNECT ON DATABASE strikenova_staging TO strikenova_staging_migrator;
GRANT CONNECT ON DATABASE strikenova_staging TO strikenova_staging_app;

-- Part 3 — Schema privileges. PG16 does not grant CREATE on public to PUBLIC.
GRANT USAGE, CREATE ON SCHEMA public TO strikenova_staging_migrator;
GRANT USAGE         ON SCHEMA public TO strikenova_staging_app;

-- Part 4 — Existing objects (empty set on a fresh database; re-run if objects
-- predate the defaults). This does NOT substitute for parts 6-8.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO strikenova_staging_app;
GRANT USAGE                          ON ALL SEQUENCES IN SCHEMA public TO strikenova_staging_app;

-- Part 5 — Membership prerequisite. ONLY if the administrator will use
-- arrangement B or C; run it BEFORE the statements that depend on it.
-- The grantor needs membership-administration authority over the role:
-- the GRANT must be issued by a superuser or by a role holding the ADMIN
-- option on this membership. An arbitrary administrator cannot grant
-- itself membership in the migrator.
--   GRANT strikenova_staging_migrator TO <admin_role> WITH INHERIT TRUE, SET TRUE;
--   INHERIT TRUE -> enables arrangement B (the FOR ROLE inherited-privilege check)
--   SET TRUE     -> enables arrangement C (SET ROLE)
-- Omit this part entirely when using arrangement A.

-- ------------------------------------------------------------------
-- Part 6 — ARRANGEMENT A (recommended): connect as the migrator.
-- No FOR ROLE clause; the role acts for itself. Do NOT also run parts 7 or 8.
-- ------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_staging_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO strikenova_staging_app;

-- Part 6.1 — Function default privileges (§6.1 below). GLOBAL scope: no
-- IN SCHEMA clause. A schema-scoped REVOKE cannot remove the built-in
-- PUBLIC EXECUTE grant; it stores nothing and changes nothing.
ALTER DEFAULT PRIVILEGES
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- ------------------------------------------------------------------
-- Part 7 — ARRANGEMENT B: administrator with INHERIT TRUE, FOR ROLE form.
-- Requires part 5 with INHERIT TRUE. Do NOT also run parts 6 or 8.
-- This sets the migrator's defaults for future objects. It does NOT make
-- objects created in this session count as the migrator's.
-- ------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_staging_app;
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO strikenova_staging_app;

-- Part 7.1 — the same global function default, named FOR ROLE. The IN SCHEMA
-- form does not work here either; the scope of the revocation must be global.
ALTER DEFAULT PRIVILEGES FOR ROLE strikenova_staging_migrator
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- ------------------------------------------------------------------
-- Part 8 — ARRANGEMENT C: administrator with SET TRUE, SET ROLE first.
-- Requires part 5 with SET TRUE on every membership edge. Do NOT also run
-- parts 6 or 7. INHERIT TRUE alone is NOT enough to run SET ROLE.
-- After SET ROLE, current_user is the migrator, so subsequent object creation
-- and the defaults below are applied as the migrator.
-- ------------------------------------------------------------------
SET ROLE strikenova_staging_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO strikenova_staging_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO strikenova_staging_app;

-- Part 8.1 — the function default, executed while current_user is the migrator.
-- No FOR ROLE, no IN SCHEMA: the global default of the current role.
ALTER DEFAULT PRIVILEGES
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

RESET ROLE;
```

**Why the statements are shaped that way:**

- Parts 1-4 are prerequisites common to every arrangement: they create the roles, grant only the intended `CONNECT`/schema privileges, and cover already-existing objects.
- Part 5 must precede parts 7 and 8 because both depend on the membership it establishes. Its two options are independent: `INHERIT TRUE` satisfies arrangement B's inherited-privilege check, `SET TRUE` enables arrangement C's `SET ROLE`.
- Part 6 (arrangement A) binds to the creator role directly, with no `FOR ROLE`; effective when migrations create objects as the migrator (§4.2).
- Part 7 (arrangement B) uses `FOR ROLE` with the administrator as executor; the administrator must already hold an `INHERIT TRUE` membership path (§4.6).
- Part 8 (arrangement C) changes `current_user` with `SET ROLE`, then installs the defaults as the migrator; it requires `SET TRUE` on every membership edge (§4.6). `RESET ROLE` restores the session's original identity.
- Parts 6, 7, and 8 are **alternatives**: each installs the same **three** default-ACL rows for the same creator role (two schema-scoped, one global). Running more than one is redundant, not additive.
- Part 4 cannot substitute for parts 6-8: it covers objects that already exist; defaults only affect future objects.
- No `REPLICATION`, no `BYPASSRLS`, no `CREATEDB`, no `CREATEROLE`, no ownership, and no schema `CREATE` is granted to `strikenova_staging_app`.
- Schema ownership (`public` belongs to `pg_database_owner`) is deliberately left alone.

### 6.1 Function default privileges — why the scope must be global

PostgreSQL grants `EXECUTE` on a newly created function to `PUBLIC` by default. That grant comes from the **global** default privileges, and per-schema default privileges can only *add* to the global setting — the PostgreSQL 16 reference gives this counter-example verbatim:

> "That's because per-schema default privileges can only add privileges to the global setting, not remove privileges granted by it."
> — [`ALTER DEFAULT PRIVILEGES`](https://www.postgresql.org/docs/16/sql-alterdefaultprivileges.html), Notes

Measured consequence on PostgreSQL 16.15 (rehearsal §33): with no function default installed, `proacl` is `NULL` and the runtime role executes the function; `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` stores **no row at all** and changes nothing; the global form stores a row with no `PUBLIC` entry, and the runtime role is then refused with `42501`.

**Expected `pg_default_acl` state after exactly one arrangement** (the corrected design — three rows, all bound to the migrator):

| Row | `defaclrole` | `defaclnamespace` | `defaclobjtype` | Expected `defaclacl` shape |
|---|---|---|---|---|
| Table defaults | `strikenova_staging_migrator` | `public` | `r` (table) | contains `strikenova_staging_app=` with `arwd`; no `PUBLIC` entry |
| Sequence defaults | `strikenova_staging_migrator` | `public` | `S` (sequence) | contains `strikenova_staging_app=U`; no `PUBLIC` entry |
| Function defaults | `strikenova_staging_migrator` | *none* (rendered `-`; catalog `0`) | `f` (function) | `{strikenova_staging_migrator=X/strikenova_staging_migrator}` — **no** leading `=X`, i.e. no `PUBLIC` |

What the rehearsal actually observed, for comparison: two schema-scoped rows (`public`, `r` and `S`) and, after the global revoke, one global row `[rehearsal_f_migrator, "-", "f", "{rehearsal_f_migrator=X/rehearsal_f_migrator}"]`. The **function row's** exact catalog text above is therefore measured; the table and sequence `defaclacl` text is described by shape only, because the rehearsal recorded those rows' role/namespace/object type and the runtime's effective privileges, not the literal ACL string. **Verification must read the value, not assume it.**

Consequences that belong to the design, not to the SQL:

- The revoke is **future-only**. Existing functions keep their ACLs (§4.5).
- The revoke applies to **every** function the migrator creates, in **every** schema of that database — this is the point, not a side effect.
- Because the statement is global, `IN SCHEMA` must not be added; doing so silently disables it.
- `PUBLIC` losing `EXECUTE` means a function intended for general use needs an explicit `GRANT EXECUTE … TO <role>` — including to the app role.
- ADR-018's accepted text covers tables and sequences only. This statement therefore extends the accepted scope and requires explicit Founder acknowledgment (§10).

---

## 7. Credential Prerequisite

Login roles are inert until credentials exist, and credentials are not part of this proposal:

- Connection credentials for `strikenova_staging_migrator` and `strikenova_staging_app` must be provisioned through an **approved provider or secret-management process** — the Neon console/API or the deployment platform's secret store (gate 4 for Render).
- Never commit a plaintext password, connection string, token, or secret to the repository, these documents, logs, or issue text.
- Never paste credentials into agent chat. Set them in the platform's secret store.
- Prefer interactive/secret-managed password setting (for example `\password` in `psql`) or the provider's role-password API over a password literal in SQL.
- Rotate on exposure; the staging database should hold no long-lived shared credential.

---

## 8. Verification Plan (read-only, with two disposable probes — NOT PERFORMED)

To be executed only after gate 2 authorization. Everything below is read-only except the two rows marked **probe**, which create a disposable object and drop it in the same session.

| Check | Statement | Expected |
|---|---|---|
| Roles exist with intended attributes | `SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname LIKE 'strikenova_staging_%' ORDER BY 1;` | migrator/app `LOGIN`, all elevated attributes `false` |
| Defaults installed for the migrator only | `\ddp`, or `SELECT defaclrole::regrole::text, CASE WHEN defaclnamespace = 0 THEN '<global>' ELSE defaclnamespace::regnamespace::text END, defaclobjtype, defaclacl::text FROM pg_default_acl ORDER BY 3, 2;` (the `CASE` matters: a global row has `defaclnamespace = 0`, which casts to `regnamespace` as `-`, not `NULL`) | Exactly **three** rows, all `defaclrole = strikenova_staging_migrator`: `public`/`r`, `public`/`S`, and a global row (`<global>`) with object type `f` whose ACL has **no** `PUBLIC` entry (§6.1) |
| No unexpected default-ACL rows | the same query | Nothing bound to another role, no global table/sequence row, no `PUBLIC` grant in any row |
| **probe** — Function `EXECUTE` is not public | as the migrator: `CREATE FUNCTION public._probe_acl() RETURNS integer LANGUAGE sql AS 'SELECT 1';` then `SELECT proacl::text FROM pg_proc WHERE proname = '_probe_acl';` then `DROP FUNCTION public._probe_acl();` | `proacl` is `{strikenova_staging_migrator=X/strikenova_staging_migrator}` — a `NULL` value or any `=X/` entry means the global revoke is missing or was scoped |
| **probe** — Runtime cannot execute a probe function | with the probe function still present, as the app role: `SELECT public._probe_acl();` | refused, `42501: permission denied for function _probe_acl` |
| Runtime cannot execute functions in another schema either | repeat the probe in a second schema as the migrator, then call it as the app | refused, `42501` — the revoke is global, so a second schema must not leak |
| `TEMPORARY` — catalog | `SELECT has_database_privilege('strikenova_staging_app', current_database(), 'TEMPORARY'), has_database_privilege('strikenova_staging_migrator', current_database(), 'TEMPORARY');` | depends on the §10 decision; record the values rather than assume them. Under the untouched `PUBLIC` default both are expected to be `true` |
| **probe** — `TEMPORARY` behaviour | as the app role: `CREATE TEMP TABLE _probe_temp (id integer);` then `DROP TABLE _probe_temp;` | must match the §10 decision: succeeds if `PUBLIC` `TEMPORARY` is retained, `42501` if it is revoked. A catalog check alone is not sufficient |
| `CONNECT` — catalog | `SELECT has_database_privilege('strikenova_staging_app', current_database(), 'CONNECT'), has_database_privilege('strikenova_staging_migrator', current_database(), 'CONNECT'), has_database_privilege('<unrelated-ordinary-role>', current_database(), 'CONNECT');` | under posture A all three are `true`; under posture B only the authorized identities are `true`. Record the third value explicitly — it is the one that shows whether `PUBLIC` still confers connection |
| `CONNECT` — limitation | an actual connection attempt by an unrelated role | a catalog check is **not** equivalent to a provider access test; `pg_hba.conf`, provider proxies and superuser status all sit in front of it. Record the attempt and its result, and do not present it as a Neon-level guarantee |
| Schema privileges | `SELECT has_schema_privilege('strikenova_staging_migrator','public','CREATE'), has_schema_privilege('strikenova_staging_app','public','CREATE');` | `true, false` |
| Runtime cannot create in schema | `SELECT has_schema_privilege('strikenova_staging_app','public','CREATE');` | `false` |
| Membership setup | `SELECT r.rolname AS member, g.rolname AS granted, m.inherit_option, m.set_option FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member JOIN pg_roles g ON g.oid=m.roleid;` | Matches the chosen arrangement (§4.6) |

These are **plans**, not results. No query in this section has been run.

---

## 9. Explicit Gaps

1. **No execution against any target.** No role exists that this document proposes, and no statement here has been run against Neon or any persistent database.
2. **Rehearsal status, corrected.** A disposable PostgreSQL 16.15 rehearsal *was* run ([run `41813028902`](https://github.com/shahid1995/options-dashboard/actions/runs/41813028902), 206 assertions, 0 failures) and it validated the membership, default-privilege and ownership behaviour in §4, including the PG16 `INHERIT`/`SET` distinction: `INHERIT TRUE, SET FALSE` permits the `FOR ROLE` form but not `SET ROLE`; `INHERIT FALSE, SET TRUE` permits `SET ROLE` but not the `FOR ROLE` form. That rehearsal executed the **table and sequence** statements only. The **function** statement in §6.1, and the three arrangements as complete units, remain unexecuted. The rehearsal did not contact Neon and says nothing about its current state.
3. **No fresh catalog read.** §2 is an earlier observation; the current `pg_roles`/`pg_default_acl` state of `strikenova_staging` has not been re-verified.
4. **No migration.** No migration has been applied to `strikenova_staging`; the database is assumed to be the existing empty Neon database.
5. **Provider role untouched.** `strikenova_staging_owner` retains `replication`, `bypass_rls`, `createdb`, `createrole`. Removing those attributes is a separate decision with its own risk (it may be the only path to administer the database).
6. **`TEMPORARY` unresolved.** The runtime role inherits `TEMPORARY` from `PUBLIC`. Nothing in the application evidence requires it, and nothing in the application evidence proves it is safe to remove. Open decision (§10).
7. **`CONNECT` unresolved.** `PUBLIC` holds `CONNECT`, so the explicit grants in §6 Part 2 guarantee access but do not restrict it. Open decision (§10).
8. **Existing functions not inventoried.** The default in §6.1 is future-only. Whether any function already exists in `strikenova_staging` is unknown (no catalog read since the earlier inspection), and the app tree creates none.
9. **Function-gap evidence lives outside this proposal.** The measurement, the artifact id and its retrieval limitation are recorded in the rehearsal report §31 and §33; this document summarises them and does not restate the raw log.

---

## 10. Founder Decision Required

| Option | Content | Consequence |
|---|---|---|
| **A** | Authorize creating `strikenova_staging_migrator` and `strikenova_staging_app` per §6, set `STRIKENOVA_MIGRATION_DATABASE_URL` to the migrator and `DATABASE_URL` to the app identity, then run the §8 read-only checks | Staging matches ADR-016/017/018; residual provider-role breadth remains |
| **B** | Defer implementation and run staging single-identity under `strikenova_staging_owner` | Simpler, but the application keeps `replication`, `bypass_rls`, `createrole`, and DDL capability; ADR-016 stays unused in staging |
| **C** | Option A, plus a documented justification to alter the provider role's attributes | Narrowest staging posture; requires a separate technical justification and its own authorization |

Three further decisions are separable from the implementation option and are **not** made by this document:

| Decision | Options | What it changes |
|---|---|---|
| **D — `TEMPORARY`** | keep the `PUBLIC` default (current text), or `REVOKE TEMPORARY ON DATABASE strikenova_staging FROM PUBLIC` and grant it only where demonstrated | the runtime role's ability to create temporary tables; provider/service roles that rely on `PUBLIC` would each need their own grant |
| **E — `CONNECT`** | posture A: `PUBLIC` `CONNECT` retained, explicit grants are additive; posture B: `REVOKE CONNECT ON DATABASE … FROM PUBLIC` and grant explicitly | whether connection is actually restricted; superusers are unaffected either way, and posture B requires the authoritative identity list |
| **F — function defaults** | accept §6.1's global `REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` as a staging control, or leave functions on PostgreSQL's `PUBLIC` default | whether a future migrator-created function is executable by any role that can reach the schema. ADR-018's accepted text covers tables and sequences only, so accepting §6.1 extends the accepted scope |

Whichever option is chosen, the verification in §8 must be run read-only afterwards and its **actual** values recorded — not the expected ones.

Authorization for this gate is **not** authorization to apply migrations (gate 3) or to change Render (gate 4).

**Status of this document:** `PROPOSAL — REVIEW ONLY — UNEXECUTED — PARTIALLY REHEARSED (A/B/C MEMBERSHIP AND TABLE/SEQUENCE DEFAULTS) — FUNCTION DEFAULT CORRECTED, UNEXECUTED — TEMP AND CONNECT AWAITING FOUNDER DECISION — AWAITING SEPARATE FOUNDER AUTHORIZATION FOR GATE 2`
