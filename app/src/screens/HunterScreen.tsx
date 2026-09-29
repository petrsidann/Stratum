import React, { useEffect, useRef, useState } from "react";
import { dispatchHunt, pollHunt, pollLatest, getToken, setToken, AGENT_COLORS } from "../lib/swarmClient";

const C = {
  bg: "#0F111A", panel: "#1E2330", panelDeep: "#0B0D12",
  border: "#2A3242", borderSoft: "#1E2330",
  accent: "#00E5FF", green: "#2EE6A6", yellow: "#FACC15",
  muted: "#8B9BB4", red: "#F87171", text: "#FFFFFF", black: "#000000",
};
const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

const page: React.CSSProperties = {
  minHeight: "100vh", backgroundColor: C.bg, color: C.text,
  display: "flex", flexDirection: "column", alignItems: "center",
  padding: "40px 16px", boxSizing: "border-box",
};
const card: React.CSSProperties = {
  backgroundColor: C.panelDeep, border: `1px solid ${C.borderSoft}`,
  borderRadius: 12, padding: 16, width: "100%", maxWidth: 720, boxSizing: "border-box",
};
const inputStyle: React.CSSProperties = {
  width: "100%", maxWidth: 560, backgroundColor: C.panel, border: `1px solid ${C.border}`,
  borderRadius: 8, padding: "12px 16px", color: C.text, fontFamily: MONO, fontSize: 15,
  boxSizing: "border-box", outline: "none",
};
const primaryBtn: React.CSSProperties = {
  backgroundColor: C.accent, color: C.black, fontWeight: 700, textTransform: "uppercase",
  letterSpacing: 2, padding: "12px 32px", borderRadius: 999, border: "none",
  cursor: "pointer", fontFamily: MONO, fontSize: 15,
};
const th: React.CSSProperties = { color: C.muted, textAlign: "left", padding: "4px 8px 4px 0", fontWeight: 400 };
const td: React.CSSProperties = { padding: "4px 10px 4px 0", verticalAlign: "top" };

export default function HunterScreen() {
  const [phase, setPhase] = useState<"IDLE" | "CONSOLE" | "RESULTS">("IDLE");
  const [query, setQuery] = useState("");
  const [sport, setSport] = useState("auto");
  const [token, setTokenUi] = useState(getToken());
  const [state, setState] = useState<any | null>(null);
  const [result, setResult] = useState<any | null>(null);
  const [err, setErr] = useState("");
  const timer = useRef<number | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => { if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight; }, [state?.lines?.length]);
  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current); }, []);

  const stop = () => { if (timer.current) window.clearInterval(timer.current); timer.current = null; };

  const start = async (q?: string) => {
    const useQ = (q ?? query).trim();
    setErr("");
    if (!getToken()) { setErr("One-time setup: open Settings below and paste your trigger token."); return; }
    if (!useQ) { setErr("Type a match first."); return; }
    if (q) setQuery(q);
    const huntId = `h${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
    setResult(null);
    setPhase("CONSOLE");
    setState({ lines: [{ agent: "SYSTEM", text: "dispatching hunters to GitHub cloud…", ts: Date.now() / 1000 }], progress: 0.02, stage: "DISPATCH" });
    try { await dispatchHunt(useQ, sport, huntId); }
    catch (e: any) { stop(); setPhase("IDLE"); setErr(e?.message || "Dispatch failed"); return; }
    let ticks = 0;
    timer.current = window.setInterval(async () => {
      ticks += 1;
      const c = await pollHunt(huntId);
      if (c) {
        setState(c);
        if (c.status === "complete" || c.status === "no_results" || c.status === "error") {
          stop(); setResult(c.result || {}); setPhase("RESULTS"); return;
        }
      }
      if (ticks % 4 === 0) {
        const l = await pollLatest();
        if (l && l.result && (l.hunt_id === huntId || ticks > 45) &&
            (l.status === "complete" || l.status === "no_results" || l.status === "error")) {
          stop(); setResult(l.result); setPhase("RESULTS");
        }
      }
      if (ticks > 150) { stop(); setPhase("IDLE"); setErr("Hunt timed out after ~5 min. Check Actions tab → Stratum Hunt On-Demand."); }
    }, 2000);
  };

  if (phase === "IDLE") return (
    <div style={page}>
      <h1 style={{ fontSize: 30, fontWeight: 700, letterSpacing: 6, margin: "0 0 8px", textAlign: "center" }}>ENTER MATCH TO HUNT</h1>
      <p style={{ color: C.muted, marginBottom: 24, textAlign: "center", maxWidth: 560 }}>
        Hunters scrape today's boards, de-vig every market, rank by true hit-probability, and return diagrams + stakes.
      </p>
      <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="e.g. Czechia vs Croatia" style={inputStyle} />
      <select value={sport} onChange={(e) => setSport(e.target.value)}
        style={{ ...inputStyle, maxWidth: 560, marginTop: 16 }}>
        {["auto", "soccer", "nba", "nfl", "mlb", "nhl"].map((s) => <option key={s}>{s}</option>)}
      </select>
      <button onClick={() => start()} style={{ ...primaryBtn, marginTop: 24 }}>[ Start Hunt ]</button>
      {err && <p style={{ marginTop: 16, color: C.red, fontFamily: MONO, fontSize: 13, textAlign: "center", maxWidth: 480 }}>{err}</p>}
      <details style={{ marginTop: 32, width: "100%", maxWidth: 560, fontSize: 14, color: C.muted }}>
        <summary style={{ cursor: "pointer", fontFamily: MONO }}>⚙ SETTINGS — trigger token (stored only on this phone)</summary>
        <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
          <input type="password" value={token} onChange={(e) => setTokenUi(e.target.value)} placeholder="paste your trigger token"
            style={{ flex: 1, backgroundColor: C.panel, border: `1px solid ${C.border}`, borderRadius: 6, padding: "8px 12px", color: C.text, fontFamily: MONO }} />
          <button onClick={() => setToken(token)}
            style={{ backgroundColor: C.green, color: C.black, fontWeight: 700, padding: "8px 16px", borderRadius: 6, border: "none", cursor: "pointer", fontFamily: MONO }}>SAVE</button>
        </div>
      </details>
    </div>
  );

  if (phase === "CONSOLE") return (
    <div style={{ ...page, alignItems: "stretch", padding: "24px 16px", fontFamily: MONO }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, gap: 12, flexWrap: "wrap" }}>
        <p style={{ color: C.muted, margin: 0 }}>TARGET :: {query} · {sport.toUpperCase()}</p>
        <button onClick={() => { stop(); setPhase("IDLE"); }}
          style={{ background: "transparent", border: `1px solid ${C.red}`, color: C.red, padding: "6px 16px", borderRadius: 6, cursor: "pointer", fontFamily: MONO }}>✕ CANCEL</button>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
        <span style={{ color: C.accent }}>[{String(state?.stage || "BOOT").toUpperCase()}]</span>
        <div style={{ flex: 1, height: 8, backgroundColor: C.panel, borderRadius: 4, overflow: "hidden" }}>
          <div style={{ height: 8, width: `${Math.round((state?.progress || 0) * 100)}%`, backgroundColor: C.accent, transition: "width .4s" }} />
        </div>
        <span style={{ color: C.muted }}>{Math.round((state?.progress || 0) * 100)}%</span>
      </div>
      <div ref={boxRef}
        style={{ height: "60vh", overflowY: "auto", backgroundColor: C.panelDeep, border: `1px solid ${C.borderSoft}`, borderRadius: 8, padding: 12, fontSize: 13 }}>
        {(state?.lines || []).map((l: any, i: number) => (
          <p key={i} style={{ margin: "4px 0" }}>
            <span style={{ color: AGENT_COLORS[l.agent] || "#94A3B8" }}>[{l.agent}]</span>{" "}{l.text}
          </p>
        ))}
      </div>
      <p style={{ marginTop: 12, color: C.muted, fontStyle: "italic" }}>swarm airborne — live agent feed…</p>
    </div>
  );

  if (!result || (result.fixtures || []).length === 0) return (
    <div style={{ ...page, justifyContent: "center" }}>
      <div style={{ border: `1px solid ${C.red}`, borderRadius: 12, padding: 32, maxWidth: 560, textAlign: "center" }}>
        <h2 style={{ color: C.red, fontWeight: 700, fontFamily: MONO, fontSize: 20, margin: "0 0 12px" }}>NO MARKETS DETECTED</h2>
        <p style={{ color: C.muted, marginBottom: 16 }}>{result?.error_message || "Honest empty result."}</p>
        {(result?.available_today || []).length > 0 && (
          <div style={{ textAlign: "left", fontSize: 13, color: C.muted, marginBottom: 16 }}>
            <p style={{ fontFamily: MONO, margin: "0 0 4px" }}>Playing today (tap to hunt):</p>
            {(result.available_today || []).slice(0, 8).map((n: string) => (
              <button key={n} onClick={() => start(n)}
                style={{ display: "block", background: "transparent", border: "none", color: C.accent, cursor: "pointer", fontFamily: MONO, fontSize: 13, padding: "2px 0", textAlign: "left" }}>{n}</button>
            ))}
          </div>
        )}
        <button onClick={() => setPhase("IDLE")} style={primaryBtn}>EDIT QUERY</button>
      </div>
    </div>
  );

  return (
    <div style={{ ...page, alignItems: "center", padding: "24px 16px" }}>
      <button onClick={() => setPhase("IDLE")}
        style={{ alignSelf: "flex-start", background: "transparent", border: "none", color: C.accent, fontFamily: MONO, cursor: "pointer", marginBottom: 16, fontSize: 14 }}>← NEW HUNT</button>
      {(result.fixtures || []).map((fx: any) => (
        <div key={fx.fixture_id} style={{ ...card, marginBottom: 32 }}>
          <h2 style={{ fontSize: 20, fontWeight: 700, margin: "0 0 4px" }}>{fx.home} @ {fx.away}</h2>
          <p style={{ color: C.muted, fontFamily: MONO, fontSize: 13, margin: "0 0 4px" }}>
            {fx.sport} · {fx.league} · {fx.kickoff_utc} · {fx.markets_scanned} market lines scanned
          </p>
          {fx.context_note && <p style={{ color: C.yellow, fontSize: 13, margin: "0 0 12px" }}>{fx.context_note}</p>}
          <table style={{ width: "100%", fontSize: 13, fontFamily: MONO, borderCollapse: "collapse" }}>
            <thead><tr><th style={th}>SELECTION</th><th style={th}>MARKET</th><th style={th}>BOOK</th><th style={th}>FAIR</th><th style={th}>HIT %</th><th style={th}>EV %</th><th style={th}>KELLY</th></tr></thead>
            <tbody>
              {(fx.top_edges || []).map((e: any, i: number) => (
                <tr key={i} style={{ borderTop: `1px solid ${C.borderSoft}` }}>
                  <td style={{ ...td, color: C.accent }}>{e.selection}</td>
                  <td style={{ ...td, color: C.muted }}>{e.market}</td>
                  <td style={td}>{e.book_odds}</td><td style={td}>{e.fair_odds}</td>
                  <td style={{ ...td, color: C.green, fontWeight: 700 }}>{e.confidence_score}</td>
                  <td style={{ ...td, color: e.ev_percent > 0 ? C.green : C.muted }}>{e.ev_percent}</td>
                  <td style={td}>{e.kelly_stake_pct}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(fx.diagrams || []).map((d: string) => (
            <img key={d} src={d} alt="diagram" style={{ marginTop: 16, borderRadius: 8, border: `1px solid ${C.borderSoft}`, width: "100%", maxWidth: 672 }} />
          ))}
        </div>
      ))}
    </div>
  );
}
