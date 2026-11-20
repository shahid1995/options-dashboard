# Production CockroachDB Version Evidence — Issue #145 / ADR-017 (D-1)

Read-only evidence gathering only. No schema, data, role, or cluster changes were made.

## Production service/deployment identity

- Platform: Render (web service)
- Service name: strikenova-api-production
- Service ID: srv-dar2cm0u01pc73e6h41g
- Region: Singapore
- Runtime: Python (FastAPI/uvicorn)
- Deployed branch: feat/strikenova-day35-portfolio-intelligence
- Health check path: /health
- Public URL: https://strikenova-api-production.onrender.com
- Environment marker: STRIKENOVA_ENV=production

## Database provider and target

- Provider: CockroachDB Cloud
- Database name: strikenova
- Authorized connection route: Render service-scoped environment variables DATABASE_URL and STRIKENOVA_MIGRATION_DATABASE_URL (values not reproduced)
- Redacted runtime endpoint identity: strikenova-production-****.jxf.gcp-asia-south1.cockroachlabs.cloud:26257
- Redacted migrator endpoint identity: same host/port
- SQL roles (names only): runtime strikenova_production_app; migrator strikenova_prod_migrator
- Production vs staging: the service above carries STRIKENOVA_ENV=production; the separate Render service strikenova-api-staging uses a Neon PostgreSQL URL, confirming the CockroachDB target here is production.

## SQL metadata queries attempted

Intended (read-only):

    SELECT current_database(), current_user, version();
    SHOW CLUSTER SETTING version;
    SHOW NODES;  -- only if the connected role is authorized

Result: NOT EXECUTED. The connection attempt failed before any SQL ran.

Error (redacted, no credentials):

    psycopg.OperationalError: connection failed: connection to server at 34.14.187.94, port 26257 failed:
    FATAL: codeParamsRoutingFailed: cluster strikenova-production-20972 not found

No version(), cluster-version, or node-build output was obtained. No advisory-lock or concurrency probe was run. No DDL, DML, or cluster-setting change was attempted.

## Compatibility assessment

Classification: 1. Insufficient evidence.

- Production endpoint identity confirmed via authorized Render configuration.
- The configured CockroachDB cluster (strikenova-production-20972) could not be reached; the CockroachDB Cloud router reports the cluster as not found.
- No connection succeeded, so neither the server build (version()) nor the active logical cluster version (SHOW CLUSTER SETTING version) nor the node build-version distribution (SHOW NODES) is available.
- Version evidence alone would not prove the lease protocol works. Proposed baseline: CockroachDB 26.4.1 or later, subject to confirmation of advisory-lock feature support in production.

## Facts not accessible with available read-only permissions

- The live CockroachDB cluster referenced by production DATABASE_URL (cluster not found at query time).
- CockroachDB Cloud cluster inventory / API access (no CockroachDB Cloud API key or session token available).
- Role-level grant introspection on the production cluster (requires a successful connection).
- Whether the cluster is paused, deleted, renamed, or region-migrated.

## Remaining tests required before D-1 can be resolved

1. Restore or confirm reachability of the production CockroachDB cluster and re-run the three metadata queries (read-only) to capture version() and the active cluster version.
2. Run SHOW NODES to confirm a consistent (non-mixed) node build-version distribution.
3. Verify the proposed baseline: CockroachDB 26.4.1 or later on both binary and active logical cluster versions.
4. Confirm pg_advisory_xact_lock / transaction-scoped advisory lock behavior is supported and non-no-op on the production cluster (via a dedicated non-production test cluster or the PG16 rehearsal — never via an operational probe on production).
5. Confirm the migrator role can execute the migration-lease functions under the migration_internal schema once implementation is approved.

## Blocker summary

D-1 cannot be resolved from this evidence. The production CockroachDB cluster referenced by the authorized Render configuration is not reachable (codeParamsRoutingFailed: cluster ... not found). No production version or cluster-version data was captured. Founder/infrastructure action is required to restore or replace the cluster before version evidence can be obtained.

No production upgrade, migration, DDL, DML, role/grant change, or service restart was performed. No source, governance, or configuration files were modified. PR #146 was not altered.