<script lang="ts">
  /**
   * The Virtual CEX — the primary training exchange.
   *
   * Every CEX-tradable product routes here: equities, ETFs, futures,
   * commodities, FX, crypto spot and perpetuals. It is NOT "the book
   * Alpaca refused" — that was a live-first architecture where direction
   * decided the venue, and a long equity signal reached a real broker by
   * default.
   *
   * The order ticket is PRODUCT-AWARE because the products are not alike:
   * shares are fractional and contracts are whole, a futures stop is
   * priced through a multiplier, FX sizes in units with a pip value, and
   * a perpetual has funding and a liquidation price that spot does not.
   * Showing one generic form for all of them is how a $1,000 risk budget
   * became a $100,000 gold position.
   */
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import StateNote from "../components/StateNote.svelte";
  import { api, type PaperSummary, type PlatformMode, type SizeResult } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();
  let mode = $state<PlatformMode | null>(null);
  let book = $state<PaperSummary | null>(null);

  // ── Product-aware ticket ────────────────────────────────────────────
  let ticket = $state({ symbol: "", equity: "100000", riskPct: "1",
                        entry: "", stop: "", side: "Long" });
  let sized = $state<SizeResult | null>(null);
  let sizing = $state(false);

  $effect(() => {
    // Both feeds in parallel — a sequential chain here is a visible stall.
    Promise.all([
      feeds.load("mode", () => api.platformMode()),
      feeds.load("book", () => api.paperSummary()),
    ]).then(([m, b]) => { mode = m; book = b; });
  });

  $effect(() => {
    const symbol = ticket.symbol.trim();
    const equity = Number(ticket.equity) || 0;
    const riskPct = Number(ticket.riskPct) || 0;
    const entry = Number(ticket.entry) || 0;
    const stop = Number(ticket.stop) || 0;
    if (!symbol || !equity || !riskPct || !entry || !stop || entry === stop) {
      sized = null;
      return;
    }
    sizing = true;
    api.sizePosition({ symbol, entry, stop, equity, risk_pct: riskPct,
                       side: ticket.side })
      .then((r) => { sized = r; })
      .catch(() => { sized = null; })
      .finally(() => { sizing = false; });
  });

  const usd = (v: number | null | undefined) =>
    v == null ? "—" : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
</script>

<div class="page">
  <header class="page-head">
    <h1>Virtual CEX</h1>
    <p>
      The primary training exchange — equities, ETFs, futures, commodities,
      FX, crypto spot and perpetuals. Orders are simulated against a book:
      market orders cross the spread, limits need the bar to trade through,
      and stops gap.
    </p>
    {#if mode}
      <div class="mode-strip" class:training={!mode.live_execution_allowed}>
        <Pill tone={mode.virtual_only ? "info" : "warm"} label={mode.mode} />
        <span>{mode.detail}</span>
      </div>
    {/if}
  </header>

  <div class="kpis">
    <KpiTile label="Equity" value={book ? usd(book.portfolio?.equity) : "—"} />
    <KpiTile label="Cash" value={book ? usd(book.portfolio?.cash) : "—"} />
    <KpiTile label="Unrealized" value={book ? usd(book.portfolio?.open_pnl) : "—"} />
    <KpiTile label="Open positions" value={book ? String(book.positions?.length ?? 0) : "—"} />
  </div>

  <div class="split">
    <Panel title="Order Ticket" meta="product-aware">
      <div class="form">
        <label>Symbol<input placeholder="MES=F / NVDA / EUR/USD / BTC/USD"
                            bind:value={ticket.symbol} /></label>
        <label>Side
          <select bind:value={ticket.side}>
            <option>Long</option><option>Short</option>
          </select>
        </label>
        <label>Account equity<input bind:value={ticket.equity} /></label>
        <label>Risk %<input bind:value={ticket.riskPct} /></label>
        <label>Entry<input placeholder="0.00" bind:value={ticket.entry} /></label>
        <label>Stop<input placeholder="0.00" bind:value={ticket.stop} /></label>
      </div>

      {#if sizing}
        <div class="empty small">Sizing…</div>
      {:else if sized?.ok}
        <!--
          THE UNIT IS PART OF THE ANSWER. "20" means nothing without
          knowing shares from contracts, and the browser is not allowed to
          derive any of this — a multiplier, a tick, a margin requirement
          and whether a product may be fractional all live server-side.
        -->
        <div class="sized">
          <div><span>Size</span><b class="num">{sized.quantity?.toFixed(sized.quantity_unit === "CONTRACTS" ? 0 : 4)}
            <em>{sized.quantity_unit?.toLowerCase()}</em></b></div>
          <div><span>Risk</span><b class="num">{usd(sized.risk_usd)}</b></div>
          <div><span>Notional</span><b class="num">{usd(sized.notional_usd)}</b></div>
          <div><span>Margin</span><b class="num">{usd(sized.margin_usd)}</b></div>
          {#if sized.multiplier && sized.multiplier !== 1}
            <div><span>Multiplier</span><b class="num">{sized.multiplier}x</b></div>
          {/if}
          <div><span>Product</span><b class="num small">{sized.product}</b></div>
        </div>
      {:else if sized}
        <div class="refused">
          <strong>{sized.reason}</strong>
          <span>{sized.detail}</span>
        </div>
      {:else}
        <div class="empty small">Enter a symbol, entry and stop</div>
      {/if}
    </Panel>

    <Panel title="Execution model" meta="why fills are not free">
      <div class="notes">
        <div><b>Market orders cross.</b> A buy lifts the ask, a sell hits the
          bid, and both then pay slippage. Filling at the mid hands the
          model half the spread on every trade.</div>
        <div><b>Limits need a trade-through.</b> A bar that merely touched
          your price is not evidence size existed there — that assumption
          fills you at your best price on exactly the bars where being
          filled mattered most.</div>
        <div><b>Stops gap.</b> When a bar opens beyond the stop, the fill is
          at the open. Booking the stop price silently caps every tail loss
          the strategy will ever record.</div>
        <div><b>Leverage does not protect you.</b> If price crosses the
          liquidation boundary before the stop, the exchange closes the
          position at its price and the stop never fills.</div>
      </div>
    </Panel>
  </div>

  <Panel title="Open positions" status={feeds.status("book")}
         meta={book ? `${book.positions?.length ?? 0} open` : "—"}>
    {#if book?.positions?.length}
      <div class="tablewrap">
        <table>
          <thead>
            <tr><th>Symbol</th><th>Side</th><th class="n">Qty</th>
                <th class="n">Entry</th><th class="n">Current</th>
                <th class="n">P&L</th></tr>
          </thead>
          <tbody>
            {#each book.positions as p (p.id ?? p.symbol)}
              <tr>
                <td class="mono">{p.symbol}</td>
                <td><Pill tone={p.side === "short" ? "bad" : "good"} label={p.side} /></td>
                <td class="n">{p.qty}</td>
                <td class="n">{p.entry_price}</td>
                <td class="n">{p.current_price}</td>
                <td class="n" class:pl-up={(p.unrealized_pnl ?? 0) > 0}
                    class:pl-down={(p.unrealized_pnl ?? 0) < 0}>{usd(p.unrealized_pnl)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <StateNote status={feeds.status("book")} noun="Virtual CEX book"
                 emptyText="No open positions." />
    {/if}
  </Panel>
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 14px; }
  .page-head h1 { margin: 0; font-size: 19px; letter-spacing: -.01em; }
  .page-head p { margin: 4px 0 0; font-size: 12.5px; color: var(--muted); max-width: 78ch; }
  .mode-strip {
    display: flex; align-items: center; gap: 9px; margin-top: 9px;
    padding: 6px 10px; border-radius: 4px; font-size: 12px;
    border: 1px solid var(--border, #222a35);
  }
  .mode-strip.training {
    border-color: color-mix(in srgb, var(--accent) 40%, transparent);
    background: color-mix(in srgb, var(--accent) 7%, transparent);
  }
  .mode-strip span { color: var(--muted); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
  .split { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(300px, 1fr); gap: 14px; }
  @media (max-width: 900px) { .split { grid-template-columns: 1fr; } }
  .form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .form label { display: flex; flex-direction: column; gap: 3px; font-size: 10.5px;
                text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .form input, .form select {
    background: var(--bg-elev, #11151c); color: var(--text, #e6e9ef);
    border: 1px solid var(--border, #222a35); border-radius: 4px;
    padding: 6px 8px; font-size: 12.5px; font-family: inherit;
  }
  .sized {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 8px; margin-top: 12px; padding-top: 10px;
    border-top: 1px solid var(--border, #222a35);
  }
  .sized div { display: flex; flex-direction: column; gap: 2px; }
  .sized span { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .sized b { font-size: 15px; font-variant-numeric: tabular-nums; }
  .sized b.small { font-size: 12px; }
  .sized em { font-style: normal; font-size: 11px; color: var(--muted); }
  .refused {
    display: flex; flex-direction: column; gap: 3px; margin-top: 12px;
    padding: 9px 11px; border-radius: 4px;
    background: color-mix(in srgb, var(--bad, #e06c75) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--bad, #e06c75) 35%, transparent);
  }
  .refused strong { font-size: 10.5px; letter-spacing: .07em; text-transform: uppercase;
                    color: var(--bad, #e06c75); }
  .refused span { font-size: 12px; color: var(--muted); }
  .notes { display: flex; flex-direction: column; gap: 9px; }
  .notes div { font-size: 12.5px; color: var(--muted); line-height: 1.5; }
  .notes b { color: var(--text, #e6e9ef); }
  .empty { color: var(--muted); font-size: 12.5px; margin-top: 10px; }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; padding: 6px 9px; color: var(--muted); font-size: 10.5px;
       text-transform: uppercase; letter-spacing: .07em;
       border-bottom: 1px solid var(--border, #222a35); }
  td { padding: 6px 9px; border-bottom: 1px solid var(--border, #222a35); }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
</style>
