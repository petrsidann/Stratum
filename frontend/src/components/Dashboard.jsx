import React, { useMemo, useState } from "react";

const BAND_STYLES = {
  green: "border-stratum-accent/60 text-stratum-accent",
  yellow: "border-stratum-warn/60 text-stratum-warn",
  grey: "border-stratum-border text-stratum-muted",
};

function fmtSigned(v, suffix = "%") {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${Number(v).toFixed(2)}${suffix}`;
}

function SignalBadge({ label, on, tone = "accent" }) {
  const color = on
    ? tone === "danger"
      ? "bg-stratum-danger/15 text-stratum-danger border-stratum-danger/50"
      : "bg-stratum-accent/15 text-stratum-accent border-stratum-accent/50"
    : "bg-stratum-bg text-stratum-muted border-stratum-border";
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${color}`}>
      {label}
    </span>
  );
}

function OpportunityCard({ opp }) {
  return (
    <div
      className={`rounded-lg border bg-stratum-panel p-3 transition hover:border-stratum-accent/40 ${
        BAND_STYLES[opp.band] || BAND_STYLES.grey
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-stratum-muted">
            {opp.market_type} · {opp.selection}
          </p>
          <p className="mt-1 text-xl font-black tabular text-gray-100">
            {opp.offered_odds > 0 ? `+${opp.offered_odds}` : opp.offered_odds}
          </p>
          <p className="text-[11px] text-stratum-muted">
            best @ {opp.best_book || "?"} · fair{" "}
            <span className="tabular">{opp.fair_odds ?? "—"}</span>
          </p>
        </div>
        <div className="text-right">
          <p className="text-2xl font-black tabular">{opp.confidence}</p>
          <p className="text-[9px] uppercase tracking-widest text-stratum-muted">conf</p>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-1 text-[11px] tabular">
        <span className={opp.edge_pct > 0 ? "text-stratum-accent" : "text-stratum-danger"}>
          EV {fmtSigned(opp.edge_pct)}
        </span>
        <span className="text-right text-stratum-muted">
          ¼Kelly {(opp.kelly_quarter * 100).toFixed(2)}%
        </span>
      </div>
      {/* confidence meter */}
      <div className="mt-2 h-1 w-full rounded bg-stratum-border">
        <div
          className="h-1 rounded bg-stratum-accent"
          style={{ width: `${Math.min(100, opp.confidence)}%` }}
        />
      </div>
    </div>
  );
}

export default function Dashboard({ scan }) {
  const [minConf, setMinConf] = useState(scan.summary?.min_confidence ?? 60);
  const results = scan.results || [];

  const stats = scan.summary || {};
  const kpis = [
    { label: "Matches", value: stats.matches_scanned ?? results.length },
    { label: "Opportunities", value: stats.total_opportunities ?? 0 },
    { label: "Above Threshold", value: stats.above_threshold ?? 0 },
    { label: "Critical", value: stats.critical_alerts ?? 0, hot: (stats.critical_alerts ?? 0) > 0 },
    { label: "Steam", value: stats.steam_signals ?? 0, hot: (stats.steam_signals ?? 0) > 0 },
    { label: "Arb %", value: stats.arbitrage_found ?? 0, hot: (stats.arbitrage_found ?? 0) > 0 },
  ];

  const visible = useMemo(
    () =>
      results.map((res) => ({
        ...res,
        opportunities: (res.opportunities || []).filter((o) => o.confidence >= minConf),
      })),
    [results, minConf]
  );

  return (
    <section className="space-y-4">
      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {kpis.map((k) => (
          <div key={k.label} className="rounded-lg border border-stratum-border bg-stratum-panel p-3">
            <p className="text-[10px] font-bold uppercase tracking-widest text-stratum-muted">{k.label}</p>
            <p className={`text-2xl font-black tabular ${k.hot ? "text-stratum-accent" : "text-gray-100"}`}>
              {k.value}
            </p>
          </div>
        ))}
      </div>

      {/* Filter control — pure client-side, never mutates data */}
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-stratum-border bg-stratum-panel px-4 py-2">
        <label htmlFor="conf" className="text-xs font-bold uppercase tracking-widest text-stratum-muted">
          Min Confidence
        </label>
        <input
          id="conf"
          type="range"
          min="0"
          max="95"
          value={minConf}
          onChange={(e) => setMinConf(Number(e.target.value))}
          className="min-h-touch w-48 accent-[#2EE6A6]"
        />
        <span className="tabular text-sm font-black text-stratum-accent">{minConf}</span>
      </div>

      {/* Match sections with opportunity card grids */}
      {visible.map((res) => (
        <div key={res.match_id} className="rounded-xl border border-stratum-border bg-stratum-bg/50 p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-base font-black text-gray-100">
                {res.away} <span className="text-stratum-muted">@</span> {res.home}
              </h2>
              <p className="text-[11px] text-stratum-muted">
                {res.sport} · {res.quote_count} quotes · {res.data_source.toUpperCase()} · {res.scanned_at}
              </p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <SignalBadge label="🔥 Steam" on={!!res.signals?.steam} />
              <SignalBadge label={`⚖️ ${res.signals?.rlm || "NONE"}`} on={res.signals?.rlm && res.signals.rlm !== "NONE"} />
              <SignalBadge label={`💸 ${(res.signals?.arb_pct || 0).toFixed(2)}%`} on={(res.signals?.arb_pct || 0) > 0} tone="danger" />
              <SignalBadge label={`Stale ${res.stale_flags?.length || 0}`} on={(res.stale_flags?.length || 0) > 0} tone="danger" />
            </div>
          </div>

          {res.insight && (
            <p className="mb-3 rounded-md border-l-2 border-stratum-accent bg-stratum-panel px-3 py-2 text-xs italic text-gray-300">
              🧠 {res.insight}
            </p>
          )}

          {res.opportunities.length === 0 ? (
            <p className="py-4 text-center text-xs text-stratum-muted">
              No signals ≥ {minConf} confidence for this match.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {res.opportunities.slice(0, 12).map((opp, i) => (
                <OpportunityCard key={`${opp.market_type}-${opp.selection}-${i}`} opp={opp} />
              ))}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}
