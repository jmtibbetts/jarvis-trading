<script lang="ts">
  import { api, type OrderBookSnapshot } from "../api";
  import { wsStore } from "../stores/ws.svelte";

  const SYMBOLS = ["BTC", "ETH"] as const;
  type Sym = (typeof SYMBOLS)[number];
  type Exch = "binance" | "coinbase";

  let symbol = $state<Sym>("BTC");
  let exchange = $state<Exch>("binance");
  // symbol -> exchange -> snapshot, so switching tabs doesn't lose data
  // already streamed in for the other combination.
  let books = $state<Record<string, Partial<Record<Exch, OrderBookSnapshot>>>>({ BTC: {}, ETH: {} });

  async function loadInitial(sym: Sym) {
    try {
      const res = await api.orderbook(sym);
      books = {
        ...books,
        [sym]: { binance: res.binance ?? books[sym]?.binance, coinbase: res.coinbase ?? books[sym]?.coinbase },
      };
    } catch {
      // no snapshot yet (streams still connecting) — WS push will fill it in
    }
  }

  $effect(() => {
    loadInitial("BTC");
    loadInitial("ETH");
    const unsub = wsStore.on("orderbook", (msg) => {
      const snap = msg.data as OrderBookSnapshot;
      if (!SYMBOLS.includes(snap.symbol as Sym)) return;
      books = { ...books, [snap.symbol]: { ...books[snap.symbol], [snap.exchange]: snap } };
    });
    return unsub;
  });

  const active = $derived(books[symbol]?.[exchange] ?? null);
  const maxLevelQty = $derived.by(() => {
    if (!active) return 1;
    const qtys = [...active.bids.map((b) => b[1]), ...active.asks.map((a) => a[1])];
    return Math.max(1e-9, ...qtys);
  });

  const fmtPrice = (p: number) => p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtQty = (q: number) => q.toLocaleString(undefined, { maximumFractionDigits: 4 });
  const secondsAgo = (ts: number) => Math.max(0, Math.round(Date.now() / 1000 - ts));
</script>

<div class="ob-toolbar">
  <div class="ob-tabs">
    {#each SYMBOLS as s (s)}
      <button class="ob-tab" class:on={symbol === s} onclick={() => (symbol = s)}>{s}</button>
    {/each}
  </div>
  <div class="ob-tabs">
    <button class="ob-tab" class:on={exchange === "binance"} onclick={() => (exchange = "binance")}>Binance</button>
    <button class="ob-tab" class:on={exchange === "coinbase"} onclick={() => (exchange = "coinbase")}>Coinbase</button>
  </div>
</div>

{#if active}
  <div class="ob-stats">
    <div class="ob-stat"><span>Best Bid</span><b class="num pl-up">{fmtPrice(active.best_bid ?? 0)}</b></div>
    <div class="ob-stat"><span>Best Ask</span><b class="num pl-down">{fmtPrice(active.best_ask ?? 0)}</b></div>
    <div class="ob-stat"><span>Spread</span><b class="num">{active.spread_bps != null ? `${active.spread_bps} bps` : "—"}</b></div>
    <div class="ob-stat">
      <span>Imbalance</span>
      <b class="num {active.imbalance != null && active.imbalance > 0 ? 'pl-up' : active.imbalance != null && active.imbalance < 0 ? 'pl-down' : ''}">
        {active.imbalance != null ? `${active.imbalance >= 0 ? "+" : ""}${(active.imbalance * 100).toFixed(0)}%` : "—"}
      </b>
    </div>
    <div class="ob-stat"><span>Updated</span><b class="num small">{secondsAgo(active.ts)}s ago</b></div>
  </div>

  <div class="ob-depth">
    <div class="ob-side">
      {#each active.bids.slice(0, 8) as [price, qty] (price)}
        <div class="ob-row">
          <div class="ob-bar bid" style="width:{(qty / maxLevelQty) * 100}%"></div>
          <span class="ob-price pl-up">{fmtPrice(price)}</span>
          <span class="ob-qty">{fmtQty(qty)}</span>
        </div>
      {/each}
    </div>
    <div class="ob-side">
      {#each active.asks.slice(0, 8) as [price, qty] (price)}
        <div class="ob-row reverse">
          <div class="ob-bar ask" style="width:{(qty / maxLevelQty) * 100}%"></div>
          <span class="ob-price pl-down">{fmtPrice(price)}</span>
          <span class="ob-qty">{fmtQty(qty)}</span>
        </div>
      {/each}
    </div>
  </div>
{:else}
  <div class="ob-empty">Connecting to {exchange} {symbol} order book…</div>
{/if}

<style>
  .ob-toolbar {
    display: flex;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .ob-tabs {
    display: flex;
    gap: 4px;
  }
  .ob-tab {
    background: none;
    border: 1px solid var(--line-bright);
    color: var(--ink-dim);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .ob-tab.on {
    color: var(--accent);
    border-color: var(--accent);
    background: rgba(124, 154, 255, 0.1);
  }
  .ob-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    margin-bottom: 14px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--line);
  }
  .ob-stat {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .ob-stat span {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
  }
  .ob-stat b {
    font-size: 14px;
  }
  .ob-stat b.small {
    font-size: 11px;
    font-weight: 500;
    color: var(--ink-faint);
  }
  .ob-depth {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }
  .ob-side {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .ob-row {
    position: relative;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 8px;
    padding: 3px 6px;
    font-size: 11px;
    overflow: hidden;
    border-radius: 3px;
  }
  .ob-row.reverse {
    direction: rtl;
  }
  .ob-row.reverse .ob-price,
  .ob-row.reverse .ob-qty {
    direction: ltr;
  }
  .ob-bar {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 0;
  }
  .ob-bar.bid {
    background: rgba(61, 220, 151, 0.14);
  }
  .ob-bar.ask {
    background: rgba(255, 92, 114, 0.14);
  }
  .ob-price,
  .ob-qty {
    position: relative;
    z-index: 1;
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
  }
  .ob-qty {
    color: var(--ink-faint);
    text-align: right;
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
  .ob-empty {
    padding: 30px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }
</style>
