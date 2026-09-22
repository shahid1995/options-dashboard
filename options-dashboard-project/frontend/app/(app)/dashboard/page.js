"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { getStatus, getExpiries, isAuthError } from "@/lib/api";
import { useChainFeed } from "@/lib/useChainFeed";
import { putCallRatio, maxPainStrike, maxOI, oiTotals } from "@/lib/analytics";
import { makeAlert, evaluateAlerts, describeAlert } from "@/lib/alerts";
import { C, TopNav, SymbolTabs, Centered, SessionExpired, fmtIN, fmtChg, useIsMobile } from "@/lib/ui";
import { useRouter } from "next/navigation";
import { MetricCard } from "@/components/app/styles";
import { loadJSON, saveJSON } from "@/lib/storage";
import { useGexCapture } from "@/lib/useGexCapture";
import GexProfileChart from "@/components/GexProfileChart";
// Day 44: shared data-state classification (loading/current/stale/empty/auth-failure).
import { chainState } from "@/lib/chainState";

const WATCHLIST_KEY = "options_dashboard_watchlist_v1";
const ALERTS_KEY = "options_dashboard_alerts_v1";

export default function Dashboard() {
  const [loggedIn, setLoggedIn] = useState(null);
  const [symbol, setSymbol] = useState("NIFTY");
  const [expiries, setExpiries] = useState([]);
  const [expiry, setExpiry] = useState(null);
  const [watchlist, setWatchlist] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [firedToasts, setFiredToasts] = useState([]);
  const [compact, setCompact] = useState(false);
  const isMobile = useIsMobile();
  const router = useRouter();

  useEffect(() => {
    setWatchlist(loadJSON(WATCHLIST_KEY, []));
    setAlerts(loadJSON(ALERTS_KEY, []));
  }, []);

  useEffect(() => {
    saveJSON(WATCHLIST_KEY, watchlist);
  }, [watchlist]);

  useEffect(() => {
    saveJSON(ALERTS_KEY, alerts);
  }, [alerts]);

  const [statusError, setStatusError] = useState(null);
  useEffect(() => {
    getStatus()
      .then((s) => setLoggedIn(s.logged_in))
      .catch((e) => {
        setStatusError(e.message);
        setLoggedIn(false);
      });
  }, []);

  const [expiryError, setExpiryError] = useState(null);
  const [expirySessionExpired, setExpirySessionExpired] = useState(false);
  const [expiryNoBrokerToken, setExpiryNoBrokerToken] = useState(false);
  useEffect(() => {
    if (!loggedIn) return;
    setExpiry(null);
    setExpiries([]);
    getExpiries(symbol)
      .then((d) => {
        setExpiries(d.expiries);
        if (d.expiries.length) setExpiry(d.expiries[0]);
      })
      .catch((e) => {
        if (isAuthError(e)) setExpirySessionExpired(true);
        else if (e?.response?.status === 403) setExpiryNoBrokerToken(true);
        else setExpiryError(e.message);
      });
  }, [loggedIn, symbol]);

  const { chain, lastUpdated, error, mode, sessionExpired, noBrokerToken } = useChainFeed(symbol, expiry, !!loggedIn);

  // Day 44: classify the chain data state (deterministic; re-evaluated on a
  // periodic tick so a silently stalled feed is reclassified as stale).
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNowTick(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);
  const chainSt = chainState({
    chain,
    lastUpdated: lastUpdated ? lastUpdated.getTime() : null,
    feedError: error || null,
    noBrokerToken,
    sessionExpired,
    nowMs: nowTick,
  });

  // Phase 7.6: GEX snapshot capture with sweep enrichment for gamma flip / walls
  const { analytics: gexAnalytics, captureCount: gexCaptureCount, latestSnapshot: gexSnapshot } = useGexCapture(chain, {
    symbol,
    persistToBackend: !!loggedIn,
    loadHistory: !!loggedIn,
    enableSweep: true,
  });

  const [centeredKey, setCenteredKey] = useState(null);
  const scrollRef = useRef(null);

  const spot = chain ? chain.underlying_spot_price : null;
  let atmStrike = null;
  if (chain && spot != null && chain.chain.length) {
    atmStrike = chain.chain.reduce((closest, row) =>
      Math.abs(row.strike - spot) < Math.abs(closest.strike - spot) ? row : closest
    ).strike;
  }

  const pcr = chain ? putCallRatio(chain.chain) : null;
  const maxPain = chain ? maxPainStrike(chain.chain) : null;
  const oiMax = chain ? maxOI(chain.chain) : 0;
  const totals = chain ? oiTotals(chain.chain) : null;

  // Center the view on the ATM strike once per symbol+expiry (not on every refresh)
  useEffect(() => {
    if (!chain || !scrollRef.current || atmStrike == null) return;
    const key = `${symbol}-${expiry}`;
    if (centeredKey === key) return;
    const el = scrollRef.current.querySelector('[data-atm="true"]');
    if (el) {
      el.scrollIntoView({ block: "center" });
      setCenteredKey(key);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chain, symbol, expiry]);

  // Evaluate price alerts on every chain update
  useEffect(() => {
    if (!chain) return;
    setAlerts((prev) => {
      const { alerts: next, fired } = evaluateAlerts(prev, chain.chain, symbol, expiry);
      if (fired.length) {
        setFiredToasts((t) => [...t, ...fired]);
        if (typeof Notification !== "undefined" && Notification.permission === "granted") {
          fired.forEach((f) => new Notification("Price alert", { body: `${describeAlert(f)} — LTP ${f.ltp}` }));
        }
      }
      return fired.length ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chain]);

  const isWatched = (strike, type) =>
    watchlist.some((w) => w.strike === strike && w.type === type && w.expiry === expiry && (w.symbol ?? "NIFTY") === symbol);

  const toggleWatch = (strike, type) => {
    setWatchlist((prev) => {
      const match = (w) => w.strike === strike && w.type === type && w.expiry === expiry && (w.symbol ?? "NIFTY") === symbol;
      if (prev.some(match)) return prev.filter((w) => !match(w));
      return [...prev, { strike, type, expiry, symbol }];
    });
  };

  const addAlert = (w, condition, level) => {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission();
    }
    setAlerts((prev) => [...prev, makeAlert({ symbol: w.symbol ?? "NIFTY", expiry: w.expiry, strike: w.strike, type: w.type, condition, level })]);
  };

  const removeAlert = (id) => setAlerts((prev) => prev.filter((a) => a.id !== id));

  const enrichedWatchlist = watchlist.map((w) => {
    if (!chain || w.expiry !== expiry || (w.symbol ?? "NIFTY") !== symbol) return { ...w, ltp: null };
    const row = chain.chain.find((r) => r.strike === w.strike);
    const ltp = row ? (w.type === "call" ? row.call.ltp : row.put.ltp) : null;
    return { ...w, ltp };
  });

  if (loggedIn === null) return <Centered>Checking login…</Centered>;
  if (statusError) return <Centered>Something went wrong: {statusError}</Centered>;
  if (loggedIn === false)
    return (
      <Centered>
        Not logged in.{" "}
        <a href="/" style={{ color: C.gold }}>
          Go back and log in
        </a>
        .
      </Centered>
    );
  // Phase A fix: Distinguish StrikeNova session expiry from broker token absence.
  // - noBrokerToken/expiryNoBrokerToken: valid session, no broker → show Connect Broker
  // - sessionExpired/expirySessionExpired: StrikeNova session invalid → show SessionExpired
  if (noBrokerToken || expiryNoBrokerToken) {
    return (
      <Centered>
        <div style={{ textAlign: "center", maxWidth: 400 }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: C.text, marginBottom: 12 }}>
            No Broker Connected
          </div>
          <div style={{ fontSize: 14, color: C.muted, marginBottom: 20, lineHeight: 1.6 }}>
            You're signed in to StrikeNova, but market data requires a broker connection.
            Connect your Upstox account to view the option chain.
          </div>
          <button
            onClick={() => router.push("/settings")}
            style={{
              padding: "10px 24px",
              borderRadius: 8,
              border: "none",
              background: C.gold,
              color: "#0B0E14",
              fontSize: 14,
              fontWeight: 700,
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            Connect Broker
          </button>
        </div>
      </Centered>
    );
  }
  if (sessionExpired || expirySessionExpired) return <SessionExpired />;
  if ((error || expiryError) && !chain) return <Centered>Something went wrong: {error || expiryError}</Centered>;
  if (!chain) return <Centered>Loading chain…</Centered>;

  const useCompact = compact || isMobile;

  // Build table rows (highest strike first) with a spot-marker row inserted
  // at the right position
  const displayChain = [...chain.chain].sort((a, b) => b.strike - a.strike);
  const tableItems = [];
  displayChain.forEach((row, i) => {
    const prevRow = displayChain[i - 1];
    if (spot != null && prevRow && prevRow.strike > spot && row.strike < spot) {
      tableItems.push({ kind: "spot", value: spot });
    }
    tableItems.push({ kind: "row", data: row });
  });

  const colCount = useCompact ? 9 : 21;

  return (
    <div style={{ padding: isMobile ? 10 : 20 }}>
      <TopNav active="chain" />

      {/* Day 44: successful-but-stale states — with or without a feed error. */}
      {(chainSt.key === "stale" || chainSt.key === "stale-with-error") && (
        <div style={{ marginBottom: 12, padding: "8px 12px", borderRadius: 6, border: `1px solid ${C.gold}`, background: "rgba(201,161,90,0.08)", color: C.gold, fontSize: 12 }}>
          {chainSt.key === "stale-with-error"
            ? `Live update failed: ${chainSt.detail} — showing the last loaded data.`
            : "Live data is delayed — the feed has not updated recently. Showing the last received snapshot."}
        </div>
      )}

      {firedToasts.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          {firedToasts.map((f) => (
            <div key={f.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", background: "rgba(201,161,90,0.12)", border: `1px solid ${C.gold}`, borderRadius: 8, padding: "8px 12px", fontSize: 12.5, marginBottom: 6 }}>
              <span>
                <span style={{ color: C.gold, fontWeight: 700 }}>ALERT</span> {describeAlert(f)} — LTP {f.ltp}
              </span>
              <button onClick={() => setFiredToasts((t) => t.filter((x) => x.id !== f.id))} style={{ background: "none", border: "none", color: C.muted, cursor: "pointer", fontSize: 14 }}>
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 20, margin: 0 }}>Option Chain</h1>
        <SymbolTabs symbol={symbol} onChange={setSymbol} />
        <select
          value={expiry ?? ""}
          onChange={(e) => setExpiry(e.target.value)}
          style={{ background: C.surface, color: C.text, border: `1px solid ${C.border}`, borderRadius: 6, padding: "6px 10px" }}
        >
          {expiries.map((exp) => (
            <option key={exp} value={exp}>
              {exp}
            </option>
          ))}
        </select>
        {!isMobile && (
          <label style={{ fontSize: 11.5, color: C.muted, display: "flex", alignItems: "center", gap: 5, cursor: "pointer" }}>
            <input type="checkbox" checked={compact} onChange={(e) => setCompact(e.target.checked)} />
            Compact
          </label>
        )}
        {lastUpdated && (
          <span style={{ color: C.muted, fontSize: 11, marginLeft: "auto" }}>
            <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 3, background: chainSt.key === "current" ? C.green : C.gold, marginRight: 6 }} />
            {chainSt.key === "stale" || chainSt.key === "stale-with-error" ? "Stale" : mode === "live" ? "Live" : "Polling"} · {lastUpdated.toLocaleTimeString("en-IN")}
          </span>
        )}
      </div>

      {/* Phase E: Market Intelligence metrics — PCR coloring fixed to neutral */}
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "repeat(2, 1fr)" : "repeat(auto-fill, minmax(150px, 1fr))", gap: 8, marginBottom: 14 }}>
        {spot != null && (
          <MetricCard label="SPOT" value={fmtIN(spot, 2)} color={C.gold} hint="Underlying price" />
        )}
        <MetricCard
          label="PCR (OI)"
          value={pcr != null ? pcr.toFixed(2) : "—"}
          color={C.text}
          hint="Put OI ÷ Call OI · Structural ratio"
        />
        <MetricCard label="MAX PAIN" value={maxPain != null ? fmtIN(maxPain) : "—"} color={C.gold} hint="Writer-wins strike" />
        <MetricCard label="IV" value="—" color={C.faint} hint="Coming in Phase 2.2" />
        {totals && <MetricCard label="CALL OI" value={fmtIN(totals.callOI)} color={C.text} hint="Total call open interest" />}
        {totals && <MetricCard label="PUT OI" value={fmtIN(totals.putOI)} color={C.text} hint="Total put open interest" />}
      </div>

      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div ref={scrollRef} style={{ flex: 3, minWidth: isMobile ? "100%" : 480, background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, overflow: "auto", maxHeight: 560 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5, whiteSpace: "nowrap" }}>
            <thead style={{ position: "sticky", top: 0, background: C.surface, zIndex: 1 }}>
              <tr style={{ color: C.muted, fontSize: 10.5 }}>
                <th colSpan={(colCount - 1) / 2} style={{ padding: "8px 6px", textAlign: "center", color: C.green, borderBottom: `1px solid ${C.border}` }}>CALLS</th>
                <th style={{ borderBottom: `1px solid ${C.border}` }}></th>
                <th colSpan={(colCount - 1) / 2} style={{ padding: "8px 6px", textAlign: "center", color: C.red, borderBottom: `1px solid ${C.border}` }}>PUTS</th>
              </tr>
              {useCompact ? (
                <tr style={{ color: C.muted, fontSize: 10.5 }}>
                  <th style={{ padding: 6 }}></th>
                  <th style={{ padding: 6 }}>OI</th>
                  <th style={{ padding: 6 }}>Chg OI</th>
                  <th style={{ padding: 6, fontWeight: 700 }}>LTP</th>
                  <th style={{ padding: 6, textAlign: "center", color: C.gold }}>Strike</th>
                  <th style={{ padding: 6, fontWeight: 700 }}>LTP</th>
                  <th style={{ padding: 6 }}>Chg OI</th>
                  <th style={{ padding: 6 }}>OI</th>
                  <th style={{ padding: 6 }}></th>
                </tr>
              ) : (
                <tr style={{ color: C.muted, fontSize: 10.5 }}>
                  <th style={{ padding: 6 }}></th>
                  <th style={{ padding: 6 }}>OI</th>
                  <th style={{ padding: 6 }}>Chg OI</th>
                  <th style={{ padding: 6 }}>Volume</th>
                  <th style={{ padding: 6 }}>IV</th>
                  <th style={{ padding: 6 }}>Vega</th>
                  <th style={{ padding: 6 }}>Theta</th>
                  <th style={{ padding: 6 }}>Gamma</th>
                  <th style={{ padding: 6 }}>Delta</th>
                  <th style={{ padding: 6, fontWeight: 700 }}>LTP</th>
                  <th style={{ padding: 6, textAlign: "center", color: C.gold }}>Strike</th>
                  <th style={{ padding: 6, fontWeight: 700 }}>LTP</th>
                  <th style={{ padding: 6 }}>Delta</th>
                  <th style={{ padding: 6 }}>Gamma</th>
                  <th style={{ padding: 6 }}>Theta</th>
                  <th style={{ padding: 6 }}>Vega</th>
                  <th style={{ padding: 6 }}>IV</th>
                  <th style={{ padding: 6 }}>Volume</th>
                  <th style={{ padding: 6 }}>Chg OI</th>
                  <th style={{ padding: 6 }}>OI</th>
                  <th style={{ padding: 6 }}></th>
                </tr>
              )}
            </thead>
            <tbody>
              {tableItems.map((item, idx) =>
                item.kind === "spot" ? (
                  <tr key={`spot-${idx}`}>
                    <td colSpan={colCount} style={{ padding: 0 }}>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          gap: 10,
                          padding: "6px 0",
                          background: "rgba(201,161,90,0.08)",
                          borderTop: `1px dashed ${C.gold}`,
                          borderBottom: `1px dashed ${C.gold}`,
                        }}
                      >
                        <span style={{ fontSize: 10.5, color: C.gold, letterSpacing: 1, fontWeight: 700 }}>SPOT</span>
                        <span style={{ fontSize: 13, color: C.gold, fontWeight: 700 }}>{fmtIN(item.value, 2)}</span>
                      </div>
                    </td>
                  </tr>
                ) : (
                  <Row
                    key={item.data.strike}
                    row={item.data}
                    compact={useCompact}
                    oiMax={oiMax}
                    isATM={item.data.strike === atmStrike}
                    isMaxPain={item.data.strike === maxPain}
                    isWatchedCall={isWatched(item.data.strike, "call")}
                    isWatchedPut={isWatched(item.data.strike, "put")}
                    onToggleCall={() => toggleWatch(item.data.strike, "call")}
                    onTogglePut={() => toggleWatch(item.data.strike, "put")}
                  />
                )
              )}
            </tbody>
          </table>
        </div>

        <div style={{ flex: 1, minWidth: isMobile ? "100%" : 240, background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10 }}>
          <div style={{ padding: "10px 14px", borderBottom: `1px solid ${C.border}`, fontSize: 12, color: C.muted, letterSpacing: 0.5 }}>
            WATCHLIST &amp; ALERTS
          </div>
          {enrichedWatchlist.length === 0 ? (
            <div style={{ padding: 16, fontSize: 12, color: C.muted }}>
              Tap the star next to any strike to pin it here, then set price alerts on it.
            </div>
          ) : (
            <div style={{ padding: 8 }}>
              {enrichedWatchlist.map((w) => (
                <WatchlistItem
                  key={`${w.symbol}-${w.strike}-${w.type}-${w.expiry}`}
                  item={w}
                  alerts={alerts.filter(
                    (a) => a.strike === w.strike && a.type === w.type && a.expiry === w.expiry && a.symbol === (w.symbol ?? "NIFTY")
                  )}
                  onRemove={() => toggleWatch(w.strike, w.type)}
                  onAddAlert={addAlert}
                  onRemoveAlert={removeAlert}
                />
              ))}
            </div>
          )}
        </div>

        <GexProfileChart
          analytics={gexAnalytics}
          latestSnapshot={gexSnapshot}
          atmStrike={atmStrike}
          isMobile={isMobile}
        />
      </div>
    </div>
  );
}

/* Metric component kept for internal table cells and watchlist items */
function InlineMetric({ label, value, color, hint }) {
  return (
    <span title={hint} style={{ color: C.muted }}>
      {label}: <span style={{ color: color || C.text, fontWeight: 600 }}>{value}</span>
    </span>
  );
}

function OICell({ oi, oiMax, side }) {
  const pct = oiMax > 0 && oi != null ? Math.min(100, (oi / oiMax) * 100) : 0;
  const barColor = side === "call" ? "rgba(225,82,82,0.25)" : "rgba(76,175,125,0.25)";
  return (
    <td style={{ padding: 6, position: "relative" }}>
      <div style={{ position: "absolute", top: 2, bottom: 2, [side === "call" ? "right" : "left"]: 0, width: `${pct}%`, background: barColor, borderRadius: 2 }} />
      <span style={{ position: "relative" }}>{fmtIN(oi)}</span>
    </td>
  );
}

function Row({ row, compact, oiMax, isATM, isMaxPain, isWatchedCall, isWatchedPut, onToggleCall, onTogglePut }) {
  const c = row.call;
  const p = row.put;
  return (
    <tr
      data-atm={isATM ? "true" : undefined}
      style={{
        borderTop: `1px solid ${C.border}`,
        background: isATM ? "rgba(201,161,90,0.06)" : "transparent",
      }}
    >
      <td style={{ padding: 6, textAlign: "center" }}>
        <StarButton active={isWatchedCall} onClick={onToggleCall} />
      </td>
      <OICell oi={c.oi} oiMax={oiMax} side="call" />
      <td style={{ padding: 6, color: c.chg_oi > 0 ? C.green : c.chg_oi < 0 ? C.red : C.muted }}>{fmtChg(c.chg_oi)}</td>
      {!compact && (
        <>
          <td style={{ padding: 6 }}>{fmtIN(c.volume)}</td>
          <td style={{ padding: 6 }}>{c.iv ?? "-"}</td>
          <td style={{ padding: 6 }}>{c.vega ?? "-"}</td>
          <td style={{ padding: 6 }}>{c.theta ?? "-"}</td>
          <td style={{ padding: 6 }}>{c.gamma ?? "-"}</td>
          <td style={{ padding: 6 }}>{c.delta ?? "-"}</td>
        </>
      )}
      <td style={{ padding: 6, color: C.green, fontWeight: 600 }}>{c.ltp ?? "-"}</td>
      <td style={{ padding: 6, textAlign: "center", fontWeight: 700 }}>
        {fmtIN(row.strike)}
        {isMaxPain && <span title="Max pain strike" style={{ color: C.gold, marginLeft: 4, fontSize: 10 }}>◆</span>}
      </td>
      <td style={{ padding: 6, color: C.red, fontWeight: 600 }}>{p.ltp ?? "-"}</td>
      {!compact && (
        <>
          <td style={{ padding: 6 }}>{p.delta ?? "-"}</td>
          <td style={{ padding: 6 }}>{p.gamma ?? "-"}</td>
          <td style={{ padding: 6 }}>{p.theta ?? "-"}</td>
          <td style={{ padding: 6 }}>{p.vega ?? "-"}</td>
          <td style={{ padding: 6 }}>{p.iv ?? "-"}</td>
          <td style={{ padding: 6 }}>{fmtIN(p.volume)}</td>
        </>
      )}
      <td style={{ padding: 6, color: p.chg_oi > 0 ? C.green : p.chg_oi < 0 ? C.red : C.muted }}>{fmtChg(p.chg_oi)}</td>
      <OICell oi={p.oi} oiMax={oiMax} side="put" />
      <td style={{ padding: 6, textAlign: "center" }}>
        <StarButton active={isWatchedPut} onClick={onTogglePut} />
      </td>
    </tr>
  );
}

function WatchlistItem({ item, alerts, onRemove, onAddAlert, onRemoveAlert }) {
  const [showForm, setShowForm] = useState(false);
  const [condition, setCondition] = useState("above");
  const [level, setLevel] = useState("");

  const submit = () => {
    const parsed = Number(level);
    if (!parsed && parsed !== 0) return;
    onAddAlert(item, condition, parsed);
    setLevel("");
    setShowForm(false);
  };

  return (
    <div style={{ padding: "8px 8px", fontSize: 12.5, borderBottom: `1px solid ${C.border}` }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <div>
            {item.symbol ?? "NIFTY"} {item.strike} {item.type === "call" ? "CE" : "PE"}
          </div>
          <div style={{ fontSize: 10.5, color: C.muted }}>{item.expiry}</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: item.type === "call" ? C.green : C.red }}>{item.ltp != null ? item.ltp : "—"}</span>
          <button onClick={() => setShowForm((s) => !s)} title="Add price alert" style={{ background: "none", border: "none", color: showForm ? C.gold : C.muted, cursor: "pointer", fontSize: 13 }}>
            ⏰
          </button>
          <button onClick={onRemove} style={{ background: "none", border: "none", color: C.muted, cursor: "pointer", fontSize: 14 }}>
            ×
          </button>
        </div>
      </div>

      {alerts.map((a) => (
        <div key={a.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 11, color: a.triggeredAt ? C.gold : C.muted, marginTop: 4 }}>
          <span>
            {a.condition === "above" ? "≥" : "≤"} {a.level} {a.triggeredAt ? "· fired" : ""}
          </span>
          <button onClick={() => onRemoveAlert(a.id)} style={{ background: "none", border: "none", color: C.faint, cursor: "pointer", fontSize: 12 }}>
            ×
          </button>
        </div>
      ))}

      {showForm && (
        <div style={{ display: "flex", gap: 6, marginTop: 6, alignItems: "center" }}>
          <select value={condition} onChange={(e) => setCondition(e.target.value)} style={{ background: C.surface2, color: C.text, border: `1px solid ${C.border}`, borderRadius: 4, padding: "3px 4px", fontSize: 11 }}>
            <option value="above">LTP ≥</option>
            <option value="below">LTP ≤</option>
          </select>
          <input
            type="number"
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            placeholder="price"
            style={{ width: 70, background: C.surface2, color: C.text, border: `1px solid ${C.border}`, borderRadius: 4, padding: "3px 5px", fontSize: 11 }}
          />
          <button onClick={submit} style={{ fontSize: 11, color: C.gold, background: "none", border: `1px solid ${C.gold}`, borderRadius: 4, padding: "3px 8px", cursor: "pointer" }}>
            Set
          </button>
        </div>
      )}
    </div>
  );
}

function StarButton({ active, onClick }) {
  return (
    <button onClick={onClick} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 14, color: active ? C.gold : C.muted }}>
      {active ? "★" : "☆"}
    </button>
  );
}
