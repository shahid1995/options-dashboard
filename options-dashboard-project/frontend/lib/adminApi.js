// ---------------------------------------------------------------------------
// Day 45 — Admin control-plane API client (Issue #90).
//
// Presentation/admin-control calls ONLY, on the existing shared axios
// instance (same cookie-authenticated transport as every other surface —
// no second networking architecture). All authorization is server-enforced
// via the AdminUser boundary; these helpers carry no broker material and
// the admin endpoints return none.
// ---------------------------------------------------------------------------

import { api } from "./api";

export const getAdminAudit = () => api.get("/admin/audit").then((r) => r.data);

export const getIngestionHealth = () =>
  api.get("/admin/ingestion-health").then((r) => r.data);

export const getAdapters = () => api.get("/admin/adapters").then((r) => r.data);

export const getFeatureFlags = () =>
  api.get("/admin/feature-flags").then((r) => r.data);

export const getModelMetadata = () =>
  api.get("/admin/model-metadata").then((r) => r.data);

export const getAdminControls = (domain) =>
  api.get(`/admin/controls/${encodeURIComponent(domain)}`).then((r) => r.data);

export const setAdminControl = (domain, key, value) =>
  api.post("/admin/controls", { domain, key, value }).then((r) => r.data);
