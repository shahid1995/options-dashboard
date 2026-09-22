// ---------------------------------------------------------------------------
// Day 44 — Data-state classification primitives (shared, frontend-owned).
//
// The Phase 1 audit found the seven-state contract (loading / current /
// successful-but-stale / empty / authorization failure / recoverable API
// failure / unexpected failure) implemented ad hoc per page. These primitives
// standardize the CLASSIFICATION only — each surface keeps its own
// presentation. Pure and deterministic: every classification takes `nowMs`
// explicitly, so identical inputs always yield identical states.
//
// Backend remains authoritative for every value; these helpers only decide
// which state the UI is in. Missing freshness is never reported as current —
// "stale" is the honest default when age cannot be proven. Recoverable
// failures are the `detail`-carrying keys; unexpected failures reach these
// helpers only after the surface's error boundary normalizes them (same
// shapes as recoverable, labeled by the presenting surface).
// ---------------------------------------------------------------------------

// A live chain feed is honest for 15s after its last update (the HTTP
// fallback polls every 5s; the WebSocket pushes every few seconds). Past
// that, the UI must say the data is stale rather than imply it is live.
export const STALE_THRESHOLD_MS = 15_000;

// Portfolio data is pull-to-refresh (no live feed). Between 2 and 10 minutes
// it is "aged" (successful but old — visible caption); past 10 minutes it is
// "stale" (explicit warning banner).
export const PORTFOLIO_STALE_AFTER_MS = 2 * 60_000;
export const PORTFOLIO_HARD_STALE_AFTER_MS = 10 * 60_000;

/**
 * Classify the live option-chain feed state.
 *
 * Precedence: authorization failure > recoverable failure > loading >
 * empty > stale-with-error > stale > current. Authorization is checked
 * first so a session problem is never masked by a feed error.
 *
 * @param {object} p
 * @param {object|null} p.chain — chain payload from useChainFeed (null = none)
 * @param {number|null} p.lastUpdated — epoch ms of the last successful update
 * @param {string|null} p.feedError — current recoverable feed error, if any
 * @param {boolean} p.noBrokerToken — valid session, no market-data authorization
 * @param {boolean} p.sessionExpired — platform session invalid/expired
 * @param {number} p.nowMs — epoch ms used for all age math (deterministic)
 * @returns {{key: string, showData: boolean, detail: string|null}}
 */
export function chainState({
  chain,
  lastUpdated,
  feedError,
  noBrokerToken,
  sessionExpired,
  nowMs,
}) {
  if (sessionExpired) {
    return { key: "session-expired", showData: false, detail: null };
  }
  if (noBrokerToken) {
    return { key: "authorization-failed", showData: false, detail: null };
  }
  if (!chain) {
    if (feedError) {
      return { key: "recoverable-failure", showData: false, detail: feedError };
    }
    return { key: "loading", showData: false, detail: null };
  }
  if (feedError) {
    // The latest update failed over RETAINED data: what is shown is stale
    // by definition, regardless of age or row count (an empty retained
    // chain with a failed refresh is still stale-with-error, not a
    // successful empty). This must precede the successful-empty branch
    // below.
    return { key: "stale-with-error", showData: true, detail: feedError };
  }
  if (!chain.chain || chain.chain.length === 0) {
    // Empty is a successful state (a fresh chain with no rows), shown as
    // such rather than as an error.
    return { key: "empty", showData: true, detail: null };
  }
  if (lastUpdated == null) {
    // Data exists but its freshness cannot be proven: stale, never current.
    return { key: "stale", showData: true, detail: null };
  }
  if (nowMs - lastUpdated > STALE_THRESHOLD_MS) {
    return { key: "stale", showData: true, detail: null };
  }
  return { key: "current", showData: true, detail: null };
}

/**
 * Classify the portfolio (pull-to-refresh) data state.
 *
 * @param {object} p
 * @param {object|null} p.analytics — GET /paper/analytics payload (null = none)
 * @param {object|null} p.capital — GET /paper/capital payload (null = none)
 * @param {boolean} p.loading — a load/refresh is in flight
 * @param {string|null} p.error — recoverable load error, if any
 * @param {number|null} p.lastLoadedAt — epoch ms of the last successful load
 * @param {number} p.nowMs — epoch ms used for all age math (deterministic)
 * @returns {{key: string, showData: boolean, refreshing: boolean, detail: string|null}}
 */
export function portfolioState({
  analytics,
  capital,
  loading,
  error,
  lastLoadedAt,
  nowMs,
}) {
  const hasData = analytics != null || capital != null;

  // An in-flight refresh never downgrades already-shown data to "loading".
  if (loading && !hasData) {
    return { key: "loading", showData: false, refreshing: false, detail: null };
  }
  if (!hasData) {
    if (error) {
      return { key: "recoverable-failure", showData: false, refreshing: false, detail: error };
    }
    // Load completed with no payload: a genuinely empty portfolio.
    return { key: "empty", showData: false, refreshing: false, detail: null };
  }

  const refreshing = loading === true;
  if (error) {
    // A refresh failed over existing data: what is shown is stale by
    // definition.
    return { key: "stale-with-error", showData: true, refreshing, detail: error };
  }
  if (lastLoadedAt == null) {
    return { key: "stale", showData: true, refreshing, detail: null };
  }
  const age = nowMs - lastLoadedAt;
  if (age > PORTFOLIO_HARD_STALE_AFTER_MS) {
    return { key: "stale", showData: true, refreshing, detail: null };
  }
  if (age > PORTFOLIO_STALE_AFTER_MS) {
    return { key: "aged", showData: true, refreshing, detail: null };
  }
  return { key: "current", showData: true, refreshing, detail: null };
}
