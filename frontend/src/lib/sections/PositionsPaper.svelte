<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import Pill from "../components/Pill.svelte";
  import { api, type PositionWithSignal, type PaperSummary, type AutoSimSummary, type SlippageSummary } from "../api";
  import { toastStore } from "../stores/toast.svelte";

  type Account = "live" | "paper" | "autosim";
  let account = $state<Account>("live");

  let live = $state<{ positions: PositionWithSignal[]; account: { equity: number; cash: number; unrealized_pl: number } } | null>(null);
  let paper = $state<PaperSummary | null>(null);
  let autosim = $state<AutoSimSummary | null>(null);
  let slippage = $state<SlippageSummary | null>(null);
  let busy = $state<Set<string>>(new Set());
  let expandedLive = $state<Set<string>>(new Set());
  let showManualOpen = $state(false);
  let manualOpen = $state({ symbol: "", asset_class: "Equity", paper_direction: "Long", entry_price: "", target_price: "", stop_loss: "" });

  function setBusy(key: string, v: boolean) {
    const next = new Set(busy);
    v ? next.add(key) : next.delete(key);
    busy = next;
  }

  function toggleExpand(symbol: string) {
    const next = new Set(expandedLive);
    next.has(symbol) ? next.delete(symbol) : next.add(symbol);
    expandedLive = next;
  }

  async function loadAll() {
    const [l, p, a, s] = await Promise.all([
      api.positionsWithSignals().catch(() => null),
      api.paperSummary().catch(() => null),
      api.autoSimSummary().catch(() => null),
      api.slippageSummary(50).catch(() => null),
    ]);
    live = l;
    paper = p;
    autosim = a;
    slippage = s;
  }

  async function openManualPosition() {
    if (!manualOpen.symbol.trim()) {
      toastStore.err("Enter a symbol");
      return;
    }
    try {
      await api.paperOpen({
        symbol: manualOpen.symbol.trim().toUpperCase(),
        asset_class: manualOpen.asset_class,
        paper_direction: manualOpen.paper_direction,
        entry_price: manualOpen.entry_price ? Number(manualOpen.entry_price) : undefined,
        target_price: manualOpen.target_price ? Number(manualOpen.target_price) : undefined,
        stop_loss: manualOpen.stop_loss ? Number(manualOpen.stop_loss) : undefined,
      });
      toastStore.ok(`${manualOpen.symbol}: paper position opened`);
      manualOpen = { symbol: "", asset_class: "Equity", paper_direction: "Long", entry_price: "", target_price: "", stop_loss: "" };
      showManualOpen = false;
      await loadAll();
    } catch (e) {
      toastStore.err(`Open failed: ${e}`);
    }
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 20_000);
    return () => clearInterval(poll);
  });

  async function closeLive(symbol: string) {
    if (!confirm(`Close live position ${symbol} at market?`)) return;
    setBusy(symbol, true);
    try {
      await api.closeLivePosition(symbol);
      toastStore.ok(`${symbol}: close submitted`);
      await loadAll();
    } catch (e) {
      toastStore.err(`${symbol}: close failed — ${e}`);
    } finally {
      setBusy(symbol, false);
    }
  }

  async function closePaper(id: string, symbol: string) {
    setBusy(id, true);
    try {
      await api.paperClose(id);
      toastStore.ok(`${symbol}: paper position closed`);
      await loadAll();
    } catch (e) {
      toastStore.err(`${symbol}: close failed — ${e}`);
    } finally {
      setBusy(id, false);
    }
  }

  async function resetPaper() {
    if (!confirm("Reset the paper portfolio to $100,000? This wipes all paper positions and trade history.")) return;
    try {
      await api.paperReset();
      toastStore.ok("Paper portfolio reset to $100,000");
      await loadAll();
    } catch (e) {
      toastStore.err(`Reset failed: ${e}`);
    }
  }

  const fmtUsd = (n: number) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const fmtPct = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
</script>

<div class="page-head">
  <h1>Positions &amp; Paper</h1>
  <div class="sub">Live Alpaca, virtual paper, and the auto-sim ledger — one workspace, three accounts</div>
</div>

<div class="tabs">
  <button class="tab" class:on={account === "live"} onclick={() => (account = "live")}>Live</button>
  <button class="tab" class:on={account === "paper"} onclick={() => (account = "paper")}>Paper</button>
  <button class="tab" class:on={account === "autosim"} onclick={() => (account = "autosim")}>Auto Sim</button>
</div>

{#if account === "live"}
  <div class="kpis">
    <KpiTile label="Equity" value={live ? fmtUsd(live.account.equity) : "—"} />
    <KpiTile label="Cash" value={live ? fmtUsd(live.account.cash) : "—"} />
    <KpiTile
      label="Unrealized P&L"
      value={live ? fmtUsd(live.account.unrealized_pl) : "—"}
      trend={live ? (live.account.unrealized_pl >= 0 ? "up" : "down") : "neutral"}
    />
    <KpiTile label="Open Positions" value={String(live?.positions.length ?? "—")} />
  </div>
  <div class="stack">
  <Panel title="Live Positions" meta="Alpaca — click a row for signal context">
    {#if live && live.positions.length}
      <table class="tbl">
        <thead>
          <tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&amp;L</th><th></th></tr>
        </thead>
        <tbody>
          {#each live.positions as p (p.symbol)}
            <tr class="expandable" onclick={() => toggleExpand(p.symbol)}>
              <td class="sym">{expandedLive.has(p.symbol) ? "▾" : "▸"} {p.symbol}</td>
              <td><Pill label={p.side} tone={p.side === "long" ? "good" : "bad"} /></td>
              <td class="num">{p.qty}</td>
              <td class="num">{p.avg_entry_price}</td>
              <td class="num">{p.current_price}</td>
              <td class="num {p.unrealized_plpc >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(p.unrealized_plpc)}</td>
              <td>
                <button class="btn tiny bad" disabled={busy.has(p.symbol)} onclick={(e) => { e.stopPropagation(); closeLive(p.symbol); }}>Close</button>
              </td>
            </tr>
            {#if expandedLive.has(p.symbol)}
              <tr class="expand-row">
                <td colspan="7">
                  <div class="sig-context">
                    <div class="sc-row">
                      <span>Direction</span><b>{p.signal.direction}</b>
                      <span>Entry</span><b class="num">{p.signal.entry_price}</b>
                      <span>Target</span><b class="num pl-up">{p.signal.target_price ?? "—"}</b>
                      <span>Stop</span><b class="num pl-down">{p.signal.stop_loss ?? "—"}</b>
                      {#if p.signal.rr != null}<span>R:R</span><b class="num">{p.signal.rr}:1</b>{/if}
                      {#if p.signal.progress_pct != null}<span>Progress</span><b class="num">{p.signal.progress_pct}%</b>{/if}
                    </div>
                    {#if p.signal.reasoning}<p class="sc-reasoning">{p.signal.reasoning}</p>{/if}
                    {#if p.signal.key_risks}<p class="sc-risks"><b>Key risks:</b> {p.signal.key_risks}</p>{/if}
                  </div>
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    {:else}
      <div class="empty">No open live positions{live ? "" : " — Alpaca unreachable"}</div>
    {/if}
  </Panel>

  <Panel title="Execution Slippage" meta={slippage?.count ? `${slippage.count} fills` : "no data"}>
    {#if slippage && slippage.count}
      <div class="slip-stats">
        <div class="slip-stat"><span>Avg</span><b class="num">{slippage.avg_slippage_pct?.toFixed(3)}%</b></div>
        <div class="slip-stat"><span>Median</span><b class="num">{slippage.median_slippage_pct?.toFixed(3)}%</b></div>
        <div class="slip-stat"><span>Worst</span><b class="num pl-down">{slippage.worst_slippage_pct?.toFixed(3)}%</b></div>
      </div>
      <table class="tbl">
        <thead><tr><th>Symbol</th><th>Intended</th><th>Filled</th><th>Slippage</th></tr></thead>
        <tbody>
          {#each slippage.trades.slice(0, 10) as t (t.fill_recorded_at + t.symbol)}
            <tr>
              <td class="sym">{t.symbol}</td>
              <td class="num">{t.entry_price}</td>
              <td class="num">{t.actual_fill_price}</td>
              <td class="num {Math.abs(t.slippage_pct) < 0.1 ? '' : 'pl-down'}">{t.slippage_pct >= 0 ? "+" : ""}{t.slippage_pct.toFixed(3)}%</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <div class="empty">No live fills recorded yet — slippage is tracked the first time manage_positions observes a filled live position.</div>
    {/if}
  </Panel>
  </div>
{:else if account === "paper"}
  <div class="kpis">
    <KpiTile label="Virtual Equity" value={paper ? fmtUsd(paper.portfolio.equity) : "—"} />
    <KpiTile label="Total Return" value={paper ? fmtPct(paper.portfolio.total_return_pct) : "—"} trend={paper && paper.portfolio.total_return_pct >= 0 ? "up" : "down"} />
    <KpiTile label="Win Rate" value={paper ? `${paper.portfolio.win_rate}%` : "—"} />
    <KpiTile label="Margin In Use" value={paper ? fmtUsd(paper.portfolio.margin_in_use) : "—"} />
  </div>
  <div class="stack">
  <Panel title="Paper Positions" meta="{paper?.positions.length ?? 0} open">
    {#snippet children()}
      <div class="panel-actions">
        <button class="btn small outline" onclick={() => (showManualOpen = !showManualOpen)}>
          {showManualOpen ? "Cancel" : "+ Open Manual Position"}
        </button>
        <button class="btn small outline" onclick={resetPaper}>Reset to $100k</button>
      </div>

      {#if showManualOpen}
        <div class="manual-form">
          <input placeholder="Symbol (AAPL, BTC/USD...)" bind:value={manualOpen.symbol} />
          <select bind:value={manualOpen.asset_class}>
            <option value="Equity">Equity</option>
            <option value="Crypto">Crypto</option>
            <option value="Futures">Futures</option>
            <option value="Forex">Forex</option>
          </select>
          <select bind:value={manualOpen.paper_direction}>
            <option value="Long">Long</option>
            <option value="Long_Leveraged">Long 2x</option>
            <option value="Long_5x">Long 5x</option>
            <option value="Short">Short</option>
            <option value="Short_Leveraged">Short 2x</option>
            <option value="Short_5x">Short 5x</option>
          </select>
          <input placeholder="Entry (blank = market)" bind:value={manualOpen.entry_price} />
          <input placeholder="Target" bind:value={manualOpen.target_price} />
          <input placeholder="Stop" bind:value={manualOpen.stop_loss} />
          <button class="btn small primary" onclick={openManualPosition}>Open</button>
        </div>
      {/if}

      {#if paper && paper.positions.length}
        <table class="tbl">
          <thead>
            <tr><th>Symbol</th><th>Direction</th><th>Lev</th><th>Entry</th><th>Current</th><th>P&amp;L</th><th></th></tr>
          </thead>
          <tbody>
            {#each paper.positions as p (p.id)}
              <tr>
                <td class="sym">{p.symbol}</td>
                <td><Pill label={p.direction} tone={p.side === "long" ? "good" : "bad"} /></td>
                <td class="num">{p.leverage}x</td>
                <td class="num">{p.entry_price}</td>
                <td class="num">{p.current_price}</td>
                <td class="num {p.unrealized_pct >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(p.unrealized_pct)}</td>
                <td><button class="btn tiny bad" disabled={busy.has(p.id)} onclick={() => closePaper(p.id, p.symbol)}>Close</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <div class="empty">No open paper positions</div>
      {/if}
    {/snippet}
  </Panel>
  <Panel title="Recent Paper Trades" meta="last {paper?.trades.length ?? 0}">
    {#if paper && paper.trades.length}
      <table class="tbl">
        <thead><tr><th>Symbol</th><th>Direction</th><th>P&amp;L</th><th>Reason</th><th>Closed</th></tr></thead>
        <tbody>
          {#each paper.trades.slice(0, 15) as t (t.id)}
            <tr>
              <td class="sym">{t.symbol}</td>
              <td>{t.direction}</td>
              <td class="num {t.realized_pnl >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(t.realized_pnl)} ({fmtPct(t.pnl_pct)})</td>
              <td>{t.close_reason}</td>
              <td class="num">{t.closed_at?.slice(0, 16).replace("T", " ")}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <div class="empty">No closed trades yet</div>
    {/if}
  </Panel>
  </div>
{:else if account === "autosim"}
  <div class="kpis">
    <KpiTile label="Equity" value={autosim ? fmtUsd(autosim.summary.equity) : "—"} />
    <KpiTile label="Realized P&L" value={autosim ? fmtUsd(autosim.summary.realized_pnl) : "—"} trend={autosim && autosim.summary.realized_pnl >= 0 ? "up" : "down"} />
    <KpiTile label="Win Rate" value={autosim ? `${autosim.summary.win_rate}%` : "—"} />
    <KpiTile label="Total Trades" value={String(autosim?.summary.total_trades ?? "—")} />
  </div>
  <div class="stack">
  <Panel title="Auto Sim" meta="follows every eligible signal automatically — paper-only, no broker">
    <p class="note">
      Auto Sim is a separate always-on paper ledger that opens a $1,000-notional virtual position for every eligible
      signal automatically, independent of manual paper trading above. {autosim?.summary.wins ?? 0} wins /
      {autosim?.summary.losses ?? 0} losses.
    </p>
  </Panel>
  </div>
{/if}

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
  .tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 14px;
    border-bottom: 1px solid var(--line);
  }
  .tab {
    background: none;
    border: none;
    color: var(--ink-faint);
    padding: 8px 14px;
    font-size: 12.5px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
  }
  .tab.on {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .kpis {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 14px;
  }
  .panel-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-bottom: 10px;
  }

  .manual-form {
    display: grid;
    grid-template-columns: 1.5fr 1fr 1fr 1fr 1fr 1fr auto;
    gap: 8px;
    margin-bottom: 14px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--line);
  }
  .manual-form input,
  .manual-form select {
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 6px 8px;
    font-size: 11.5px;
    min-width: 0;
  }
  .btn.primary {
    background: rgba(124, 154, 255, 0.15);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }

  tr.expandable {
    cursor: pointer;
  }
  tr.expandable:hover td {
    background: rgba(124, 154, 255, 0.04);
  }
  tr.expand-row td {
    padding: 0;
    border-bottom: 1px solid var(--line);
  }
  .sig-context {
    background: rgba(124, 154, 255, 0.03);
    padding: 10px 14px;
  }
  .sc-row {
    display: grid;
    grid-template-columns: repeat(6, auto);
    gap: 4px 10px;
    font-size: 11px;
    margin-bottom: 8px;
  }
  .sc-row span {
    color: var(--ink-faint);
  }
  .sc-reasoning,
  .sc-risks {
    font-size: 11.5px;
    color: var(--ink-dim);
    line-height: 1.5;
    margin: 4px 0;
  }

  .slip-stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    margin-bottom: 12px;
  }
  .slip-stat {
    text-align: center;
    background: var(--surface-raised);
    border-radius: 8px;
    padding: 8px;
  }
  .slip-stat span {
    display: block;
    font-size: 9.5px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 3px;
  }
  .slip-stat b {
    font-size: 14px;
  }
  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 7px 11px;
    border-radius: 7px;
    font-size: 12px;
    cursor: pointer;
  }
  .btn.small {
    padding: 6px 10px;
  }
  .btn.outline {
    background: transparent;
  }
  .btn.tiny {
    padding: 4px 9px;
    font-size: 11px;
  }
  .btn.tiny.bad {
    border-color: var(--bad);
    color: var(--bad);
    background: transparent;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  table.tbl {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  table.tbl th {
    text-align: left;
    font-size: 9.5px;
    letter-spacing: 0.08em;
    color: var(--ink-faint);
    font-weight: 600;
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
    text-transform: uppercase;
  }
  table.tbl td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
  }
  table.tbl tr:last-child td {
    border-bottom: none;
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
    padding: 24px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }
  .note {
    font-size: 12.5px;
    color: var(--ink-dim);
    line-height: 1.6;
    margin: 0;
  }
  .stack {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  @media (max-width: 900px) {
    .kpis {
      grid-template-columns: repeat(2, 1fr);
    }
  }
</style>
