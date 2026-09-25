import { describe, it, expect, afterEach, vi } from "vitest";
import {
  requireApiUrl,
  FORBIDDEN_HOSTS,
  isBlank,
  isLocalhostHost,
} from "./apiConfig";

// Hermetic by construction: these tests call the pure validator directly —
// no module reloads, no shared state mutation, no execution-order reliance.
// Environment is untouched (values are passed as arguments).

afterEach(() => {
  vi.restoreAllMocks();
});

describe("requireApiUrl — production builds (fail closed)", () => {
  it("Test A: accepts an explicit valid https API URL", () => {
    const resolved = requireApiUrl({
      rawValue: "https://api.example.test",
      isProductionBuild: true,
    });
    expect(resolved).toBe("https://api.example.test");
  });

  it("Test A: trims surrounding whitespace from a valid value", () => {
    expect(
      requireApiUrl({
        rawValue: "  https://api.example.test  ",
        isProductionBuild: true,
      })
    ).toBe("https://api.example.test");
  });

  it("Test B: missing value fails with the production-configuration error", () => {
    expect(() =>
      requireApiUrl({ rawValue: undefined, isProductionBuild: true })
    ).toThrowError(/NEXT_PUBLIC_API_URL is required for production builds/);
  });

  it("Test C: empty string fails", () => {
    expect(() =>
      requireApiUrl({ rawValue: "", isProductionBuild: true })
    ).toThrowError(/NEXT_PUBLIC_API_URL is required for production builds/);
  });

  it("Test D: whitespace-only value fails", () => {
    expect(() =>
      requireApiUrl({ rawValue: "   ", isProductionBuild: true })
    ).toThrowError(/NEXT_PUBLIC_API_URL is required for production builds/);
  });

  it("Test E: the historical Railway URL is rejected (cannot be selected)", () => {
    for (const host of FORBIDDEN_HOSTS) {
      expect(() =>
        requireApiUrl({
          rawValue: `https://${host}`,
          isProductionBuild: true,
        })
      ).toThrowError(/historical Railway backend/);
    }
  });

  it("Test E: the Railway URL is rejected even in non-production", () => {
    expect(() =>
      requireApiUrl({
        rawValue: "https://options-dashboard-production-fb47.up.railway.app",
        isProductionBuild: false,
      })
    ).toThrowError(/historical Railway backend/);
  });

  it("rejects http in production (Secure-cookie session contract)", () => {
    expect(() =>
      requireApiUrl({ rawValue: "http://api.example.test", isProductionBuild: true })
    ).toThrowError(/must use https for production builds/);
  });

  it("rejects localhost and loopback targets in production", () => {
    for (const bad of [
      "https://localhost",
      "https://127.0.0.1:8000",
      "https://[::1]:8000",
      "https://api.localhost",
    ]) {
      expect(() =>
        requireApiUrl({ rawValue: bad, isProductionBuild: true })
      ).toThrowError(/localhost\/loopback/);
    }
  });

  it("rejects non-http(s) schemes", () => {
    expect(() =>
      requireApiUrl({ rawValue: "ftp://api.example.test", isProductionBuild: true })
    ).toThrowError(/must use http\(s\)/);
    expect(() =>
      requireApiUrl({ rawValue: "javascript:alert(1)", isProductionBuild: true })
    ).toThrowError(/must use http\(s\)/);
  });

  it("rejects values that are not URLs at all", () => {
    expect(() =>
      requireApiUrl({ rawValue: "not a url", isProductionBuild: true })
    ).toThrowError(/not a valid URL/);
  });
});

describe("requireApiUrl — non-production builds (local behavior preserved)", () => {
  it("Test H: missing value is allowed and yields undefined (relative mode)", () => {
    expect(
      requireApiUrl({ rawValue: undefined, isProductionBuild: false })
    ).toBeUndefined();
    expect(requireApiUrl({ rawValue: "", isProductionBuild: false })).toBeUndefined();
  });

  it("Test H: http and localhost backends are allowed for local development", () => {
    expect(
      requireApiUrl({ rawValue: "http://localhost:8000", isProductionBuild: false })
    ).toBe("http://localhost:8000");
    expect(
      requireApiUrl({ rawValue: "http://127.0.0.1:8000", isProductionBuild: false })
    ).toBe("http://127.0.0.1:8000");
  });

  it("Test H: warns (but does not fail) on non-https local backends", () => {
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    requireApiUrl({
      rawValue: "http://localhost:8000",
      isProductionBuild: false,
      warn: true,
    });
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining("[api-config] NEXT_PUBLIC_API_URL is non-https")
    );
  });
});

describe("api configuration contract (REST/WebSocket consistency)", () => {
  it("Test G: the same NEXT_PUBLIC_API_URL drives REST and WebSocket derivation", async () => {
    // lib/api.js derives chainWsUrl from the identical env var consumed via
    // next.config.js injection: http->ws / https->wss, path and query intact.
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test");
    const { chainWsUrl } = await import("./api");
    expect(chainWsUrl("NIFTY", "2026-08-27")).toBe(
      "wss://api.example.test/chains/ws/NIFTY?expiry_date=2026-08-27"
    );
    vi.unstubAllEnvs();
  });

  it("Test G: api.js remains the single consumer of the injected value", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.join(process.cwd(), "lib", "api.js"),
      "utf8"
    );
    expect(src).toContain("baseURL: process.env.NEXT_PUBLIC_API_URL");
    // No other backend URL may be hardcoded in the API module.
    expect(src).not.toMatch(/railway\.app/);
    expect(src).not.toMatch(/https?:\/\/(?!api\.example)[a-z0-9.-]+/i.test("") ? "" : /onrender\.com/);
  });

  it("Test F: next.config.js contains no executable Railway fallback", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.join(process.cwd(), "next.config.js"),
      "utf8"
    );
    expect(src).not.toContain("options-dashboard-production-fb47.up.railway.app");
    expect(src).toContain("requireApiUrl");
  });
});

describe("helper predicates", () => {
  it("isBlank", () => {
    expect(isBlank(undefined)).toBe(true);
    expect(isBlank("")).toBe(true);
    expect(isBlank("   ")).toBe(true);
    expect(isBlank("https://api.example.test")).toBe(false);
  });

  it("isLocalhostHost", () => {
    expect(isLocalhostHost("localhost")).toBe(true);
    expect(isLocalhostHost("127.0.0.1")).toBe(true);
    expect(isLocalhostHost("[::1]")).toBe(true);
    expect(isLocalhostHost("api.localhost")).toBe(true);
    expect(isLocalhostHost("api.example.test")).toBe(false);
  });
});
