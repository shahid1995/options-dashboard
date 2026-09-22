// ---------------------------------------------------------------------------
// Day 45 — Admin control-plane API client (Issue #90).
//
// Presentation/admin-control calls ONLY, on the existing shared axios
// instance (same cookie-authenticated transport as every other surface —
// no second networking architecture). All authorization is server-enforced
// via the AdminUser boundary; these helpers carry no broker material and
// the admin endpoints return none.
//
// Backend admin routes are mounted on the versioned Day 43 surface
// (/api/v1/admin/*) — these helpers target that prefix explicitly.
// ---------------------------------------------------------------------------

import { api } from "./api";

export const getAdminAudit = () => api.get("/api/v1/admin/audit").then((r) => r.data);

export const getIngestionHealth = () =>
  api.get("/api/v1/admin/ingestion-health").then((r) => r.data);

export const getAdapters = () => api.get("/api/v1/admin/adapters").then((r) => r.data);

export const getFeatureFlags = () =>
  api.get("/api/v1/admin/feature-flags").then((r) => r.data);

export const getModelMetadata = () =>
  api.get("/api/v1/admin/model-metadata").then((r) => r.data);

export const getAdminControls = (domain) =>
  api.get(`/api/v1/admin/controls/${encodeURIComponent(domain)}`).then((r) => r.data);

export const setAdminControl = (domain, key, value) =>
  api.post("/api/v1/admin/controls", { domain, key, value }).then((r) => r.data);

export const runAcquisition = (body) =>
  api.post("/api/v1/admin/acquisition/run", body).then((r) => r.data);
