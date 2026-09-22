import { describe, it, expect, vi, afterEach } from "vitest";

// ---------------------------------------------------------------------------
// Day 45 — Admin control plane frontend (Issue #90): the admin surface is
// presentation-only, consumes the SAME shared axios client, and surfaces the
// server's 403 as an explicit not-authorized state. Frontend state can never
// grant admin access — there is no local role logic to bypass.
// ---------------------------------------------------------------------------

const T0 = 1_700_000_000_000;

afterEach(() => {
  vi.resetModules();
  vi.restoreAllMocks();
});

function ok(data) {
  return Promise.resolve({ data, status: 200 });
}

function httpError(status, message) {
  const e = new Error(message);
  e.response = { status, data: { detail: message } };
  return e;
}

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const feRoot = join(here, "..");
const read = (p) => readFileSync(join(feRoot, p), "utf8");

async function loadAdminApi({ mockGet }) {
  vi.doMock("@/lib/api", () => ({
    api: { get: mockGet, post: vi.fn() },
    isAuthError: (e) => e?.response?.status === 401,
  }));
  return import("@/lib/adminApi");
}

describe("adminApi — presentation client on the shared transport", () => {
  it("calls the five operational-view endpoints on /admin/*", async () => {
    const get = vi.fn((path) => ok({ path }));
    const mod = await loadAdminApi({ mockGet: get });
    await mod.getAdminAudit();
    await mod.getIngestionHealth();
    await mod.getAdapters();
    await mod.getFeatureFlags();
    await mod.getModelMetadata();
    await mod.getAdminControls("retention");
    const paths = get.mock.calls.map((c) => c[0]);
    expect(paths).toEqual([
      "/admin/audit",
      "/admin/ingestion-health",
      "/admin/adapters",
      "/admin/feature-flags",
      "/admin/model-metadata",
      "/admin/controls/retention",
    ]);
  });

  it("setAdminControl posts the sanitized body via the shared client", async () => {
    const post = vi.fn(() => Promise.resolve(ok({ status: "ok" })));
    vi.doMock("@/lib/api", () => ({
      api: { get: vi.fn(), post },
      isAuthError: () => false,
    }));
    const mod = await import("@/lib/adminApi");
    await mod.setAdminControl("retention", "chain_snapshots_days", 90);
    expect(post).toHaveBeenCalledWith("/admin/controls", {
      domain: "retention",
      key: "chain_snapshots_days",
      value: 90,
    });
  });

  it("never touches token stores or carries credential material", async () => {
    const get = vi.fn(() => ok({}));
    await loadAdminApi({ mockGet: get });
    // The module source must not reference credential material at all.
    // (Word-boundary match so the comment's "no second networking
    // architecture" language does not false-positive.)
    const src = read("lib/adminApi.js");
    expect(src).not.toMatch(/\b(token|secret|credentials?|password|api_key)\b/i);
    expect(src).toContain('from "./api"');
  });
});

describe("admin page source — boundary wiring", () => {
  const pageSrc = read("app/(app)/admin/page.js");

  it("surfaces 401/403 as an explicit not-authorized state (no local role logic)", () => {
    expect(pageSrc).toContain("setNotAuthorized(true)");
    expect(pageSrc).toContain("isAuthError(e)");
    expect(pageSrc).toContain("status === 403");
    // No client-side admin decision exists to bypass.
    expect(pageSrc).not.toMatch(/isAdmin|is_admin/);
  });

  it("renders the five operational views and stays presentation-only", () => {
    for (const marker of [
      "INGESTION HEALTH",
      "ADAPTERS",
      "FEATURE FLAGS",
      "MODEL METADATA",
      "AUDIT ACTIVITY",
    ]) {
      expect(pageSrc).toContain(marker);
    }
    expect(pageSrc).toContain('from "@/lib/adminApi"');
  });
});
