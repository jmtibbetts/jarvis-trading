<script lang="ts">
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import {
    api,
    type CalibrationSummary,
    type LearningFullSummary,
    type ScoreVariantsReport,
    type SelectionBiasReport,
    type TradeOutcome,
    type SignalAccuracy,
    type PatternMemory,
    type RegimeStat,
    type Lesson,
  } from "../api";
  import { toastStore } from "../stores/toast.svelte";

  let mode = $state<"all" | "live" | "paper">("all");
  let summary = $state<LearningFullSummary | null>(null);
  let outcomes = $state<TradeOutcome[]>([]);
  let accuracy = $state<SignalAccuracy[]>([]);
  let patterns = $state<PatternMemory[]>([]);
  let regimes = $state<RegimeStat[]>([]);
  let lessons = $state<Lesson[]>([]);
  let symbolFilter = $state("");
  let outcomeFilter = $state("");
  let collapsedDays = $state<Set<string>>(new Set());
  let backfilling = $state(false);

  let calibration = $state<CalibrationSummary | null>(null);
  let gateExp = $state<any | null>(null);
  let variants = $state<ScoreVariantsReport | null>(null);
  let selBias = $state<SelectionBiasReport | null>(null);
  let promo = $state<any | null>(null);

  async function loadAll() {
    const [s, o, a, p, r, l, cal, ge, v, sb, pr] = await Promise.all([
      api.learningSummary(mode).catch(() => null),
      api.learningOutcomes(mode, 300).catch(() => []),
      api.learningAccuracy().catch(() => []),
      api.learningPatterns().catch(() => []),
      api.learningRegimes().catch(() => []),
      api.learningLessons(30).catch(() => []),
      api.calibration().catch(() => null),
      fetch("/api/gate-experiment").then(r => r.ok ? r.json() : null).catch(() => null),
      api.scoreVariants().catch(() => null),
      api.selectionBias().catch(() => null),
      fetch("/api/promotion/status").then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    summary = s;
    outcomes = o;
    accuracy = a;
    patterns = p;
    regimes = r;
    lessons = l;
    calibration = cal;
    gateExp = ge;
    variants = v;
    selBias = sb;
    promo = pr;
  }

  $effect(() => {
    mode;
    loadAll();
  });

  const symbols = $derived([...new Set(outcomes.map((o) => o.symbol))].sort());
  const filteredOutcomes = $derived(
    outcomes
      .filter((o) => !symbolFilter || o.symbol === symbolFilter)
      .filter((o) => !outcomeFilter || o.outcome === outcomeFilter),
  );
  const groupedByDay = $derived.by(() => {
    const groups = new Map<string, TradeOutcome[]>();
    for (const o of filteredOutcomes) {
      const day = (o.exited_at || "").slice(0, 10) || "unknown";
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day)!.push(o);
    }
    return [...groups.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
  });

  function toggleDay(day: string) {
    const next = new Set(collapsedDays);
    next.has(day) ? next.delete(day) : next.add(day);
    collapsedDays = next;
  }

  async function backfillPaper() {
    backfilling = true;
    try {
      const res = await api.learningBackfillPaper();
      toastStore.ok(`Imported ${res.imported ?? 0} paper trades into the learning engine`);
      await loadAll();
    } catch (e) {
      toastStore.err(`Backfill failed: ${e}`);
    } finally {
      backfilling = false;
    }
  }

  const fmtUsd = (n: number) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const fmtPct = (n: number | null | undefined) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`);
  const fmtMin = (m: number | null) => (m == null ? "—" : m < 60 ? `${Math.round(m)}m` : `${(m / 60).toFixed(1)}h`);
</script>

<div class="learn-head">
  <div class="mode-toggle">
    <button class="m-btn" class:on={mode === "all"} onclick={() => (mode = "all")}>All</button>
    <button class="m-btn" class:on={mode === "live"} onclick={() => (mode = "live")}>Live</button>
    <button class="m-btn" class:on={mode === "paper"} onclick={() => (mode = "paper")}>Paper</button>
  </div>
  <button class="btn small outline" disabled={backfilling} onclick={backfillPaper} title="Import all closed paper trades into the learning engine">
    {backfilling ? "Importing…" : "Backfill Paper"}
  </button>
</div>

{#if summary && summary.total > 0}
  <div class="kpis">
    <div class="kpi"><span>Total Trades</span><b>{summary.total}</b></div>
    <div class="kpi"><span>Win Rate</span><b class="pl-up">{(summary.win_rate * 100).toFixed(1)}%</b></div>
    <div class="kpi"><span>Avg P&amp;L</span><b class={summary.avg_pnl >= 0 ? "pl-up" : "pl-down"}>{fmtPct(summary.avg_pnl)}</b></div>
    <div class="kpi"><span>Total Realized</span><b class={summary.total_pnl_usd >= 0 ? "pl-up" : "pl-down"}>{fmtUsd(summary.total_pnl_usd)}</b></div>
    <div class="kpi"><span>Best Trade</span><b class="pl-up">{fmtPct(summary.best_trade)}</b></div>
    <div class="kpi"><span>Worst Trade</span><b class="pl-down">{fmtPct(summary.worst_trade)}</b></div>
    <div class="kpi"><span>Avg Hold</span><b>{fmtMin(summary.avg_hold_min)}</b></div>
    <div class="kpi"><span>W / L</span><b>{summary.wins} / {summary.losses}</b></div>
  </div>

  <div class="learn-grid">
    {#if gateExp && gateExp.overlap && gateExp.overlap.candidates_with_both_verdicts > 0}
    {@const L = gateExp.legacy}
    {@const V = gateExp.v8}
    <div class="grid-row">
      <Panel title="Gate Experiment — legacy vs v8"
             meta="{gateExp.overlap.candidates_with_both_verdicts.toLocaleString()} candidates carry both verdicts · judged by the same resolver">
        <div class="gate-grid num">
          <div class="gate-col-h"></div><div class="gate-col-h">selected</div><div class="gate-col-h">resolved</div><div class="gate-col-h">win %</div><div class="gate-col-h">avg P&L %</div><div class="gate-col-h">avg MFE R</div>
          <div class="gate-row-h" title="what the retired composite>=threshold gate WOULD have taken — records only, executes nothing">legacy (records)</div>
          <div>{L.selected.toLocaleString()}</div><div>{L.resolved.toLocaleString()}</div>
          <div>{L.win_rate ?? "—"}</div><div>{L.avg_pnl_pct ?? "—"}</div><div>{L.avg_mfe_r ?? "—"}</div>
          <div class="gate-row-h" title="validity + measured expectancy with robust lower bound — the arm that actually executes">v8 (executes)</div>
          <div>{V.selected.toLocaleString()}</div><div>{V.resolved.toLocaleString()}</div>
          <div>{V.win_rate ?? "—"}</div><div>{V.avg_pnl_pct ?? "—"}</div><div>{V.avg_mfe_r ?? "—"}</div>
        </div>
        <div class="gate-foot dim">
          agree on {gateExp.overlap.both_take.toLocaleString()} · legacy-only {gateExp.overlap.legacy_only.toLocaleString()} · v8-only {gateExp.overlap.v8_only.toLocaleString()}
          {#if gateExp.v8_decision_mix}
            · v8 mix: {Object.entries(gateExp.v8_decision_mix).map(([k, n]) => `${k} ${n}`).join(", ")}
          {/if}
        </div>
        <div class="gate-foot dim">{gateExp.note}</div>
      </Panel>
    </div>
  {/if}

  {#if calibration}
      <div class="span-7">
        <Panel title="Measured Calibration" meta="{calibration.sample.toLocaleString()} outcomes · rates are measured, not claimed">
          <div class="cal-cols">
            <div>
              <div class="cal-h">By timeframe</div>
              <table class="tbl">
                <thead><tr><th>TF</th><th>Win%</th><th>n</th></tr></thead>
                <tbody>
                  {#each calibration.by_timeframe as r (r.timeframe)}
                    <tr>
                      <td class="sym">{r.timeframe}</td>
                      <td class="num {r.win_rate >= 50 ? 'pl-up' : 'pl-down'}">{r.win_rate.toFixed(1)}%</td>
                      <td class="num dim">{Math.round(r.sample).toLocaleString()}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
            <div>
              <div class="cal-h">By score band <span class="dim">(inverted = the bug)</span></div>
              <table class="tbl">
                <thead><tr><th>Band</th><th>Win%</th><th>n</th></tr></thead>
                <tbody>
                  {#each calibration.by_score as r (r.band)}
                    <tr>
                      <td class="sym">{r.band}</td>
                      <td class="num {r.win_rate >= 50 ? 'pl-up' : 'pl-down'}">{r.win_rate.toFixed(1)}%</td>
                      <td class="num dim">{Math.round(r.sample).toLocaleString()}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
            <div>
              <div class="cal-h">By strategy</div>
              <table class="tbl">
                <thead><tr><th>Strategy</th><th>Win%</th><th>n</th></tr></thead>
                <tbody>
                  {#each calibration.by_strategy as r (r.strategy)}
                    <tr>
                      <td class="sym">{r.strategy}</td>
                      <td class="num {r.win_rate >= 50 ? 'pl-up' : 'pl-down'}">{r.win_rate.toFixed(1)}%</td>
                      <td class="num dim">{Math.round(r.sample).toLocaleString()}</td>
                    </tr>
                  {:else}
                    <tr><td colspan="3" class="empty">No classified outcomes yet</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </div>
        </Panel>
      </div>
    {/if}

    {#if variants}
      <div class="span-5">
        <Panel title="Shadow Score Variants" meta="gate {variants.gate} · {variants.schema}">
          <table class="tbl">
            <thead><tr><th>Variant</th><th>Selected</th><th>Win%</th><th>Avg P&amp;L</th><th>MFE</th><th>Stop-1st</th></tr></thead>
            <tbody>
              {#each Object.entries(variants.variants) as [name, v] (name)}
                <tr>
                  <td class="sym">{name === "A" ? "A · live" : name === "B" ? "B · inverted" : name === "C" ? "C · calibrated" : "MS · market-state"}</td>
                  <td class="num">{v.n.toLocaleString()}</td>
                  <td class="num">{v.win_rate != null ? v.win_rate.toFixed(1) + "%" : "—"}</td>
                  <td class="num {(v.avg_pnl_pct ?? 0) >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(v.avg_pnl_pct)}</td>
                  <td class="num">{v.avg_mfe_r != null ? v.avg_mfe_r.toFixed(2) + "R" : "—"}</td>
                  <td class="num">{v.stop_first_pct != null ? v.stop_first_pct.toFixed(0) + "%" : "—"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="cal-note">Retrospective over resolved outcomes. Shadow only — none of these gate real trades.</div>
        </Panel>
      </div>
    {/if}

    {#if selBias && selBias.by_verdict.length > 0}
      <div class="span-5">
        <Panel title="Selection Bias — Rejected vs Accepted" meta="counterfactually resolved candidates">
          <table class="tbl">
            <thead><tr><th>Verdict</th><th>n</th><th>Win%</th><th>Avg P&amp;L</th><th>MFE</th></tr></thead>
            <tbody>
              {#each selBias.by_verdict as r (r.verdict)}
                <tr>
                  <td class="sym">{r.verdict}</td>
                  <td class="num">{r.n.toLocaleString()}</td>
                  <td class="num">{r.win_rate?.toFixed(1)}%</td>
                  <td class="num {(r.avg_pnl_pct ?? 0) >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(r.avg_pnl_pct)}</td>
                  <td class="num">{r.avg_mfe_r != null ? r.avg_mfe_r.toFixed(2) + "R" : "—"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="cal-note">If rejected beats persisted, the filters discard winners — the question this table exists to answer.</div>
        </Panel>
      </div>
    {/if}

    {#if promo && promo.challengers}
      <div class="span-7">
        <Panel title="Promotion Framework — §4.3"
               meta="champion: {promo.champion?.variant} · {promo.resolved_universe?.toLocaleString?.() ?? promo.resolved_universe} resolved candidates · gate {promo.gate}">
          <table class="tbl">
            <thead><tr><th>Challenger</th><th>Verdict</th><th>OOS n</th><th>Sel freq</th><th>Net R (ch / champ)</th><th>Folds won</th><th>Blocking</th></tr></thead>
            <tbody>
              {#each Object.entries(promo.challengers) as [name, ev] (name)}
                {@const cs = ev.challenger_stats}
                {@const ps = ev.champion_stats}
                {@const wf = ev.criteria?.walk_forward}
                <tr>
                  <td class="sym">{name}</td>
                  <td>
                    <span class="promo-badge" class:ok={ev.verdict === "PROMOTE_ELIGIBLE"} class:warn={ev.verdict === "INSUFFICIENT_DATA"}>
                      {ev.verdict === "PROMOTE_ELIGIBLE" ? "ELIGIBLE" : ev.verdict === "INSUFFICIENT_DATA" ? "GATHERING" : "NOT ELIGIBLE"}
                    </span>
                  </td>
                  <td class="num">{ev.oos_universe?.toLocaleString?.() ?? "—"}</td>
                  <td class="num">{ev.criteria?.selection_frequency ? (ev.criteria.selection_frequency.challenger * 100).toFixed(1) + "%" : "—"}</td>
                  <td class="num">{cs?.mean_net_r != null && ps?.mean_net_r != null ? `${cs.mean_net_r.toFixed(2)} / ${ps.mean_net_r.toFixed(2)}` : "—"}</td>
                  <td class="num">{wf ? `${wf.folds_won}/${wf.valid_folds}` : "—"}</td>
                  <td class="dim promo-fail">{ev.verdict === "INSUFFICIENT_DATA" ? "needs span + sample" : (ev.failed ?? []).join(", ") || "none"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="cal-note">Chronological walk-forward on stored-at-birth scores only — a variant is never graded on the data that calibrated it. Promotion writes an immutable champion artifact; nothing rewires live scoring until Phase 8.</div>
        </Panel>
      </div>
    {/if}

    <div class="span-5">
      <Panel title="Signal Accuracy by Symbol" meta="{accuracy.length} symbols">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Sym</th><th>Trades</th><th>Win%</th><th>Avg P&amp;L</th><th>Best</th><th>Worst</th></tr></thead>
            <tbody>
              {#each accuracy as a (a.id)}
                <tr>
                  <td class="sym">{a.symbol}</td>
                  <td class="num">{a.total_trades}</td>
                  <td class="num">{(a.win_rate * 100).toFixed(0)}%</td>
                  <td class="num {a.avg_pnl_pct >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(a.avg_pnl_pct)}</td>
                  <td class="num pl-up">{fmtPct(a.best_pnl_pct)}</td>
                  <td class="num pl-down">{fmtPct(a.worst_pnl_pct)}</td>
                </tr>
              {:else}
                <tr><td colspan="6" class="empty">No accuracy data yet</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>

    <div class="span-7">
      <Panel title="Trade Outcome Log" meta="{filteredOutcomes.length} shown">
        {#snippet children()}
          <div class="log-filters">
            <select bind:value={symbolFilter}>
              <option value="">All Symbols</option>
              {#each symbols as s (s)}<option value={s}>{s}</option>{/each}
            </select>
            <select bind:value={outcomeFilter}>
              <option value="">All Outcomes</option>
              <option value="WIN">WIN</option>
              <option value="LOSS">LOSS</option>
              <option value="BREAKEVEN">BREAKEVEN</option>
            </select>
          </div>
          <div class="day-groups">
            {#each groupedByDay as [day, rows] (day)}
              <div class="day-group">
                <button class="day-head" onclick={() => toggleDay(day)}>
                  <span>{collapsedDays.has(day) ? "▸" : "▾"} {day}</span>
                  <span class="day-count">{rows.length} trades</span>
                </button>
                {#if !collapsedDays.has(day)}
                  <div class="day-rows">
                    {#each rows as o (o.id)}
                      <div class="out-row">
                        <Pill label={o.outcome} tone={o.outcome === "WIN" ? "good" : o.outcome === "LOSS" ? "bad" : "neutral"} />
                        <span class="out-sym">{o.symbol}</span>
                        <span class="out-dir">{o.direction}</span>
                        <span class="num {o.pnl_pct >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(o.pnl_pct)}</span>
                        <span class="out-reason">{o.exit_reason}</span>
                      </div>
                    {/each}
                  </div>
                {/if}
              </div>
            {:else}
              <div class="empty">No trades match these filters</div>
            {/each}
          </div>
        {/snippet}
      </Panel>
    </div>
  </div>

  <div class="learn-grid">
    <div class="span-4">
      <Panel title="Pattern Memory" meta="Tier 3">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Pattern</th><th>Count</th><th>Win%</th><th>Avg P&amp;L</th></tr></thead>
            <tbody>
              {#each patterns as p (p.id)}
                <tr>
                  <td>{p.pattern_desc}</td>
                  <td class="num">{p.total}</td>
                  <td class="num">{(p.win_rate * 100).toFixed(0)}%</td>
                  <td class="num {p.avg_pnl_pct >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(p.avg_pnl_pct)}</td>
                </tr>
              {:else}
                <tr><td colspan="4" class="empty">No patterns learned yet</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>

    <div class="span-4">
      <Panel title="Regime Performance" meta="Tier 4">
        <div class="tbl-wrap">
          <table class="tbl">
            <thead><tr><th>Regime</th><th>Trades</th><th>Win%</th><th>Avg P&amp;L</th></tr></thead>
            <tbody>
              {#each regimes as r (r.id)}
                <tr>
                  <td>{r.regime}</td>
                  <td class="num">{r.total}</td>
                  <td class="num">{(r.win_rate * 100).toFixed(0)}%</td>
                  <td class="num {r.avg_pnl_pct >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(r.avg_pnl_pct)}</td>
                </tr>
              {:else}
                <tr><td colspan="4" class="empty">No regime data yet</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>

    <div class="span-4">
      <Panel title="AI Lessons" meta="Tier 5">
        <div class="lessons-list">
          {#each lessons as l (l.id)}
            <div class="lesson-row">
              <div class="lesson-head">
                <Pill label={l.outcome} tone={l.outcome === "WIN" ? "good" : "bad"} />
                <span class="lesson-sym">{l.symbol}</span>
              </div>
              <p>{l.lesson}</p>
            </div>
          {:else}
            <div class="empty">No lessons recorded yet</div>
          {/each}
        </div>
      </Panel>
    </div>
  </div>
{:else}
  <div class="learn-empty">
    No closed trades in this mode yet.<br />
    <span>Switch between All / Live / Paper above, or wait for positions to close.</span>
  </div>
{/if}

<style>
  .learn-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
  }
  .mode-toggle {
    display: flex;
    gap: 4px;
    border: 1px solid var(--line-bright);
    border-radius: 8px;
    padding: 3px;
  }
  .m-btn {
    background: none;
    border: none;
    color: var(--ink-faint);
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .m-btn.on {
    background: var(--accent);
    color: var(--bg);
    font-weight: 700;
  }
  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 6px 12px;
    border-radius: 7px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .btn.outline {
    background: transparent;
    border-color: var(--accent);
    color: var(--accent);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .kpis {
    display: grid;
    grid-template-columns: repeat(8, 1fr);
    gap: 10px;
    margin-bottom: 14px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    text-align: center;
  }
  .kpi span {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
  }
  .kpi b {
    font-size: 15px;
    font-family: var(--mono);
  }

  .learn-grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
    margin-bottom: 14px;
    align-items: start;
  }
  .span-4 {
    grid-column: span 4;
  }
  .span-5 {
    grid-column: span 5;
  }
  .span-7 {
    grid-column: span 7;
  }

  .tbl-wrap {
    max-height: 340px;
    overflow-y: auto;
  }
  table.tbl {
    width: 100%;
    border-collapse: collapse;
    font-size: 11.5px;
  }
  table.tbl th {
    text-align: left;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
    padding: 7px 8px;
    border-bottom: 1px solid var(--line-strong);
    position: sticky;
    top: 0;
    background: var(--surface);
  }
  table.tbl td {
    padding: 7px 8px;
    border-bottom: 1px solid var(--line);
  }
  .sym {
    font-weight: 650;
  }
  .num {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
  }
  .pl-up {
    color: var(--good);
  }
  .pl-down {
    color: var(--bad);
  }
  .empty {
    padding: 16px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 11.5px;
  }

  .cal-cols {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 14px;
  }
  .cal-h {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-dim);
    margin-bottom: 6px;
  }
  .cal-note {
    margin-top: 8px;
    font-size: 10.5px;
    color: var(--ink-faint);
  }
  .dim {
    color: var(--ink-faint);
  }

  .log-filters {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  select {
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 5px 8px;
    font-size: 11.5px;
  }
  .day-groups {
    max-height: 340px;
    overflow-y: auto;
  }
  .day-group {
    margin-bottom: 6px;
  }
  .day-head {
    width: 100%;
    display: flex;
    justify-content: space-between;
    background: var(--surface-raised);
    border: none;
    border-radius: 6px;
    padding: 6px 10px;
    color: var(--ink-dim);
    font-size: 11.5px;
    cursor: pointer;
    font-family: var(--mono);
  }
  .day-count {
    color: var(--ink-faint);
  }
  .day-rows {
    padding: 4px 6px;
  }
  .out-row {
    display: grid;
    grid-template-columns: auto 70px 55px 60px 1fr;
    gap: 8px;
    align-items: center;
    padding: 5px 4px;
    font-size: 11px;
    border-bottom: 1px solid var(--line);
  }
  .out-row:last-child {
    border-bottom: none;
  }
  .out-sym {
    font-weight: 650;
  }
  .out-dir {
    color: var(--ink-faint);
  }
  .out-reason {
    color: var(--ink-faint);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .lessons-list {
    max-height: 340px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .lesson-row {
    border-bottom: 1px solid var(--line);
    padding-bottom: 8px;
  }
  .lesson-row:last-child {
    border-bottom: none;
  }
  .lesson-head {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 4px;
  }
  .lesson-sym {
    font-size: 11.5px;
    font-weight: 650;
  }
  .lesson-row p {
    margin: 0;
    font-size: 11px;
    color: var(--ink-dim);
    line-height: 1.5;
  }

  .learn-empty {
    padding: 60px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 13px;
  }
  .learn-empty span {
    font-size: 11.5px;
  }

  @media (max-width: 1180px) {
    .kpis {
      grid-template-columns: repeat(4, 1fr);
    }
    .span-4,
    .span-5,
    .span-7 {
      grid-column: span 12;
    }
  }

  .promo-badge { font-size: 10px; font-weight: 700; letter-spacing: 0.06em; padding: 2px 7px; border-radius: 3px; background: var(--bg-3, rgba(255,255,255,0.06)); color: var(--text-dim, #8b93a7); }
  .promo-badge.ok { background: rgba(52, 211, 153, 0.14); color: var(--up, #34d399); }
  .promo-badge.warn { background: rgba(250, 204, 21, 0.12); color: #facc15; }
  .promo-fail { font-size: 11px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gate-grid { display: grid; grid-template-columns: 1.4fr repeat(5, 1fr); gap: 6px 10px; padding: 4px 0; }
  .gate-col-h { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.55; }
  .gate-row-h { font-weight: 600; }
  .gate-foot { font-size: 11px; margin-top: 6px; }
</style>
