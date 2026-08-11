<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import Pill from "../components/Pill.svelte";
  import LearningPanel from "../components/LearningPanel.svelte";
  import { api, type PerformanceAnalytics, type Decision, type BacktestRun, type RMultipleSummary } from "../api";
  import { toastStore } from "../stores/toast.svelte";
  import { downloadCsv } from "../csv";

  let perf = $state<PerformanceAnalytics | null>(null);
  let decisions = $state<Decision[]>([]);
  let decisionSource = $state("");
  let decisionAction = $state("");
  let rmult = $state<RMultipleSummary | null>(null);

  async function loadAll() {
    const [p, d, r] = await Promise.all([
      api.performanceAnalytics(30).catch(() => null),
      api.decisions(300).catch(() => []),
      api.rMultiples(200).catch(() => null),
    ]);
    perf = p;
    decisions = d;
    rmult = r;
  }

  const rBuckets = $derived.by(() => {
    if (!rmult?.trades.length) return [];
    const edges = [-Infinity, -1, -0.5, 0, 0.5, 1, 2, Infinity];
    const labels = ["< -1R", "-1R to -0.5R", "-0.5R to 0R", "0R to 0.5R", "0.5R to 1R", "1R to 2R", "> 2R"];
    const counts = new Array(labels.length).fill(0);
    for (const t of rmult.trades) {
      for (let i = 0; i < edges.length - 1; i++) {
        if (t.r_multiple > edges[i] && t.r_multiple <= edges[i + 1]) { counts[i]++; break; }
      }
    }
    const max = Math.max(1, ...counts);
    return labels.map((label, i) => ({ label, count: counts[i], pct: (counts[i] / max) * 100 }));
  });

  const filteredDecisions = $derived(
    decisions
      .filter((d) => !decisionSource || d.source === decisionSource)
      .filter((d) => !decisionAction || d.action === decisionAction),
  );
  const decisionCounts = $derived.by(() => {
    const counts: Record<string, number> = {};
    for (const d of decisions) counts[d.action] = (counts[d.action] ?? 0) + 1;
    return counts;
  });

  function exportDecisionsCsv() {
    downloadCsv(
      "ai_decision_log",
      ["timestamp", "source", "action", "symbol", "reasoning"],
      filteredDecisions.map((d) => [d.created_at, d.source, d.action, d.symbol ?? "", d.reasoning ?? ""]),
    );
  }

  function exportRMultiplesCsv() {
    if (!rmult) return;
    downloadCsv(
      "r_multiples",
      ["symbol", "direction", "entry_price", "stop_loss", "exit_price", "qty", "realized_pnl", "pnl_pct", "r_multiple", "close_reason", "closed_at"],
      rmult.trades.map((t) => [t.symbol, t.direction, t.entry_price, t.stop_loss, t.exit_price, t.qty, t.realized_pnl, t.pnl_pct, t.r_multiple, t.close_reason, t.closed_at]),
    );
  }

  async function clearDecisions() {
    if (!confirm("Clear the entire AI decision log? This cannot be undone.")) return;
    try {
      await api.clearDecisions();
      toastStore.ok("Decision log cleared");
      await loadAll();
    } catch (e) {
      toastStore.err(`Clear failed: ${e}`);
    }
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 30_000);
    return () => clearInterval(poll);
  });

  // ── backtester ──────────────────────────────────────────────────────
  let btSymbols = $state("AAPL,SPY");
  let btStart = $state(new Date(Date.now() - 90 * 86400_000).toISOString().slice(0, 10));
  let btEnd = $state(new Date().toISOString().slice(0, 10));
  let btTradeMode = $state<"scalp" | "longer" | "all">("longer");
  let btRunning = $state(false);
  let btRuns = $state<Awaited<ReturnType<typeof api.backtestList>>["runs"]>([]);
  let btSelected = $state<BacktestRun | null>(null);
  let btPolling: number | undefined;

  async function loadRuns() {
    const res = await api.backtestList().catch(() => ({ runs: [] }));
    btRuns = res.runs;
  }

  async function startBacktest() {
    const symbols = btSymbols
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!symbols.length) {
      toastStore.err("Enter at least one symbol");
      return;
    }
    btRunning = true;
    try {
      const res = await api.backtestRun({
        symbols,
        start_date: btStart,
        end_date: btEnd,
        trade_mode: btTradeMode,
      });
      toastStore.ok(`Backtest started (${res.run_id.slice(0, 8)})`);
      await loadRuns();
      pollRun(res.run_id);
    } catch (e) {
      toastStore.err(`Backtest failed to start: ${e}`);
    } finally {
      btRunning = false;
    }
  }

  function pollRun(runId: string) {
    clearInterval(btPolling);
    btPolling = window.setInterval(async () => {
      const run = await api.backtestGet(runId).catch(() => null);
      if (!run) return;
      if (run.status !== "running") {
        clearInterval(btPolling);
        await loadRuns();
      }
      if (btSelected?.id === runId) btSelected = run;
    }, 2000);
  }

  async function viewRun(runId: string) {
    btSelected = await api.backtestGet(runId).catch(() => null);
  }

  $effect(() => {
    loadRuns();
    return () => clearInterval(btPolling);
  });

  const fmtPct = (n: number | null | undefined) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`);
</script>

<div class="page-head">
  <h1>Performance &amp; Learning</h1>
  <div class="sub">Analytics, the AI decision log, learning insights, and historical backtesting</div>
</div>

<div class="grid">
  <div class="span-8">
    <Panel title="AI Decision Log" meta="{filteredDecisions.length} of {decisions.length}">
      {#snippet children()}
        <div class="decision-toolbar">
          <div class="decision-stats">
            {#each Object.entries(decisionCounts) as [action, count] (action)}
              <Pill label="{action}: {count}" tone={action === "EXIT" ? "bad" : action === "HOLD" ? "good" : action === "TIGHTEN_STOP" ? "warm" : "neutral"} />
            {/each}
          </div>
          <div class="decision-filters">
            <select bind:value={decisionSource}>
              <option value="">All Sources</option>
              <option value="guardian">Guardian</option>
              <option value="positions">Positions</option>
              <option value="paper">Paper</option>
              <option value="signals">Signals</option>
            </select>
            <select bind:value={decisionAction}>
              <option value="">All Actions</option>
              <option value="EXIT">EXIT</option>
              <option value="HOLD">HOLD</option>
              <option value="TIGHTEN_STOP">TIGHTEN_STOP</option>
              <option value="APPROVED">APPROVED</option>
              <option value="REJECTED">REJECTED</option>
            </select>
            <button class="btn tiny outline" onclick={exportDecisionsCsv}>Export CSV</button>
            <button class="btn tiny outline" onclick={clearDecisions}>Clear</button>
          </div>
        </div>
        <div class="list">
          {#each filteredDecisions.slice(0, 30) as d (d.id)}
            <div class="drow">
              <Pill label={d.action} tone={d.action === "EXIT" ? "bad" : d.action === "HOLD" ? "good" : d.action === "TIGHTEN_STOP" ? "warm" : "neutral"} />
              <div class="drow-main">
                <div class="drow-title">{d.symbol ?? d.source}</div>
                <div class="drow-meta">{(d.reasoning ?? "").slice(0, 90)}</div>
              </div>
              <div class="drow-time num">{d.created_at?.slice(5, 16).replace("T", " ")}</div>
            </div>
          {:else}
            <div class="empty">No decisions logged yet</div>
          {/each}
        </div>
      {/snippet}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Performance Analytics" meta="{perf?.period_days ?? 30}d">
      <div class="stat-list">
        <div class="stat"><span>Sharpe Ratio</span><b class="num">{perf?.sharpe_ratio?.toFixed(2) ?? "—"}</b></div>
        <div class="stat"><span>Max Drawdown</span><b class="num">{perf ? fmtPct(perf.max_drawdown_pct) : "—"}</b></div>
        <div class="stat"><span>Trades Analyzed</span><b class="num">{perf?.trades_analyzed ?? "—"}</b></div>
      </div>
      {#if perf?.by_signal_source?.length}
        <div class="src-head">By signal source</div>
        {#each perf.by_signal_source as s (s.signal_source)}
          <div class="src-row">
            <span>{s.signal_source}</span>
            <span class="num">{s.total} trades &middot; {s.win_rate_pct?.toFixed(1)}% win</span>
          </div>
        {/each}
      {/if}
    </Panel>
  </div>

  <div class="span-6">
    <Panel title="R-Multiple Distribution" meta="{rmult?.count ?? 0} closed trades with a stop on record">
      {#snippet children()}
        {#if rmult && rmult.count}
          <button class="btn tiny outline r-export" onclick={exportRMultiplesCsv}>Export CSV</button>
          <div class="stat-list r-kpis">
            <div class="stat"><span>Avg R</span><b class="num {rmult.avg_r != null && rmult.avg_r >= 0 ? 'pl-up' : 'pl-down'}">{rmult.avg_r?.toFixed(2) ?? "—"}</b></div>
            <div class="stat"><span>Win Rate</span><b class="num">{rmult.win_rate_pct?.toFixed(1) ?? "—"}%</b></div>
            <div class="stat"><span>Avg Win R</span><b class="num pl-up">{rmult.avg_win_r?.toFixed(2) ?? "—"}</b></div>
            <div class="stat"><span>Avg Loss R</span><b class="num pl-down">{rmult.avg_loss_r?.toFixed(2) ?? "—"}</b></div>
            <div class="stat"><span>Best R</span><b class="num pl-up">{rmult.best_r?.toFixed(2) ?? "—"}</b></div>
            <div class="stat"><span>Worst R</span><b class="num pl-down">{rmult.worst_r?.toFixed(2) ?? "—"}</b></div>
          </div>
          <div class="r-hist">
            {#each rBuckets as b (b.label)}
              <div class="r-hist-row">
                <span class="r-hist-label">{b.label}</span>
                <div class="r-hist-track"><div class="r-hist-fill" style="width:{b.pct}%"></div></div>
                <span class="num r-hist-count">{b.count}</span>
              </div>
            {/each}
          </div>
          {#if rmult.skipped}
            <p class="r-skipped">{rmult.skipped} closed trade{rmult.skipped > 1 ? "s" : ""} skipped — no stop recorded at open.</p>
          {/if}
        {:else}
          <div class="empty">No closed trades with a recorded stop yet</div>
        {/if}
      {/snippet}
    </Panel>
  </div>


  <div class="span-6">
    <Panel title="Backtester" meta="deterministic TA-fallback pipeline, no LLM">
      {#snippet children()}
        <div class="bt-form">
          <div class="field">
            <label for="bt-symbols">Symbols (comma-separated, max 10)</label>
            <input id="bt-symbols" bind:value={btSymbols} placeholder="AAPL,SPY,NVDA" />
          </div>
          <div class="field">
            <label for="bt-start">Start</label>
            <input id="bt-start" type="date" bind:value={btStart} />
          </div>
          <div class="field">
            <label for="bt-end">End</label>
            <input id="bt-end" type="date" bind:value={btEnd} />
          </div>
          <div class="field">
            <label for="bt-mode">Trade Mode</label>
            <select id="bt-mode" bind:value={btTradeMode}>
              <option value="scalp">Scalp</option>
              <option value="longer">Longer</option>
              <option value="all">All</option>
            </select>
          </div>
          <button class="btn primary" onclick={startBacktest} disabled={btRunning}>
            {btRunning ? "Starting…" : "Run Backtest"}
          </button>
        </div>

        <div class="bt-body">
          <div class="bt-runs">
            <div class="bt-runs-head">Recent runs</div>
            {#each btRuns as r (r.id)}
              <button class="bt-run-row" class:on={btSelected?.id === r.id} onclick={() => viewRun(r.id)}>
                <span class="bt-run-sym">{r.symbols.join(", ")}</span>
                <Pill
                  label={r.status}
                  tone={r.status === "completed" ? "good" : r.status === "failed" ? "bad" : "warm"}
                />
              </button>
            {:else}
              <div class="empty">No backtests run yet</div>
            {/each}
          </div>
          <div class="bt-result">
            {#if btSelected?.status === "running"}
              <div class="empty">Running…</div>
            {:else if btSelected?.status === "failed"}
              <div class="empty err">Failed: {btSelected.error}</div>
            {:else if btSelected?.result}
              <div class="bt-stats">
                <div class="bt-stat"><span>Win Rate</span><b class="num">{btSelected.result.win_rate_pct.toFixed(1)}%</b></div>
                <div class="bt-stat"><span>Total Return</span><b class="num">{fmtPct(btSelected.result.total_return_pct)}</b></div>
                <div class="bt-stat"><span>Max Drawdown</span><b class="num">{fmtPct(btSelected.result.max_drawdown?.max_drawdown_pct)}</b></div>
                <div class="bt-stat"><span>Sharpe</span><b class="num">{btSelected.result.sharpe_ratio?.toFixed(2) ?? "—"}</b></div>
                <div class="bt-stat"><span>Signals</span><b class="num">{btSelected.result.total_signals} ({btSelected.result.decided} decided)</b></div>
              </div>
              {#if btSelected.result.date_range_clamped}
                <div class="bt-note">Date range was clamped to what's actually backfillable for the chosen timeframes.</div>
              {/if}
              {#if btSelected.result.symbols_skipped?.length}
                <div class="bt-note">
                  Skipped: {btSelected.result.symbols_skipped.map((s) => `${s.symbol} (${s.reason})`).join(", ")}
                </div>
              {/if}
            {:else}
              <div class="empty">Select a run to see results</div>
            {/if}
          </div>
        </div>
      {/snippet}
    </Panel>
  </div>

  <div class="span-12">
    <div class="section-divider">Learning Engine</div>
    <LearningPanel />
  </div>
</div>

<style>
  .page-head {
    margin-bottom: 16px;
  }
  .page-head h1 {
    font-size: 19px;
    margin: 0 0 4px;
    font-weight: 650;
  }
  .sub {
    font-size: 12px;
    color: var(--ink-faint);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
  }
  .span-4 {
    grid-column: span 4;
  }
  .span-6 {
    grid-column: span 6;
  }
  .span-8 {
    grid-column: span 8;
  }
  .span-12 {
    grid-column: span 12;
  }

  .stat-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .stat {
    display: flex;
    justify-content: space-between;
    font-size: 12.5px;
    color: var(--ink-dim);
  }
  .src-head {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    margin-bottom: 6px;
  }
  .src-row {
    display: flex;
    justify-content: space-between;
    font-size: 11.5px;
    color: var(--ink-dim);
    padding: 4px 0;
  }

  .decision-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .decision-stats {
    display: flex;
    gap: 5px;
    flex-wrap: wrap;
  }
  .decision-filters {
    display: flex;
    gap: 6px;
  }
  .decision-filters select {
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 4px 7px;
    font-size: 11px;
  }
  .btn.tiny.outline {
    background: transparent;
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 4px 9px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
  }

  .list {
    display: flex;
    flex-direction: column;
    max-height: 340px;
    overflow-y: auto;
  }
  .drow {
    display: flex;
    gap: 8px;
    padding: 7px 0;
    border-bottom: 1px solid var(--line);
    align-items: flex-start;
  }
  .drow:last-child {
    border-bottom: none;
  }
  .drow-main {
    flex: 1;
    min-width: 0;
  }
  .drow-title {
    font-size: 12px;
    font-weight: 600;
  }
  .drow-meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .drow-time {
    font-size: 10px;
    color: var(--ink-faint);
    flex: none;
  }

  .section-divider {
    font-size: 13px;
    font-weight: 700;
    color: var(--ink);
    margin-bottom: 10px;
    padding-top: 4px;
  }

  .bt-form {
    display: grid;
    grid-template-columns: 2fr 1fr 1fr 1fr auto;
    gap: 10px;
    align-items: end;
    margin-bottom: 16px;
  }
  .field label {
    display: block;
    font-size: 11px;
    color: var(--ink-dim);
    margin-bottom: 5px;
  }
  input,
  select {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 7px 9px;
    font-size: 12.5px;
  }
  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 8px 14px;
    border-radius: 7px;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }
  .btn.primary {
    background: rgba(124, 154, 255, 0.15);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .bt-body {
    display: grid;
    grid-template-columns: 260px 1fr;
    gap: 16px;
    border-top: 1px solid var(--line);
    padding-top: 14px;
  }
  .bt-runs-head {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    margin-bottom: 8px;
  }
  .bt-run-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    background: none;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 7px 9px;
    margin-bottom: 6px;
    color: var(--ink-dim);
    font-size: 11.5px;
    cursor: pointer;
  }
  .bt-run-row.on {
    border-color: var(--accent);
    color: var(--ink);
  }
  .bt-run-sym {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 140px;
  }
  .bt-stats {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
  }
  .bt-stat {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .bt-stat span {
    font-size: 10px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .bt-stat b {
    font-size: 16px;
  }
  .bt-note {
    margin-top: 12px;
    font-size: 11px;
    color: var(--warm);
    background: rgba(255, 180, 84, 0.08);
    border: 1px solid rgba(255, 180, 84, 0.25);
    border-radius: 6px;
    padding: 8px 10px;
  }
  .empty {
    padding: 20px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }
  .empty.err {
    color: var(--bad);
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

  .r-export {
    float: right;
    margin-bottom: 10px;
  }
  .r-kpis {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 10px;
    margin-bottom: 16px;
  }
  .r-kpis .stat {
    flex-direction: column;
    gap: 4px;
    text-align: center;
    background: var(--surface-raised);
    border-radius: 8px;
    padding: 8px;
  }
  .r-kpis .stat span {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
  }
  .r-kpis .stat b {
    font-size: 14px;
  }
  .r-hist {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .r-hist-row {
    display: grid;
    grid-template-columns: 120px 1fr 30px;
    align-items: center;
    gap: 8px;
    font-size: 11px;
  }
  .r-hist-label {
    color: var(--ink-faint);
  }
  .r-hist-track {
    height: 8px;
    border-radius: 4px;
    background: var(--surface-raised);
    overflow: hidden;
  }
  .r-hist-fill {
    height: 100%;
    background: var(--accent);
  }
  .r-hist-count {
    text-align: right;
  }
  .r-skipped {
    margin: 12px 0 0;
    font-size: 11px;
    color: var(--ink-faint);
  }

  @media (max-width: 1180px) {
    .span-4,
    .span-6,
    .span-8 {
      grid-column: span 12;
    }
    .bt-form {
      grid-template-columns: 1fr 1fr;
    }
    .bt-body {
      grid-template-columns: 1fr;
    }
  }
</style>
