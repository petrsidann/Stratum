import React, { useMemo } from "react";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Cell,
} from "recharts";

const ACCENT = "#2EE6A6";
const DANGER = "#FF4D4D";
const MUTED = "#8B949E";
const GRID = "#21262D";

function Panel({ title, children, empty }) {
  return (
    <div className="rounded-xl border border-stratum-border bg-stratum-panel p-4">
      <h3 className="mb-3 text-xs font-black uppercase tracking-widest text-stratum-muted">{title}</h3>
      {empty ? (
        <p className="py-10 text-center text-xs text-stratum-muted">{empty}</p>
      ) : (
        children
      )}
    </div>
  );
}

const tooltipStyle = {
  backgroundColor: "#161B22",
  border: `1px solid ${GRID}`,
  borderRadius: 8,
  fontSize: 12,
  color: "#E6EDF3",
};

export default function Charts({ scan, stats }) {
  const results = scan.results || [];

  // Top edges across the whole board (one bar per flagged opportunity).
  const edgeData = useMemo(() => {
    const rows = [];
    for (const res of results) {
      for (const opp of res.opportunities || []) {
        if (opp.edge_pct > 0) {
          rows.push({
            name: `${res.match_id.split(":")[1] || res.match_id} ${opp.selection}`.slice(0, 22),
            edge: opp.edge_pct,
            conf: opp.confidence,
          });
        }
      }
    }
    rows.sort((a, b) => b.edge - a.edge);
    return rows.slice(0, 12);
  }, [results]);

  // Confidence distribution per band (green >80, yellow 60–80, grey <60).
  const bandData = useMemo(() => {
    const bands = { green: 0, yellow: 0, grey: 0 };
    for (const res of results) {
      for (const opp of res.opportunities || []) bands[opp.band] = (bands[opp.band] || 0) + 1;
    }
    return [
      { name: "GREEN >80", value: bands.green || 0, fill: ACCENT },
      { name: "YELLOW 60-80", value: bands.yellow || 0, fill: "#F5C518" },
      { name: "GREY <60", value: bands.grey || 0, fill: MUTED },
    ];
  }, [results]);

  const s = stats || {};
  const statCards = [
    { label: "ROI %", value: s.has_data ? `${s.roi_pct}%` : "N/A" },
    { label: "Win Rate", value: s.has_data ? `${s.win_rate_pct}%` : "N/A" },
    { label: "Avg CLV", value: s.avg_clv_pct != null ? `${s.avg_clv_pct}%` : "N/A" },
    { label: "Beat Close", value: s.beat_close_rate_pct != null ? `${s.beat_close_rate_pct}%` : "N/A" },
    { label: "Settled Bets", value: s.n_settled ?? 0 },
    { label: "Net P/L", value: s.has_data ? `$${s.net_profit}` : "N/A" },
  ];

  return (
    <section className="space-y-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel
          title="Top Positive Edges (EV %)"
          empty={edgeData.length === 0 ? "No positive-EV edges above threshold in this scan." : null}
        >
          <div className="h-72 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={edgeData} margin={{ left: -18, right: 8, top: 4 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="name" tick={{ fill: MUTED, fontSize: 9 }} interval={0} angle={-28} textAnchor="end" height={54} />
                <YAxis tick={{ fill: MUTED, fontSize: 10 }} unit="%" />
                <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "#21262D55" }} />
                <Bar dataKey="edge" radius={[3, 3, 0, 0]}>
                  {edgeData.map((d, i) => (
                    <Cell key={i} fill={d.conf >= 80 ? ACCENT : d.conf >= 60 ? "#F5C518" : MUTED} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>

        <Panel title="Signal Confidence Bands">
          <div className="h-72 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={bandData} layout="vertical" margin={{ left: 8, right: 24 }}>
                <CartesianGrid stroke={GRID} horizontal={false} />
                <XAxis type="number" tick={{ fill: MUTED, fontSize: 10 }} allowDecimals={false} />
                <YAxis type="category" dataKey="name" width={110} tick={{ fill: MUTED, fontSize: 10 }} />
                <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "#21262D55" }} />
                <Bar dataKey="value" radius={[0, 3, 3, 0]}>
                  {bandData.map((d, i) => (
                    <Cell key={i} fill={d.fill} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      </div>

      {/* Portfolio ledger strip (from data/portfolio_stats.json) */}
      <Panel title={`Portfolio · last ${s.window_days ?? 30} days`}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          {statCards.map((c) => (
            <div key={c.label} className="rounded-lg border border-stratum-border bg-stratum-bg p-3">
              <p className="text-[10px] font-bold uppercase tracking-widest text-stratum-muted">{c.label}</p>
              <p className="text-lg font-black tabular text-gray-100">{c.value}</p>
            </div>
          ))}
        </div>
        {!s.has_data && (
          <p className="mt-3 text-[11px] text-stratum-muted">
            Ledger empty — performance metrics appear once settled bets exist. Unknown is valid; we never fabricate results.
          </p>
        )}
      </Panel>
    </section>
  );
}
