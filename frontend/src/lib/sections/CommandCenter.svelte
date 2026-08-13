<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import EquityChart from "../components/EquityChart.svelte";
  import RadialScore from "../components/RadialScore.svelte";
  import Pill from "../components/Pill.svelte";
  import SignalAnalysisModal from "../components/SignalAnalysisModal.svelte";
  import { api, type Signal, type Threat, type PositionsResponse, type EquityPoint, type JobStatusMap, type Regime, type RankedOpportunity, type CatalystCalendar, type EnrichedWatchlist, type AnalystAnswer, type PsychologyIndex } from "../api";
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
  let focus = $state<Awaited<ReturnType<typeof api.focusList>> | null>(null);
  let focusSym = $state("");
  let focusNote = $state("");
  let focusBusy = $state(false);
  let expandedFocus = $state<string | null>(null);

  async function loadFocus() {
    focus = await api.focusList().catch(() => focus);
  }

  // ── On-demand focus scan, per coin ──────────────────────────────────
  // The scheduled sweep decides when it looks; this answers "is anything
  // ready right now?" for ONE coin. It runs the SAME pipeline narrowed to
  // that symbol, so what it finds is graded by the rules that would trade
  // it — and asking about one coin does not spend an LLM call on the rest.
  //
  // State is keyed by symbol so two coins can be interrogated at once and
  // each row shows only its own progress.
  type ScanState = {
    busy: boolean; pct: number; phase: string; elapsed: string;
    result: { new_signals: number } | null; error: string | null;
  };
  let scans = $state<Record<string, ScanState>>({});
  const scanOf = (sym: string): ScanState =>
    scans[sym] ?? { busy: false, pct: 0, phase: "", elapsed: "0.0", result: null, error: null };

  // The work the server actually does, with the elapsed time each reaches.
  // The bar holds short of full until the response lands — a focus sweep is
  // several LLM calls and the client cannot know when they finish.
  const SCAN_PHASES: [number, string][] = [
    [0, "reading cached TA and indicators"],
    [3_000, "loading focus profiles"],
    [7_000, "gathering threat, news and regime context"],
    [12_000, "asking the model for setups"],
    [30_000, "scoring, level and cost gates"],
  ];

  function setScan(sym: string, patch: Partial<ScanState>) {
    scans = { ...scans, [sym]: { ...scanOf(sym), ...patch } };
  }

  async function scanFocusNow(sym: string) {
    if (scanOf(sym).busy) return;
    const { toastStore } = await import("../stores/toast.svelte");
    setScan(sym, { busy: true, pct: 0, result: null, error: null });
    const t0 = Date.now();
    const tick = setInterval(() => {
      const ms = Date.now() - t0;
      const span = SCAN_PHASES[SCAN_PHASES.length - 1][0];
      setScan(sym, {
        elapsed: (ms / 1000).toFixed(1),
        phase: ([...SCAN_PHASES].reverse().find(([at]) => ms >= at) ?? SCAN_PHASES[0])[1],
        pct: ms < span ? (ms / span) * 70 : 90 - 20 * Math.exp(-(ms - span) / 25_000),
      });
    }, 200);
    try {
      const started = await api.scanFocus(sym);
      if (started.status === "already_running") toastStore.ok(`${sym}: a scan is already running`);
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500));
        const st = await api.focusScanStatus(sym);
        if (st.running) continue;
        if (st.error) throw new Error(st.error);
        setScan(sym, { result: st.result });
        break;
      }
      setScan(sym, { pct: 100 });
      const found = scanOf(sym).result?.new_signals ?? 0;
      toastStore.ok(found > 0
        ? `${sym}: ${found} new signal${found === 1 ? "" : "s"}`
        : `${sym}: no setup cleared the focus score floor`);
      await loadFocus();
    } catch (e) {
      setScan(sym, { error: e instanceof Error ? e.message : String(e) });
      toastStore.err(`${sym} scan failed: ${scanOf(sym).error}`);
    } finally {
      clearInterval(tick);
      setScan(sym, { busy: false });
    }
  }

  async function addFocus() {
    const sym = focusSym.trim().toUpperCase();
    if (!sym || focusBusy) return;
    focusBusy = true;
    try {
      const { toastStore } = await import("../stores/toast.svelte");
      // A focus symbol must already be tracked so prices exist; add it to the
      // watchlist first when it is not, rather than failing in the user's face.
      try {
        await api.setFocus(sym, true, focusNote.trim() || undefined);
      } catch (e) {
        if (String(e).includes("not tracked")) {
          await api.watchlistAdd(sym);
          await api.setFocus(sym, true, focusNote.trim() || undefined);
        } else {
          throw e;
        }
      }
      focusSym = ""; focusNote = "";
      toastStore.ok(`${sym} added to focus watch — profile building`);
      await loadFocus();
    } catch (e) {
      const { toastStore } = await import("../stores/toast.svelte");
      toastStore.err(`Focus add failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      focusBusy = false;
    }
  }

  async function removeFocus(sym: string) {
    try {
      await api.setFocus(sym, false);
      const { toastStore } = await import("../stores/toast.svelte");
      toastStore.ok(`${sym} removed from focus watch`);
      await loadFocus();
    } catch (e) {
      const { toastStore } = await import("../stores/toast.svelte");
      toastStore.err(`Remove failed: ${e}`);
    }
  }

  let newTicker = $state("");
  let addingTicker = $state(false);

  async function addTicker() {
    const sym = newTicker.trim().toUpperCase();
    if (!sym || addingTicker) return;
    addingTicker = true;
    try {
      const res = await api.watchlistAdd(sym);
      newTicker = "";
      const { toastStore } = await import("../stores/toast.svelte");
      toastStore.ok(res.already_tracked ? `${res.symbol} already tracked` : `${res.symbol} added to watchlist`);
      await loadAll();
    } catch (e) {
      const { toastStore } = await import("../stores/toast.svelte");
      toastStore.err(`Add failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      addingTicker = false;
    }
  }
  let expandedOpp = $state<string | null>(null);
  let threats = $state<Threat[]>([]);
  let positionsResp = $state<PositionsResponse | null>(null);
  let equity = $state<EquityPoint[]>([]);
  let equityHours = $state(24 * 7);

  async function setEquityRange(hours: number) {
    equityHours = hours;
    equity = await api.equity(hours).catch(() => equity);
  }
  let jobs = $state<JobStatusMap>({});
  let regime = $state<Regime | null>(null);
  let winRate = $state<number | null>(null);
  let profitFactor = $state<number | null>(null);
  let sharpe = $state<number | null>(null);
  let maxDrawdown = $state<number | null>(null);
  let news = $state<{ title: string; sentiment: string | null; source: string }[]>([]);
  let psychology = $state<PsychologyIndex | null>(null);
  let fxRates = $state<Awaited<ReturnType<typeof api.fxRates>> | null>(null);
  let cryptoMarkets = $state<Awaited<ReturnType<typeof api.cryptoMarkets>> | null>(null);
  let webNews = $state<Awaited<ReturnType<typeof api.webNews>> | null>(null);
  let postmortems = $state<{ window_days: number; total_failures: number; by_reason: Record<string, number> } | null>(null);
  let loadError = $state<string | null>(null);

  async function loadAll() {
    try {
      const [sigRes, threatRes, posRes, eqRes, jobRes, regimeRes, perfRes, sigPerfRes, newsRes, oppRes, catRes, wlRes, psyRes] = await Promise.all([
        api.signals("Active", 8),
        api.threats(8),
        api.positions().catch(() => null), // Alpaca may be unreachable/unconfigured — degrade gracefully
        api.equity(equityHours),
        api.jobStatus(),
        api.regime().catch(() => null),
        api.performanceAnalytics(30).catch(() => null),
        fetch("/api/signals/performance").then((r) => r.json()).catch(() => null),
        api.news(12).catch(() => []),
        api.opportunitiesRanked(8).catch(() => []),
        api.catalystCalendar().catch(() => null),
        api.enrichedWatchlist(25).catch(() => null),
        api.psychology().catch(() => null),
      ]);
      Promise.all([
        api.fxRates().catch(() => null),
        api.cryptoMarkets().catch(() => null),
        api.webNews().catch(() => null),
        fetch("/api/learning/postmortems?days=30").then((r) => r.json()).catch(() => null),
        api.focusList().catch(() => null),
      ]).then(([fx, cg, wn, pm, fc]) => {
        fxRates = fx ?? fxRates;
        cryptoMarkets = cg ?? cryptoMarkets;
        webNews = wn ?? webNews;
        postmortems = pm ?? postmortems;
        focus = fc ?? focus;
      });
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
      psychology = psyRes;
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
  const cryptoMovers = $derived(
    (cryptoMarkets?.coins ?? [])
      .filter((c0) => c0.chg_24h != null)
      .sort((a, b) => Math.abs(b.chg_24h!) - Math.abs(a.chg_24h!))
      .slice(0, 6),
  );
  const fxMajors = $derived((fxRates?.pairs ?? []).slice(0, 5));
  const failureReasons = $derived(
    Object.entries(postmortems?.by_reason ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 5),
  );
  const failureMax = $derived(failureReasons.length ? failureReasons[0][1] : 0);
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
    <KpiTile label="Sharpe" value={sharpe != null && Math.abs(sharpe) <= 20 ? sharpe.toFixed(2) : "—"} />
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
    <KpiTile label="Account Equity" value={positionsResp ? fmtUsd(positionsResp.account.equity) : "—"} />
    <KpiTile label="Buying Power" value={positionsResp ? fmtUsd(positionsResp.account.buying_power) : "—"} />
    <KpiTile
      label="Active Signals"
      value={String(signals.length)}
      period={threats.length ? `${threats.length} threats` : ""}
    />
    <KpiTile
      label="Market Psychology"
      value={psychology?.score != null ? `${Math.round(psychology.score)}` : "—"}
      period={psychology?.label ?? ""}
      trend={psychology?.score != null ? (psychology.score >= 60 ? "up" : psychology.score <= 40 ? "down" : "neutral") : "neutral"}
    />
  </div>

  <div class="span-8">
    <Panel title="Equity Curve" meta="{equity.length} snapshots">
      <EquityChart points={equity} rangeHours={equityHours} onRange={setEquityRange} />
    </Panel>
  </div>
  <div class="span-4">
    <Panel title="Open Positions" meta="{positionsResp?.positions.length ?? 0} open">
      {#if positionsResp && positionsResp.positions.length}
        <div class="pos-scroll">
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
        </div>
      {:else}
        <div class="empty">No open positions</div>
      {/if}
    </Panel>
  </div>
  <div class="span-4">
    <Panel title="Market Movers" meta={cryptoMarkets || fxRates ? "crypto 24h · FX 30d" : "—"}>
      <div class="movers">
        <div class="mv-col">
          <div class="mv-head">CRYPTO — BIGGEST 24H MOVES</div>
          {#if cryptoMovers.length}
            {#each cryptoMovers as c0 (c0.id)}
              <div class="mv-row">
                <span class="sym">{c0.symbol.toUpperCase()}</span>
                <span class="num dim">${c0.price < 1 ? c0.price.toPrecision(3) : c0.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
                <span class="num {c0.chg_24h! >= 0 ? 'pl-up' : 'pl-down'}">{c0.chg_24h! >= 0 ? "+" : ""}{c0.chg_24h!.toFixed(1)}%</span>
              </div>
            {/each}
          {:else}
            <div class="empty">no data yet</div>
          {/if}
        </div>
        <div class="mv-col">
          <div class="mv-head">FX MAJORS — LIVE INTERBANK</div>
          {#if fxMajors.length}
            {#each fxMajors as fp (fp.pair)}
              <div class="mv-row">
                <span class="sym">{fp.pair}</span>
                <span class="num dim">{fp.rate ?? "—"}</span>
                <span class="num {(fp.change_pct ?? 0) >= 0 ? 'pl-up' : 'pl-down'}">{fp.change_pct != null ? `${fp.change_pct >= 0 ? "+" : ""}${fp.change_pct.toFixed(2)}%` : "—"}</span>
              </div>
            {/each}
          {:else}
            <div class="empty">no data yet</div>
          {/if}
        </div>
      </div>
    </Panel>
  </div>
  <div class="span-4">
    <Panel title="Live Web Pulse" meta={webNews?.as_of ? `refreshed ${new Date(webNews.as_of).toLocaleTimeString()}` : "—"}>
      {#if webNews && webNews.items.length}
        <div class="pulse">
          {#each webNews.items.slice(0, 5) as it, i (i)}
            <div class="pulse-row">
              <div class="pulse-title">{it.title}</div>
              {#if it.snippet}<div class="pulse-snippet dim">{it.snippet}</div>{/if}
            </div>
          {/each}
        </div>
        <div class="pulse-note dim">Unverified live web search — the same block injected into signal-generation prompts.</div>
      {:else}
        <div class="empty">Populates on the next signal-generation run</div>
      {/if}
    </Panel>
  </div>
  <div class="span-4">
    <Panel title="Learning Loop" meta={postmortems ? `${postmortems.total_failures} failures analyzed · ${postmortems.window_days}d` : "—"}>
      {#if failureReasons.length}
        {#each failureReasons as [reason, count] (reason)}
          <div class="fail-row">
            <span class="fail-label">{reason.replaceAll("_", " ").toLowerCase()}</span>
            <span class="fail-bar"><span class="fail-fill" style={`width:${failureMax ? (count / failureMax) * 100 : 0}%`}></span></span>
            <span class="num dim">{count}</span>
          </div>
        {/each}
        <div class="pulse-note dim">Why signals died, classified deterministically — feeds the scoring penalty so repeated failure patterns get downranked.</div>
      {:else}
        <div class="empty">No failures recorded in this window</div>
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
    <Panel title="Threat Intelligence" dotColor="var(--critical)" meta="{threats.length} active · map on Intelligence tab">
      {#if threats.length}
        <div class="threat-list">
          {#each threats.slice(0, 6) as t (t.id)}
            <div class="threat-row">
              <Pill label={t.severity} tone={t.severity === "Critical" ? "critical" : t.severity === "High" ? "warm" : "neutral"} />
              <div class="threat-main">
                <div class="threat-title">{t.title}</div>
                <div class="threat-meta dim">{t.country || t.region || "Global"} · {fmtAgo(t.published_at)}</div>
              </div>
            </div>
          {/each}
        </div>
      {:else}
        <div class="empty">No active threats</div>
      {/if}
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
                {#if opp.opportunity_breakdown.historical_adjustment !== 0}
                  &middot; history {opp.opportunity_breakdown.historical_adjustment > 0 ? "+" : ""}{opp.opportunity_breakdown.historical_adjustment.toFixed(1)}
                {/if}
              </div>
              <div class="opp-why dim">
                {opp.opportunity_breakdown.smart_money_note}{opp.historical ? ` · ${opp.opportunity_breakdown.historical_note}` : ""}
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
                  <div class="opp-actions">
                    <button
                      class="btn tiny"
                      onclick={(e) => { e.stopPropagation(); analysisSignalId = opp.signal_id; }}
                    >Full Analysis</button>
                    <button
                      class="btn tiny outline"
                      title="Open on the Signals tab — approve, execute, verify, or paper-trade it there"
                      onclick={(e) => { e.stopPropagation(); linkStore.link(opp.symbol); sectionStore.go("signals"); }}
                    >Trade →</button>
                  </div>
                </div>
              {/if}
            </div>
            <div class="sig-rr">
              <div class="lbl">Opportunity <span class="caret">{expandedOpp === opp.signal_id ? "▾" : "▸"}</span></div>
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
    <Panel
      title="Coins to Watch"
      dotColor="var(--warm)"
      meta={focus ? `${focus.focus.length} under focus · signals only at score ${focus.min_score}+` : "—"}
    >
      <form class="wl-add" onsubmit={(e) => { e.preventDefault(); addFocus(); }}>
        <input placeholder="Symbol (BEAT/USD)" bind:value={focusSym} disabled={focusBusy} />
        <input placeholder="Why watch it? (optional)" bind:value={focusNote} disabled={focusBusy} />
        <button class="ask-btn" type="submit" disabled={focusBusy || !focusSym.trim()}>
          {focusBusy ? "Adding…" : "+ Watch"}
        </button>
      </form>


      {#if focus && focus.focus.length}
        {#each focus.focus as f (f.symbol)}
          <div class="fc-row">
            <div class="fc-head">
              <button class="fc-sym" onclick={() => { linkStore.link(f.symbol); sectionStore.go("signals"); }}>{f.symbol}</button>
              {#if f.price != null}<span class="num dim">${f.price < 1 ? f.price.toPrecision(3) : f.price.toLocaleString()}</span>{/if}
              {#if f.change_percent != null}
                <span class="num {f.change_percent >= 0 ? 'pl-up' : 'pl-down'}">{f.change_percent >= 0 ? "+" : ""}{f.change_percent.toFixed(1)}%</span>
              {/if}
              <button
                class="fc-scan-btn"
                disabled={scanOf(f.symbol).busy}
                title="Scan {f.symbol} now — its cached multi-timeframe TA and indicators, its accumulated profile, live threat/news/regime context, then the same scoring and cost gates the scheduled run uses"
                onclick={() => scanFocusNow(f.symbol)}
              >{scanOf(f.symbol).busy ? "scanning…" : "scan"}</button>
              <button class="fc-more" onclick={() => (expandedFocus = expandedFocus === f.symbol ? null : f.symbol)}>
                {expandedFocus === f.symbol ? "hide profile" : "profile"}
              </button>
              <button class="fc-rm" title="Stop watching" onclick={() => removeFocus(f.symbol)}>×</button>
            </div>
            {#if scanOf(f.symbol).busy}
              {@const sc = scanOf(f.symbol)}
              <div class="fc-scan-row">
                <div class="scan-bar"><div class="scan-fill" style="width:{sc.pct}%"></div></div>
                <span class="scan-note dim">{sc.phase} · {sc.elapsed}s</span>
              </div>
            {:else if scanOf(f.symbol).result}
              {@const n = scanOf(f.symbol).result?.new_signals ?? 0}
              <div class="fc-scan-row">
                <span class="scan-note {n > 0 ? 'pl-up' : 'dim'}">
                  {n > 0
                    ? `${n} new signal${n === 1 ? "" : "s"} — see Signals`
                    : "nothing ready — silence is a real answer here"}
                </span>
              </div>
            {:else if scanOf(f.symbol).error}
              <div class="fc-scan-row"><span class="scan-note pl-down">{scanOf(f.symbol).error}</span></div>
            {/if}
            {#if f.note}<div class="fc-note dim">{f.note}</div>{/if}
            {#if f.profile?.summary}
              <div class="fc-measured num">{f.profile.summary}</div>
            {/if}
            {#if expandedFocus === f.symbol}
              <div class="fc-detail">
                {#if f.profile?.narrative}
                  <div class="fc-narr">{f.profile.narrative}</div>
                  <div class="fc-tag dim">AI interpretation — measured statistics above are from real bars</div>
                {:else}
                  <div class="dim">No written profile yet — it builds on the next cycle.</div>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
        <div class="fc-foot dim">{focus.note}</div>
      {:else}
        <div class="empty">
          Nothing under focus watch. Add a symbol and it gets analysed every cycle, ahead of
          everything else — but only speaks when a setup is genuinely ready.
        </div>
      {/if}
    </Panel>
  </div>
  <div class="span-8">
    <Panel title="Watchlist 2.0" meta={watchlist ? `${watchlist.rows.length} symbols · fused intelligence` : "—"}>
      <form
        class="wl-add"
        onsubmit={(e) => {
          e.preventDefault();
          addTicker();
        }}
      >
        <input placeholder="Add ticker (NVDA or BTC/USD)" bind:value={newTicker} disabled={addingTicker} />
        <button class="ask-btn" type="submit" disabled={addingTicker || !newTicker.trim()}>
          {addingTicker ? "Verifying…" : "+ Add"}
        </button>
      </form>
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
  }
  .kpis {
    grid-column: span 12;
    display: grid;
    /* 10 tiles → two even rows of 5 */
    grid-template-columns: repeat(5, 1fr);
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
  .fc-row {
    padding: 8px 0;
    border-bottom: 1px solid var(--line);
  }
  .fc-row:last-of-type {
    border-bottom: none;
  }
  .fc-head {
    display: flex;
    align-items: baseline;
    gap: 9px;
  }
  .fc-sym {
    background: none;
    border: none;
    color: var(--ink);
    font: inherit;
    font-weight: 700;
    font-size: 12.5px;
    cursor: pointer;
    padding: 0;
  }
  .fc-sym:hover {
    color: var(--accent);
  }
  .fc-scan-btn,
  .fc-more,
  .fc-rm {
    background: none;
    border: 1px solid var(--line);
    color: var(--ink-faint);
    font: inherit;
    font-size: 9.5px;
    border-radius: 5px;
    padding: 1px 6px;
    cursor: pointer;
  }
  .fc-rm {
    margin-left: auto;
    font-size: 12px;
    line-height: 1;
    padding: 1px 7px;
  }
  .fc-more:hover,
  .fc-rm:hover {
    border-color: var(--accent);
    color: var(--ink);
  }
  /* Scan is the one button on the row that DOES something rather than
     revealing something, so it carries the accent while profile and remove
     stay quiet. */
  .fc-scan-btn {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
    color: var(--accent);
    font-weight: 600;
  }
  .fc-scan-btn:hover:not(:disabled) {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
  }
  .fc-scan-btn:disabled {
    opacity: 0.65;
    cursor: default;
  }
  /* Progress and result sit under the row they belong to, indented to the
     symbol so a list of several coins stays readable while two scan. */
  .fc-scan-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 3px 0 2px;
  }
  .fc-note {
    font-size: 10.5px;
    margin-top: 2px;
    font-style: italic;
  }
  .fc-measured {
    font-size: 10.5px;
    color: var(--ink-dim);
    margin-top: 3px;
    line-height: 1.4;
  }
  .fc-detail {
    margin-top: 6px;
    padding: 8px 10px;
    background: var(--surface-raised);
    border-radius: var(--radius-sm);
  }
  .fc-narr {
    font-size: 11px;
    line-height: 1.5;
  }
  .fc-tag {
    font-size: 9px;
    margin-top: 5px;
  }
  .fc-foot {
    font-size: 9.5px;
    margin-top: 8px;
    border-top: 1px solid var(--line);
    padding-top: 7px;
    line-height: 1.4;
  }
  .scan-bar {
    flex: 1 1 120px;
    height: 3px;
    min-width: 80px;
    border-radius: 2px;
    background: var(--stroke-faint, rgba(127, 127, 127, 0.22));
    overflow: hidden;
  }
  .scan-fill {
    height: 100%;
    border-radius: 2px;
    background: linear-gradient(90deg, var(--accent, #4c8dff), var(--warm, #e0a33e));
    transition: width 200ms linear;
  }
  .scan-note {
    font-size: 10px;
    line-height: 1.3;
    flex: 1 1 100%;
  }
  @media (prefers-reduced-motion: reduce) {
    .scan-fill {
      transition: none;
    }
  }
  .wl-add {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  .wl-add input {
    flex: 1;
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--ink);
    font: inherit;
    font-size: 12px;
    padding: 6px 10px;
  }
  .wl-add input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .opp-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
  }
  .btn {
    font: inherit;
    font-size: 11px;
    font-weight: 600;
    padding: 5px 12px;
    border-radius: 7px;
    border: 1px solid var(--accent);
    background: rgba(124, 154, 255, 0.14);
    color: var(--ink);
    cursor: pointer;
  }
  .btn.outline {
    background: none;
    border-color: var(--line-bright);
    color: var(--ink-dim);
  }
  .btn:hover {
    filter: brightness(1.2);
  }
  .caret {
    color: var(--ink-faint);
    font-size: 9px;
  }
  .opp-why {
    font-size: 10px;
    margin-top: 3px;
    line-height: 1.4;
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

  .threat-row {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 7px 0;
    border-bottom: 1px solid var(--line);
  }
  .threat-row:last-child {
    border-bottom: none;
  }
  .threat-title {
    font-size: 12px;
    font-weight: 600;
    line-height: 1.35;
  }
  .threat-meta {
    font-size: 10px;
    margin-top: 2px;
  }
  .movers {
    display: grid;
    grid-template-columns: 1fr;
    gap: 14px;
  }
  .fail-row {
    display: grid;
    grid-template-columns: minmax(110px, 1fr) 1fr auto;
    gap: 10px;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--line);
    font-size: 11.5px;
  }
  .fail-row:last-of-type {
    border-bottom: none;
  }
  .fail-label {
    text-transform: capitalize;
  }
  .fail-bar {
    height: 5px;
    background: var(--surface-raised);
    border-radius: 3px;
    overflow: hidden;
  }
  .fail-fill {
    display: block;
    height: 100%;
    background: var(--bad);
    opacity: 0.75;
    border-radius: 3px;
  }
  .mv-head {
    font-size: 9px;
    letter-spacing: 0.09em;
    color: var(--ink-faint);
    font-weight: 650;
    margin-bottom: 7px;
  }
  .mv-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 10px;
    padding: 5.5px 0;
    border-bottom: 1px solid var(--line);
    font-size: 12px;
    align-items: baseline;
  }
  .mv-row:last-child {
    border-bottom: none;
  }
  .pulse-row {
    padding: 6px 0;
    border-bottom: 1px solid var(--line);
  }
  .pulse-row:last-child {
    border-bottom: none;
  }
  .pulse-title {
    font-size: 12px;
    font-weight: 600;
    line-height: 1.35;
  }
  .pulse-snippet {
    font-size: 10.5px;
    line-height: 1.4;
    margin-top: 2px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .pulse-note {
    font-size: 9.5px;
    margin-top: 8px;
  }
  /* 40+ open positions made this table grow unbounded, stretching the whole
     grid row and leaving a huge void under the equity chart — cap it to the
     chart's height and scroll internally so the panels below flow up. */
  .pos-scroll {
    max-height: 372px;
    overflow-y: auto;
    margin: -14px;
  }
  .pos-scroll table.pos {
    margin: 0;
    width: 100%;
  }
  table.pos {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin: -14px;
    width: calc(100% + 28px);
  }
  table.pos thead th {
    position: sticky;
    top: 0;
    background: var(--surface);
    z-index: 1;
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
