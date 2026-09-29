import React, { useEffect, useRef, useState } from "react";
import { dispatchHunt, pollHunt, pollLatest, getToken, setToken, AGENT_COLORS } from "../lib/swarmClient";

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
    <div className="min-h-screen bg-[#0F111A] text-white flex flex-col items-center px-4 py-10">
      <h1 className="text-3xl font-bold tracking-widest mb-2">ENTER MATCH TO HUNT</h1>
      <p className="text-[#8B9BB4] mb-6 text-center max-w-xl">Hunters scrape today's boards, de-vig every market, rank by true hit-probability, and return diagrams + stakes.</p>
      <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="e.g. Czechia vs Croatia"
        className="w-full max-w-xl bg-[#1E2330] border border-[#2A3242] rounded-lg px-4 py-3 font-mono" />
      <select value={sport} onChange={(e) => setSport(e.target.value)}
        className="mt-4 bg-[#1E2330] border border-[#2A3242] rounded-lg px-4 py-2 font-mono">
        {["auto", "soccer", "nba", "nfl", "mlb", "nhl"].map((s) => <option key={s}>{s}</option>)}
      </select>
      <button onClick={() => start()} className="mt-6 bg-[#00E5FF] text-black font-bold uppercase tracking-wider px-8 py-3 rounded-full shadow-lg">[ Start Hunt ]</button>
      {err && <p className="mt-4 text-red-400 font-mono text-sm text-center max-w-md">{err}</p>}
      <details className="mt-8 w-full max-w-xl text-sm text-[#8B9BB4]">
        <summary className="cursor-pointer font-mono">⚙ SETTINGS — trigger token (stored only on this phone)</summary>
        <div className="mt-2 flex gap-2">
          <input type="password" value={token} onChange={(e) => setTokenUi(e.target.value)} placeholder="paste your trigger token"
            className="flex-1 bg-[#1E2330] border border-[#2A3242] rounded px-3 py-2 font-mono" />
          <button onClick={() => setToken(token)} className="bg-[#2EE6A6] text-black font-bold px-4 py-2 rounded">SAVE</button>
        </div>
      </details>
    </div>
  );

  if (phase === "CONSOLE") return (
    <div className="min-h-screen bg-[#0F111A] text-white px-4 py-6 font-mono">
      <div className="flex justify-between items-center mb-4">
        <p className="text-[#8B9BB4]">TARGET :: {query} · {sport.toUpperCase()}</p>
        <button onClick={() => { stop(); setPhase("IDLE"); }} className="border border-red-400 text-red-400 px-4 py-1 rounded">✕ CANCEL</button>
      </div>
      <div className="flex items-center gap-3 mb-2">
        <span className="text-[#00E5FF]">[{String(state?.stage || "BOOT").toUpperCase()}]</span>
        <div className="flex-1 h-2 bg-[#1E2330] rounded"><div className="h-2 bg-[#00E5FF] rounded transition-all" style={{ width: `${Math.round((state?.progress || 0) * 100)}%` }} /></div>
        <span className="text-[#8B9BB4]">{Math.round((state?.progress || 0) * 100)}%</span>
      </div>
      <div ref={boxRef} className="h-[60vh] overflow-y-auto bg-[#0B0D12] border border-[#1E2330] rounded p-3 text-sm space-y-1">
        {(state?.lines || []).map((l: any, i: number) => (
          <p key={i}><span style={{ color: AGENT_COLORS[l.agent] || "#94A3B8" }}>[{l.agent}]</span> {l.text}</p>
        ))}
      </div>
      <p className="mt-3 text-[#8B9BB4] italic">swarm airborne — live agent feed…</p>
    </div>
  );

  if (!result || (result.fixtures || []).length === 0) return (
    <div className="min-h-screen bg-[#0F111A] text-white flex items-center justify-center px-4">
      <div className="border border-red-400 rounded-xl p-8 max-w-xl text-center">
        <h2 className="text-red-400 font-bold font-mono text-xl mb-3">NO MARKETS DETECTED</h2>
        <p className="text-[#8B9BB4] mb-4">{result?.error_message || "Honest empty result."}</p>
        {(result?.available_today || []).length > 0 && (
          <div className="text-left text-sm text-[#8B9BB4] mb-4">
            <p className="font-mono mb-1">Playing today (tap to hunt):</p>
            {(result.available_today || []).slice(0, 8).map((n: string) => (
              <button key={n} onClick={() => start(n)} className="block text-[#00E5FF] hover:underline">{n}</button>
            ))}
          </div>
        )}
        <button onClick={() => setPhase("IDLE")} className="bg-[#00E5FF] text-black font-bold px-6 py-2 rounded-full">EDIT QUERY</button>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen bg-[#0F111A] text-white px-4 py-6">
      <button onClick={() => setPhase("IDLE")} className="mb-4 text-[#00E5FF] font-mono">← NEW HUNT</button>
      {(result.fixtures || []).map((fx: any) => (
        <div key={fx.fixture_id} className="mb-8 border border-[#1E2330] rounded-xl p-4 bg-[#0B0D12]">
          <h2 className="text-xl font-bold">{fx.home} @ {fx.away}</h2>
          <p className="text-[#8B9BB4] font-mono text-sm mb-1">{fx.sport} · {fx.league} · {fx.kickoff_utc} · {fx.markets_scanned} market lines scanned</p>
          {fx.context_note && <p className="text-[#FACC15] text-sm mb-3">{fx.context_note}</p>}
          <table className="w-full text-sm font-mono">
            <thead><tr className="text-[#8B9BB4] text-left"><th className="py-1">SELECTION</th><th>MARKET</th><th>BOOK</th><th>FAIR</th><th>HIT %</th><th>EV %</th><th>KELLY</th></tr></thead>
            <tbody>
              {(fx.top_edges || []).map((e: any, i: number) => (
                <tr key={i} className="border-t border-[#1E2330]">
                  <td className="py-1 text-[#00E5FF]">{e.selection}</td>
                  <td className="text-[#8B9BB4]">{e.market}</td>
                  <td>{e.book_odds}</td><td>{e.fair_odds}</td>
                  <td className="text-[#2EE6A6] font-bold">{e.confidence_score}</td>
                  <td className={e.ev_percent > 0 ? "text-[#2EE6A6]" : "text-[#8B9BB4]"}>{e.ev_percent}</td>
                  <td>{e.kelly_stake_pct}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(fx.diagrams || []).map((d: string) => (<img key={d} src={d} alt="diagram" className="mt-4 rounded border border-[#1E2330] w-full max-w-2xl" />))}
        </div>
      ))}
    </div>
  );
}
