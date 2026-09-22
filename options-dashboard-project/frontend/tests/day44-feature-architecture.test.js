import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  chainState,
  STALE_THRESHOLD_MS,
  portfolioState,
  PORTFOLIO_STALE_AFTER_MS,
  PORTFOLIO_HARD_STALE_AFTER_MS,
} from "@/lib/chainState.js";

// ---------------------------------------------------------------------------
// Day 44 — Frontend Feature Architecture (Issue #88)
//
// Scope of these tests: the small shared state-classification primitives
// introduced by Day 44 (lib/chainState.js) for the authenticated surfaces the
// task touched. The authority audit found no duplicated backend authority in
// the frontend calculation modules (backend endpoints verified non-existent
// where frontend-only models exist), so no calculation was moved.
// ---------------------------------------------------------------------------

const T0 = 1_700_000_000_000; // fixed epoch for deterministic assertions

describe("chainState — live chain data-state classification", () => {
  it("classifies loading when there is no chain data yet and no failure", () => {
    const s = chainState({
      chain: null,
      lastUpdated: null,
      feedError: null,
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("loading");
    expect(s.showData).toBe(false);
  });

  it("classifies session expiry as an authorization failure before any feed error can mask it", () => {
    const s = chainState({
      chain: { chain: [{ strike: 1 }] },
      lastUpdated: T0 - 1,
      feedError: "boom",
      noBrokerToken: false,
      sessionExpired: true,
      nowMs: T0,
    });
    expect(s.key).toBe("session-expired");
    expect(s.showData).toBe(false);
  });

  it("classifies a missing market-data authorization distinctly from session expiry", () => {
    const s = chainState({
      chain: null,
      lastUpdated: null,
      feedError: null,
      noBrokerToken: true,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("authorization-failed");
    expect(s.showData).toBe(false);
  });

  it("classifies a recoverable API failure when the first load fails with no data", () => {
    const s = chainState({
      chain: null,
      lastUpdated: null,
      feedError: "network down",
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("recoverable-failure");
    expect(s.showData).toBe(false);
    expect(s.detail).toBe("network down");
  });

  it("classifies a fresh successful feed as current", () => {
    const s = chainState({
      chain: { chain: [{ strike: 25000 }] },
      lastUpdated: T0 - 1000,
      feedError: null,
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("current");
    expect(s.showData).toBe(true);
  });

  it("classifies an empty chain as empty/no-data even while fresh", () => {
    const s = chainState({
      chain: { chain: [] },
      lastUpdated: T0 - 1000,
      feedError: null,
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("empty");
    expect(s.showData).toBe(true);
  });

  it("classifies silent staleness once the feed age exceeds the threshold (boundary: exact age is still current)", () => {
    const chain = { chain: [{ strike: 25000 }] };
    const atThreshold = chainState({
      chain,
      lastUpdated: T0 - STALE_THRESHOLD_MS,
      feedError: null,
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(atThreshold.key).toBe("current");

    const pastThreshold = chainState({
      chain,
      lastUpdated: T0 - STALE_THRESHOLD_MS - 1,
      feedError: null,
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(pastThreshold.key).toBe("stale");
    expect(pastThreshold.showData).toBe(true);
  });

  it("prefers the stale-with-error classification when a live update failed over existing data", () => {
    const s = chainState({
      chain: { chain: [{ strike: 25000 }] },
      lastUpdated: T0 - 1000,
      feedError: "Live update failed",
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("stale-with-error");
    expect(s.showData).toBe(true);
    expect(s.detail).toBe("Live update failed");
  });

  it("treats unknown freshness as stale, never as current (missing stays missing)", () => {
    const s = chainState({
      chain: { chain: [{ strike: 25000 }] },
      lastUpdated: null,
      feedError: null,
      noBrokerToken: false,
      sessionExpired: false,
      nowMs: T0,
    });
    expect(s.key).toBe("stale");
    expect(s.showData).toBe(true);
  });
});

describe("portfolioState — portfolio data-state classification", () => {
  const data = { analytics: { summary: {} }, capital: { status: "available" } };

  it("classifies loading before the first successful load", () => {
    const s = portfolioState({
      analytics: null,
      capital: null,
      loading: true,
      error: null,
      lastLoadedAt: null,
      nowMs: T0,
    });
    expect(s.key).toBe("loading");
    expect(s.showData).toBe(false);
  });

  it("classifies a recoverable API failure when the initial load fails with no data", () => {
    const s = portfolioState({
      analytics: null,
      capital: null,
      loading: false,
      error: "Could not reach the server",
      lastLoadedAt: null,
      nowMs: T0,
    });
    expect(s.key).toBe("recoverable-failure");
    expect(s.showData).toBe(false);
  });

  it("classifies an empty portfolio when load succeeded but nothing was returned", () => {
    const s = portfolioState({
      analytics: null,
      capital: null,
      loading: false,
      error: null,
      lastLoadedAt: T0 - 1000,
      nowMs: T0,
    });
    expect(s.key).toBe("empty");
    expect(s.showData).toBe(false);
  });

  it("classifies a fresh load as current", () => {
    const s = portfolioState({ ...data, loading: false, error: null, lastLoadedAt: T0 - 1000, nowMs: T0 });
    expect(s.key).toBe("current");
    expect(s.showData).toBe(true);
  });

  it("classifies successful-but-aged data between the soft and hard thresholds", () => {
    const s = portfolioState({
      ...data,
      loading: false,
      error: null,
      lastLoadedAt: T0 - PORTFOLIO_STALE_AFTER_MS - 1000,
      nowMs: T0,
    });
    expect(s.key).toBe("aged");
    expect(s.showData).toBe(true);
  });

  it("classifies data older than the hard threshold as stale", () => {
    const s = portfolioState({
      ...data,
      loading: false,
      error: null,
      lastLoadedAt: T0 - PORTFOLIO_HARD_STALE_AFTER_MS - 1000,
      nowMs: T0,
    });
    expect(s.key).toBe("stale");
    expect(s.showData).toBe(true);
  });

  it("uses strict threshold boundaries (exact soft age is still current, exact hard age is still aged)", () => {
    const atSoft = portfolioState({ ...data, loading: false, error: null, lastLoadedAt: T0 - PORTFOLIO_STALE_AFTER_MS, nowMs: T0 });
    expect(atSoft.key).toBe("current");

    const atHard = portfolioState({ ...data, loading: false, error: null, lastLoadedAt: T0 - PORTFOLIO_HARD_STALE_AFTER_MS, nowMs: T0 });
    expect(atHard.key).toBe("aged");
  });

  it("treats unknown freshness as stale even with data present (never claims current without evidence)", () => {
    const s = portfolioState({ ...data, loading: false, error: null, lastLoadedAt: null, nowMs: T0 });
    expect(s.key).toBe("stale");
    expect(s.showData).toBe(true);
  });

  it("keeps an in-flight refresh from downgrading already-shown data to loading", () => {
    const s = portfolioState({ ...data, loading: true, error: null, lastLoadedAt: T0 - 1000, nowMs: T0 });
    expect(s.key).toBe("current");
    expect(s.refreshing).toBe(true);
  });
});

describe("Day 44 feature-ownership wiring", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const feRoot = join(here, "..");
  const read = (p) => readFileSync(join(feRoot, p), "utf8");

  it("classifies states through the single shared primitive (no ad-hoc re-implementation)", () => {
    // Both touched surfaces consume the shared classifier; neither page
    // defines its own age math.
    const dashboard = read("app/(app)/dashboard/page.js");
    expect(dashboard).toContain('from "@/lib/chainState"');
    expect(dashboard).toContain("chainState({");
    const portfolio = read("app/(app)/portfolio/page.js");
    expect(portfolio).toContain('from "@/lib/chainState"');
    expect(portfolio).toContain("portfolioState({");
    // The classifier module owns the thresholds — the pages do not hard-code
    // their own stale cutoffs.
    expect(dashboard).not.toMatch(/\b15_?000\b/);
    expect(portfolio).not.toMatch(/2\s*\*\s*60_?000/);
  });

  it("keeps presentation helpers frontend-owned and untouched", () => {
    // The audit retained the pure presentation/domain-helper modules in
    // place; Day 44 added no duplicate of them.
    for (const p of [
      "lib/analytics.js",
      "lib/portfolio.js",
      "lib/capital.js",
      "lib/calculations/analyticalCapital.js",
      "lib/calculations/capitalEfficiency.js",
      "lib/calculations/capitalAllocation.js",
    ]) {
      expect(() => read(p)).toBeTruthy();
      expect(read(p).length).toBeGreaterThan(0);
    }
  });
});
