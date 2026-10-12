// PROF-DASH item 5/6 — "NEVER BLANK" cross-sport TOP PICKS betslip.
// Inline styles ONLY (no Tailwind). T1 STAKE / T2 STAKE(caution) / T3 SMELL TEST
// (T3 always emits the single best fallback, min 0.25u, with explicit warnings).
// Hit-rate ranked (p_eff desc then edge), SAFE% beside HIT%, WHY-THIS-BET proof list,
// ensemble-agreement badge, gates passed/failed, exposure cap 15% bankroll,
// max 2 bets same class same day, correlation cap max 2 bets/fixture never both
// sides of same total, parlay rows w/ joint p + correlation label, grey chips.

import React from 'react';

export interface PickRow {
  sport: string;
  fixture: string;
  market: string;
  class_key: string;
  line?: number | string;
  price: number;
  source?: string;
  p_eff: number;      // HIT%
  safe_pct?: number;  // bootstrap 25th-pct SAFE%
  edge: number;
  tier: 'T1' | 'T2' | 'T3';
  stake_u?: number;   // min(0.25*Kelly, 2u); T3 floor 0.25u
  agreement_ok?: boolean;
  gates_passed?: string[];
  gates_failed?: string[];
  why?: string[];
  kind?: string;
}

export interface ParlayRow {
  combo: string;
  p_joint: number;
  p_indep: number;
  correlation: string;
  fair_price: number;
  book_price?: number | null;
  edge?: number | null;
  why?: string[];
}

const C = {
  card: '#101418', head: '#1a2129', row: '#151b22', border: '#2a3440',
  t1: '#2ecc71', t2: '#f1c40f', t3: '#e67e22', grey: '#5b6672', text: '#dfe6ee',
  dim: '#8a97a5', bad: '#e74c3c', chipBg: '#232c36',
};

const fmtPct = (v: number) => `${(v * 100).toFixed(1)}%`;

export function buildSlip(picks: PickRow[], bankrollU: number, log: (s: string) => void) {
  // hit-rate rank: p_eff desc then edge desc
  const ranked = [...picks].sort((a, b) => b.p_eff - a.p_eff || b.edge - a.edge);
  const capU = bankrollU * 0.15;
  const perDay: Record<string, number> = {};
  const perFixture: Record<string, number> = {};
  const perSide: Record<string, string> = {}; // fixture|total -> side taken
  const accepted: PickRow[] = [];
  const rejected: Array<{ p: PickRow; reason: string }> = [];
  let usedU = 0;
  for (const p of ranked) {
    const stake = Math.max(p.tier === 'T3' ? 0.25 : 0.25, Math.min(p.stake_u ?? 0.25, 2));
    const dayKey = `${p.class_key}`;
    const fxKey = p.fixture;
    if ((perDay[dayKey] || 0) >= 2) { rejected.push({ p, reason: 'max 2 bets same class same day' }); continue; }
    if ((perFixture[fxKey] || 0) >= 2) { rejected.push({ p, reason: 'correlation cap: max 2 bets/fixture' }); continue; }
    const totKey = `${fxKey}|total|${p.line}`;
    const sideNow = String(p.market).toLowerCase().includes('under') ? 'under' : 'over';
    if (perSide[totKey] && perSide[totKey] !== sideNow) { rejected.push({ p, reason: 'never both sides of same total' }); continue; }
    if (usedU + stake > capU) { rejected.push({ p, reason: `exposure cap 15% bankroll (${capU.toFixed(1)}u)` }); continue; }
    usedU += stake;
    perDay[dayKey] = (perDay[dayKey] || 0) + 1;
    perFixture[fxKey] = (perFixture[fxKey] || 0) + 1;
    perSide[totKey] = sideNow;
    accepted.push({ ...p, stake_u: stake });
    log(`[BETSLIP] ${p.tier} ${p.sport} ${p.fixture} :: ${p.market}${p.line !== undefined ? ' ' + p.line : ''} @${p.price} HIT=${fmtPct(p.p_eff)} SAFE=${p.safe_pct !== undefined ? fmtPct(p.safe_pct) : '-'} ${p.why?.[0] ?? 'edge ' + fmtPct(p.edge)}`);
  }
  return { accepted, rejected, usedU, capU };
}

const Badge: React.FC<{ ok?: boolean }> = ({ ok }) => (
  <span style={{ fontSize: 10, padding: '2px 6px', borderRadius: 8, marginRight: 6,
    background: ok === false ? C.bad : C.t1, color: '#fff' }}>
    {ok === false ? 'ENSEMBLE SPLIT' : 'ENSEMBLE AGREE'}
  </span>
);

const Row: React.FC<{ p: PickRow; caution?: boolean }> = ({ p, caution }) => {
  const [whyOpen, setWhyOpen] = React.useState(p.tier !== 'T3');
  return (
  <div style={{ background: C.row, border: `1px solid ${C.border}`, borderRadius: 8, padding: 10, marginBottom: 8 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap' }}>
      <div style={{ fontWeight: 700, color: C.text }}>
        <span style={{ color: p.tier === 'T1' ? C.t1 : p.tier === 'T2' ? C.t2 : C.t3, marginRight: 6 }}>
          {p.tier}{caution ? ' (caution)' : p.tier === 'T3' ? ' SMELL TEST' : ' STAKE'}
        </span>
        {p.sport.toUpperCase()} · {p.fixture} — {p.market}{p.line !== undefined ? ` ${p.line}` : ''} @{p.price}
      </div>
      <div style={{ color: C.dim, fontSize: 12 }}>stake {Math.max(p.stake_u ?? 0.25, 0.25).toFixed(2)}u · src {p.source || '-'}</div>
    </div>
    <div style={{ marginTop: 6, fontSize: 12, color: C.text }}>
      <Badge ok={p.agreement_ok} />
      HIT {fmtPct(p.p_eff)} &nbsp;·&nbsp; SAFE {p.safe_pct !== undefined ? fmtPct(p.safe_pct) : '--'} &nbsp;·&nbsp; EDGE {fmtPct(p.edge)}
      &nbsp;·&nbsp; gates ✓[{(p.gates_passed || []).join(', ') || 'all'}] ✗[{(p.gates_failed || []).join(', ') || 'none'}]
    </div>
    {p.tier === 'T3' && (
      <div style={{ marginTop: 6, fontSize: 11, color: C.t3 }}>⚠ WARNING: smell-test only — best available fallback; review before any stake.</div>
    )}
    {(p.why || []).length > 0 && (
      <div style={{ marginTop: 8, borderTop: `1px dashed ${C.border}`, paddingTop: 6 }}>
        <button onClick={() => setWhyOpen(o => !o)}
          style={{ background: 'transparent', border: 'none', cursor: 'pointer', fontSize: 11, fontWeight: 700, color: C.dim, padding: 0, fontFamily: 'inherit' }}>
          WHY THIS BET {whyOpen ? '▾' : '▸'}
        </button>
        {whyOpen && (
          <ul style={{ margin: '4px 0 0 18px', padding: 0, fontSize: 12, color: C.text }}>
            {(p.why || []).map((w, i) => (<li key={i} style={{ marginBottom: 2 }}>{w}</li>))}
          </ul>
        )}
      </div>
    )}
  </div>
  );
};

export const BetslipCard: React.FC<{
  picks: PickRow[]; parlays?: ParlayRow[]; bankrollU?: number; greyChips?: string[];
  onLog?: (s: string) => void;
}> = ({ picks, parlays = [], bankrollU = 100, greyChips = ['cricket', 'golf', 'motorsports', 'darts', 'esports', 'MMA'], onLog }) => {
  const log = onLog || (() => {});
  const { accepted, rejected, usedU, capU } = buildSlip(picks, bankrollU, log);
  const byTier = { T1: [] as PickRow[], T2: [] as PickRow[], T3: [] as PickRow[] };
  accepted.forEach(p => byTier[p.tier].push(p));
  // NEVER EMPTY: every hunted match must show something — if all filtered, force top candidate into T3.
  if (accepted.length === 0 && picks.length > 0) {
    const best = [...picks].sort((a, b) => b.p_eff - a.p_eff)[0];
    byTier.T3.push({ ...best, tier: 'T3', stake_u: 0.25, gates_failed: best.gates_failed || ['all gates'] });
  }
  const hasT12 = byTier.T1.length + byTier.T2.length > 0;
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: 14, fontFamily: 'system-ui, sans-serif' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div style={{ fontSize: 16, fontWeight: 800, color: C.text }}>🎯 TOP PICKS — CROSS-SPORT</div>
        <div style={{ fontSize: 12, color: usedU > capU ? C.bad : C.dim }}>
          open exposure {usedU.toFixed(2)}u / cap {capU.toFixed(1)}u (15%)
        </div>
      </div>
      {byTier.T1.map(p => <Row key={`t1${p.fixture}${p.market}`} p={p} />)}
      {byTier.T2.map(p => <Row key={`t2${p.fixture}${p.market}`} p={p} caution />)}
      {byTier.T3.map(p => <Row key={`t3${p.fixture}${p.market}`} p={p} />)}
      {!hasT12 && byTier.T3.length > 0 && (
        <div style={{ fontSize: 11, color: C.dim, fontStyle: 'italic', marginTop: 4 }}>
          footnote: no T1/T2 qualified today — discipline over gambling; T3 shown so the board is never blank.
        </div>
      )}
      {rejected.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 11, color: C.grey }}>
          {rejected.map((r, i) => (<div key={i}>✗ dropped {r.p.fixture} {r.p.market}: {r.reason}</div>))}
        </div>
      )}
      {parlays.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: C.text, marginBottom: 6 }}>🔗 CORRELATED PARLAYS (exact score-matrix)</div>
          {parlays.slice(0, 4).map((pr, i) => (
            <div key={i} style={{ background: C.head, border: `1px solid ${C.border}`, borderRadius: 8, padding: 8, marginBottom: 6, fontSize: 12, color: C.text }}>
              {pr.combo} — joint {fmtPct(pr.p_joint)} vs indep {fmtPct(pr.p_indep)}{' '}
              <span style={{ color: pr.correlation.includes('POSITIVE') ? C.t2 : C.dim }}>[{pr.correlation}]</span>{' '}
              fair {pr.fair_price.toFixed(2)}{pr.book_price ? ` vs book ${pr.book_price.toFixed(2)} (edge ${pr.edge! >= 0 ? '+' : ''}${fmtPct(pr.edge!)})` : ''}
              {(pr.why || []).map((w, j) => (<div key={j} style={{ color: C.dim, fontSize: 11 }}>• {w}</div>))}
            </div>
          ))}
        </div>
      )}
      <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {greyChips.map(g => (
          <span key={g} title="needs data source" style={{ background: C.chipBg, color: C.grey, borderRadius: 12, padding: '3px 10px', fontSize: 11, opacity: 0.75 }}>
            {g} · needs data source
          </span>
        ))}
      </div>
    </div>
  );
};

export default BetslipCard;
