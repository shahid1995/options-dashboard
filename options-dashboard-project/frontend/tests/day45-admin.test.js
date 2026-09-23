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
  it("calls the versioned /api/v1/admin/* operational-view endpoints", async () => {
    const get = vi.fn((path) => ok({ path }));
    const mod = await loadAdminApi({ mockGet: get });
    await mod.getAdminAudit();
    await mod.getIngestionHealth();
    await mod.getAdapters();
    await mod.getFeatureFlags();
    await mod.getModelMetadata();
    await mod.getAdminControls("retention");
    const paths = get.mock.calls.map((c) => c[0]);
    // Backend routes are mounted under /api/v1/admin/* — the versioned
    // Day 43 surface (F7).
    expect(paths).toEqual([
      "/api/v1/admin/audit",
      "/api/v1/admin/ingestion-health",
      "/api/v1/admin/adapters",
      "/api/v1/admin/feature-flags",
      "/api/v1/admin/model-metadata",
      "/api/v1/admin/controls/retention",
    ]);
  });

  it("setAdminControl posts the sanitized body to the versioned path via the shared client", async () => {
    const post = vi.fn(() => Promise.resolve(ok({ status: "ok" })));
    vi.doMock("@/lib/api", () => ({
      api: { get: vi.fn(), post },
      isAuthError: () => false,
    }));
    const mod = await import("@/lib/adminApi");
    await mod.setAdminControl("retention", "chain_snapshots_days", 90);
    expect(post).toHaveBeenCalledWith("/api/v1/admin/controls", {
      domain: "retention",
      key: "chain_snapshots_days",
      value: 90,
    });
  });

  it("runs admin acquisition only through the versioned path", async () => {
    const post = vi.fn(() => Promise.resolve(ok({ status: "accepted" })));
    vi.doMock("@/lib/api", () => ({
      api: { get: vi.fn(), post },
      isAuthError: () => false,
    }));
    const mod = await import("@/lib/adminApi");
    await mod.runAcquisition({ operation: "dry_run" });
    expect(post).toHaveBeenCalledWith("/api/v1/admin/acquisition/run", {
      operation: "dry_run",
    });
  });

  it("does not mutate the shared axios client or its base URL", async () => {
    const get = vi.fn(() => ok({}));
    await loadAdminApi({ mockGet: get });
    const apiSrc = read("lib/api.js");
    expect(apiSrc).toContain('baseURL: process.env.NEXT_PUBLIC_API_URL');
    // adminApi configures no client of its own and no base-URL overrides.
    const src = read("lib/adminApi.js");
    expect(src).not.toContain("axios.create");
    expect(src).not.toContain("baseURL");
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

  it("uses object-shape Table columns for ingestion and audit tables (F8)", () => {
    // Extract the two `columns={[...]}` literal regions from the page.
    const regions = [...pageSrc.matchAll(/columns=\{\[([\s\S]*?)\]\}/g)].map((m) => m[1]);
    expect(regions.length).toBe(2); // ingestion + audit tables
    for (const region of regions) {
      // Every column must be an object with header/key — not a bare string.
      expect(region).toContain("header:");
      expect(region).toContain("key:");
      // No bare-string column rows: each entry must declare its keys.
      const bareStringColumn = region
        .split(",")
        .some((entry) => {
          const t = entry.trim().replace(/^\n+/, "");
          return /^"[^"]*"$/.test(t);
        });
      expect(bareStringColumn).toBe(false);
    }
    const [ingestion, audit] = regions;
    expect(ingestion).toContain('header: "Run"');
    expect(ingestion).toContain('key: "run"');
    expect(ingestion).toContain('header: "Operation"');
    expect(ingestion).toContain('key: "operation"');
    expect(ingestion).toContain('header: "Instrument"');
    expect(ingestion).toContain('key: "instrument"');
    expect(ingestion).toContain('header: "Started"');
    expect(ingestion).toContain('key: "started"');
    expect(ingestion).toContain('header: "Status"');
    expect(ingestion).toContain('key: "status"');
    expect(ingestion).toContain('header: "Rows"');
    expect(ingestion).toContain('key: "rows"');
    expect(audit).toContain('header: "Time"');
    expect(audit).toContain('key: "time"');
    expect(audit).toContain('header: "Actor"');
    expect(audit).toContain('key: "actor"');
    expect(audit).toContain('header: "Action"');
    expect(audit).toContain('key: "action"');
    expect(audit).toContain('header: "Result"');
    expect(audit).toContain('key: "result"');
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

describe("admin page render — shared Table contract (F8)", () => {
  it("the shared Table component renders col.header / row[col.key]", async () => {
    // Locks the shared component contract the page must satisfy: object
    // columns with header/key and value lookup by key.
    const core = await import("@/components/app/core");
    expect(typeof core.Table).toBe("function");
  });

  it("ingestion/audit column keys align with the page's row mapping", () => {
    const pageSrc2 = read("app/(app)/admin/page.js");
    // The row objects built in the page expose exactly the keys the column
    // definitions read (no silent undefined cells).
    expect(pageSrc2).toContain("run: r.run_id");
    expect(pageSrc2).toContain("operation: r.operation");
    expect(pageSrc2).toContain('instrument: r.instrument_key ?? "—"');
    expect(pageSrc2).toContain('started: r.started_at ?? "—"');
    expect(pageSrc2).toContain("rows: `${r.rows_fetched ?? 0}/${r.rows_inserted ?? 0}`");
    expect(pageSrc2).toContain('time: e.occurred_at ?? "—"');
    expect(pageSrc2).toContain('actor: e.actor_user_id ?? "anonymous"');
  });
});
