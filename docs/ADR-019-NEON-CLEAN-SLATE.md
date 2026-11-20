# ADR-019 — Adopt Neon PostgreSQL as the Clean-Slate Production Database Target

**Status:** Accepted — strategic target decision only  
**Decision date:** 2026-11-20  
**Supersedes:** the previously documented CockroachDB production-database-target direction (earlier project guidance and topology documents)  
**Related:** ADR-017 (migration serialization), ADR-018 (database privilege defaults)

## Context

StrikeNova historically used CockroachDB as its production database target. The configured production Render service referenced a CockroachDB endpoint for cluster `strikenova-production-20972`. An authorized read-only connection attempt could not reach that cluster; the router returned `codeParamsRoutingFailed: cluster ... not found`. No SQL version evidence was obtained. The evidence report is available at [PROD_CRDB_VERSION_EVIDENCE.md](https://github.com/shahid1995/options-dashboard/blob/docs/prod-crdb-version-evidence/docs/PROD_CRDB_VERSION_EVIDENCE.md) on its documentation evidence branch.

The founder has confirmed that existing CockroachDB data is not required. No recovery, export, or historical-data migration from CockroachDB is required for the new production database.

Neon PostgreSQL has already been selected as StrikeNova's strategic database target. The existing Neon staging environment is not the future production database and must remain separate.

## Decision

1. **Retire CockroachDB as the strategic production database target.** New architecture, migration, and acceptance-test work shall target PostgreSQL, using Neon as the managed provider.
2. **Establish production on a clean Neon PostgreSQL database.** Apply the approved Alembic migration chain to create the application schema. No historical CockroachDB data is to be restored, exported, or migrated.
3. **Keep Neon staging separate.** Do not repurpose the existing staging database as production or assume it contains production-ready data, roles, or configuration.
4. **Treat production provisioning and cutover as separate controlled work.** This ADR does not provision a Neon project/database, create roles or credentials, change Render environment variables, deploy code, or authorize production cutover.
5. **Require production readiness evidence before cutover.** At minimum, verify the production Neon endpoint and isolation from staging; migration-chain compatibility and successful clean-database application; required roles, ownership, grants, and default privileges; application startup and representative functionality; backup/recovery configuration; and the approved Render connection-variable plan.
6. **Preserve historical traceability without rewriting it.** Earlier project guidance and topology documents described CockroachDB as the production database target. This standalone ADR records the new direction and does not modify those existing documents; they remain historical evidence of the prior direction.

## Consequences

- Neon PostgreSQL is the strategic database target for future production work.
- CockroachDB-specific support is no longer a requirement for new migration-lease protocol design or its acceptance tests.
- The clean-slate decision means existing CockroachDB application data will not be carried forward. Any required initial reference/configuration data must be identified and deliberately seeded through an approved process; it must not be copied implicitly from staging.
- The old CockroachDB endpoint may remain in historical deployment configuration until a separately authorized decommissioning/cutover task. Its prior configuration is not proof that the cluster is currently reachable or that production is healthy.
- ADR-017 and ADR-018 remain separate proposals. This ADR does not approve their implementation or settle their remaining design decisions.

## Explicitly out of scope

- CockroachDB recovery, backup restoration, export, or data migration.
- Neon production provisioning or activation.
- Changes to Render service configuration, environment variables, secrets, or deployment state.
- Application source-code, Alembic migration-file, or database-role/privilege changes.
- Approval, implementation, merge, or deployment of the migration-lease protocol.

## Acceptance record

The founder's direction, recorded on 2026-11-20, is to proceed with Neon PostgreSQL as the clean-slate production target and not retain or migrate CockroachDB data. Acceptance of this strategic target does not authorize production provisioning or cutover.

---
