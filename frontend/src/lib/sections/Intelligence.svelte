<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import ThreatMap from "../components/ThreatMap.svelte";
  import { api, type Regime, type Threat, type NewsArticle, type MarketAsset } from "../api";

  let regime = $state<Regime | null>(null);
  let threats = $state<Threat[]>([]);
  let news = $state<NewsArticle[]>([]);
  let equities = $state<MarketAsset[]>([]);
  let crypto = $state<MarketAsset[]>([]);
  let marketTab = $state<"equities" | "crypto">("equities");

  async function loadAll() {
    const [r, t, n, m] = await Promise.all([
      api.regime().catch(() => null),
      api.threats(30),
      api.news(30),
      api.marketFull().catch(() => ({ equities: [], crypto: [], count: 0 })),
    ]);
    regime = r;
    threats = t;
    news = n;
    equities = m.equities.slice(0, 12);
    crypto = m.crypto.slice(0, 12);
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 30_000);
    return () => clearInterval(poll);
  });

  const sevTone = (s: string) => (s === "Critical" ? "critical" : s === "High" ? "warm" : s === "Low" ? "good" : "neutral");
  const sentTone = (s: string | null) => (s === "positive" ? "good" : s === "negative" ? "bad" : "neutral");
  const fmtAgo = (iso: string | null) => {
    if (!iso) return "—";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
</script>

<div class="page-head">
  <h1>Intelligence</h1>
  <div class="sub">Market regime, geopolitical threats, and news — the world-awareness layer</div>
</div>

<div class="grid">
  <div class="span-4">
    <Panel title="Market Regime" meta={regime?.spy_trend ?? ""}>
      {#if regime}
        <div class="regime-label">{regime.label}</div>
        <div class="regime-risk">risk: {regime.risk}</div>
        <p class="regime-rec">{regime.recommendation}</p>
        {#if regime.spy_last != null}
          <div class="spy-grid">
            <span>SPY <b class="num">{regime.spy_last}</b></span>
            <span>RSI <b class="num">{regime.spy_rsi}</b></span>
            <span>ADX <b class="num">{regime.spy_adx}</b></span>
            <span>DD <b class="num">{regime.spy_drawdown_pct}%</b></span>
          </div>
        {/if}
      {:else}
        <div class="empty">Regime unavailable</div>
      {/if}
    </Panel>
  </div>

  <div class="span-8">
    <Panel title="Threat Map" dotColor="var(--critical)" meta="{threats.length} active" noPad>
      <ThreatMap {threats} />
    </Panel>
  </div>

  <div class="span-6">
    <Panel title="Active Threats" meta="{threats.length} active">
      <div class="list">
        {#each threats as t (t.id)}
          <div class="row">
            <Pill label={t.severity} tone={sevTone(t.severity)} />
            <div class="row-main">
              <div class="row-title">{t.title}</div>
              <div class="row-meta">{t.country || t.region || "Global"} &middot; {fmtAgo(t.published_at)}</div>
            </div>
          </div>
        {:else}
          <div class="empty">No active threats</div>
        {/each}
      </div>
    </Panel>
  </div>

  <div class="span-6">
    <Panel title="News" meta="{news.length} items">
      <div class="list">
        {#each news as n (n.id)}
          <div class="row">
            <Pill label={n.sentiment ?? "neutral"} tone={sentTone(n.sentiment)} />
            <div class="row-main">
              <div class="row-title">{n.title}</div>
              <div class="row-meta">{n.source} &middot; {fmtAgo(n.published_at)}</div>
            </div>
          </div>
        {:else}
          <div class="empty">No recent news</div>
        {/each}
      </div>
    </Panel>
  </div>

  <div class="span-12">
    <Panel title="Market Watchlist" meta="{marketTab === 'equities' ? equities.length : crypto.length} shown">
      {#snippet children()}
        <div class="mtabs">
          <button class="mtab" class:on={marketTab === "equities"} onclick={() => (marketTab = "equities")}>Equities</button>
          <button class="mtab" class:on={marketTab === "crypto"} onclick={() => (marketTab = "crypto")}>Crypto</button>
        </div>
        <table class="tbl">
          <thead>
            <tr><th>Symbol</th><th>Name</th><th>Price</th><th>Change</th></tr>
          </thead>
          <tbody>
            {#each marketTab === "equities" ? equities : crypto as a (a.symbol)}
              <tr>
                <td class="sym">{a.symbol}</td>
                <td class="name">{a.name}</td>
                <td class="num">{a.price}</td>
                <td class="num {a.change_percent >= 0 ? 'pl-up' : 'pl-down'}">{a.change_percent >= 0 ? "+" : ""}{a.change_percent?.toFixed(2)}%</td>
              </tr>
            {:else}
              <tr><td colspan="4" class="empty">No market data cached yet</td></tr>
            {/each}
          </tbody>
        </table>
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
  .span-6 {
    grid-column: span 6;
  }
  .span-8 {
    grid-column: span 8;
  }
  .span-12 {
    grid-column: span 12;
  }

  .regime-label {
    font-size: 20px;
    font-weight: 650;
  }
  .regime-risk {
    font-size: 11px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-top: 2px;
  }
  .regime-rec {
    font-size: 12px;
    color: var(--ink-dim);
    margin: 10px 0;
    line-height: 1.5;
  }
  .spy-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    font-size: 11.5px;
    color: var(--ink-dim);
  }

  .list {
    display: flex;
    flex-direction: column;
    max-height: 340px;
    overflow-y: auto;
  }
  .row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid var(--line);
  }
  .row:last-child {
    border-bottom: none;
  }
  .row-main {
    min-width: 0;
  }
  .row-title {
    font-size: 12.5px;
    line-height: 1.4;
  }
  .row-meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    margin-top: 3px;
  }

  .mtabs {
    display: flex;
    gap: 4px;
    margin-bottom: 10px;
  }
  .mtab {
    background: none;
    border: 1px solid var(--line-bright);
    color: var(--ink-dim);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .mtab.on {
    color: var(--accent);
    border-color: var(--accent);
    background: rgba(124, 154, 255, 0.1);
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
  .name {
    color: var(--ink-faint);
    font-size: 11px;
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
    padding: 20px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }

  @media (max-width: 1180px) {
    .span-4,
    .span-6,
    .span-8 {
      grid-column: span 12;
    }
  }
</style>
