<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import Pill from "../components/Pill.svelte";
  import LearningPanel from "../components/LearningPanel.svelte";
  import { api, type PerformanceAnalytics, type Decision, type BacktestRun } from "../api";
  import { toastStore } from "../stores/toast.svelte";

  let perf = $state<PerformanceAnalytics | null>(null);
  let decisions = $state<Decision[]>([]);
  let decisionSource = $state("");
  let decisionAction = $state("");

  async function loadAll() {
    const [p, d] = await Promise.all([api.performanceAnalytics(30).catch(() => null), api.decisions(300).catch(() => [])]);
    perf = p;
    decisions = d;
  }

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

  <div class="span-12">
    <div class="section-divider">Learning Engine</div>
    <LearningPanel />
  </div>

  <div class="span-12">
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
    align-items: start;
  }
  .span-4 {
    grid-column: span 4;
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

  @media (max-width: 1180px) {
    .span-4 {
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
