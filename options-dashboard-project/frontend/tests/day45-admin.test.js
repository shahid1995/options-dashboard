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

  it("guards the full request lifecycle against stale responses (F5)", () => {
    expect(pageSrc).toContain("createRequestSequence");
    expect(pageSrc).toContain("seqRef.current.begin()");
    // Every state write after the await is gated by the generation check:
    // data, error/notAuthorized, and loading (the finally block) — an old
    // request can neither overwrite nor terminate a newer one.
    expect(pageSrc).toContain("if (!isCurrent()) return;");
    expect(pageSrc).toContain("if (isCurrent()) setLoading(false)");
  });
});

describe("admin tab request ordering — stale-response race (F5)", () => {
  it("requestSequence: only the newest generation passes isCurrent", async () => {
    const { createRequestSequence } = await import("@/lib/requestSequence");
    const seq = createRequestSequence();
    const isA = seq.begin();
    const isB = seq.begin();
    expect(isA()).toBe(false); // A superseded by B
    expect(isB()).toBe(true);
    const isC = seq.begin();
    expect(isB()).toBe(false);
    expect(isC()).toBe(true);
  });

  it("a late response from a superseded tab never overwrites the newer tab", async () => {
    const { createRequestSequence } = await import("@/lib/requestSequence");

    function deferred() {
      let resolve;
      const promise = new Promise((r) => {
        resolve = r;
      });
      return { promise, resolve };
    }

    // Deferred responses per tab: A = ingestion, B = audit.
    const responses = { ingestion: deferred(), audit: deferred() };
    const fetchers = {
      ingestion: () => responses.ingestion.promise,
      audit: () => responses.audit.promise,
    };

    // Faithful harness of the admin page's load() — same state set
    // (data/loading/error/notAuthorized), same write order, same guard
    // placement as the wired page (asserted by the wiring test above).
    const state = { data: null, loading: false, error: null, notAuthorized: false };
    const setState = (patch) => Object.assign(state, patch);
    const seq = createRequestSequence();

    async function load(tab) {
      const isCurrent = seq.begin();
      setState({ loading: true, error: null, notAuthorized: false });
      try {
        const payload = await fetchers[tab]();
        if (!isCurrent()) return;
        setState({ data: payload });
      } catch (e) {
        if (!isCurrent()) return;
        setState({ error: e.message || "Failed to load admin data." });
      } finally {
        if (isCurrent()) setState({ loading: false });
      }
    }

    const A = { marker: "A-ingestion" };
    const B = { marker: "B-audit" };

    // 1. start request for tab A (suspends)
    const pA = load("ingestion");
    // 2. switch to tab B; 3. start request for tab B
    const pB = load("audit");
    // 4. resolve B first
    responses.audit.resolve(B);
    await pB;
    // 5. resolve A afterwards
    responses.ingestion.resolve(A);
    await pA;

    // 6. the UI state remains B's payload; A overwrote nothing.
    expect(state.data).toEqual(B);
    expect(state.data).not.toEqual(A);
    expect(state.loading).toBe(false); // B finished; A did not revive loading
    expect(state.error).toBeNull();
    expect(state.notAuthorized).toBe(false);
  });

  it("a late FAILED response from a superseded tab cannot surface as an error", async () => {
    const { createRequestSequence } = await import("@/lib/requestSequence");

    function deferred() {
      let resolve;
      let reject;
      const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
      });
      return { promise, resolve, reject };
    }

    const responses = { ingestion: deferred(), audit: deferred() };
    const fetchers = {
      ingestion: () => responses.ingestion.promise,
      audit: () => responses.audit.promise,
    };

    const state = { data: null, loading: false, error: null, notAuthorized: false };
    const setState = (patch) => Object.assign(state, patch);
    const seq = createRequestSequence();

    async function load(tab) {
      const isCurrent = seq.begin();
      setState({ loading: true, error: null, notAuthorized: false });
      try {
        const payload = await fetchers[tab]();
        if (!isCurrent()) return;
        setState({ data: payload });
      } catch (e) {
        if (!isCurrent()) return;
        setState({ error: e.message || "Failed to load admin data." });
      } finally {
        if (isCurrent()) setState({ loading: false });
      }
    }

    const B = { marker: "B-audit" };
    const pA = load("ingestion");
    const pB = load("audit");
    responses.audit.resolve(B);
    await pB;
    responses.ingestion.reject(new Error("late A failure"));
    await pA.catch(() => {}); // harness mirrors the page's caught rejection

    expect(state.data).toEqual(B);
    expect(state.error).toBeNull(); // A's late failure is discarded
    expect(state.loading).toBe(false);
  });
});
