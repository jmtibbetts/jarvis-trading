<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import EquityChart from "../components/EquityChart.svelte";
  import ThreatMap from "../components/ThreatMap.svelte";
  import RadialScore from "../components/RadialScore.svelte";
  import Pill from "../components/Pill.svelte";
  import SignalAnalysisModal from "../components/SignalAnalysisModal.svelte";
  import { api, type Signal, type Threat, type PositionsResponse, type EquityPoint, type JobStatusMap, type Regime, type RankedOpportunity, type CatalystCalendar, type EnrichedWatchlist, type AnalystAnswer } from "../api";
  import { wsStore } from "../stores/ws.svelte";
  import { linkStore } from "../stores/link.svelte";
  import { sectionStore } from "../stores/section.svelte";

  let analysisSignalId = $state<string | null>(null);
  let signals = $state<Signal[]>([]);
  let opportunities = $state<RankedOpportunity[]>([]);
  let catalysts = $state<CatalystCalendar | null>(null);
  let watchlist = $state<EnrichedWatchlist | null>(null);
  let analystQ = $state("");
  let analystBusy = $state(false);
  let analystAnswer = $state<AnalystAnswer | null>(null);
  let analystError = $state<string | null>(null);
  let expandedOpp = $state<string | null>(null);
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
      const [sigRes, threatRes, posRes, eqRes, jobRes, regimeRes, perfRes, sigPerfRes, newsRes, oppRes, catRes, wlRes] = await Promise.all([
        api.signals("Active", 8),
        api.threats(8),
        api.positions().catch(() => null), // Alpaca may be unreachable/unconfigured — degrade gracefully
        api.equity(24 * 7),
        api.jobStatus(),
        api.regime().catch(() => null),
        api.performanceAnalytics(30).catch(() => null),
        fetch("/api/signals/performance").then((r) => r.json()).catch(() => null),
        api.news(12).catch(() => []),
        api.opportunitiesRanked(8).catch(() => []),
        api.catalystCalendar().catch(() => null),
        api.enrichedWatchlist(25).catch(() => null),
      ]);
      signals = sigRes.sort((a, b) => (b.composite_score ?? b.confidence ?? 0) - (a.composite_score ?? a.confidence ?? 0)).slice(0, 6);
      opportunities = oppRes;
      catalysts = catRes;
      watchlist = wlRes;
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

  async function askAnalyst() {
    const q = analystQ.trim();
    if (!q || analystBusy) return;
    analystBusy = true;
    analystError = null;
    try {
      analystAnswer = await api.askAnalyst(q);
    } catch (e) {
      analystError = e instanceof Error ? e.message : String(e);
    } finally {
      analystBusy = false;
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

{#if analysisSignalId}
  <SignalAnalysisModal signalId={analysisSignalId} onClose={() => (analysisSignalId = null)} />
{/if}

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
  <div class="span-7">
    <Panel title="Active Signals" meta="top {signals.length} by score">
      <div class="sig-list">
        {#each signals as sig (sig.id)}
          <div
            class="sig"
            onclick={() => (analysisSignalId = sig.id)}
            onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (analysisSignalId = sig.id)}
            role="button"
            tabindex="0"
          >
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
  <div class="span-5">
    <Panel title="Threat Intelligence" dotColor="var(--critical)" meta="{threats.length} active" noPad>
      <ThreatMap {threats} />
      <div class="map-legend">
        <span><i style="background:var(--critical)"></i> Critical</span>
        <span><i style="background:var(--warm)"></i> High</span>
        <span><i style="background:var(--accent)"></i> Medium</span>
      </div>
    </Panel>
  </div>
  <div class="span-4">
    <Panel title="Top Opportunities" meta="JARVIS Opportunity Score &middot; {opportunities.length} ranked">
      <div class="sig-list cap-h">
        {#each opportunities as opp (opp.signal_id)}
          <div
            class="sig opp"
            onclick={() => (expandedOpp = expandedOpp === opp.signal_id ? null : opp.signal_id)}
            onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (expandedOpp = expandedOpp === opp.signal_id ? null : opp.signal_id)}
            role="button"
            tabindex="0"
          >
            <RadialScore score={Math.round(opp.opportunity_score)} />
            <div>
              <div class="sig-sym">
                {opp.symbol}
                <Pill label={opp.direction} tone={opp.direction.toLowerCase().includes("short") ? "bad" : "good"} />
                {#if opp.smart_money?.alignment_score != null}
                  <Pill
                    label="smart money {opp.smart_money.agreement}"
                    tone={opp.smart_money.alignment_score > 55 ? "good" : opp.smart_money.alignment_score < 45 ? "bad" : "neutral"}
                  />
                {/if}
                {#if opp.anomaly?.flags.length}
                  <Pill label="{opp.anomaly.flags.length} anomaly" tone="warm" />
                {/if}
              </div>
              <div class="sig-meta num">
                base {opp.base_composite_score.toFixed(0)} &middot; {opp.timeframe ?? "—"}
                {#if opp.opportunity_breakdown.smart_money_adjustment !== 0}
                  &middot; smart money {opp.opportunity_breakdown.smart_money_adjustment > 0 ? "+" : ""}{opp.opportunity_breakdown.smart_money_adjustment.toFixed(1)}
                {/if}
              </div>
              {#if expandedOpp === opp.signal_id}
                <div class="opp-detail">
                  <div>{opp.opportunity_breakdown.smart_money_note}</div>
                  <div>{opp.opportunity_breakdown.historical_note}</div>
                  {#if opp.crypto_context}
                    <div>
                      Funding {opp.crypto_context.funding_rate != null ? `${(opp.crypto_context.funding_rate * 100).toFixed(3)}%` : "—"}
                      &middot; L/S ratio {opp.crypto_context.long_short_ratio?.toFixed(2) ?? "—"}
                    </div>
                  {/if}
                  {#each opp.anomaly?.flags ?? [] as f (f.flag)}
                    <div class="anomaly-line">⚠ {f.detail}</div>
                  {/each}
                </div>
              {/if}
            </div>
            <div class="sig-rr">
              <div class="lbl">Opportunity</div>
              <span class="num">{opp.opportunity_score.toFixed(1)}</span>
            </div>
          </div>
        {:else}
          <div class="empty">No ranked opportunities right now</div>
        {/each}
      </div>
    </Panel>
  </div>
  <div class="span-8">
    <Panel title="Watchlist 2.0" meta={watchlist ? `${watchlist.rows.length} symbols · fused intelligence` : "—"}>
      {#if watchlist && watchlist.rows.length}
        <div class="wl-scroll cap-h">
          <table class="wl">
            <thead>
              <tr><th>Sym</th><th>Price</th><th>Chg</th><th>Signal</th><th>Insider</th><th>Congress 90d</th><th>13F</th><th>Dark Pool</th></tr>
            </thead>
            <tbody>
              {#each watchlist.rows as r (r.symbol)}
                <tr>
                  <td class="sym">
                    <button
                      class="wl-sym"
                      title="Link {r.symbol} across windows and open Signals"
                      onclick={() => { linkStore.link(r.symbol); sectionStore.go("signals"); }}
                    >{r.symbol}</button>
                  </td>
                  <td class="num">{r.price != null ? (r.price > 1000 ? r.price.toLocaleString(undefined, { maximumFractionDigits: 0 }) : r.price.toFixed(2)) : "—"}</td>
                  <td class="num {r.change_percent == null ? '' : r.change_percent >= 0 ? 'pl-up' : 'pl-down'}">{fmtPct(r.change_percent)}</td>
                  <td class="num">
                    {#if r.signal}{r.signal.composite_score?.toFixed(0) ?? "—"}
                      <span class="dim">{r.signal.direction?.[0] ?? ""}</span>{:else}—{/if}
                  </td>
                  <td>
                    {#if r.insider_flags.length}
                      <Pill
                        label={r.insider_flags[0].replaceAll("_", " ").toLowerCase()}
                        tone={r.insider_flags[0].includes("BUY") || r.insider_flags[0].includes("OFFICER") ? "good" : "bad"}
                      />
                    {:else}<span class="dim">—</span>{/if}
                  </td>
                  <td class="num">
                    {#if r.congress_90d}
                      <span class={r.congress_90d.purchases > r.congress_90d.sales ? "pl-up" : r.congress_90d.sales > r.congress_90d.purchases ? "pl-down" : ""}>
                        {r.congress_90d.purchases}P/{r.congress_90d.sales}S
                      </span>
                    {:else}<span class="dim">—</span>{/if}
                  </td>
                  <td class="num">{r.institutional_holders ?? "—"}</td>
                  <td>{#if r.in_dark_pool_top}<Pill label="top 100" tone="warm" />{:else}<span class="dim">—</span>{/if}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <div class="wl-note">{watchlist.note}</div>
      {:else}
        <div class="empty">Watchlist unavailable</div>
      {/if}
    </Panel>
  </div>
  <div class="span-8">
    <Panel title="Ask JARVIS" meta="analyst over system data · cites its sources">
      <form
        class="ask-row"
        onsubmit={(e) => {
          e.preventDefault();
          askAnalyst();
        }}
      >
        <input
          class="ask-input"
          type="text"
          maxlength="500"
          placeholder="e.g. What threatens my current positions right now?"
          bind:value={analystQ}
          disabled={analystBusy}
        />
        <button class="ask-btn" type="submit" disabled={analystBusy || !analystQ.trim()}>
          {analystBusy ? "Thinking…" : "Ask"}
        </button>
      </form>
      {#if analystError}
        <div class="err">Analyst unavailable: {analystError}</div>
      {:else if analystAnswer}
        <div class="ask-answer">{analystAnswer.answer}</div>
        <div class="wl-note">
          Context: {analystAnswer.context_used.join(", ")} — answers come only from system data; verify numbers against their panels.
        </div>
      {:else}
        <div class="ask-hint dim">
          One model call per question, grounded in the system's own data (regime, psychology, opportunities, portfolio risk, alerts).
        </div>
      {/if}
    </Panel>
  </div>
  <div class="span-4">
    <Panel title="Catalyst Calendar" meta={catalysts ? `${catalysts.events.length} upcoming` : "—"}>
      {#if catalysts && catalysts.events.length}
        <div class="cat-list">
          {#each catalysts.events.slice(0, 9) as e (e.type + e.date + (e.title ?? ""))}
            <div class="cat-item">
              <div class="cat-date num">{e.date.slice(5)}</div>
              <div>
                <div class="cat-title">{e.title}</div>
                {#if e.tickers?.length}
                  <div class="cat-meta dim">{e.tickers.slice(0, 8).join(", ")}{e.tickers.length > 8 ? "…" : ""}</div>
                {:else if e.days_away != null}
                  <div class="cat-meta dim">{e.days_away === 0 ? "this week" : `in ${e.days_away}d`}{e.approximation ? " · approx" : ""}</div>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      {:else}
        <div class="empty">No upcoming catalysts assembled</div>
      {/if}
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
  .cap-h {
    /* Cap panel body height so tall lists scroll INSIDE their panel instead
       of stretching the whole grid row — the source of the giant blank areas
       beside shorter panels. Height chosen to align the opportunities and
       watchlist rows. */
    max-height: 480px;
    overflow-y: auto;
  }
  .cap-h table thead th {
    position: sticky;
    top: 0;
    background: var(--surface);
    z-index: 1;
  }
  .span-7 {
    grid-column: span 7;
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
    cursor: pointer;
  }
  .sig:hover {
    background: rgba(124, 154, 255, 0.05);
  }
  .sig:last-child {
    border-bottom: none;
  }
  .opp-detail {
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px dashed var(--line);
    font-size: 10.5px;
    color: var(--ink-dim);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .anomaly-line {
    color: var(--warm);
  }
  .dim {
    color: var(--ink-faint);
  }
  .wl-scroll {
    overflow-x: auto;
  }
  table.wl {
    width: 100%;
    border-collapse: collapse;
    font-size: 11.5px;
  }
  table.wl th {
    text-align: left;
    font-size: 9px;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--ink-faint);
    padding: 4px 8px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  table.wl td {
    padding: 5px 8px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  table.wl tr:last-child td {
    border-bottom: none;
  }
  .wl-sym {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    font-weight: 650;
    color: var(--ink);
    cursor: pointer;
  }
  .wl-sym:hover {
    color: var(--accent);
    text-decoration: underline;
  }
  .wl-note {
    margin-top: 8px;
    font-size: 10px;
    line-height: 1.5;
    color: var(--ink-faint);
  }
  .ask-row {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  .ask-input {
    flex: 1;
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-radius: 6px;
    color: var(--ink);
    font: inherit;
    font-size: 12px;
    padding: 7px 10px;
  }
  .ask-input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .ask-btn {
    background: rgba(124, 154, 255, 0.14);
    border: 1px solid var(--accent-dim);
    border-radius: 6px;
    color: var(--accent);
    font: inherit;
    font-size: 12px;
    font-weight: 650;
    padding: 7px 16px;
    cursor: pointer;
  }
  .ask-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .ask-answer {
    font-size: 12px;
    line-height: 1.65;
    white-space: pre-wrap;
    max-height: 260px;
    overflow-y: auto;
  }
  .ask-hint {
    font-size: 11px;
    line-height: 1.5;
  }
  .cat-list {
    display: flex;
    flex-direction: column;
  }
  .cat-item {
    display: grid;
    grid-template-columns: 44px 1fr;
    gap: 9px;
    padding: 6px 0;
    border-bottom: 1px solid var(--line);
    align-items: baseline;
  }
  .cat-item:last-child {
    border-bottom: none;
  }
  .cat-date {
    font-size: 11px;
    font-weight: 700;
    color: var(--accent);
  }
  .cat-title {
    font-size: 11.5px;
    line-height: 1.4;
  }
  .cat-meta {
    font-size: 10px;
    margin-top: 1px;
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

  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
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
    .span-7,
    .span-5,
    .span-4 {
      grid-column: span 12;
    }
  }
</style>
