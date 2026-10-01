import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { isAuthError, chainWsUrl, submitPaperFill, closePaperLeg, getPaperJournal, getMarketStatus, getPaperAnalytics, getBrokerProfile, api, getStrategyTemplates, createStrategyTemplate, updateStrategyTemplate, duplicateStrategyTemplate, deleteStrategyTemplate, registerEmail, loginEmail, getMe, logoutUser, connectBroker, connectAnalyticsToken, getAnalyticsTokenStatus, removeAnalyticsToken } from "./api";

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_API_URL", "http://localhost:8000");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("isAuthError", () => {
  it("is true for axios errors with a 401 response", () => {
    expect(isAuthError({ response: { status: 401 } })).toBe(true);
  });

  it("is false for other statuses and shapeless errors", () => {
    expect(isAuthError({ response: { status: 502 } })).toBe(false);
    expect(isAuthError(new Error("network"))).toBe(false);
    expect(isAuthError(null)).toBe(false);
  });
});

describe("chainWsUrl", () => {
  // nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket
  it("derives a ws:// URL from the API base and encodes the expiry", () => {
    expect(chainWsUrl("NIFTY", "2026-08-27")).toBe("ws://localhost:8000/chains/ws/NIFTY?expiry_date=2026-08-27"); // nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket
  });

  it("maps https to wss", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    expect(chainWsUrl("BANKNIFTY", "2026-08-27")).toBe("wss://api.example.com/chains/ws/BANKNIFTY?expiry_date=2026-08-27");
  });
});

describe("paper journal api", () => {
  it("posts an executed fill to /paper/fills", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { id: 1 } });
    const order = { symbol: "NIFTY", strategy_tag: "Long Call", legs: [] };
    await submitPaperFill(order);
    expect(spy).toHaveBeenCalledWith("/paper/fills", order);
    spy.mockRestore();
  });

  it("posts a leg close with the exit price", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { status: "closed" } });
    await closePaperLeg(7, 3, 45.5);
    expect(spy).toHaveBeenCalledWith("/paper/trades/7/legs/3/close", { exit_price: 45.5 });
    spy.mockRestore();
  });

  it("gets the journal from /paper/journal", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: { account: { balance: 500000 } } });
    await getPaperJournal();
    expect(spy).toHaveBeenCalledWith("/paper/journal");
    spy.mockRestore();
  });

  it("gets the market status from /paper/market-status and normalizes trade_date", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: { status: "open", open: true, source: "upstox", trade_date: "2026-08-14" } });
    const st = await getMarketStatus();
    expect(spy).toHaveBeenCalledWith("/paper/market-status");
    expect(st.status).toBe("open");
    expect(st.tradeDate).toBe("2026-08-14");
    spy.mockRestore();
  });

  it("gets the Phase 5.1 analytics from /paper/analytics with optional filters", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: { performance: { total_completed_trades: 3 } } });
    const result = await getPaperAnalytics({ date_from: "2026-08-01", strategy: "Long Call" });
    expect(spy).toHaveBeenCalledWith("/paper/analytics", {
      params: { date_from: "2026-08-01", strategy: "Long Call" },
    });
    expect(result.performance.total_completed_trades).toBe(3);
    spy.mockRestore();
  });
});

describe("broker profile api (Phase 6.4.1)", () => {
  it("gets the broker profile from /paper/broker/profile", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({
      data: { status: "available", source: "BROKER_REPORTED", profile: { user_id: "UCC12345" } },
    });
    const result = await getBrokerProfile();
    expect(spy).toHaveBeenCalledWith("/paper/broker/profile", { params: {} });
    expect(result.profile.user_id).toBe("UCC12345");
    spy.mockRestore();
  });

  it("passes refresh=true to bypass the backend user-scoped cache", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: { status: "available" } });
    await getBrokerProfile(true);
    expect(spy).toHaveBeenCalledWith("/paper/broker/profile", { params: { refresh: true } });
    spy.mockRestore();
  });
});

describe("strategy template API (Phase 6.7)", () => {
  it("gets templates from /paper/templates", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: [{ id: 1, name: "My Bull" }] });
    const result = await getStrategyTemplates();
    expect(spy).toHaveBeenCalledWith("/paper/templates");
    expect(result).toEqual([{ id: 1, name: "My Bull" }]);
    spy.mockRestore();
  });

  it("creates a template via POST /paper/templates", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { id: 2, name: "New Strategy" } });
    const payload = { name: "New Strategy", symbol: "NIFTY", legs: [] };
    const result = await createStrategyTemplate(payload);
    expect(spy).toHaveBeenCalledWith("/paper/templates", payload);
    expect(result.id).toBe(2);
    spy.mockRestore();
  });

  it("updates a template via PUT /paper/templates/:id", async () => {
    const spy = vi.spyOn(api, "put").mockResolvedValue({ data: { id: 2, name: "Renamed" } });
    const result = await updateStrategyTemplate(2, { name: "Renamed" });
    expect(spy).toHaveBeenCalledWith("/paper/templates/2", { name: "Renamed" });
    expect(result.name).toBe("Renamed");
    spy.mockRestore();
  });

  it("duplicates a template via POST /paper/templates/:id/duplicate", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { id: 3, name: "My Bull (copy)" } });
    const result = await duplicateStrategyTemplate(2, "My Bull (copy)");
    expect(spy).toHaveBeenCalledWith("/paper/templates/2/duplicate", null, { params: { new_name: "My Bull (copy)" } });
    expect(result.id).toBe(3);
    spy.mockRestore();
  });

  it("duplicates without new_name when not provided", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { id: 4 } });
    await duplicateStrategyTemplate(2);
    expect(spy).toHaveBeenCalledWith("/paper/templates/2/duplicate", null, { params: {} });
    spy.mockRestore();
  });

  it("deletes a template via DELETE /paper/templates/:id", async () => {
    const spy = vi.spyOn(api, "delete").mockResolvedValue({ data: { ok: true } });
    const result = await deleteStrategyTemplate(5);
    expect(spy).toHaveBeenCalledWith("/paper/templates/5");
    expect(result.ok).toBe(true);
    spy.mockRestore();
  });
});

describe("auth API (Phase 10.2B-5)", () => {
  it("registers via POST /auth/register", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { ok: true, user_id: "u1" } });
    const result = await registerEmail("test@example.com", "password123", "Test User");
    expect(spy).toHaveBeenCalledWith("/auth/register", { email: "test@example.com", password: "password123", display_name: "Test User" });
    expect(result.ok).toBe(true);
    spy.mockRestore();
  });

  it("logs in via POST /auth/login-email", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { ok: true, session_id: "sess-123" } });
    const result = await loginEmail("test@example.com", "password123");
    expect(spy).toHaveBeenCalledWith("/auth/login-email", { email: "test@example.com", password: "password123" });
    expect(result.session_id).toBe("sess-123");
    spy.mockRestore();
  });

  it("gets current user via GET /auth/me", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: { user_id: "u1", email: "test@example.com" } });
    const result = await getMe();
    expect(spy).toHaveBeenCalledWith("/auth/me");
    expect(result.user_id).toBe("u1");
    spy.mockRestore();
  });

  it("logs out via POST /auth/logout", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { ok: true } });
    const result = await logoutUser();
    expect(spy).toHaveBeenCalledWith("/auth/logout");
    expect(result.ok).toBe(true);
    spy.mockRestore();
  });

  it("stores broker credentials via POST /auth/connect", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { ok: true, status: "pending" } });
    const result = await connectBroker("UPSTOX", "key123", "secret456");
    expect(spy).toHaveBeenCalledWith("/auth/connect", { broker: "UPSTOX", api_key: "key123", api_secret: "secret456", redirect_uri: undefined, display_label: undefined });
    expect(result.status).toBe("pending");
    spy.mockRestore();
  });

  it("stores analytics token via POST /auth/connect-analytics-token", async () => {
    const spy = vi.spyOn(api, "post").mockResolvedValue({ data: { ok: true, broker: "UPSTOX" } });
    const result = await connectAnalyticsToken("atoken123");
    expect(spy).toHaveBeenCalledWith("/auth/connect-analytics-token", { broker: "UPSTOX", analytics_token: "atoken123" });
    expect(result.ok).toBe(true);
    spy.mockRestore();
  });

  it("gets analytics token status via GET /auth/analytics-token/status", async () => {
    const spy = vi.spyOn(api, "get").mockResolvedValue({ data: { has_analytics_token: true, broker: "UPSTOX" } });
    const result = await getAnalyticsTokenStatus();
    expect(spy).toHaveBeenCalledWith("/auth/analytics-token/status", { params: { broker: "UPSTOX" } });
    expect(result.has_analytics_token).toBe(true);
    spy.mockRestore();
  });

  it("removes analytics token via DELETE /auth/analytics-token", async () => {
    const spy = vi.spyOn(api, "delete").mockResolvedValue({ data: { ok: true } });
    const result = await removeAnalyticsToken();
    expect(spy).toHaveBeenCalledWith("/auth/analytics-token", { params: { broker: "UPSTOX" } });
    expect(result.ok).toBe(true);
    spy.mockRestore();
  });
});
