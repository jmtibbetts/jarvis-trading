<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import Pill from "../components/Pill.svelte";
  import { api, type PositionWithSignal, type PaperSummary, type AutoSimSummary, type SlippageSummary, type EarningsWatchlist } from "../api";
  import { toastStore } from "../stores/toast.svelte";
  import { downloadCsv } from "../csv";

  type Account = "live" | "paper" | "autosim";
  let account = $state<Account>("live");

  let live = $state<{ positions: PositionWithSignal[]; account: { equity: number; cash: number; unrealized_pl: number } } | null>(null);
  let paper = $state<PaperSummary | null>(null);
  let autosim = $state<AutoSimSummary | null>(null);
  let slippage = $state<SlippageSummary | null>(null);
  let earnings = $state<EarningsWatchlist | null>(null);
  let busy = $state<Set<string>>(new Set());
  let orders = $state<{ id: string; symbol: string; qty: number; side: string; status: string; type: string }[]>([]);
  let orderBusy = $state<Set<string>>(new Set());

  async function loadOrders() {
    orders = await api.alpacaOrders().catch(() => []);
  }

  async function cancelOrder(id: string, symbol: string) {
    const next = new Set(orderBusy); next.add(id); orderBusy = next;
    try {
      await api.cancelOrder(id);
      toastStore.ok(`${symbol}: order cancelled`);
      await loadOrders();
    } catch (e) {
      toastStore.err(`Cancel failed: ${e}`);
    } finally {
      const done = new Set(orderBusy); done.delete(id); orderBusy = done;
    }
  }

  async function cancelAllOrders() {
    if (!confirm(`Cancel all ${orders.length} open orders?`)) return;
    try {
      await api.cancelAllOrders();
      toastStore.ok("All open orders cancelled");
      await loadOrders();
    } catch (e) {
      toastStore.err(`Cancel all failed: ${e}`);
    }
  }
  let expandedLive = $state<Set<string>>(new Set());
  let showManualOpen = $state(false);
  let manualOpen = $state({ symbol: "", asset_class: "Equity", paper_direction: "Long", entry_price: "", target_price: "", stop_loss: "" });
  let sizer = $state({ equity: "", riskPct: "1", entry: "", stop: "" });

  const earningsRisk = (symbol: string) => earnings?.at_risk_symbols.includes(symbol.replace("/USD", "").toUpperCase()) ?? false;

  const sizerResult = $derived.by(() => {
    const equity = Number(sizer.equity) || 0;
    const riskPct = Number(sizer.riskPct) || 0;
    const entry = Number(sizer.entry) || 0;
    const stop = Number(sizer.stop) || 0;
    if (!equity || !riskPct || !entry || !stop || entry === stop) return null;
    const riskDollars = equity * (riskPct / 100);
    const perUnitRisk = Math.abs(entry - stop);
    const qty = riskDollars / perUnitRisk;
    const notional = qty * entry;
    return { riskDollars, qty, notional, notionalPctOfEquity: equity ? (notional / equity) * 100 : 0 };
  });

  const exposure = $derived.by(() => {
    type Row = { symbol: string; asset_class: string; direction: "long" | "short"; value: number };
    const rows: Row[] = [];
    if (live) for (const p of live.positions) rows.push({ symbol: p.symbol, asset_class: p.asset_class, direction: p.side === "short" ? "short" : "long", value: Math.abs(p.market_value) });
    if (paper) for (const p of paper.positions) rows.push({ symbol: p.symbol, asset_class: p.asset_class, direction: p.side === "short" ? "short" : "long", value: Math.abs(p.qty * p.current_price) });
    const totalEquity = (live?.account.equity ?? 0) + (paper?.portfolio.equity ?? 0);
    const byClass = new Map<string, number>();
    let long = 0, short = 0;
    for (const r of rows) {
      byClass.set(r.asset_class, (byClass.get(r.asset_class) ?? 0) + r.value);
      if (r.direction === "short") short += r.value; else long += r.value;
    }
    const topConcentration = rows.length ? rows.reduce((max, r) => (r.value > max.value ? r : max), rows[0]) : null;
    return {
      totalEquity,
      byClass: [...byClass.entries()]
        .map(([asset_class, value]) => ({ asset_class, value, pct: totalEquity ? (value / totalEquity) * 100 : 0 }))
        .sort((a, b) => b.value - a.value),
      long, short,
      longPct: totalEquity ? (long / totalEquity) * 100 : 0,
      shortPct: totalEquity ? (short / totalEquity) * 100 : 0,
      topConcentration: topConcentration ? { symbol: topConcentration.symbol, pct: totalEquity ? (topConcentration.value / totalEquity) * 100 : 0 } : null,
    };
  });

  const atRiskPositions = $derived.by(() => {
    type Row = { symbol: string; kind: "live" | "paper"; pctToStop: number };
    const rows: Row[] = [];
    if (live) for (const p of live.positions) {
      const stop = p.signal?.stop_loss;
      const entry = p.signal?.entry_price ?? p.avg_entry_price;
      if (stop == null || entry == null || entry === stop) continue;
      const totalDist = Math.abs(entry - stop);
      const remaining = Math.abs(p.current_price - stop);
      const pct = totalDist ? (remaining / totalDist) * 100 : 100;
      if (pct <= 25) rows.push({ symbol: p.symbol, kind: "live", pctToStop: Math.max(0, Math.round(pct)) });
    }
    if (paper) for (const p of paper.positions) {
      if (p.stop_loss == null || p.entry_price === p.stop_loss) continue;
      const totalDist = Math.abs(p.entry_price - p.stop_loss);
      const remaining = Math.abs(p.current_price - p.stop_loss);
      const pct = totalDist ? (remaining / totalDist) * 100 : 100;
      if (pct <= 25) rows.push({ symbol: p.symbol, kind: "paper", pctToStop: Math.max(0, Math.round(pct)) });
    }
    return rows.sort((a, b) => a.pctToStop - b.pctToStop);
  });

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

  let liveLoadFailed = $state(false);

  async function loadAll() {
    const [l, p, a, s, e] = await Promise.all([
      api.positionsWithSignals().catch(() => {
        liveLoadFailed = true;
        return null;
      }),
      api.paperSummary().catch(() => null),
      api.autoSimSummary().catch(() => null),
      api.slippageSummary(50).catch(() => null),
      api.earningsWatchlist().catch(() => null),
    ]);
    if (l) liveLoadFailed = false;
    // A failed fetch keeps the LAST GOOD data on screen instead of flashing
    // an empty "0 positions" state that reads as if everything was closed.
    live = l ?? live;
    paper = p;
    autosim = a;
    slippage = s;
    earnings = e;
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
    loadOrders();
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

  function exportPaperTradesCsv() {
    if (!paper) return;
    downloadCsv(
      "paper_trades",
      ["symbol", "direction", "realized_pnl", "pnl_pct", "close_reason", "closed_at"],
      paper.trades.map((t) => [t.symbol, t.direction, t.realized_pnl, t.pnl_pct, t.close_reason, t.closed_at]),
    );
  }

  let flattenBusy = $state(false);

  async function doFlatten(scope: "live" | "paper" | "all") {
    const label = scope === "all" ? "LIVE AND PAPER" : scope.toUpperCase();
    const typed = prompt(
      `This closes EVERY open ${label} position at market, cancels all working orders, ` +
      `and rejects all pending signals.

Type FLATTEN to confirm:`,
    );
    if (typed !== "FLATTEN") {
      if (typed !== null) toastStore.err("Flatten cancelled — confirmation text did not match");
      return;
    }
    flattenBusy = true;
    try {
      const res = await api.flattenTrading(scope);
      const bits: string[] = [];
      if (res.live) bits.push(`live: ${res.live.positions_closed} closed, ${res.live.orders_cancelled} orders cancelled`);
      if (res.paper) bits.push(`paper: ${res.paper.positions_closed} closed`);
      if (res.autosim) bits.push(`auto sim: ${res.autosim.closed} closed`);
      bits.push(`${res.signals_rejected} pending signals rejected`);
      toastStore.ok(`Flattened — ${bits.join(" · ")}`);
      const errs = [...(res.live?.errors ?? []), ...(res.paper?.errors ?? [])];
      if (errs.length) toastStore.err(`${errs.length} issue(s): ${errs[0]}`);
      await loadAll();
      await loadOrders();
    } catch (e) {
      toastStore.err(`Flatten failed: ${e}`);
    } finally {
      flattenBusy = false;
    }
  }

  async function resetAutoSim() {
    if (!confirm("Reset the Auto Sim account? This wipes its positions and history.")) return;
    try {
      await api.autosimReset();
      toastStore.ok("Auto Sim account reset");
      await loadAll();
    } catch (e) {
      toastStore.err(`Auto Sim reset failed: ${e}`);
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

  /** How much of the asset is held.
   *
   * A single decimal rule cannot serve this column: one book holds 0.1595 BTC
   * and 70,161 ARB at the same time, five orders of magnitude apart. Fixed
   * decimals would print "0.00" for the first or a wall of zeros for the
   * second, so precision follows magnitude and trailing zeros are trimmed —
   * the quantity should read as the number you would place an order for. */
  const fmtQty = (n: number | null | undefined) => {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    if (v === 0) return "0";
    const a = Math.abs(v);
    if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
    if (a >= 1) return trimZeros(v.toFixed(4));
    // Sub-unit holdings keep six significant figures, so a $0.0000045 coin
    // and a $95,000 one are both legible.
    return trimZeros(v.toPrecision(6));
  };
  const trimZeros = (s: string) =>
    s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;

  /** Money at work in a position, so a quantity has a scale beside it. */
  const fmtValue = (qty: number | null | undefined, px: number | null | undefined) => {
    const v = Number(qty) * Number(px);
    return Number.isFinite(v) ? fmtUsd(v) : "—";
  };

  /** A price, without the float noise.
   *
   * Raw values reach the UI as `245.77142750000002` and `0.7709252354048964`
   * — an artifact of dividing notional by price, not precision anyone
   * trades on. Significant figures rather than fixed decimals, because a
   * $0.0000045 coin and a $95,000 one share this column. */
  const fmtPrice = (n: number | null | undefined) => {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    if (v === 0) return "0";
    const a = Math.abs(v);
    if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (a >= 1) return trimZeros(v.toFixed(4));
    return trimZeros(v.toPrecision(6));
  };
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

{#if atRiskPositions.length}
  <div class="at-risk-banner">
    <b>⚠ {atRiskPositions.length} position{atRiskPositions.length > 1 ? "s" : ""} near stop:</b>
    {#each atRiskPositions as r (r.kind + r.symbol)}
      <span class="at-risk-chip">{r.symbol} <i>({r.kind})</i> {r.pctToStop}% of stop buffer left</span>
    {/each}
  </div>
{/if}

<div class="stack" style="margin-bottom:14px">
  <div class="two-col">
    <Panel title="Portfolio Risk" meta="{fmtUsd(exposure.totalEquity)} combined equity">
      {#snippet children()}
        {#if exposure.totalEquity}
          <div class="risk-split">
            <div class="risk-bar">
              <div class="risk-bar-long" style="width:{exposure.longPct}%"></div>
              <div class="risk-bar-short" style="width:{exposure.shortPct}%"></div>
            </div>
            <div class="risk-split-labels">
              <span class="pl-up">Long {fmtUsd(exposure.long)} ({exposure.longPct.toFixed(1)}%)</span>
              <span class="pl-down">Short {fmtUsd(exposure.short)} ({exposure.shortPct.toFixed(1)}%)</span>
            </div>
          </div>
          <div class="exposure-list">
            {#each exposure.byClass as c (c.asset_class)}
              <div class="exposure-row">
                <span>{c.asset_class}</span>
                <div class="exposure-track"><div class="exposure-fill" style="width:{Math.min(100, c.pct)}%"></div></div>
                <b class="num">{c.pct.toFixed(1)}%</b>
              </div>
            {/each}
          </div>
          {#if exposure.topConcentration && exposure.topConcentration.pct >= 20}
            <p class="risk-warning">⚠ {exposure.topConcentration.symbol} is {exposure.topConcentration.pct.toFixed(1)}% of combined equity — concentrated.</p>
          {/if}
        {:else}
          <div class="empty">No open exposure</div>
        {/if}
      {/snippet}
    </Panel>

    <Panel title="Position Sizing Calculator" meta="risk-based share count">
      {#snippet children()}
        <div class="sizer-form">
          <label>Account equity<input placeholder="100000" bind:value={sizer.equity} /></label>
          <label>Risk %<input placeholder="1" bind:value={sizer.riskPct} /></label>
          <label>Entry price<input placeholder="0.00" bind:value={sizer.entry} /></label>
          <label>Stop price<input placeholder="0.00" bind:value={sizer.stop} /></label>
        </div>
        {#if sizerResult}
          <div class="sizer-result">
            <div><span>Risk</span><b class="num">{fmtUsd(sizerResult.riskDollars)}</b></div>
            <div><span>Size</span><b class="num">{sizerResult.qty.toFixed(4)}</b></div>
            <div><span>Notional</span><b class="num">{fmtUsd(sizerResult.notional)}</b></div>
            <div><span>% of equity</span><b class="num {sizerResult.notionalPctOfEquity > 50 ? 'pl-down' : ''}">{sizerResult.notionalPctOfEquity.toFixed(1)}%</b></div>
          </div>
        {:else}
          <div class="empty small">Enter equity, risk %, entry, and stop</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
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
          <tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Value</th><th>Entry</th><th>Current</th><th>P&amp;L</th><th></th></tr>
        </thead>
        <tbody>
          {#each live.positions as p (p.symbol)}
            <tr class="expandable" onclick={() => toggleExpand(p.symbol)}>
              <td class="sym">{expandedLive.has(p.symbol) ? "▾" : "▸"} {p.symbol}{#if earningsRisk(p.symbol)}<span class="earnings-tag" title="Reports earnings this week">EARNINGS</span>{/if}</td>
              <td><Pill label={p.side} tone={p.side === "long" ? "good" : "bad"} /></td>
              <td class="num qty">{fmtQty(p.qty)}</td>
              <td class="num dim">{fmtUsd(p.market_value)}</td>
              <td class="num">{fmtPrice(p.avg_entry_price)}</td>
              <td class="num">{fmtPrice(p.current_price)}</td>
              <td class="num {p.unrealized_plpc >= 0 ? 'pl-up' : 'pl-down'}">
                {fmtUsd(p.unrealized_pl)}<em class="pl-pct">{fmtPct(p.unrealized_plpc)}</em>
              </td>
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
      {#if liveLoadFailed}
        <div class="empty err-note">Couldn't load live positions (server/Alpaca unreachable) — retrying automatically. This is a data-fetch failure, not zero positions.</div>
      {:else}
        <div class="empty">No open live positions</div>
      {/if}
    {/if}
  </Panel>


  <div class="two-col">
  <Panel title="Open Orders" meta="{orders.length} working at Alpaca">
    {#snippet children()}
      {#if orders.length}
        <button class="btn tiny outline" style="margin-bottom:8px" onclick={cancelAllOrders}>Cancel All</button>
        <table class="tbl">
          <thead><tr><th>Sym</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {#each orders as o (o.id)}
              <tr>
                <td class="sym">{o.symbol}</td>
                <td class={o.side === "buy" ? "pl-up" : "pl-down"}>{o.side}</td>
                <td class="num">{o.qty}</td>
                <td>{o.type}</td>
                <td>{o.status}</td>
                <td><button class="btn tiny ghost" disabled={orderBusy.has(o.id)} onclick={() => cancelOrder(o.id, o.symbol)}>✕</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <div class="empty">No working orders — brackets appear here once submitted</div>
      {/if}
    {/snippet}
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
  </div>
{:else if account === "paper"}
  <div class="kpis">
    <KpiTile label="Virtual Equity" value={paper ? fmtUsd(paper.portfolio.equity) : "—"} />
    <KpiTile label="Total Return" value={paper ? fmtPct(paper.portfolio.total_return_pct) : "—"} trend={paper && paper.portfolio.total_return_pct >= 0 ? "up" : "down"} />
    <KpiTile label="Win Rate" value={paper ? `${paper.portfolio.win_rate}%` : "—"} />
    <KpiTile label="Margin In Use" value={paper ? fmtUsd(paper.portfolio.margin_in_use) : "—"} />
  </div>
  {#if paper}
    {@const startCash = paper.portfolio.starting_capital || 100000}
    {@const unrealized = paper.portfolio.open_pnl ?? 0}
    {@const realized = (paper.portfolio.equity ?? startCash) - startCash - unrealized}
    <div class="pnl-row">
      <span class="pnl-label">P&amp;L</span>
      <span class="pnl-cell">
        <i>realized</i>
        <b class="num {realized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(realized)}</b>
        <em class="num {realized >= 0 ? 'pl-up' : 'pl-down'}">{startCash ? fmtPct((realized / startCash) * 100) : "—"}</em>
      </span>
      <span class="pnl-cell">
        <i>unrealized</i>
        <b class="num {unrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(unrealized)}</b>
        <em class="num {unrealized >= 0 ? 'pl-up' : 'pl-down'}">{startCash ? fmtPct((unrealized / startCash) * 100) : "—"}</em>
      </span>
      <span class="pnl-cell total">
        <i>total</i>
        <b class="num {realized + unrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(realized + unrealized)}</b>
        <em class="num {realized + unrealized >= 0 ? 'pl-up' : 'pl-down'}">{startCash ? fmtPct(((realized + unrealized) / startCash) * 100) : "—"}</em>
      </span>
      <span class="pnl-note dim">percentages are against the {fmtUsd(startCash)} starting capital</span>
    </div>
  {/if}
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
            <tr><th>Symbol</th><th>Direction</th><th>Lev</th><th>Qty</th><th>Value</th><th>Entry</th><th>Current</th><th>P&amp;L</th><th></th></tr>
          </thead>
          <tbody>
            {#each paper.positions as p (p.id)}
              <tr>
                <td class="sym">{p.symbol}{#if earningsRisk(p.symbol)}<span class="earnings-tag" title="Reports earnings this week">EARNINGS</span>{/if}</td>
                <td><Pill label={p.direction} tone={p.side === "long" ? "good" : "bad"} /></td>
                <td class="num">{p.leverage}x</td>
                <td class="num qty">{fmtQty(p.qty)}</td>
                <td class="num dim">{fmtValue(p.qty, p.current_price)}</td>
                <td class="num">{fmtPrice(p.entry_price)}</td>
                <td class="num">{fmtPrice(p.current_price)}</td>
                <td class="num {p.unrealized_pct >= 0 ? 'pl-up' : 'pl-down'}">
                  {fmtUsd(p.unrealized_pnl)}<em class="pl-pct">{fmtPct(p.unrealized_pct)}</em>
                </td>
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
  </div>
{:else if account === "autosim"}
  <div class="kpis">
    <KpiTile label="Equity" value={autosim ? fmtUsd(autosim.summary.equity) : "—"} />
    <KpiTile label="Realized P&L" value={autosim ? fmtUsd(autosim.summary.realized_pnl) : "—"} trend={autosim && autosim.summary.realized_pnl >= 0 ? "up" : "down"} />
    <KpiTile label="Win Rate" value={autosim ? `${autosim.summary.win_rate}%` : "—"} />
    <KpiTile label="Total Trades" value={String(autosim?.summary.total_trades ?? "—")} />
    <KpiTile label="Costs Paid" value={autosim ? fmtUsd(autosim.summary.total_fees ?? 0) : "—"} trend="down" />
  </div>
  {#if autosim}
    {@const aRealized = autosim.summary.realized_pnl ?? 0}
    {@const aUnrealized = autosim.summary.unrealized_pnl ?? 0}
    {@const aStart = autosim.summary.starting_cash || 100000}
    <div class="pnl-row">
      <span class="pnl-label">P&amp;L</span>
      <span class="pnl-cell">
        <i>realized</i>
        <b class="num {aRealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(aRealized)}</b>
        <em class="num {aRealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct((aRealized / aStart) * 100)}</em>
      </span>
      <span class="pnl-cell">
        <i>unrealized</i>
        <b class="num {aUnrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(aUnrealized)}</b>
        <em class="num {aUnrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct((aUnrealized / aStart) * 100)}</em>
      </span>
      <span class="pnl-cell total">
        <i>total</i>
        <b class="num {aRealized + aUnrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(aRealized + aUnrealized)}</b>
        <em class="num {aRealized + aUnrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(((aRealized + aUnrealized) / aStart) * 100)}</em>
      </span>
      <span class="pnl-note dim">percentages are against the {fmtUsd(aStart)} starting capital</span>
    </div>
    {@const aFees = autosim.summary.total_fees ?? 0}
    {@const aBefore = autosim.summary.pnl_before_costs ?? 0}
    <div class="pnl-row cost-row">
      <span class="pnl-label">Costs</span>
      <span class="pnl-cell">
        <i>before costs</i>
        <b class="num {aBefore >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(aBefore)}</b>
      </span>
      <span class="pnl-cell">
        <i>fees &amp; spread</i>
        <b class="num pl-down">−{fmtUsd(aFees)}</b>
        <em class="dim">{fmtUsd(autosim.summary.fees_reserved_open ?? 0)} reserved on open</em>
      </span>
      <span class="pnl-cell total">
        <i>after costs</i>
        <b class="num {aRealized + aUnrealized >= 0 ? 'pl-up' : 'pl-down'}">{fmtUsd(aRealized + aUnrealized)}</b>
        {#if autosim.summary.cost_drag_pct != null}
          <em class="num dim">costs took {autosim.summary.cost_drag_pct}% of gross</em>
        {:else if aFees > 0}
          <em class="num dim">costs deepened a losing book</em>
        {/if}
      </span>
      <span class="pnl-note dim">
        the round trip is charged when a position opens, so an untouched
        position shows what it would cost to unwind
      </span>
    </div>
  {/if}
  <div class="stack">
  <div class="two-col">
  <Panel title="Recent Paper Trades" meta="last {paper?.trades.length ?? 0}">
    {#if paper && paper.trades.length}
      <button class="btn tiny outline export-btn" onclick={exportPaperTradesCsv}>Export CSV</button>
      <table class="tbl">
        <thead><tr><th>Symbol</th><th>Direction</th><th>Qty</th><th>P&amp;L</th><th>Reason</th><th>Closed</th></tr></thead>
        <tbody>
          {#each paper.trades.slice(0, 15) as t (t.id)}
            <tr>
              <td class="sym">{t.symbol}</td>
              <td>{t.direction}</td>
              <td class="num qty">{fmtQty(t.qty)}</td>
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
  <Panel title="Auto Sim" meta="follows every eligible signal automatically — paper-only, no broker">
    <button class="btn small outline" style="margin-bottom:8px" onclick={resetAutoSim}>Reset Auto Sim</button>
    <p class="note">
      Auto Sim is a separate always-on paper ledger that opens a $1,000-margin virtual position for every eligible
      signal automatically, independent of manual paper trading above. {autosim?.summary.wins ?? 0} wins /
      {autosim?.summary.losses ?? 0} losses.
    </p>
  </Panel>
  </div>
  <div class="span-12">
    <Panel title="Auto Sim Positions" meta="{autosim?.positions.length ?? 0} open">
      {#if autosim && autosim.positions.length}
        <table class="tbl">
          <thead>
            <tr><th>Symbol</th><th>Direction</th><th>Lev</th><th>Qty</th><th>Value</th><th>Entry</th><th>Current</th><th>Fees</th><th>P&amp;L</th></tr>
          </thead>
          <tbody>
            {#each autosim.positions as p (p.id)}
              <tr>
                <td class="sym">{p.symbol}</td>
                <td><Pill label={p.direction} tone={(p.direction ?? "").toLowerCase().includes("short") ? "bad" : "good"} /></td>
                <td class="num">{p.leverage}x</td>
                <td class="num qty">{fmtQty(p.qty)}</td>
                <td class="num dim">{fmtValue(p.qty, p.current_price ?? p.entry_price)}</td>
                <td class="num">{fmtPrice(p.entry_price)}</td>
                <td class="num">{fmtPrice(p.current_price)}</td>
                <td class="num dim" title={p.fee_basis ?? ""}>−{fmtUsd(p.fees ?? 0)}</td>
                <td class="num {(p.unrealized_pnl ?? 0) >= 0 ? 'pl-up' : 'pl-down'}">
                  {fmtUsd(p.unrealized_pnl ?? 0)}
                  <em class="pl-pct">{fmtPct(((p.unrealized_pnl ?? 0) / (p.margin_used || 1000)) * 100)}</em>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <div class="empty">No open Auto Sim positions</div>
      {/if}
    </Panel>
  </div>
  <div class="span-12">
    <Panel title="Danger Zone" dotColor="var(--bad)" meta="typed confirmation required for every action">
      <div class="dz-row">
        <div class="dz-item">
          <button class="btn small dz-btn" disabled={flattenBusy} onclick={() => doFlatten("live")}>Flatten LIVE</button>
          <span class="dz-desc">Close all Alpaca positions at market, cancel all working orders, reject pending live signals.</span>
        </div>
        <div class="dz-item">
          <button class="btn small dz-btn" disabled={flattenBusy} onclick={() => doFlatten("paper")}>Flatten PAPER</button>
          <span class="dz-desc">Close every open paper AND Auto Sim position at last price, reject pending paper signals. Cash and history preserved.</span>
        </div>
        <div class="dz-item">
          <button class="btn small dz-btn" disabled={flattenBusy} onclick={() => doFlatten("all")}>Flatten EVERYTHING</button>
          <span class="dz-desc">Both of the above in one action.</span>
        </div>
      </div>
      <div class="dz-row">
        <div class="dz-item">
          <button class="btn small outline" onclick={resetPaper}>Reset Paper → $100k</button>
          <span class="dz-desc">Wipes paper positions, trades, and history; fresh $100,000.</span>
        </div>
        <div class="dz-item">
          <button class="btn small outline" onclick={resetAutoSim}>Reset Auto Sim → $100k</button>
          <span class="dz-desc">Same, for the follow-everything simulator.</span>
        </div>
      </div>
      <p class="dz-note">
        Live account balance: Alpaca has no API to reset paper-account equity to a fixed $100,000 — flattening returns it to
        all-cash at current value. For an exact $100k, use the Alpaca dashboard: Account → <b>Reset paper account</b> (one click),
        then restart Jarvis so equity history starts clean.
      </p>
    </Panel>
  </div>
  </div>
{/if}

<style>
  .pnl-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 22px;
    padding: 10px 14px;
    margin-bottom: 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm, 8px);
    background: var(--surface);
  }
  .pnl-label {
    font-size: 10px;
    letter-spacing: 0.1em;
    color: var(--ink-faint);
    font-weight: 700;
  }
  /* The cost row sits directly under P&L and reads as its subordinate: the
     same grid, quieter ground, so the gap between gross and net is legible
     at a glance rather than something you have to compute. */
  .cost-row {
    margin-top: -6px;
    background: color-mix(in srgb, var(--surface) 72%, transparent);
    border-top: none;
    border-top-left-radius: 0;
    border-top-right-radius: 0;
  }
  .cost-row .pnl-cell em {
    font-size: 9.5px;
    font-style: normal;
  }
  .pnl-cell {
    display: flex;
    align-items: baseline;
    gap: 7px;
  }
  .pnl-cell i {
    font-style: normal;
    font-size: 9.5px;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    text-transform: uppercase;
  }
  .pnl-cell b {
    font-size: 15px;
    font-weight: 650;
  }
  .pnl-cell em {
    font-style: normal;
    font-size: 11px;
  }
  .pnl-cell.total b {
    font-size: 17px;
  }
  .pnl-note {
    margin-left: auto;
    font-size: 9.5px;
  }

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
  .export-btn {
    float: right;
    margin-bottom: 10px;
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
  /* Quantity is the number you would place an order for, so it carries the
     same weight as the symbol rather than reading as a secondary figure. */
  .qty {
    font-weight: 600;
    color: var(--ink);
    white-space: nowrap;
  }
  /* The percentage rides beside the cash figure instead of replacing it:
     "+15.88%" alone never says whether that is eight dollars or eight
     hundred. */
  .pl-pct {
    margin-left: 6px;
    font-style: normal;
    font-size: 0.85em;
    opacity: 0.72;
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

  .at-risk-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    background: rgba(255, 180, 84, 0.08);
    border: 1px solid rgba(255, 180, 84, 0.35);
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 12px;
    color: var(--warm);
    margin-bottom: 14px;
  }
  .at-risk-chip {
    background: rgba(255, 180, 84, 0.12);
    border-radius: 5px;
    padding: 3px 8px;
    font-size: 11px;
    color: var(--ink-dim);
  }
  .at-risk-chip i {
    font-style: normal;
    color: var(--ink-faint);
  }

  .two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    align-items: start;
  }

  .risk-split {
    margin-bottom: 14px;
  }
  .risk-bar {
    display: flex;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--surface-raised);
  }
  .risk-bar-long {
    background: var(--good);
  }
  .risk-bar-short {
    background: var(--bad);
  }
  .risk-split-labels {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    margin-top: 6px;
  }
  .exposure-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .exposure-row {
    display: grid;
    grid-template-columns: 70px 1fr 44px;
    align-items: center;
    gap: 8px;
    font-size: 11.5px;
  }
  .exposure-track {
    height: 6px;
    border-radius: 3px;
    background: var(--surface-raised);
    overflow: hidden;
  }
  .exposure-fill {
    height: 100%;
    background: var(--accent);
  }
  .risk-warning {
    margin: 12px 0 0;
    font-size: 11.5px;
    color: var(--warm);
  }

  .sizer-form {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    margin-bottom: 14px;
  }
  .sizer-form label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 10.5px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .sizer-form input {
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 7px 9px;
    font-size: 12.5px;
    font-family: var(--mono);
  }
  .sizer-result {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--line);
  }
  .sizer-result div {
    text-align: center;
  }
  .sizer-result span {
    display: block;
    font-size: 9.5px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 3px;
  }
  .sizer-result b {
    font-size: 13px;
  }

  .earnings-tag {
    display: inline-block;
    margin-left: 6px;
    font-size: 8.5px;
    letter-spacing: 0.05em;
    color: var(--warm);
    border: 1px solid rgba(255, 180, 84, 0.4);
    border-radius: 4px;
    padding: 1px 4px;
    vertical-align: middle;
  }

  .empty.small {
    padding: 10px 0;
    font-size: 11px;
  }

  @media (max-width: 900px) {
    .kpis {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  @media (max-width: 1000px) {
    .two-col {
      grid-template-columns: 1fr;
    }
  }
  .err-note {
    color: var(--bad);
  }
  .dz-row {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    margin-bottom: 12px;
  }
  .dz-item {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1 1 280px;
  }
  .dz-btn {
    border-color: var(--bad);
    color: var(--bad);
    background: rgba(255, 92, 114, 0.08);
  }
  .dz-desc {
    font-size: 10.5px;
    color: var(--ink-faint);
    line-height: 1.4;
  }
  .dz-note {
    font-size: 10.5px;
    color: var(--ink-dim);
    border-top: 1px solid var(--line);
    padding-top: 10px;
    margin: 4px 0 0;
    line-height: 1.5;
  }
</style>
