"use client";
import { useCallback, useEffect, useState } from "react";
import { C } from "@/lib/ui";
import { LoadingState, ErrorState, Table, Badge } from "@/components/app/core";
import {
  getAdminAudit,
  getAdapters,
  getFeatureFlags,
  getIngestionHealth,
  getModelMetadata,
  getAdminControls,
} from "@/lib/adminApi";
import { isAuthError } from "@/lib/api";

/**
 * Day 45 — Admin control plane (Issue #90). Presentation/admin-control UI
 * only: every value comes from the admin API (server-side AdminUser
 * boundary); nothing here authorizes, decides, or executes. Non-admin
 * sessions get the backend's 403 surfaced as an explicit "not authorized"
 * state — frontend state can never grant access.
 */

const TABS = [
  ["ingestion", "Ingestion health"],
  ["adapters", "Adapters"],
  ["flags", "Feature flags"],
  ["models", "Model metadata"],
  ["audit", "Audit activity"],
];

const STATUS_COLORS = {
  SUCCESS: C.green,
  COMPLETED: C.green,
  DRY_RUN: C.green,
  FAILED: C.red,
  PENDING: C.gold,
  RUNNING: C.gold,
};

const panel = { background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 14, minWidth: 0 };
const sectionTitle = { fontSize: 12, fontWeight: 800, letterSpacing: 0.8, color: C.muted, marginBottom: 8 };

function Row({ label, value }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "4px 0", fontSize: 12 }}>
      <span style={{ color: C.muted }}>{label}</span>
      <span style={{ color: C.text, fontWeight: 600 }}>{value}</span>
    </div>
  );
}

export default function AdminPage() {
  const [tab, setTab] = useState("ingestion");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [notAuthorized, setNotAuthorized] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotAuthorized(false);
    try {
      let payload;
      if (tab === "ingestion") payload = await getIngestionHealth();
      else if (tab === "adapters") payload = await getAdapters();
      else if (tab === "flags") payload = await getFeatureFlags();
      else if (tab === "models") payload = await getModelMetadata();
      else payload = await getAdminAudit();
      setData(payload);
    } catch (e) {
      if (isAuthError(e)) {
        setNotAuthorized(true);
      } else if (e?.response?.status === 403) {
        setNotAuthorized(true);
      } else {
        setError(e.message || "Failed to load admin data.");
      }
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !data && !error && !notAuthorized) {
    return (
      <div style={{ maxWidth: 1200 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: "0 0 4px 0" }}>Admin</h1>
        <LoadingState message="Loading admin data…" />
      </div>
    );
  }

  if (notAuthorized) {
    return (
      <div style={{ maxWidth: 1200 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: "0 0 4px 0" }}>Admin</h1>
        <div style={{ ...panel, textAlign: "center", padding: 40 }}>
          <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>Not authorized</div>
          <div style={{ fontSize: 12.5, color: C.muted }}>
            Admin privileges are required for this control plane.
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ maxWidth: 1200 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: "0 0 4px 0" }}>Admin</h1>
        <ErrorState message={error} onRetry={load} />
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 1200 }}>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0, marginBottom: 4 }}>Admin</h1>
        <p style={{ fontSize: 13, color: C.muted, margin: 0 }}>
          Platform operations — separated from ordinary user workflows
        </p>
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
        {TABS.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            style={{
              fontSize: 11.5,
              fontWeight: 700,
              padding: "5px 12px",
              borderRadius: 6,
              cursor: "pointer",
              fontFamily: "inherit",
              border: `1px solid ${tab === key ? C.gold : C.border}`,
              background: tab === key ? "rgba(201,161,90,0.10)" : C.surface,
              color: tab === key ? C.gold : C.muted,
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "ingestion" && (
        <div style={panel}>
          <div style={sectionTitle}>INGESTION HEALTH</div>
          <Row label="Recent runs" value={data?.total ?? 0} />
          <Row label="Failures" value={data?.failed ?? 0} />
          <div style={{ marginTop: 10 }}>
            <Table
              columns={["Run", "Operation", "Instrument", "Started", "Status", "Rows"]}
              data={(data?.runs ?? []).map((r) => ({
                id: `${r.run_id}-${r.instrument_key ?? ""}`,
                run: r.run_id,
                operation: r.operation,
                instrument: r.instrument_key ?? "—",
                started: r.started_at ?? "—",
                status: (
                  <Badge variant={STATUS_COLORS[r.status] ? "neutral" : "neutral"}>
                    <span style={{ color: STATUS_COLORS[r.status] ?? C.muted }}>{r.status}</span>
                  </Badge>
                ),
                rows: `${r.rows_fetched ?? 0}/${r.rows_inserted ?? 0}`,
              }))}
              keyExtractor={(row) => row.id}
              emptyMessage="No ingestion runs recorded."
            />
          </div>
        </div>
      )}

      {tab === "adapters" && (
        <div style={panel}>
          <div style={sectionTitle}>BROKER / DATA ADAPTERS</div>
          {(data?.adapters ?? []).map((a) => (
            <Row key={a.broker} label={a.broker} value={a.adapter} />
          ))}
          {(data?.adapters ?? []).length === 0 && (
            <div style={{ fontSize: 12, color: C.faint }}>No adapters registered.</div>
          )}
        </div>
      )}

      {tab === "flags" && (
        <div style={panel}>
          <div style={sectionTitle}>FEATURE FLAGS</div>
          {(data?.controls ?? []).length === 0 && (
            <div style={{ fontSize: 12, color: C.faint }}>No flags configured.</div>
          )}
          {(data?.controls ?? []).map((f) => (
            <Row key={f.key} label={`${f.key} (v${f.version})`} value={String(f.value)} />
          ))}
        </div>
      )}

      {tab === "models" && (
        <div style={panel}>
          <div style={sectionTitle}>MODEL METADATA</div>
          {(data?.models ?? []).map((m) => (
            <Row key={m.name} label={m.name} value={m.role} />
          ))}
        </div>
      )}

      {tab === "audit" && (
        <div style={panel}>
          <div style={sectionTitle}>AUDIT ACTIVITY</div>
          <Table
            columns={["Time", "Actor", "Action", "Result"]}
            data={(data?.events ?? []).map((e) => ({
              id: e.id,
              time: e.occurred_at ?? "—",
              actor: e.actor_user_id ?? "anonymous",
              action: e.action,
              result: (
                <span style={{ color: e.result === "success" ? C.green : e.result === "denied" ? C.red : C.gold }}>
                  {e.result}
                </span>
              ),
            }))}
            keyExtractor={(row) => row.id}
            emptyMessage="No admin activity recorded."
          />
        </div>
      )}
    </div>
  );
}
