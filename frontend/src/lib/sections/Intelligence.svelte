<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Sparkline from "../components/Sparkline.svelte";
  import Pill from "../components/Pill.svelte";
  import ThreatMap from "../components/ThreatMap.svelte";
  import OrderBookPanel from "../components/OrderBookPanel.svelte";
  import CryptoDerivativesPanel from "../components/CryptoDerivativesPanel.svelte";
  import { api, type Regime, type Threat, type NewsArticle, type MarketAsset, type IntelligenceSource, type IntelligenceStatus, type ThreatExposure, type InsiderClustersResponse, type YieldCurveSnapshot, type MacroSnapshot, type DarkPoolTopActivity, type DarkPoolVenues, type SqueezeTopResponse, type InstitutionalAccumulation, type CongressTradesResponse, type CongressActivityResponse, type PsychologyIndex, type IpoPipelineResponse, type InsiderTransaction } from "../api";

  let {
    view = "world",
  }: { view?: "world" | "smartmoney" | "macro" | "cryptodesk" } = $props();

  let regime = $state<Regime | null>(null);
  let threats = $state<Threat[]>([]);
  let news = $state<NewsArticle[]>([]);
  let equities = $state<MarketAsset[]>([]);
  let crypto = $state<MarketAsset[]>([]);
  let marketTab = $state<"equities" | "crypto">("equities");
  let sources = $state<IntelligenceSource[]>([]);
  let intelStatus = $state<IntelligenceStatus | null>(null);

  let threatConfirm = $state<string>("");
  let threatMinReliability = $state<number>(0);
  let newsConfirm = $state<string>("");
  let newsMinReliability = $state<number>(0);
  let newsStale = $state<string>("");
  let expandedThreat = $state<string | null>(null);
  let expandedNews = $state<string | null>(null);
  let showSources = $state(false);
  let exposure = $state<ThreatExposure | null>(null);
  let insiderClusters = $state<InsiderClustersResponse | null>(null);
  let insiderTxs = $state<InsiderTransaction[]>([]);
  let yieldCurve = $state<YieldCurveSnapshot | null>(null);
  let macro = $state<MacroSnapshot | null>(null);
  let darkPoolTop = $state<DarkPoolTopActivity | null>(null);
  let expandedDarkPoolSymbol = $state<string | null>(null);
  let darkPoolVenues = $state<DarkPoolVenues | null>(null);
  let darkPoolVenuesLoading = $state(false);
  let squeeze = $state<SqueezeTopResponse | null>(null);
  let institutional = $state<InstitutionalAccumulation | null>(null);
  let congress = $state<CongressTradesResponse | null>(null);
  let congressActivity = $state<CongressActivityResponse | null>(null);
  let congressOfficials = $state<Awaited<ReturnType<typeof api.congressByOfficial>> | null>(null);
  let fxRates = $state<Awaited<ReturnType<typeof api.fxRates>> | null>(null);
  let cryptoMarkets = $state<Awaited<ReturnType<typeof api.cryptoMarkets>> | null>(null);
  let webNews = $state<Awaited<ReturnType<typeof api.webNews>> | null>(null);
  let expandedOfficial = $state<string | null>(null);
  let psychology = $state<PsychologyIndex | null>(null);
  let ipo = $state<IpoPipelineResponse | null>(null);
  let expandedSqueezeSymbol = $state<string | null>(null);

  async function toggleDarkPoolExpand(symbol: string, weekStart: string) {
    if (expandedDarkPoolSymbol === symbol) {
      expandedDarkPoolSymbol = null;
      darkPoolVenues = null;
      return;
    }
    expandedDarkPoolSymbol = symbol;
    darkPoolVenues = null;
    darkPoolVenuesLoading = true;
    try {
      darkPoolVenues = await api.darkPoolVenues(symbol, weekStart);
    } catch {
      darkPoolVenues = null;
    } finally {
      darkPoolVenuesLoading = false;
    }
  }

  const stripMpidPrefix = (name: string | null, mpid: string | null) =>
    name && mpid && name.startsWith(mpid + " ") ? name.slice(mpid.length + 1) : name;

  const threatTrend = $derived.by(() => {
    const days: { date: string; critical: number; high: number; other: number; total: number }[] = [];
    const byDate = new Map<string, { critical: number; high: number; other: number }>();
    for (const t of threats) {
      const raw = t.created_date || t.published_at;
      if (!raw) continue;
      const day = raw.slice(0, 10);
      const bucket = byDate.get(day) ?? { critical: 0, high: 0, other: 0 };
      if (t.severity === "Critical") bucket.critical++;
      else if (t.severity === "High") bucket.high++;
      else bucket.other++;
      byDate.set(day, bucket);
    }
    const sortedDays = [...byDate.keys()].sort().slice(-7);
    for (const day of sortedDays) {
      const b = byDate.get(day)!;
      days.push({ date: day, ...b, total: b.critical + b.high + b.other });
    }
    return days;
  });

  async function loadThreats() {
    threats = await api.threats(60, {
      confirmation: threatConfirm || undefined,
      minReliability: threatMinReliability || undefined,
    });
  }

  async function loadNews() {
    news = await api.news(40, {
      confirmation: newsConfirm || undefined,
      minReliability: newsMinReliability || undefined,
      stale: newsStale === "" ? undefined : newsStale === "stale",
    });
  }

  async function loadAll() {
    // Each view fetches only its own data — the skipped entries resolve to
    // null/[] instantly. squeezeTop alone is 12 paginated FINRA requests, so
    // the world/macro/crypto views not paying for it matters, and a popout
    // window polling every 30s only polls its own tab's endpoints.
    const W = view === "world", SM = view === "smartmoney", MA = view === "macro", CD = view === "cryptodesk";
    const none = <T,>(v: T) => Promise.resolve(v);
    const [r, t, n, m, s, st, ex, ic, yc, fr, dp, sq, inst, cg, cga, psy, ipoRes] = await Promise.all([
      W ? api.regime().catch(() => null) : none(null),
      W ? api.threats(60, { confirmation: threatConfirm || undefined, minReliability: threatMinReliability || undefined }) : none([]),
      W ? api.news(40, { confirmation: newsConfirm || undefined, minReliability: newsMinReliability || undefined, stale: newsStale === "" ? undefined : newsStale === "stale" }) : none([]),
      W ? api.marketFull().catch(() => ({ equities: [], crypto: [], count: 0 })) : none({ equities: [], crypto: [], count: 0 }),
      W ? api.intelligenceSources().catch(() => []) : none([]),
      W ? api.intelligenceStatus().catch(() => null) : none(null),
      W ? api.threatExposure().catch(() => null) : none(null),
      SM ? api.insiderClusters(14).catch(() => null) : none(null),
      MA ? api.yieldCurve().catch(() => null) : none(null),
      MA ? api.macroFred().catch(() => null) : none(null),
      SM ? api.darkPoolTop("T1", 20).catch(() => null) : none(null),
      SM ? api.squeezeTop(20, 3).catch(() => null) : none(null),
      SM ? api.institutionalAccumulation(20).catch(() => null) : none(null),
      SM ? api.congressTrades(25).catch(() => null) : none(null),
      SM ? api.congressActivity(12).catch(() => null) : none(null),
      MA ? api.psychology().catch(() => null) : none(null),
      SM ? api.ipoPipeline(30).catch(() => null) : none(null),
    ]);
    const [fxR, cgR, wnR] = await Promise.all([
      MA ? api.fxRates().catch(() => null) : none(null),
      CD ? api.cryptoMarkets().catch(() => null) : none(null),
      W || MA ? api.webNews().catch(() => null) : none(null),
    ]);
    fxRates = fxR ?? fxRates;
    cryptoMarkets = cgR ?? cryptoMarkets;
    webNews = wnR ?? webNews;
    regime = r;
    threats = t;
    news = n;
    equities = m.equities.slice(0, 12);
    crypto = m.crypto.slice(0, 12);
    sources = s;
    intelStatus = st;
    exposure = ex;
    insiderClusters = ic;
    if (SM) {
      api.insiderActivity(undefined, 14, 40).then((rows) => (insiderTxs = rows)).catch(() => {});
    }
    yieldCurve = yc;
    macro = fr;
    darkPoolTop = dp;
    squeeze = sq;
    institutional = inst;
    congress = cg;
    congressActivity = cga;
    if (SM) {
      api.congressByOfficial(365, 40).then((r) => (congressOfficials = r)).catch(() => {});
    }
    psychology = psy;
    ipo = ipoRes;
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 30_000);
    return () => clearInterval(poll);
  });

  const sevTone = (s: string) => (s === "Critical" ? "critical" : s === "High" ? "warm" : s === "Low" ? "good" : "neutral");
  const sentTone = (s: string | null) => (s === "positive" ? "good" : s === "negative" ? "bad" : "neutral");
  const confirmTone = (s: string | null | undefined) =>
    s === "corroborated" ? "good" : s === "unconfirmed_social" ? "warm" : s === "single_source" ? "neutral" : "neutral";
  const confirmLabel = (s: string | null | undefined) =>
    s === "corroborated" ? "corroborated" : s === "unconfirmed_social" ? "unconfirmed" : s === "single_source" ? "single source" : "—";
  const sourceStatusTone = (s: string) => (s === "healthy" ? "good" : s === "degraded" ? "warm" : "bad");
  const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
  const MACRO_LABELS: Record<string, string> = {
    cpi: "CPI", core_cpi: "Core CPI", pce: "PCE", core_pce: "Core PCE",
    unemployment_rate: "Unemployment", fed_funds_rate: "Fed Funds Rate",
    nonfarm_payrolls: "Nonfarm Payrolls", real_gdp: "Real GDP", jobless_claims: "Jobless Claims",
  };
  const fmtMacroValue = (v: number, unit: string) => {
    const isChange = unit.includes("YoY") || unit.includes("MoM");
    const sign = isChange && v >= 0 ? "+" : "";
    const num = unit.includes("%") ? v.toFixed(1) : v.toLocaleString();
    return `${sign}${num}${unit.includes("%") ? "%" : ""}`;
  };
  const reportDelayDays = (txDate: string | null, filedAt: string | null) => {
    if (!txDate || !filedAt) return null;
    const d = (new Date(filedAt).getTime() - new Date(txDate).getTime()) / 86400000;
    return d >= 0 ? Math.round(d) : null;
  };
  const fmtUsdShort = (v: number | null) =>
    v == null ? "—" : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${Math.round(v).toLocaleString()}`;

  const fmtAgo = (iso: string | null | undefined) => {
    if (!iso) return "—";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
</script>

<div class="page-head">
  <h1>{view === "world" ? "Intelligence" : view === "smartmoney" ? "Smart Money" : view === "macro" ? "Macro Desk" : "Crypto Desk"}</h1>
  <div class="sub">
    {view === "world"
      ? "Market regime, geopolitical threats, and news — the world-awareness layer"
      : view === "smartmoney"
        ? "Insiders, Congress, institutions, dark pool, short interest, IPOs — who is positioning"
        : view === "macro"
          ? "Yield curve, inflation and employment data, market psychology"
          : "Live order books and perpetual-futures positioning"}
  </div>
</div>

<div class="grid">
  {#if view === "world"}
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
  {/if}

  {#if view === "world"}
  <div class="span-8">
    <Panel title="Threat Map" dotColor="var(--critical)" meta="{threats.length} active" noPad>
      <ThreatMap {threats} />
    </Panel>
  </div>
  {/if}

  {#if view === "world" || view === "macro"}
  <div class="span-12">
    <Panel title="Live Web Pulse" meta={webNews?.as_of ? `refreshed ${new Date(webNews.as_of).toLocaleTimeString()}` : "—"}>
      {#snippet children()}
        {#if webNews && webNews.items.length}
          <div class="list">
            {#each webNews.items as it, i (i)}
              <div class="row">
                <div class="row-main">
                  <div class="row-title">{it.title}</div>
                  {#if it.snippet}<div class="row-meta">{it.snippet}</div>{/if}
                </div>
              </div>
            {/each}
          </div>
          <p class="insider-note">Unverified live web search (tavily/exa) — exactly the FRESH WEB NEWS block injected into every signal-generation LLM prompt. Refreshes every 30 minutes.</p>
        {:else}
          <div class="empty">No web pulse yet — populates on the next signal-generation run</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "macro"}
  <div class="span-12">
    <Panel title="FX Rates — Live Interbank" meta={fxRates ? `${fxRates.pairs.length} pairs · AllRatesToday` : "—"}>
      {#snippet children()}
        {#if fxRates && fxRates.pairs.length}
          <div class="fx-grid">
            {#each fxRates.pairs as p (p.pair)}
              <div class="fx-card">
                <div class="fx-head">
                  <span class="fx-pair">{p.pair}</span>
                  <span class="fx-chg num {p.change_pct != null && p.change_pct >= 0 ? 'pl-up' : 'pl-down'}">
                    {p.change_pct != null ? `${p.change_pct >= 0 ? "+" : ""}${p.change_pct.toFixed(2)}% 30d` : "—"}
                  </span>
                </div>
                <div class="fx-rate num">{p.rate ?? "—"}</div>
                {#if p.history.length >= 2}
                  <Sparkline points={p.history.map((h) => h.rate)} width={170} height={38}
                    color={p.change_pct != null && p.change_pct >= 0 ? "var(--good)" : "var(--bad)"} />
                {/if}
              </div>
            {/each}
          </div>
          <p class="insider-note">Live interbank mid rates with 30 days of daily closes — the same feed injected into forex signal generation. Refreshes every 15 minutes.</p>
        {:else}
          <div class="empty">FX rates unavailable — needs ALLRATES_API_KEY</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "macro"}
  <div class="span-12">
    <Panel title="Treasury Yield Curve" meta={yieldCurve ? `as of ${yieldCurve.latest.date}` : "—"}>
      {#snippet children()}
        {#if yieldCurve}
          <div class="ih-strip">
            <div class="ih-stat">
              <span class="ih-label">2yr</span>
              <span class="ih-val">{yieldCurve.latest["2yr"]?.toFixed(2) ?? "—"}%</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">10yr</span>
              <span class="ih-val">{yieldCurve.latest["10yr"]?.toFixed(2) ?? "—"}%</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">30yr</span>
              <span class="ih-val">{(yieldCurve.latest as any)["30yr"]?.toFixed(2) ?? "—"}%</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">2s10s spread</span>
              <span class="ih-val {yieldCurve.latest["2s10s_inverted"] ? 'bad' : 'good'}">
                {yieldCurve.latest.spread_2s10s != null ? `${yieldCurve.latest.spread_2s10s >= 0 ? "+" : ""}${yieldCurve.latest.spread_2s10s.toFixed(2)}` : "—"}
              </span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">3m10y spread</span>
              <span class="ih-val {yieldCurve.latest["3m10y_inverted"] ? 'bad' : 'good'}">
                {yieldCurve.latest.spread_3m10y != null ? `${yieldCurve.latest.spread_3m10y >= 0 ? "+" : ""}${yieldCurve.latest.spread_3m10y.toFixed(2)}` : "—"}
              </span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">Curve state</span>
              <span class="ih-val small {yieldCurve.latest["2s10s_inverted"] || yieldCurve.latest["3m10y_inverted"] ? 'bad' : 'good'}">
                {yieldCurve.latest["2s10s_inverted"] || yieldCurve.latest["3m10y_inverted"] ? "Inverted" : "Normal"}
              </span>
            </div>
          </div>
          <p class="insider-note">US Treasury daily yield curve (free, official Treasury.gov data). An inverted curve — short-term yields above long-term — has historically preceded recessions, though timing and lead time vary widely.</p>
        {:else}
          <div class="empty">Yield curve data unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "macro"}
  <div class="span-12">
    <Panel title="Macro Indicators" meta={macro?.configured ? "FRED · St. Louis Fed" : "not configured"}>
      {#snippet children()}
        {#if macro?.configured && macro.readings}
          <div class="macro-grid">
            {#each Object.entries(macro.readings) as [key, reading] (key)}
              <div class="macro-card">
                <span class="macro-label">{MACRO_LABELS[key] ?? key}</span>
                {#if reading}
                  <span class="macro-val">{fmtMacroValue(reading.value, reading.unit)}</span>
                  <span class="macro-unit">{reading.unit} · {reading.date}</span>
                {:else}
                  <span class="macro-val dim">—</span>
                {/if}
              </div>
            {/each}
          </div>
        {:else if macro && !macro.configured}
          <div class="empty">
            Not configured — add a free FRED API key (FRED_API_KEY in .env, instant signup at fred.stlouisfed.org) to enable CPI, unemployment, nonfarm payrolls, GDP, and Fed funds rate tracking.
          </div>
        {:else}
          <div class="empty">Macro data unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "world"}
  <div class="span-12">
    <Panel title="Intelligence Ingestion Health" meta={intelStatus ? intelStatus.status : "—"}>
      {#snippet children()}
        {#if intelStatus}
          <div class="ih-strip">
            <div class="ih-stat">
              <span class="ih-label">Sources</span>
              <span class="ih-val">{intelStatus.source_count}</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">Healthy</span>
              <span class="ih-val good">{intelStatus.healthy_sources}</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">Failing</span>
              <span class="ih-val {intelStatus.failing_sources > 0 ? 'bad' : ''}">{intelStatus.failing_sources}</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">News (24h)</span>
              <span class="ih-val">{intelStatus.recent_news}</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">Corroborated</span>
              <span class="ih-val good">{intelStatus.corroborated_recent}</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">Unconfirmed social</span>
              <span class="ih-val warm">{intelStatus.social_unconfirmed_recent}</span>
            </div>
            <div class="ih-stat">
              <span class="ih-label">Last run</span>
              <span class="ih-val small">{intelStatus.latest_run ? fmtAgo(intelStatus.latest_run.finished_at) : "never"}</span>
            </div>
            <button class="ih-toggle" onclick={() => (showSources = !showSources)}>
              {showSources ? "Hide sources ▲" : "Show sources ▼"}
            </button>
          </div>
          {#if showSources}
            <table class="tbl src-tbl">
              <thead>
                <tr><th>Source</th><th>Kind</th><th>Reliability</th><th>Status</th><th>Success/Fail</th><th>Last success</th><th>Last error</th></tr>
              </thead>
              <tbody>
                {#each sources as s (s.source)}
                  <tr>
                    <td class="sym">{s.source}</td>
                    <td class="name">{s.source_kind ?? "—"}{s.provider ? ` · ${s.provider}` : ""}</td>
                    <td class="num">{pct(s.reliability_score)}</td>
                    <td><Pill label={s.status} tone={sourceStatusTone(s.status)} /></td>
                    <td class="num">{s.success_count}/{s.failure_count}</td>
                    <td class="num">{fmtAgo(s.last_success_at)}</td>
                    <td class="err">{s.last_error || "—"}</td>
                  </tr>
                {:else}
                  <tr><td colspan="7" class="empty">No source health data yet</td></tr>
                {/each}
              </tbody>
            </table>
          {/if}
        {:else}
          <div class="empty">Ingestion status unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "world"}
  <div class="span-6">
    <Panel title="Threat Escalation Trend" meta="last {threatTrend.length} days">
      {#snippet children()}
        {#if threatTrend.length}
          <div class="trend-chart">
            {#each threatTrend as d (d.date)}
              {@const maxTotal = Math.max(1, ...threatTrend.map((x) => x.total))}
              <div class="trend-col">
                <div class="trend-bar" style="height:{(d.total / maxTotal) * 100}%">
                  {#if d.critical}<div class="trend-seg critical" style="height:{(d.critical / d.total) * 100}%"></div>{/if}
                  {#if d.high}<div class="trend-seg high" style="height:{(d.high / d.total) * 100}%"></div>{/if}
                  {#if d.other}<div class="trend-seg other" style="height:{(d.other / d.total) * 100}%"></div>{/if}
                </div>
                <span class="trend-count">{d.total}</span>
                <span class="trend-date">{d.date.slice(5)}</span>
              </div>
            {/each}
          </div>
          <div class="trend-legend">
            <span><i class="dot critical"></i>Critical</span>
            <span><i class="dot high"></i>High</span>
            <span><i class="dot other"></i>Other</span>
          </div>
        {:else}
          <div class="empty">Not enough dated threats to chart a trend</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "world"}
  <div class="span-6">
    <Panel title="Position Exposure to Active Threats" meta={exposure ? `${exposure.symbols_exposed}/${exposure.symbols_checked} symbols` : "—"}>
      {#snippet children()}
        {#if exposure && exposure.symbols_exposed}
          <div class="list">
            {#each Object.entries(exposure.exposure) as [symbol, matches] (symbol)}
              <div class="row">
                <Pill label={matches[0].severity} tone={sevTone(matches[0].severity)} />
                <div class="row-main">
                  <div class="row-title">{symbol}</div>
                  <div class="row-meta">{matches.map((m) => m.title).join(" · ").slice(0, 100)}</div>
                </div>
              </div>
            {/each}
          </div>
        {:else if exposure}
          <div class="empty">No open position is directly named in an active threat</div>
        {:else}
          <div class="empty">Exposure check unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "smartmoney"}
  <div class="span-12">
    <Panel title="Insider Activity" meta={insiderClusters ? `${insiderClusters.transactions_analyzed} open-market buys/sells, last ${insiderClusters.window_days}d · ${insiderTxs.length} recent filings` : "—"}>
      {#snippet children()}
        {#if insiderClusters && insiderClusters.clusters.length}
          <div class="insider-list">
            {#each insiderClusters.clusters as c (c.ticker)}
              <div class="insider-row">
                <div class="insider-sym">{c.ticker}</div>
                <div class="insider-flags">
                  {#each c.flags as flag (flag)}
                    <Pill label={flag.replaceAll("_", " ")} tone={flag.includes("SELL") ? "bad" : flag.includes("BUY") || flag.includes("OFFICER") ? "good" : "neutral"} />
                  {/each}
                </div>
                <div class="insider-stats">
                  <span>{c.distinct_buyers} buyer{c.distinct_buyers === 1 ? "" : "s"} / {c.distinct_sellers} seller{c.distinct_sellers === 1 ? "" : "s"}</span>
                  <span class="num {c.net_value >= 0 ? 'pl-up' : 'pl-down'}">net {c.net_value >= 0 ? "+" : ""}${Math.round(c.net_value).toLocaleString()}</span>
                </div>
                {#if c.officer_buyers.length}
                  <div class="insider-officers">Officer buying: {c.officer_buyers.join(", ")}</div>
                {/if}
              </div>
            {/each}
          </div>
          <p class="insider-note">Sourced from SEC Form 4 filings (free EDGAR API). Only open-market buys/sells (codes P/S) are analyzed — grants, option exercises, and tax withholding are excluded. This does not imply wrongdoing or predict price direction.</p>
        {:else if insiderClusters}
          <div class="empty">No notable insider buy/sell clusters in the last {insiderClusters.window_days} days</div>
        {:else}
          <div class="empty">Insider activity unavailable</div>
        {/if}
        {#if insiderTxs.length}
          <div class="itx-head">Recent filings — price, value, and reporting lag</div>
          <div class="wl-scroll cap-h">
            <table class="tbl">
              <thead>
                <tr><th>Ticker</th><th>Insider</th><th>Action</th><th>@ Price</th><th>Total</th><th>Traded</th><th>Reported</th></tr>
              </thead>
              <tbody>
                {#each insiderTxs.slice(0, 15) as t (t.id)}
                  <tr>
                    <td class="sym">{t.ticker ?? "—"}</td>
                    <td title={t.owner_title ?? ""}>{(t.owner_name ?? "").slice(0, 22)}{t.is_officer ? " • officer" : t.is_director ? " • director" : ""}</td>
                    <td class={t.transaction_code === "P" ? "pl-up" : t.transaction_code === "S" ? "pl-down" : "dim"}>{t.transaction_label}</td>
                    <td class="num">{t.price_per_share != null ? `$${t.price_per_share.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—"}</td>
                    <td class="num">{fmtUsdShort(t.total_value)}</td>
                    <td class="num">{t.transaction_date ?? "—"}</td>
                    <td class="num">
                      {#if reportDelayDays(t.transaction_date, t.filed_at) != null}
                        +{reportDelayDays(t.transaction_date, t.filed_at)}d
                      {:else}—{/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "smartmoney"}
  <div class="span-12">
    <Panel title="Dark Pool / Off-Exchange (ATS) Activity" meta={darkPoolTop ? `week of ${darkPoolTop.week_start} · ${darkPoolTop.tier}` : "—"}>
      {#snippet children()}
        {#if darkPoolTop && darkPoolTop.symbols.length}
          <div class="dp-banner">⚠ Delayed, weekly-aggregated FINRA data — published ~2-4 weeks after the trading week, not real-time order flow. Click a row for the per-venue breakdown.</div>
          <table class="tbl">
            <thead>
              <tr><th>Symbol</th><th>Shares</th><th>Trades</th><th>Notional</th><th>WoW</th><th>Published</th></tr>
            </thead>
            <tbody>
              {#each darkPoolTop.symbols as s (s.symbol)}
                <tr
                  class="expandable"
                  role="button"
                  tabindex="0"
                  onclick={() => toggleDarkPoolExpand(s.symbol, s.week_start)}
                  onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDarkPoolExpand(s.symbol, s.week_start); } }}
                >
                  <td class="sym">{expandedDarkPoolSymbol === s.symbol ? "▾" : "▸"} {s.symbol}</td>
                  <td class="num">{s.shares?.toLocaleString() ?? "—"}</td>
                  <td class="num">{s.trade_count?.toLocaleString() ?? "—"}</td>
                  <td class="num">{s.notional != null ? `$${Math.round(s.notional / 1_000_000).toLocaleString()}M` : "—"}</td>
                  <td class="num {s.wow_pct == null ? '' : s.wow_pct >= 0 ? 'pl-up' : 'pl-down'}">
                    {s.wow_pct != null ? `${s.wow_pct >= 0 ? "+" : ""}${s.wow_pct}%` : "—"}
                  </td>
                  <td class="num small">{s.published_at ?? "—"} ({s.reporting_delay_days ?? "?"}d delay)</td>
                </tr>
                {#if expandedDarkPoolSymbol === s.symbol}
                  <tr class="expand-row">
                    <td colspan="6">
                      <div class="dp-venues">
                        {#if darkPoolVenuesLoading}
                          <div class="empty small">Loading venue breakdown…</div>
                        {:else if darkPoolVenues && darkPoolVenues.venues.length}
                          {#each darkPoolVenues.venues.slice(0, 10) as v (v.mpid)}
                            <div class="dp-venue-row">
                              <span class="dp-venue-name">{stripMpidPrefix(v.name, v.mpid)}</span>
                              <span class="num">{v.shares?.toLocaleString() ?? "—"} sh</span>
                              <span class="num">{v.trade_count?.toLocaleString() ?? "—"} trades</span>
                            </div>
                          {/each}
                        {:else}
                          <div class="empty small">No per-venue data for this symbol/week</div>
                        {/if}
                      </div>
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        {:else}
          <div class="empty">Dark pool / ATS data unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "macro"}
  <div class="span-12">
    <Panel
      title="Market Psychology Index"
      meta={psychology?.score != null
        ? `${psychology.components_available}/${psychology.components_possible} components`
        : "—"}
    >
      {#snippet children()}
        {#if psychology && psychology.score != null}
          <div class="psy-head">
            <div class="psy-score">
              <div class="psy-num num">{psychology.score.toFixed(0)}</div>
              <Pill
                label={(psychology.label ?? "").replace("_", " ")}
                tone={psychology.score >= 60 ? "good" : psychology.score < 40 ? "bad" : "neutral"}
              />
            </div>
            <div class="psy-meter" aria-hidden="true">
              <div class="psy-track"></div>
              <div class="psy-marker" style="left: {psychology.score}%"></div>
            </div>
            <div class="psy-scale">
              <span>extreme fear</span><span>neutral</span><span>extreme greed</span>
            </div>
            {#if psychology.rate_of_change}
              <div class="psy-roc num {psychology.rate_of_change.delta >= 0 ? 'pl-up' : 'pl-down'}">
                {psychology.rate_of_change.delta >= 0 ? "+" : ""}{psychology.rate_of_change.delta.toFixed(1)}
                over {psychology.rate_of_change.hours.toFixed(1)}h · {psychology.rate_of_change.direction.replace("_", " ")}
              </div>
            {/if}
          </div>

          {#if psychology.markets}
            <div class="psy-markets">
              {#each Object.values(psychology.markets) as m (m.market)}
                <div class="psy-mkt">
                  <div class="psy-mkt-name">{m.market}</div>
                  <div class="psy-mkt-score num">{m.score != null ? m.score.toFixed(0) : "—"}</div>
                  <div class="psy-mkt-label {m.score == null ? '' : m.score >= 60 ? 'pl-up' : m.score < 40 ? 'pl-down' : ''}">
                    {(m.label ?? "no data").replaceAll("_", " ").toLowerCase()}
                  </div>
                  <div class="psy-mkt-meta dim">{m.components_available}/{m.components_possible ?? "?"} inputs</div>
                </div>
              {/each}
            </div>
          {/if}
          <table class="tbl">
            <thead>
              <tr><th>Component</th><th>Reading</th><th>Detail</th></tr>
            </thead>
            <tbody>
              {#each Object.entries(psychology.components) as [name, comp] (name)}
                <tr>
                  <td class="sym">{name.replace("_", "/")}</td>
                  <td class="num {comp == null ? '' : comp.score >= 60 ? 'pl-up' : comp.score < 40 ? 'pl-down' : ''}">
                    {comp == null ? "—" : comp.score.toFixed(0)}
                  </td>
                  <td class={comp == null ? "dim" : ""}>
                    {comp == null ? "no data — abstained rather than scored neutral" : comp.detail}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="si-footnote">{psychology.note}</div>
        {:else}
          <div class="empty">
            Market psychology index unavailable — no component inputs could be computed.
          </div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "smartmoney"}
  <div class="span-12">
    <Panel
      title="Congressional Trade Disclosures"
      meta={congress ? `${congress.filings_processed} filings processed · House` : "—"}
    >
      {#snippet children()}
        {#if congress && congress.trades.length}
          <div class="dp-banner">
            ⚠ Amounts are disclosed <strong>ranges</strong>, never exact values. Disclosure is
            delayed by statute (STOCK Act allows up to 45 days). These are legally required
            filings — their presence does not imply wrongdoing or insider knowledge, and trades
            are frequently made by advisors in managed accounts. House only.
          </div>

          {#if congressActivity && congressActivity.tickers.length}
            <div class="cg-chips">
              {#each congressActivity.tickers.slice(0, 10) as t (t.ticker)}
                <span class="cg-chip">
                  <strong>{t.ticker}</strong>
                  <span class="num {t.net_direction === 'net_buying' ? 'pl-up' : t.net_direction === 'net_selling' ? 'pl-down' : ''}">
                    {t.purchases}P/{t.sales}S
                  </span>
                  <span class="dim">{t.member_count} {t.member_count === 1 ? "member" : "members"}</span>
                </span>
              {/each}
            </div>
          {/if}

          <table class="tbl">
            <thead>
              <tr><th>Ticker</th><th>Member</th><th>Type</th><th>Transaction</th><th>Disclosed</th><th>Amount Range</th></tr>
            </thead>
            <tbody>
              {#each congress.trades as t (t.id)}
                <tr>
                  <td class="sym">
                    {#if t.ticker}{t.ticker}{:else}<span class="dim" title={t.asset_name ?? ""}>no ticker</span>{/if}
                  </td>
                  <td>
                    {t.member_name}
                    {#if t.owner}<span class="dim"> ({t.owner})</span>{/if}
                  </td>
                  <td>
                    <Pill
                      label={t.transaction_label}
                      tone={t.transaction_code.startsWith("P") ? "good" : t.transaction_code.startsWith("S") ? "bad" : "neutral"}
                    />
                  </td>
                  <td class="num">{t.transaction_date}</td>
                  <td class="num">
                    {t.notification_date}
                    {#if t.filing_delay_days != null}<span class="dim"> +{t.filing_delay_days}d</span>{/if}
                  </td>
                  <td class="num">{t.amount_text ?? "—"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="si-footnote">{congress.disclaimer.interpretation}</div>
        {:else}
          <div class="empty">
            No congressional disclosures ingested yet — the scheduled job processes filings in
            batches and builds coverage over successive runs.
          </div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "smartmoney"}
  <div class="span-12">
    <Panel
      title="IPO Pipeline"
      meta={ipo
        ? `${ipo.stage_counts.filed} filed · ${ipo.stage_counts.amended} amended · ${ipo.stage_counts.priced} priced`
        : "—"}
    >
      {#snippet children()}
        {#if ipo && ipo.pipeline.length}
          <table class="tbl">
            <thead>
              <tr><th>Company</th><th>Stage</th><th>Ticker</th><th>Price</th><th>Shares</th><th>Offering</th><th>Filed</th></tr>
            </thead>
            <tbody>
              {#each ipo.pipeline as r (r.cik)}
                <tr>
                  <td>
                    {r.company_name}
                    {#if r.is_likely_spac}<Pill label="likely SPAC" tone="warm" />{/if}
                    {#if r.cover_mentions_ipo === false}<Pill label="follow-on, not IPO" tone="neutral" />{/if}
                  </td>
                  <td>
                    <Pill
                      label={r.stage}
                      tone={r.stage === "priced" ? "good" : r.stage === "amended" ? "neutral" : "warm"}
                    />
                  </td>
                  <td class="sym">{r.ticker ?? "—"}</td>
                  <td class="num">{r.offer_price != null ? `$${r.offer_price}` : "—"}</td>
                  <td class="num">{r.shares_offered != null ? r.shares_offered.toLocaleString() : "—"}</td>
                  <td class="num">{r.total_offering_usd != null ? `$${Math.round(r.total_offering_usd / 1_000_000)}M` : "—"}</td>
                  <td class="num">{r.latest_filed_at?.slice(0, 10) ?? "—"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="si-footnote">{ipo.note}</div>
        {:else}
          <div class="empty">
            No registration filings ingested yet — the scheduled job builds pipeline coverage over successive runs.
          </div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "smartmoney"}
  <div class="span-12">
    <Panel title="Trades by Official" meta={congressOfficials ? `${congressOfficials.officials.length} officials · 365d` : "—"}>
      {#snippet children()}
        {#if congressOfficials && congressOfficials.officials.length}
          <div class="cap-h">
            {#each congressOfficials.officials as o (o.member_name)}
              <div
                class="off-row"
                role="button"
                tabindex="0"
                onclick={() => (expandedOfficial = expandedOfficial === o.member_name ? null : o.member_name)}
                onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (expandedOfficial = expandedOfficial === o.member_name ? null : o.member_name)}
              >
                <span class="off-caret">{expandedOfficial === o.member_name ? "▾" : "▸"}</span>
                <span class="off-name">{o.member_name} <i class="dim">({o.state_district ?? o.chamber})</i></span>
                <span class="num">{o.trade_count} trades</span>
                <span class="num"><span class="pl-up">{o.purchases}P</span>/<span class="pl-down">{o.sales}S</span></span>
                <span class="num dim">${Math.round(o.range_low_total / 1000).toLocaleString()}k–${Math.round(o.range_high_total / 1000).toLocaleString()}k range</span>
              </div>
              {#if expandedOfficial === o.member_name}
                <div class="off-detail">
                  <table class="tbl">
                    <thead><tr><th>Ticker</th><th>Action</th><th>Amount</th><th>Traded</th><th>Reported</th></tr></thead>
                    <tbody>
                      {#each o.trades as t (t.id)}
                        <tr>
                          <td class="sym">{t.ticker ?? "—"}</td>
                          <td class={t.transaction_code.startsWith("P") ? "pl-up" : t.transaction_code.startsWith("S") ? "pl-down" : "dim"}>{t.transaction_label}</td>
                          <td class="num">{t.amount_text ?? "—"}</td>
                          <td class="num">{t.transaction_date}</td>
                          <td class="num">{t.notification_date}{#if t.filing_delay_days != null} <span class="dim">+{t.filing_delay_days}d</span>{/if}</td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/if}
            {/each}
          </div>
          <div class="si-footnote">{congressOfficials.note}</div>
        {:else}
          <div class="empty">No per-official data yet — coverage builds as filings are processed.</div>
        {/if}
      {/snippet}
    </Panel>
  </div>

  <div class="span-12">
    <Panel
      title="Institutional Ownership (13F)"
      meta={institutional ? `Q ending ${institutional.current_period} · ${institutional.tickers.length} tickers` : "—"}
    >
      {#snippet children()}
        {#if institutional && institutional.tickers.length}
          <div class="dp-banner">
            ⚠ Quarterly 13F snapshot, filed up to 45 days after quarter-end — long US equity
            positions only. Never shows shorts, hedges, or current buying.
            {#if institutional.insufficient_history}
              Only one quarter ingested so far, so quarter-over-quarter change is not yet available.
            {/if}
          </div>
          <table class="tbl">
            <thead>
              <tr><th>Ticker</th><th>Holders</th><th>Total Value</th><th>Shares</th><th>QoQ Change</th></tr>
            </thead>
            <tbody>
              {#each institutional.tickers as t (t.ticker)}
                <tr>
                  <td class="sym">{t.ticker}</td>
                  <td class="num">
                    {t.holder_count}{#if t.holder_delta != null && t.holder_delta !== 0}<span
                        class="num {t.holder_delta > 0 ? 'pl-up' : 'pl-down'}"
                      > ({t.holder_delta > 0 ? "+" : ""}{t.holder_delta})</span
                    >{/if}
                  </td>
                  <td class="num">${Math.round(t.total_value_usd / 1_000_000).toLocaleString()}M</td>
                  <td class="num">{Math.round(t.total_shares).toLocaleString()}</td>
                  <td class="num {t.share_change_pct == null ? '' : t.share_change_pct >= 0 ? 'pl-up' : 'pl-down'}">
                    {#if t.insufficient_history}
                      <span class="dim">no prior quarter</span>
                    {:else if t.share_change_pct == null}
                      {t.status === "newly_reported" ? "newly reported" : "—"}
                    {:else}
                      {t.share_change_pct >= 0 ? "+" : ""}{t.share_change_pct.toFixed(1)}%
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="si-footnote">{institutional.disclaimer.coverage_note}</div>
        {:else}
          <div class="empty">
            No 13F holdings ingested yet — the scheduled job builds coverage over successive runs.
          </div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "smartmoney"}
  <div class="span-12">
    <Panel
      title="Short Interest / Squeeze Fuel"
      meta={squeeze ? `settled ${squeeze.settlement_date} · ${squeeze.qualified_count.toLocaleString()} qualified` : "—"}
    >
      {#snippet children()}
        {#if squeeze && squeeze.candidates.length}
          <div class="dp-banner">
            ⚠ Delayed semi-monthly FINRA data — settled {squeeze.settlement_date}
            {#if squeeze.reporting_lag_days != null}({squeeze.reporting_lag_days}d ago){/if}, not a live short book.
            Measures squeeze <em>fuel</em> (crowding + short buildup), not a prediction. Click a row for components.
          </div>
          <table class="tbl">
            <thead>
              <tr><th>Symbol</th><th>Days to Cover</th><th>Short Shares</th><th>Chg %</th><th>Squeeze</th></tr>
            </thead>
            <tbody>
              {#each squeeze.candidates as c (c.symbol)}
                <tr
                  class="expandable"
                  role="button"
                  tabindex="0"
                  onclick={() => (expandedSqueezeSymbol = expandedSqueezeSymbol === c.symbol ? null : c.symbol)}
                  onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); expandedSqueezeSymbol = expandedSqueezeSymbol === c.symbol ? null : c.symbol; } }}
                >
                  <td class="sym">
                    {expandedSqueezeSymbol === c.symbol ? "▾" : "▸"} {c.symbol}
                    <span class="si-name">{c.issue_name ?? ""}</span>
                  </td>
                  <td class="num">{c.days_to_cover?.toFixed(2) ?? "—"}</td>
                  <td class="num">{c.current_short_shares?.toLocaleString() ?? "—"}</td>
                  <td class="num {c.change_percent == null ? '' : c.change_percent >= 0 ? 'pl-up' : 'pl-down'}">
                    {c.change_percent != null ? `${c.change_percent >= 0 ? "+" : ""}${c.change_percent.toFixed(1)}%` : "—"}
                  </td>
                  <td class="num"><b>{c.squeeze.squeeze_score?.toFixed(1) ?? "—"}</b></td>
                </tr>
                {#if expandedSqueezeSymbol === c.symbol}
                  <tr class="expand-row">
                    <td colspan="5">
                      <div class="si-detail">
                        <div>
                          <span class="si-lbl">Days-to-cover component</span>
                          <span class="num">{c.squeeze.days_to_cover_component ?? "—"}</span>
                        </div>
                        <div>
                          <span class="si-lbl">Short-change component</span>
                          <span class="num">{c.squeeze.short_change_component ?? "—"}</span>
                        </div>
                        <div>
                          <span class="si-lbl">Avg daily volume</span>
                          <span class="num">{c.avg_daily_volume?.toLocaleString() ?? "—"}</span>
                        </div>
                        <div class="si-note">{c.squeeze.float_note}</div>
                      </div>
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
          <div class="si-footnote">
            Ranked {squeeze.qualified_count.toLocaleString()} of {squeeze.universe_size.toLocaleString()} exchange-listed rows
            ({squeeze.exchanges_included.join(", ")}; OTC excluded). Filtered out:
            {squeeze.excluded.below_min_days_to_cover.toLocaleString()} below min days-to-cover,
            {squeeze.excluded.funds_and_spacs.toLocaleString()} funds/SPACs/warrants,
            {squeeze.excluded.sentinel_days_to_cover.toLocaleString()} sentinel values,
            {squeeze.excluded.implausible_days_to_cover.toLocaleString()} implausible ratios.
          </div>
        {:else}
          <div class="empty">Short interest data unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "cryptodesk"}
  <div class="span-12">
    <Panel title="Market Structure" meta={cryptoMarkets ? `${cryptoMarkets.coins.length} coins · CoinGecko live` : "—"}>
      {#snippet children()}
        {#if cryptoMarkets && cryptoMarkets.coins.length}
          <div class="wl-scroll cap-h">
            <table class="tbl">
              <thead>
                <tr><th>Coin</th><th class="num">Price</th><th class="num">1h</th><th class="num">24h</th><th class="num">7d</th><th class="num">Vol 24h</th><th class="num">Mkt Cap</th><th class="num">From ATH</th></tr>
              </thead>
              <tbody>
                {#each cryptoMarkets.coins as c0 (c0.id)}
                  <tr>
                    <td class="sym">{c0.symbol.toUpperCase()}</td>
                    <td class="num">${c0.price < 1 ? c0.price.toPrecision(4) : c0.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
                    <td class="num {`${(c0.chg_1h ?? 0) >= 0 ? 'pl-up' : 'pl-down'}`}">{c0.chg_1h != null ? `${c0.chg_1h >= 0 ? "+" : ""}${c0.chg_1h.toFixed(1)}%` : "—"}</td>
                    <td class="num {`${(c0.chg_24h ?? 0) >= 0 ? 'pl-up' : 'pl-down'}`}">{c0.chg_24h != null ? `${c0.chg_24h >= 0 ? "+" : ""}${c0.chg_24h.toFixed(1)}%` : "—"}</td>
                    <td class="num {`${(c0.chg_7d ?? 0) >= 0 ? 'pl-up' : 'pl-down'}`}">{c0.chg_7d != null ? `${c0.chg_7d >= 0 ? "+" : ""}${c0.chg_7d.toFixed(1)}%` : "—"}</td>
                    <td class="num dim">${(c0.volume_24h / 1e9).toFixed(2)}B</td>
                    <td class="num dim">${(c0.market_cap / 1e9).toFixed(1)}B</td>
                    <td class="num">
                      <span class="ath-bar" title={`ATH $${c0.ath.toLocaleString()}`}>
                        <span class="ath-fill" style={`width:${Math.max(2, Math.min(100, 100 + c0.ath_chg_pct))}%`}></span>
                      </span>
                      <span class="dim">{c0.ath_chg_pct.toFixed(0)}%</span>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="insider-note">Live CoinGecko market data (same source Deep Verify feeds the LLM). "From ATH" shows how far below the all-time high each coin trades. Refreshes every 5 minutes.</p>
        {:else}
          <div class="empty">CoinGecko market data unavailable</div>
        {/if}
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "cryptodesk"}
  <div class="span-12">
    <Panel title="Crypto Order Book (Level 2)" meta="Binance + Coinbase · live">
      <OrderBookPanel />
    </Panel>
  </div>
  {/if}

  {#if view === "cryptodesk"}
  <div class="span-12">
    <Panel title="Crypto Derivatives" meta="OKX perpetuals · funding, OI, liquidations">
      <CryptoDerivativesPanel />
    </Panel>
  </div>
  {/if}

  {#if view === "world"}
  <div class="span-6">
    <Panel title="Active Threats" meta="{threats.length} active">
      {#snippet children()}
        <div class="filters">
          <select bind:value={threatConfirm} onchange={loadThreats}>
            <option value="">All confirmations</option>
            <option value="corroborated">Corroborated</option>
            <option value="single_source">Single source</option>
            <option value="unconfirmed_social">Unconfirmed social</option>
          </select>
          <select bind:value={threatMinReliability} onchange={loadThreats}>
            <option value={0}>Any reliability</option>
            <option value={0.5}>≥ 50%</option>
            <option value={0.7}>≥ 70%</option>
            <option value={0.85}>≥ 85%</option>
          </select>
        </div>
        <div class="list">
          {#each threats as t (t.id)}
            <div
              class="row clickable"
              role="button"
              tabindex="0"
              onclick={() => (expandedThreat = expandedThreat === t.id ? null : t.id)}
              onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); expandedThreat = expandedThreat === t.id ? null : t.id; } }}
            >
              <Pill label={t.severity} tone={sevTone(t.severity)} />
              <div class="row-main">
                <div class="row-title">{t.title}</div>
                <div class="row-meta">
                  {t.country || t.region || "Global"} &middot; {fmtAgo(t.published_at)}
                  {#if t.confirmation_status}
                    &middot; <span class="confirm-tag {confirmTone(t.confirmation_status)}">{confirmLabel(t.confirmation_status)}{t.corroboration_count ? ` (${t.corroboration_count})` : ""}</span>
                  {/if}
                  {#if t.reliability_score != null}
                    &middot; reliability {pct(t.reliability_score)}
                  {/if}
                </div>
                {#if expandedThreat === t.id}
                  <div class="row-detail">
                    {#if t.description}<p>{t.description}</p>{/if}
                    {#if t.source_url}<a href={t.source_url} target="_blank" rel="noopener">{t.source || "source"} ↗</a>{/if}
                  </div>
                {/if}
              </div>
            </div>
          {:else}
            <div class="empty">No active threats</div>
          {/each}
        </div>
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "world"}
  <div class="span-6">
    <Panel title="News" meta="{news.length} items">
      {#snippet children()}
        <div class="filters">
          <select bind:value={newsConfirm} onchange={loadNews}>
            <option value="">All confirmations</option>
            <option value="corroborated">Corroborated</option>
            <option value="single_source">Single source</option>
            <option value="unconfirmed_social">Unconfirmed social</option>
          </select>
          <select bind:value={newsStale} onchange={loadNews}>
            <option value="">Fresh + stale</option>
            <option value="fresh">Fresh only</option>
            <option value="stale">Stale only</option>
          </select>
        </div>
        <div class="list">
          {#each news as n (n.id)}
            <div
              class="row clickable"
              role="button"
              tabindex="0"
              onclick={() => (expandedNews = expandedNews === n.id ? null : n.id)}
              onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); expandedNews = expandedNews === n.id ? null : n.id; } }}
            >
              <Pill label={n.sentiment ?? "neutral"} tone={sentTone(n.sentiment)} />
              <div class="row-main">
                <div class="row-title">{n.title}{#if n.is_stale}<span class="stale-tag"> stale</span>{/if}</div>
                <div class="row-meta">
                  {n.source} &middot; {fmtAgo(n.published_at)}
                  {#if n.confirmation_status}
                    &middot; <span class="confirm-tag {confirmTone(n.confirmation_status)}">{confirmLabel(n.confirmation_status)}{n.corroboration_count ? ` (${n.corroboration_count})` : ""}</span>
                  {/if}
                </div>
                {#if expandedNews === n.id}
                  <div class="row-detail">
                    {#if n.summary}<p>{n.summary}</p>{/if}
                    {#if n.url}<a href={n.url} target="_blank" rel="noopener">Read source ↗</a>{/if}
                  </div>
                {/if}
              </div>
            </div>
          {:else}
            <div class="empty">No recent news</div>
          {/each}
        </div>
      {/snippet}
    </Panel>
  </div>
  {/if}

  {#if view === "world"}
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
  {/if}
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
  .row.clickable {
    cursor: pointer;
  }
  .row-detail {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--line);
    font-size: 11.5px;
    color: var(--ink-dim);
    line-height: 1.5;
  }
  .row-detail p {
    margin: 0 0 6px;
  }
  .row-detail a {
    color: var(--accent);
    text-decoration: none;
    font-size: 11px;
  }
  .confirm-tag {
    text-transform: capitalize;
  }
  .confirm-tag.good {
    color: var(--good);
  }
  .confirm-tag.warm {
    color: var(--warm);
  }
  .confirm-tag.neutral {
    color: var(--ink-faint);
  }
  .stale-tag {
    color: var(--warm);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-left: 6px;
  }

  .filters {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  .filters select {
    background: var(--bg-alt, #0d1117);
    border: 1px solid var(--line-bright);
    color: var(--ink-dim);
    padding: 5px 8px;
    border-radius: 6px;
    font-size: 11px;
  }

  .ih-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 22px;
  }
  .ih-stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .ih-label {
    font-size: 9.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .ih-val {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    font-size: 15px;
    font-weight: 650;
  }
  .ih-val.small {
    font-size: 12px;
    font-weight: 500;
  }
  .ih-val.good {
    color: var(--good);
  }
  .ih-val.bad {
    color: var(--bad);
  }
  .ih-val.warm {
    color: var(--warm);
  }
  .ih-toggle {
    margin-left: auto;
    background: none;
    border: 1px solid var(--line-bright);
    color: var(--accent);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
  }
  .src-tbl {
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
  }
  .src-tbl .err {
    color: var(--ink-faint);
    font-size: 10.5px;
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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

  .trend-chart {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    height: 120px;
    padding-top: 8px;
  }
  .trend-col {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    height: 100%;
    gap: 4px;
  }
  .trend-bar {
    width: 100%;
    max-width: 28px;
    display: flex;
    flex-direction: column-reverse;
    border-radius: 3px 3px 0 0;
    overflow: hidden;
    margin-top: auto;
    min-height: 2px;
  }
  .trend-seg.critical {
    background: var(--critical, #ff3864);
  }
  .trend-seg.high {
    background: var(--warm);
  }
  .trend-seg.other {
    background: var(--accent);
  }
  .trend-count {
    font-size: 10px;
    color: var(--ink-dim);
    font-family: var(--mono);
  }
  .trend-date {
    font-size: 9px;
    color: var(--ink-faint);
  }
  .trend-legend {
    display: flex;
    gap: 14px;
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    font-size: 10.5px;
    color: var(--ink-faint);
  }
  .trend-legend .dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-right: 4px;
    vertical-align: middle;
  }
  .trend-legend .dot.critical {
    background: var(--critical, #ff3864);
  }
  .trend-legend .dot.high {
    background: var(--warm);
  }
  .trend-legend .dot.other {
    background: var(--accent);
  }

  .insider-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .insider-row {
    display: grid;
    grid-template-columns: 70px 1fr auto;
    align-items: center;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid var(--line);
  }
  .insider-row:last-child {
    border-bottom: none;
  }
  .insider-sym {
    font-weight: 650;
    font-size: 13px;
  }
  .insider-flags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }
  .insider-stats {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 3px;
    font-size: 11px;
    color: var(--ink-faint);
  }
  .insider-officers {
    grid-column: 1 / -1;
    font-size: 11px;
    color: var(--ink-dim);
    margin-top: -2px;
  }
  .insider-note {
    margin: 14px 0 0;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    font-size: 10.5px;
    color: var(--ink-faint);
    line-height: 1.5;
  }

  .dp-banner {
    font-size: 11px;
    color: var(--warm);
    background: rgba(255, 180, 84, 0.08);
    border: 1px solid rgba(255, 180, 84, 0.25);
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 12px;
    line-height: 1.5;
  }
  .si-name {
    color: var(--ink-faint);
    font-weight: 400;
    font-size: 10px;
    margin-left: 6px;
  }
  .si-detail {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    padding: 8px 4px;
    font-size: 11px;
  }
  .si-detail > div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .si-lbl {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
  }
  .si-note {
    flex-basis: 100%;
    color: var(--ink-faint);
    font-style: italic;
  }
  .dim {
    color: var(--ink-faint);
  }
  .psy-head {
    margin-bottom: 12px;
  }
  .psy-score {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .psy-num {
    font-size: 30px;
    font-weight: 700;
    line-height: 1;
  }
  .psy-meter {
    position: relative;
    height: 6px;
    margin: 10px 0 4px;
  }
  .psy-track {
    position: absolute;
    inset: 0;
    border-radius: 3px;
    background: linear-gradient(90deg, var(--bad), var(--warm), var(--good));
    opacity: 0.55;
  }
  .psy-marker {
    position: absolute;
    top: -3px;
    width: 2px;
    height: 12px;
    background: var(--ink);
    transform: translateX(-1px);
  }
  .psy-scale {
    display: flex;
    justify-content: space-between;
    font-size: 9px;
    color: var(--ink-faint);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .psy-markets {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: var(--space-sm);
    margin: 10px 0 12px;
  }
  .psy-mkt {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    padding: 8px 10px;
    text-align: center;
    background: var(--surface-raised);
  }
  .psy-mkt-name {
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .psy-mkt-score {
    font-size: 22px;
    font-weight: 700;
    line-height: 1.2;
  }
  .psy-mkt-label {
    font-size: 10px;
  }
  .psy-mkt-meta {
    font-size: 9px;
    margin-top: 2px;
  }
  .fx-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 12px;
  }
  .fx-card {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .fx-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .fx-pair {
    font-weight: 650;
    font-size: 12px;
  }
  .fx-chg {
    font-size: 10.5px;
  }
  .fx-rate {
    font-size: 19px;
    font-weight: 650;
    letter-spacing: 0.2px;
  }
  .ath-bar {
    display: inline-block;
    width: 46px;
    height: 5px;
    background: var(--surface-raised);
    border-radius: 3px;
    overflow: hidden;
    margin-right: 6px;
    vertical-align: middle;
  }
  .ath-fill {
    display: block;
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
  }
  .cap-h {
    max-height: 420px;
    overflow-y: auto;
  }
  .off-row {
    display: grid;
    grid-template-columns: 16px 1fr auto auto auto;
    gap: 10px;
    align-items: baseline;
    padding: 7px 4px;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    font-size: 12px;
  }
  .off-row:hover {
    background: rgba(124, 154, 255, 0.05);
  }
  .off-caret {
    color: var(--ink-faint);
  }
  .off-name {
    font-weight: 600;
  }
  .off-detail {
    padding: 4px 0 10px 26px;
  }
  .itx-head {
    margin: 12px 0 6px;
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .wl-scroll {
    overflow-x: auto;
  }
  .psy-roc {
    margin-top: 8px;
    font-size: 11px;
  }
  .cg-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }
  .cg-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    font-size: 10.5px;
    padding: 4px 8px;
    border: 1px solid var(--line);
    border-radius: 5px;
    background: var(--surface-raised);
  }
  .si-footnote {
    margin-top: 10px;
    font-size: 10px;
    line-height: 1.6;
    color: var(--ink-faint);
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
  .dp-venues {
    background: rgba(124, 154, 255, 0.03);
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .dp-venue-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 14px;
    font-size: 11.5px;
    color: var(--ink-dim);
  }
  .dp-venue-name {
    font-weight: 550;
  }
  .num.small {
    font-size: 10px;
    color: var(--ink-faint);
  }
  .empty.small {
    padding: 10px 0;
    font-size: 11px;
  }

  .macro-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 10px;
  }
  .macro-card {
    display: flex;
    flex-direction: column;
    gap: 3px;
    background: var(--surface-raised, rgba(255, 255, 255, 0.03));
    border-radius: 8px;
    padding: 10px 12px;
  }
  .macro-label {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
  }
  .macro-val {
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    font-size: 16px;
    font-weight: 650;
  }
  .macro-val.dim {
    color: var(--ink-faint);
    font-weight: 500;
  }
  .macro-unit {
    font-size: 10px;
    color: var(--ink-faint);
  }

  @media (max-width: 1180px) {
    .span-4,
    .span-6,
    .span-8 {
      grid-column: span 12;
    }
  }
</style>
