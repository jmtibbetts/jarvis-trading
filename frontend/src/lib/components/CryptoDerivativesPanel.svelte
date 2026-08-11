<script lang="ts">
  import { api, type CryptoDerivativesSnapshot } from "../api";
  import { wsStore } from "../stores/ws.svelte";

  const SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE"] as const;
  type Sym = (typeof SYMBOLS)[number];

  let symbol = $state<Sym>("BTC");
  let snapshots = $state<Partial<Record<Sym, CryptoDerivativesSnapshot>>>({});
  let loading = $state(false);
  let errored = $state<Sym | null>(null);

  async function load(sym: Sym) {
    loading = true;
    errored = null;
    try {
      const res = await api.cryptoDerivatives(sym);
      snapshots = { ...snapshots, [sym]: res };
    } catch {
      errored = sym;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    load(symbol);
  });

  $effect(() => {
    const unsub = wsStore.on("liquidation", (msg) => {
      const data = msg.data as { symbol?: string };
      if (data.symbol === symbol) load(symbol);
    });
    return unsub;
  });

  const active = $derived(snapshots[symbol] ?? null);

  const OI_ACTION_LABEL: Record<string, string> = {
    long_buildup: "Long Buildup", short_buildup: "Short Buildup",
    short_covering: "Short Covering", long_unwinding: "Long Unwinding",
  };

  const fmtUsd = (n: number | null) =>
    n == null ? "—" : n >= 1e9 ? `$${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `$${(n / 1e6).toFixed(1)}M` : `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  const fmtPct = (n: number | null) => (n == null ? "—" : `${(n * 100).toFixed(3)}%`);
  const timeAgo = (iso: string) => {
    const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    return `${Math.round(secs / 3600)}h ago`;
  };
</script>

<div class="cd-toolbar">
  <div class="cd-tabs">
    {#each SYMBOLS as s (s)}
      <button class="cd-tab" class:on={symbol === s} onclick={() => (symbol = s)}>{s}</button>
    {/each}
  </div>
</div>

{#if errored === symbol}
  <div class="cd-empty">No derivatives data yet for {symbol} — ingestion job may still be warming up.</div>
{:else if active}
  <div class="cd-stats">
    <div class="cd-stat"><span>Funding Rate</span><b class="num {active.funding_rate != null && active.funding_rate >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(active.funding_rate)}</b></div>
    <div class="cd-stat"><span>Open Interest</span><b class="num">{fmtUsd(active.open_interest_usd)}</b></div>
    <div class="cd-stat"><span>Long/Short Ratio</span><b class="num">{active.long_short_ratio != null ? active.long_short_ratio.toFixed(2) : "—"}</b></div>
    <div class="cd-stat">
      <span>OI/Price Action</span>
      <b class="num small">{active.oi_price_action ? OI_ACTION_LABEL[active.oi_price_action] : "—"}</b>
    </div>
    <div class="cd-stat"><span>Updated</span><b class="num small">{timeAgo(active.fetched_at)}</b></div>
  </div>

  <div class="cd-liq-summary">
    <div class="cd-liq-bar">
      {#if active.liquidations_summary.total_liquidated_usd > 0}
        <div class="cd-liq-seg long" style="width:{(active.liquidations_summary.long_liquidation_share ?? 0) * 100}%"></div>
        <div class="cd-liq-seg short" style="width:{(1 - (active.liquidations_summary.long_liquidation_share ?? 0)) * 100}%"></div>
      {/if}
    </div>
    <div class="cd-liq-legend">
      <span class="pl-up">Longs liq'd {fmtUsd(active.liquidations_summary.long_liquidated_usd)}</span>
      <span class="pl-down">Shorts liq'd {fmtUsd(active.liquidations_summary.short_liquidated_usd)}</span>
      <span class="dim">({active.liquidations_summary.count} events, 24h)</span>
    </div>
  </div>

  {#if active.liquidations.length}
    <table class="cd-liq-table">
      <thead><tr><th>Side</th><th>Price</th><th>Size</th><th>Notional</th><th>Time</th></tr></thead>
      <tbody>
        {#each active.liquidations.slice(0, 10) as l (l.liquidated_at + l.price)}
          <tr>
            <td class={l.pos_side === "long" ? "pl-down" : "pl-up"}>{l.pos_side === "long" ? "Long liq" : "Short liq"}</td>
            <td class="num">{l.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
            <td class="num">{l.size.toLocaleString(undefined, { maximumFractionDigits: 4 })}</td>
            <td class="num">{fmtUsd(l.notional_usd)}</td>
            <td class="dim">{timeAgo(l.liquidated_at)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <div class="cd-empty">No liquidations in the last 24h for {symbol}.</div>
  {/if}
{:else if loading}
  <div class="cd-empty">Loading {symbol} derivatives…</div>
{/if}

<style>
  .cd-toolbar { display: flex; margin-bottom: 12px; }
  .cd-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
  .cd-tab {
    background: none; border: 1px solid var(--line-bright); color: var(--ink-dim);
    padding: 5px 10px; border-radius: 6px; font-size: 11.5px; cursor: pointer;
  }
  .cd-tab.on { color: var(--accent); border-color: var(--accent); background: rgba(124, 154, 255, 0.1); }
  .cd-stats {
    display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 14px;
    padding-bottom: 12px; border-bottom: 1px solid var(--line);
  }
  .cd-stat { display: flex; flex-direction: column; gap: 3px; }
  .cd-stat span { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-faint); }
  .cd-stat b { font-size: 14px; }
  .cd-stat b.small { font-size: 11px; font-weight: 500; color: var(--ink-faint); }
  .cd-liq-summary { margin-bottom: 12px; }
  .cd-liq-bar { display: flex; height: 6px; border-radius: 3px; overflow: hidden; background: var(--line); margin-bottom: 6px; }
  .cd-liq-seg.long { background: var(--bad); }
  .cd-liq-seg.short { background: var(--good); }
  .cd-liq-legend { display: flex; gap: 12px; font-size: 10.5px; }
  .cd-liq-legend .dim { color: var(--ink-faint); }
  .cd-liq-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .cd-liq-table th {
    text-align: left; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--ink-faint); padding: 4px 6px; border-bottom: 1px solid var(--line);
  }
  .cd-liq-table td { padding: 4px 6px; border-bottom: 1px solid var(--line); }
  .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .pl-up { color: var(--good); }
  .pl-down { color: var(--bad); }
  .dim { color: var(--ink-faint); }
  .cd-empty { padding: 30px 0; text-align: center; color: var(--ink-faint); font-size: 12px; }
</style>
