<script lang="ts">
  /**
   * The virtual DEX exchange — a book that prices swaps the way a pool does.
   *
   * `lib/dex_paper` and `lib/dex_swap_math` were built, tested and had NO
   * routes and no surface at all. This is that surface.
   *
   * It is deliberately not the paper book's UI, because an AMM swap and a
   * broker fill differ in the fields that matter:
   *
   *   NO LEVERAGE      a constant-product pool does not lend
   *   NO SHORT SIDE    you cannot borrow from a pool
   *   DEPTH, NOT CASH  size is bounded by what the pool can absorb; an
   *                    account-equity sizer will happily propose a trade
   *                    that moves the price against itself
   *
   * PRICE IMPACT IS NOT A FEE and is never added into one. Impact is a
   * function of YOUR size against pool depth — the remedy is trading
   * smaller. The pool fee is the venue's cut and the network fee is the
   * chain's; neither shrinks when you do. Netting the three into one
   * "cost" hides which one made a trade unprofitable.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import KpiTile from "./KpiTile.svelte";
  import StateNote from "./StateNote.svelte";
  import { api, type DexBook, type DexQuote, type DexTrade } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import { toastStore } from "../stores/toast.svelte";

  const feeds = new FeedTracker();

  let book = $state<DexBook | null>(null);
  let trades = $state<DexTrade[]>([]);
  let quote = $state<DexQuote | null>(null);
  let busy = $state(false);
  let quoting = $state(false);

  // Ticket. Reserve and price are required because the pool's depth is what
  // makes the quote real — there is no "market order" abstraction here.
  let mint = $state("");
  let symbol = $state("");
  let reserveUsd = $state<number | null>(null);
  let priceUsd = $state<number | null>(null);
  let sizeUsd = $state<number | null>(null);
  let stopUsd = $state<number | null>(null);
  let targetUsd = $state<number | null>(null);
  let dex = $state("");

  // Close ticket, per position.
  let closing = $state<Record<string, { price: number | null; reserve: number | null }>>({});

  async function load() {
    const [b, t] = await Promise.all([
      feeds.load("book", () => api.dexBook()),
      feeds.load("trades", () => api.dexTrades(40)),
    ]);
    book = b;
    trades = t?.trades ?? [];
  }

  $effect(() => {
    load();
    const poll = setInterval(load, 45_000);
    return () => clearInterval(poll);
  });

  const money = (v: number | null | undefined, dp = 2) =>
    v === null || v === undefined ? "—" : `$${v.toLocaleString(undefined, {
      minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
  const pct = (v: number | null | undefined, dp = 2) =>
    v === null || v === undefined ? "—" : `${v.toFixed(dp)}%`;
  const num = (v: number | null | undefined, dp = 4) =>
    v === null || v === undefined ? "—" : v.toLocaleString(undefined, {
      maximumFractionDigits: dp });

  /** Impact severity, by the same ceiling the engine enforces. */
  function impactTone(v: number | null | undefined) {
    const cap = book?.limits?.max_impact_pct ?? 3;
    if (v === null || v === undefined) return "neutral";
    if (v >= cap) return "critical";
    if (v >= cap * 0.6) return "warm";
    return "good";
  }

  async function getQuote() {
    if (!reserveUsd || !sizeUsd || quoting) return;
    quoting = true;
    try {
      quote = await api.dexQuote(sizeUsd, reserveUsd, {
        dex: dex || undefined,
      });
      if (!quote?.ok && quote?.reason) toastStore.push(quote.reason, "bad");
    } catch (e: any) {
      quote = null;
      toastStore.err(`Quote failed: ${e?.message ?? e}`);
    } finally {
      quoting = false;
    }
  }

  async function submit() {
    if (busy || !mint || !reserveUsd || !priceUsd) return;
    busy = true;
    try {
      const r = await api.dexOpen({
        mint, symbol: symbol || null, dex: dex || null,
        reserve_usd: reserveUsd, price_usd: priceUsd,
        size_usd: sizeUsd, stop_price_usd: stopUsd,
        target_price_usd: targetUsd,
      });
      // The engine names its own refusals; surface the sentence verbatim
      // rather than a generic failure.
      if (r?.error) toastStore.push(r.error, "bad");
      else {
        toastStore.ok(`Opened ${symbol || mint.slice(0, 8)}`);
        mint = ""; symbol = ""; sizeUsd = null; stopUsd = null; targetUsd = null;
        quote = null;
        await load();
      }
    } catch (e: any) {
      toastStore.err(`Open failed: ${e?.message ?? e}`);
    } finally {
      busy = false;
    }
  }

  async function closePosition(id: string) {
    const t = closing[id];
    if (!t?.price || busy) {
      toastStore.push("An exit needs a price — the pool has to fill it", "bad");
      return;
    }
    busy = true;
    try {
      const r = await api.dexClose({
        position_id: id, price_usd: t.price,
        reserve_usd: t.reserve ?? null, reason: "manual",
      });
      if (r?.error) toastStore.push(r.error, "bad");
      else {
        toastStore.ok("Position closed");
        delete closing[id];
        await load();
      }
    } catch (e: any) {
      toastStore.err(`Close failed: ${e?.message ?? e}`);
    } finally {
      busy = false;
    }
  }

  function ticketFor(id: string) {
    if (!closing[id]) closing[id] = { price: null, reserve: null };
    return closing[id];
  }
</script>

<div class="dex">
  <Panel title="DEX Book" status={feeds.status("book")}
         meta={book ? `${book.open_positions} open · reset ${book.reset_at?.slice(0, 10) ?? "never"}` : ""}>
    {#if book}
      <div class="kpis">
        <KpiTile label="Equity" value={money(book.equity_usd)} />
        <KpiTile label="Cash" value={money(book.cash_usd)} />
        <KpiTile label="Open value" value={money(book.open_value_usd)} />
        <KpiTile label="Realized P&L" value={money(book.realized_pnl_usd)}
                 trend={book.realized_pnl_usd > 0 ? "up" : book.realized_pnl_usd < 0 ? "down" : undefined} />
        <KpiTile label="Trades" value={`${book.total_trades}`} />
        <KpiTile label="W / L" value={`${book.wins} / ${book.losses}`} />
      </div>
      <div class="limits">
        <Pill tone="neutral" label={`max impact ${book.limits.max_impact_pct}%`} />
        <Pill tone="neutral" label={`min pool ${money(book.limits.min_pool_reserve_usd, 0)}`} />
        <Pill tone="info" label="no leverage — a pool does not lend" />
        <Pill tone="info" label="spot only — no short side" />
      </div>
    {:else}
      <StateNote status={feeds.status("book")} noun="DEX book" />
    {/if}
  </Panel>

  <Panel title="New Swap" status={null}>
    <p class="hint">
      Size is bounded by <strong>pool depth</strong>, not by cash. Quote first —
      the price you get is the price your own size creates.
    </p>
    <div class="form">
      <label>Mint<input bind:value={mint} placeholder="token mint address" /></label>
      <label>Symbol<input bind:value={symbol} placeholder="optional" /></label>
      <label>DEX<input bind:value={dex} placeholder="raydium / orca …" /></label>
      <label>Pool reserve USD<input type="number" bind:value={reserveUsd} placeholder="required" /></label>
      <label>Price USD<input type="number" step="any" bind:value={priceUsd} placeholder="required" /></label>
      <label>Size USD<input type="number" bind:value={sizeUsd} placeholder="auto if blank" /></label>
      <label>Stop USD<input type="number" step="any" bind:value={stopUsd} placeholder="optional" /></label>
      <label>Target USD<input type="number" step="any" bind:value={targetUsd} placeholder="optional" /></label>
    </div>
    <div class="actions">
      <button onclick={getQuote} disabled={quoting || !reserveUsd || !sizeUsd}>
        {quoting ? "Quoting…" : "Quote swap"}
      </button>
      <button class="primary" onclick={submit} disabled={busy || !mint || !reserveUsd || !priceUsd}>
        {busy ? "Working…" : "Open position"}
      </button>
    </div>

    {#if quote}
      {#if quote.ok}
        <div class="quote">
          <div class="qrow">
            <span>Price impact</span>
            <Pill tone={impactTone(quote.price_impact_pct)}
                  label={pct(quote.price_impact_pct)} />
          </div>
          <div class="qrow"><span>Effective price</span><b>{money(quote.effective_price_usd, 6)}</b></div>
          <div class="qrow"><span>Tokens out</span><b>{num(quote.tokens_out)}</b></div>
          <div class="qrow"><span>Pool fee</span><b>{money(quote.pool_fee_usd)}</b></div>
          <div class="qrow"><span>Network fee</span><b>{money(quote.network_fee_usd)}</b></div>
          <div class="qrow strong">
            <span>Round trip cost</span><b>{pct(quote.round_trip_cost_pct)}</b>
          </div>
          <div class="qrow strong">
            <span>Breakeven move</span><b>{pct(quote.breakeven_move_pct)}</b>
          </div>
          <p class="hint">
            Max size at 1% impact {money(quote.max_size_1pct_impact, 0)} ·
            at 2% {money(quote.max_size_2pct_impact, 0)}
          </p>
        </div>
      {:else}
        <p class="refusal">{quote.reason ?? "The pool refused this swap."}</p>
      {/if}
    {/if}
  </Panel>

  <Panel title="Open Positions" status={feeds.status("book")}
         meta={book ? `${book.positions.length}` : ""}>
    {#if book && book.positions.length}
      <div class="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Token</th><th>Qty</th><th>Entry</th><th>Quoted</th>
              <th>Now</th><th>Notional</th><th>Entry impact</th>
              <th>Pool depth</th><th>Exit</th>
            </tr>
          </thead>
          <tbody>
            {#each book.positions as p (p.id)}
              {@const t = ticketFor(p.id)}
              <tr>
                <td>
                  <b>{p.symbol ?? p.mint.slice(0, 8)}</b>
                  {#if p.dex}<span class="dim">{p.dex}</span>{/if}
                </td>
                <td class="n">{num(p.qty_tokens)}</td>
                <td class="n">{money(p.entry_price_usd, 6)}</td>
                <!-- The mid and what the pool actually charged you differ by
                     impact; showing both is the point. -->
                <td class="n">{money(p.quoted_price_usd, 6)}</td>
                <td class="n">{money(p.current_price_usd, 6)}</td>
                <td class="n">{money(p.notional_usd)}</td>
                <td class="n">
                  <Pill tone={impactTone(p.entry_impact_pct)}
                        label={pct(p.entry_impact_pct)} />
                </td>
                <td class="n">{money(p.pool_reserve_usd_at_entry, 0)}</td>
                <td class="exit">
                  <input type="number" step="any" placeholder="exit px"
                         bind:value={t.price} />
                  <input type="number" placeholder="reserve"
                         bind:value={t.reserve} />
                  <button onclick={() => closePosition(p.id)} disabled={busy}>Close</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else if book}
      <p class="empty">No open DEX positions.</p>
    {:else}
      <StateNote status={feeds.status("book")} noun="DEX positions" />
    {/if}
  </Panel>

  <Panel title="Closed Swaps" status={feeds.status("trades")}
         meta={`${trades.length}`}>
    {#if trades.length}
      <div class="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Token</th><th>Entry</th><th>Exit</th><th>Notional</th>
              <th>Gross</th><th>Fees</th><th>Net</th><th>%</th>
              <th>Impact in / out</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {#each trades as t (t.id)}
              <tr>
                <td><b>{t.symbol ?? t.mint.slice(0, 8)}</b></td>
                <td class="n">{money(t.entry_price_usd, 6)}</td>
                <td class="n">{money(t.exit_price_usd, 6)}</td>
                <td class="n">{money(t.notional_usd)}</td>
                <td class="n">{money(t.gross_pnl_usd)}</td>
                <td class="n">{money(t.total_fees_usd)}</td>
                <td class="n" class:up={(t.net_pnl_usd ?? 0) > 0}
                    class:down={(t.net_pnl_usd ?? 0) < 0}>{money(t.net_pnl_usd)}</td>
                <td class="n">{pct(t.pnl_pct)}</td>
                <!-- Impact stays separate from fees: it is a function of
                     your own size, and the remedy is trading smaller. -->
                <td class="n">{pct(t.entry_impact_pct)} / {pct(t.exit_impact_pct)}</td>
                <td class="dim">{t.exit_reason ?? "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <StateNote status={feeds.status("trades")} noun="Closed swaps"
                 emptyText="No swaps closed yet." />
    {/if}
  </Panel>
</div>

<style>
  .dex { display: flex; flex-direction: column; gap: 14px; }
  .kpis {
    display: grid; gap: 10px;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  }
  .limits { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
  .form {
    display: grid; gap: 10px;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  }
  .form label {
    display: flex; flex-direction: column; gap: 4px;
    font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted);
  }
  .form input, .exit input {
    background: var(--bg-elev, #11151c); color: var(--text, #e6e9ef);
    border: 1px solid var(--border, #222a35); border-radius: 4px;
    padding: 6px 8px; font-size: 13px; font-family: inherit; min-width: 0;
  }
  .actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  button {
    background: var(--bg-elev, #11151c); color: var(--text, #e6e9ef);
    border: 1px solid var(--border, #222a35); border-radius: 4px;
    padding: 7px 12px; font-size: 12px; cursor: pointer; font-family: inherit;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.primary { border-color: var(--accent); }
  .quote {
    margin-top: 14px; padding: 12px; border-radius: 4px;
    border: 1px solid var(--border, #222a35);
  }
  .qrow {
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; padding: 3px 0; font-size: 13px;
  }
  .qrow span { color: var(--muted); }
  .qrow.strong { border-top: 1px solid var(--border, #222a35); margin-top: 6px; padding-top: 8px; }
  .refusal {
    margin-top: 12px; padding: 10px 12px; border-radius: 4px; font-size: 13px;
    border: 1px solid var(--warn, #6b5330); color: var(--warn-text, #e0b070);
  }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; padding: 7px 10px; color: var(--muted);
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
    border-bottom: 1px solid var(--border, #222a35); white-space: nowrap;
  }
  td {
    padding: 7px 10px; border-bottom: 1px solid var(--border, #222a35);
    vertical-align: middle;
  }
  td.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.up { color: var(--good, #4ec9a0); }
  td.down { color: var(--bad, #e06c75); }
  .dim { color: var(--muted); font-size: 11px; }
  .exit { display: flex; gap: 5px; align-items: center; }
  .exit input { width: 82px; }
  .empty { color: var(--muted); font-size: 13px; margin: 0; }
</style>
