"use client";
import { useEffect, useState, useCallback } from "react";
import { C, Centered, SessionExpired, useIsMobile } from "@/lib/ui";
import {
  getPaperCapital,
  getPaperAnalytics,
  getPaperPositions,
  getPaperPositionsFiltered,
  isAuthError,
} from "@/lib/api";
import { getStatus } from "@/lib/api";
import CapitalPanel from "../paper/CapitalPanel";
import PortfolioAnalyticsPanel from "../paper/PortfolioAnalyticsPanel";
// Day 44: shared data-state classification (loading/current/aged/stale/empty/failure).
import { portfolioState } from "@/lib/chainState";

/**
 * Phase 2.1b — Portfolio page
 * Extracts capital + portfolio analytics from the /paper monolith.
 * Fetches its own data through the existing APIs.
 */

export default function PortfolioPage() {
  const [loggedIn, setLoggedIn] = useState(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [error, setError] = useState(null);
  const isMobile = useIsMobile();

  // Data state
  const [capital, setCapital] = useState(null);
  const [capitalError, setCapitalError] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [analyticsError, setAnalyticsError] = useState(null);
  const [positions, setPositions] = useState([]);
  const [positionsLtp, setPositionsLtp] = useState([]);
  const [loading, setLoading] = useState(true);
  // Day 44: freshness evidence for the data-state classification (epoch ms of
  // the last fully successful load).
  const [lastLoadedAt, setLastLoadedAt] = useState(null);

  // Auth check
  useEffect(() => {
    getStatus()
      .then((s) => setLoggedIn(s.logged_in))
      .catch((e) => {
        setError(e.message);
        setLoggedIn(false);
      });
  }, []);

  const loadPortfolio = useCallback(async () => {
    // A retry must not keep presenting the previous failure as the status of
    // the request now starting: clear retained errors up front. Existing
    // data is untouched — the panels keep rendering while loading is true.
    setCapitalError(null);
    setAnalyticsError(null);
    setLoading(true);
    try {
      const [analyticsData, capitalData, positionsData] = await Promise.all([
        getPaperAnalytics(),
        getPaperCapital(),
        getPaperPositionsFiltered({ all: true, limit: 500 }),
      ]);
      setAnalytics(analyticsData);
      setCapital(capitalData);
      setPositionsLtp(positionsData);
      setCapitalError(null);
      setAnalyticsError(null);
      setLastLoadedAt(Date.now());
    } catch (e) {
      if (isAuthError(e)) {
        setSessionExpired(true);
        return;
      }
      setCapitalError(e.message);
      setAnalyticsError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!loggedIn) return;
    loadPortfolio();
  }, [loggedIn, loadPortfolio]);

  // Day 44: classify the portfolio data state (deterministic; re-evaluated on
  // a periodic tick so aged data is reclassified without a reload).
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNowTick(Date.now()), 30000);
    return () => clearInterval(t);
  }, []);
  const dataState = portfolioState({
    analytics,
    capital,
    loading,
    error: capitalError || analyticsError,
    lastLoadedAt,
    nowMs: nowTick,
  });

  if (loggedIn === null) {
    return <Centered>Checking login…</Centered>;
  }
  if (sessionExpired) return <SessionExpired />;
  if (error && !capital && !analytics) {
    return <Centered>Something went wrong: {error}</Centered>;
  }

  return (
    <div style={{ maxWidth: 1400 }}>
      {/* Page Header */}
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0, marginBottom: 4 }}>
          Portfolio
        </h1>
        <p style={{ fontSize: 13, color: C.muted, margin: 0 }}>
          Capital allocation, risk controls, and performance analytics
        </p>
      </div>

      {/* Capital Panel */}
      <div style={{ marginBottom: 16 }}>
        <CapitalPanel
          capital={capital}
          loading={loading}
          error={capitalError}
        />
      </div>

      {/* Portfolio Analytics Panel */}
      <PortfolioAnalyticsPanel
        analytics={analytics}
        positionsWithLtp={positionsLtp}
        capital={capital}
        loading={loading}
        error={analyticsError}
      />

      {/* Refresh button */}
      <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12 }}>
        <button
          onClick={loadPortfolio}
          disabled={loading}
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: C.gold,
            background: "rgba(201,161,90,0.08)",
            border: `1px solid ${C.gold}66`,
            borderRadius: 6,
            padding: "5px 12px",
            cursor: loading ? "default" : "pointer",
            opacity: loading ? 0.5 : 1,
          }}
        >
          {loading ? "Refreshing…" : "↻ Refresh Portfolio"}
        </button>
        {dataState.key === "stale-with-error" && (
          <span style={{ fontSize: 11, color: C.red }}>
            ⚠ Refresh failed ({dataState.detail}) — showing values from the last successful load. You can refresh again.
          </span>
        )}
        {dataState.key === "stale" && (
          <span style={{ fontSize: 11, color: C.gold }}>
            ⚠ Data is stale — refresh to update.
          </span>
        )}
        {dataState.key === "aged" && (
          <span style={{ fontSize: 11, color: C.muted }}>
            Showing data loaded {lastLoadedAt ? new Date(lastLoadedAt).toLocaleTimeString("en-IN") : ""}.
          </span>
        )}
      </div>
    </div>
  );
}
