<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import ThreatMap from "../components/ThreatMap.svelte";
  import { api, type Regime, type Threat, type NewsArticle, type MarketAsset, type IntelligenceSource, type IntelligenceStatus, type ThreatExposure } from "../api";

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
    const [r, t, n, m, s, st, ex] = await Promise.all([
      api.regime().catch(() => null),
      api.threats(60, { confirmation: threatConfirm || undefined, minReliability: threatMinReliability || undefined }),
      api.news(40, { confirmation: newsConfirm || undefined, minReliability: newsMinReliability || undefined, stale: newsStale === "" ? undefined : newsStale === "stale" }),
      api.marketFull().catch(() => ({ equities: [], crypto: [], count: 0 })),
      api.intelligenceSources().catch(() => []),
      api.intelligenceStatus().catch(() => null),
      api.threatExposure().catch(() => null),
    ]);
    regime = r;
    threats = t;
    news = n;
    equities = m.equities.slice(0, 12);
    crypto = m.crypto.slice(0, 12);
    sources = s;
    intelStatus = st;
    exposure = ex;
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

  @media (max-width: 1180px) {
    .span-4,
    .span-6,
    .span-8 {
      grid-column: span 12;
    }
  }
</style>
