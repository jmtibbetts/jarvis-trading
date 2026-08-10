<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import EquityChart from "../components/EquityChart.svelte";
  import ThreatMap from "../components/ThreatMap.svelte";
  import RadialScore from "../components/RadialScore.svelte";
  import Pill from "../components/Pill.svelte";
  import { api, type Signal, type Threat, type PositionsResponse, type EquityPoint, type JobStatusMap, type Regime } from "../api";
  import { wsStore } from "../stores/ws.svelte";

  let signals = $state<Signal[]>([]);
  let threats = $state<Threat[]>([]);
  let positionsResp = $state<PositionsResponse | null>(null);
  let equity = $state<EquityPoint[]>([]);
  let jobs = $state<JobStatusMap>({});
  let regime = $state<Regime | null>(null);
  let winRate = $state<number | null>(null);
  let profitFactor = $state<number | null>(null);
  let sharpe = $state<number | null>(null);
  let maxDrawdown = $state<number | null>(null);
  let news = $state<{ title: string; sentiment: string | null; source: string }[]>([]);
  let loadError = $state<string | null>(null);

  async function loadAll() {
    try {
      const [sigRes, threatRes, posRes, eqRes, jobRes, regimeRes, perfRes, sigPerfRes, newsRes] = await Promise.all([
        api.signals("Active", 8),
        api.threats(8),
        api.positions().catch(() => null), // Alpaca may be unreachable/unconfigured — degrade gracefully
        api.equity(24 * 7),
        api.jobStatus(),
        api.regime().catch(() => null),
        api.performanceAnalytics(30).catch(() => null),
        fetch("/api/signals/performance").then((r) => r.json()).catch(() => null),
        api.news(12).catch(() => []),
      ]);
      signals = sigRes.sort((a, b) => (b.composite_score ?? b.confidence ?? 0) - (a.composite_score ?? a.confidence ?? 0)).slice(0, 6);
      threats = threatRes;
      positionsResp = posRes;
      equity = eqRes;
      jobs = jobRes;
      regime = regimeRes;
      sharpe = perfRes?.sharpe_ratio ?? null;
      maxDrawdown = perfRes?.max_drawdown_pct ?? null;
      winRate = sigPerfRes?.summary?.hit_rate ?? null;
      profitFactor = sigPerfRes?.summary?.profit_factor ?? null;
      news = newsRes;
      loadError = null;
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    }
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 30_000);
    const unsub = wsStore.on("job_status", (msg) => {
      jobs = { ...jobs, ...(msg.data as JobStatusMap) };
    });
    return () => {
      clearInterval(poll);
      unsub();
    };
  });

  const jobEntries = $derived(Object.entries(jobs));
  const fmtUsd = (n: number) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const fmtPct = (n: number | null) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`);
  const fmtAgo = (iso: string | null) => {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
</script>

<div class="page-head">
  <div>
    <h1>Command Center</h1>
    <div class="sub">
      {#if regime}<span>{regime.label} &middot;</span>{/if}
      Live + paper &middot; last sync <span class="num">just now</span>
    </div>
  </div>
</div>

{#if loadError}
  <div class="err">Some data failed to load: {loadError}</div>
{/if}

<div class="grid">
  <div class="kpis">
    <KpiTile label="Win Rate" value={winRate != null ? `${winRate.toFixed(1)}%` : "—"} period="signals" />
    <KpiTile label="Sharpe" value={sharpe != null ? sharpe.toFixed(2) : "—"} />
    <KpiTile
      label="Max Drawdown"
      value={maxDrawdown != null ? `${maxDrawdown.toFixed(1)}%` : "—"}
      trend={maxDrawdown != null && maxDrawdown < 0 ? "down" : "neutral"}
    />
    <KpiTile label="Profit Factor" value={profitFactor != null ? profitFactor.toFixed(2) : "—"} />
    <KpiTile label="Open Positions" value={String(positionsResp?.positions.length ?? "—")} />
    <KpiTile
      label="Unrealized P&L"
      value={positionsResp ? fmtUsd(positionsResp.account.unrealized_pl) : "—"}
      trend={positionsResp ? (positionsResp.account.unrealized_pl >= 0 ? "up" : "down") : "neutral"}
    />
  </div>

  <div class="span-8">
    <Panel title="Equity Curve" meta="{equity.length} snapshots &middot; 7d">
      <EquityChart points={equity} />
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Threat Intelligence" dotColor="var(--critical)" meta="{threats.length} active" noPad>
      <ThreatMap {threats} />
      <div class="map-legend">
        <span><i style="background:var(--critical)"></i> Critical</span>
        <span><i style="background:var(--warm)"></i> High</span>
        <span><i style="background:var(--accent)"></i> Medium</span>
      </div>
    </Panel>
  </div>

  <div class="span-5">
    <Panel title="Active Signals" meta="top {signals.length} by score">
      <div class="sig-list">
        {#each signals as sig (sig.id)}
          <div class="sig">
            <RadialScore score={Math.round(sig.composite_score ?? sig.confidence ?? 0)} />
            <div>
              <div class="sig-sym">
                {sig.asset_symbol}
                <Pill label={sig.direction} tone={sig.direction.toLowerCase().includes("short") ? "bad" : "good"} />
              </div>
              <div class="sig-meta num">entry {sig.entry_price ?? "—"} &middot; {sig.timeframe ?? "—"}</div>
            </div>
            <div class="sig-rr">
              <div class="lbl">R:R</div>
              <span class="num">{sig.rr_ratio != null ? `${sig.rr_ratio}:1` : "—"}</span>
            </div>
          </div>
        {:else}
          <div class="empty">No active signals right now</div>
        {/each}
      </div>
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Open Positions" meta="{positionsResp?.positions.length ?? 0} open">
      {#if positionsResp && positionsResp.positions.length}
        <table class="pos">
          <thead>
            <tr><th>Sym</th><th>Side</th><th>P&amp;L</th></tr>
          </thead>
          <tbody>
            {#each positionsResp.positions as p (p.symbol)}
              <tr>
                <td class="sym">{p.symbol}</td>
                <td>{p.side}</td>
                <td class={p.unrealized_plpc >= 0 ? "pl-up" : "pl-down"}>{fmtPct(p.unrealized_plpc)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <div class="empty">No open positions</div>
      {/if}
    </Panel>
  </div>

  <div class="span-3">
    <Panel title="Job Health" meta="{jobEntries.filter(([, j]) => j.status === 'ok').length}/{jobEntries.length}">
      <div class="ops-list">
        {#each jobEntries as [name, job] (name)}
          <div class="op-row">
            <span class="d {job.status}"></span>
            <span class="name">{name}</span>
            <span class="t num">{fmtAgo(job.last)}</span>
          </div>
        {/each}
      </div>
    </Panel>
  </div>

  <div class="ticker">
    <div class="lbl">WIRE</div>
    <div class="ticker-track">
      {#each news.concat(news) as n, i (i)}
        <span>{n.title}</span>
      {:else}
        <span>No recent news</span>
      {/each}
    </div>
  </div>
</div>

<style>
  .page-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 16px;
  }
  .page-head h1 {
    font-size: 19px;
    margin: 0;
    font-weight: 650;
    letter-spacing: -0.01em;
  }
  .sub {
    font-size: 12px;
    color: var(--ink-faint);
  }
  .err {
    background: rgba(255, 92, 114, 0.08);
    border: 1px solid rgba(255, 92, 114, 0.3);
    color: var(--bad);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 12px;
    margin-bottom: 12px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
    align-items: start;
  }
  .kpis {
    grid-column: span 12;
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 14px;
  }
  .span-8 {
    grid-column: span 8;
  }
  .span-5 {
    grid-column: span 5;
  }
  .span-4 {
    grid-column: span 4;
  }
  .span-3 {
    grid-column: span 3;
  }

  .map-legend {
    display: flex;
    gap: 12px;
    padding: 9px 14px;
    border-top: 1px solid var(--line);
    font-size: 10.5px;
    color: var(--ink-dim);
  }
  .map-legend span {
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .map-legend i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .sig-list {
    display: flex;
    flex-direction: column;
    margin: -14px;
  }
  .sig {
    display: grid;
    grid-template-columns: 40px 1fr auto;
    gap: 11px;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--line);
  }
  .sig:last-child {
    border-bottom: none;
  }
  .sig-sym {
    font-weight: 650;
    font-size: 13px;
    display: flex;
    align-items: center;
    gap: 7px;
  }
  .sig-meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    margin-top: 3px;
  }
  .sig-rr {
    text-align: right;
    font-size: 12px;
  }
  .sig-rr .lbl {
    font-size: 9px;
    color: var(--ink-faint);
    letter-spacing: 0.06em;
  }
  .empty {
    padding: 20px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }

  table.pos {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin: -14px;
    width: calc(100% + 28px);
  }
  table.pos th {
    text-align: left;
    font-size: 9.5px;
    letter-spacing: 0.08em;
    color: var(--ink-faint);
    font-weight: 600;
    padding: 9px 14px;
    border-bottom: 1px solid var(--line);
  }
  table.pos td {
    padding: 9px 14px;
    border-bottom: 1px solid var(--line);
    font-family: var(--mono);
  }
  table.pos tr:last-child td {
    border-bottom: none;
  }
  .sym {
    font-family: var(--sans) !important;
    font-weight: 600;
  }
  .pl-up {
    color: var(--good);
  }
  .pl-down {
    color: var(--bad);
  }

  .ops-list {
    display: flex;
    flex-direction: column;
    margin: -14px;
  }
  .op-row {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 6px 14px;
    font-size: 11.5px;
  }
  .op-row .d {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex: none;
    background: var(--ink-faint);
  }
  .op-row .d.ok {
    background: var(--good);
    box-shadow: 0 0 6px var(--good);
  }
  .op-row .d.running {
    background: var(--warm);
    box-shadow: 0 0 6px var(--warm);
    animation: pulse 1.4s ease-in-out infinite;
  }
  .op-row .d.error {
    background: var(--bad);
    box-shadow: 0 0 6px var(--bad);
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }
  .op-row .name {
    flex: 1;
    color: var(--ink-dim);
  }
  .op-row .t {
    font-size: 10px;
    color: var(--ink-faint);
  }

  .ticker {
    grid-column: span 12;
    display: flex;
    align-items: center;
    gap: 10px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface);
    overflow: hidden;
  }
  .ticker .lbl {
    flex: none;
    padding: 9px 12px;
    font-size: 9.5px;
    letter-spacing: 0.1em;
    color: var(--critical);
    border-right: 1px solid var(--line);
    font-weight: 700;
    background: rgba(255, 56, 100, 0.06);
  }
  .ticker-track {
    display: flex;
    gap: 34px;
    white-space: nowrap;
    animation: marquee 40s linear infinite;
    padding: 9px 0;
  }
  .ticker-track span {
    font-size: 11.5px;
    color: var(--ink-dim);
  }
  @keyframes marquee {
    from {
      transform: translateX(0);
    }
    to {
      transform: translateX(-50%);
    }
  }

  @media (max-width: 1180px) {
    .kpis {
      grid-template-columns: repeat(3, 1fr);
    }
    .span-8,
    .span-5,
    .span-4,
    .span-3 {
      grid-column: span 12;
    }
  }
</style>
