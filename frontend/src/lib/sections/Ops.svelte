<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import StateNote from "../components/StateNote.svelte";
  import TelegramWizard from "../components/TelegramWizard.svelte";
  import TrainingIntegrity from "../components/TrainingIntegrity.svelte";
  import { api, type JobStatusMap, type PlatformConfig, type ConfigCreate, type LlmHealth, type CacheStats, type ErrorRateSummary , type TradingPreference, type DataPlatformHealth, type ParityReport, type FeatureCorpus, type WalletActivityStatus, type HeliusHealth } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import { toastStore } from "../stores/toast.svelte";
  import { wsStore } from "../stores/ws.svelte";

  const feeds = new FeedTracker();

  let jobs = $state<JobStatusMap>({});
  let configs = $state<PlatformConfig[]>([]);
  let busy = $state<Set<string>>(new Set());
  let showAddForm = $state(false);
  let newCfg = $state<ConfigCreate>({ label: "", platform: "llm", config_type: "api", api_key: "", api_url: "" });
  let llmHealth = $state<LlmHealth | null>(null);
  let cacheStats = $state<CacheStats | null>(null);
  let backfilling = $state(false);
  let orders = $state<{ id: string; symbol: string; qty: number; side: string; status: string; type: string }[]>([]);
  let errorRate = $state<ErrorRateSummary | null>(null);
  let execPrefs = $state<TradingPreference | null>(null);
  let execSaving = $state(false);
  let platform = $state<DataPlatformHealth | null>(null);
  let parity = $state<ParityReport | null>(null);
  let corpus = $state<FeatureCorpus | null>(null);
  let wallet = $state<WalletActivityStatus | null>(null);
  let helius = $state<HeliusHealth | null>(null);

  const heliusEndpoints = $derived(
    Object.entries(helius?.metrics ?? {}).sort((a, b) => b[1].calls - a[1].calls),
  );
  const heliusOk = $derived(
    !!helius?.health?.rpc?.ok && !!helius?.health?.wallet_api?.ok,
  );

  async function loadExecPrefs() {
    execPrefs = await feeds.load("execPrefs", () => api.tradingPreference());
  }

  async function saveExecPrefs() {
    if (!execPrefs) return;
    execSaving = true;
    try {
      execPrefs = await api.updateExecutionCriteria({
        live_min_score: execPrefs.live_min_score,
        live_min_rr: execPrefs.live_min_rr,
        live_min_confidence: execPrefs.live_min_confidence,
      });
      toastStore.ok("Execution criteria saved — applies from the next execute run");
    } catch (e) {
      toastStore.err(`Save failed: ${e}`);
    } finally {
      execSaving = false;
    }
  }

  function setBusy(key: string, v: boolean) {
    const next = new Set(busy);
    v ? next.add(key) : next.delete(key);
    busy = next;
  }

  // Ops is the page an operator opens BECAUSE something looks wrong, so it is
  // the worst possible place for a failed request to render as a healthy
  // empty panel — "no errors in the last 15 minutes" and "could not reach the
  // error-rate endpoint" were the same screen. Each feed now reports itself.
  //
  // These also stopped being sequential: eleven awaits in a row meant the
  // page filled in over eleven round trips, and one slow provider held up
  // every panel behind it.
  async function loadAll() {
    loadExecPrefs();
    const [j, c, lh, cs, o, er, p, pa, co, w, hx] = await Promise.all([
      feeds.load("jobs", () => api.jobStatus()),
      feeds.load("configs", () => api.settingsList()),
      feeds.load("llmHealth", () => api.llmHealth()),
      feeds.load("cache", () => api.cacheStats()),
      feeds.load("orders", () => api.alpacaOrders()),
      feeds.load("errorRate", () => api.errorRate(15)),
      feeds.load("platform", () => api.dataPlatformHealth()),
      feeds.load("parity", () => api.dataParity()),
      feeds.load("corpus", () => api.featureCorpus()),
      feeds.load("wallet", () => api.walletActivityStatus()),
      feeds.load("helius", () => api.heliusHealth()),
    ]);
    jobs = j ?? {};
    configs = c ?? [];
    llmHealth = lh;
    cacheStats = cs;
    orders = o ?? [];
    errorRate = er;
    platform = p;
    parity = pa;
    corpus = co;
    wallet = w;
    helius = hx;
  }

  // Bytes/day per event kind — the §46 measurement, summed across symbols.
  const bytesByKind = $derived.by(() => {
    const acc: Record<string, { events: number; bytes: number }> = {};
    for (const r of platform?.bytes_by_day ?? []) {
      const k = (acc[r.kind] ??= { events: 0, bytes: 0 });
      k.events += r.events;
      k.bytes += r.bytes;
    }
    return Object.entries(acc).sort((a, b) => b[1].bytes - a[1].bytes);
  });

  // `status` is a free-form string from the API, so the accumulator needs an
  // index signature — but spreading one into an object literal drops it, so
  // the mapped rows lost `resolved`/`pending`/`abstained` and the template's
  // reads of them were the four "pre-existing" Ops errors. Naming the row
  // type keeps both: the known columns are checked, unknown statuses still
  // accumulate.
  type LabelRow = {
    horizon: number;
    avg?: number | null;
    resolved?: number;
    pending?: number;
    abstained?: number;
  };
  const labelRows = $derived.by(() => {
    const acc: Record<number, Record<string, number> & { avg?: number | null }> = {};
    for (const l of corpus?.labels ?? []) {
      const h = (acc[l.horizon_min] ??= {});
      h[l.status] = l.n;
      if (l.status === "resolved") h.avg = l.avg_forward_ret_pct;
    }
    return Object.entries(acc).map(([h, v]): LabelRow => ({ horizon: Number(h), ...v }));
  });

  const mb = (b: number) => (b / 1048576).toFixed(1);
  const fmtHorizon = (m: number) => (m >= 1440 ? `${m / 1440}d` : m >= 60 ? `${m / 60}h` : `${m}m`);

  async function runBackfill() {
    backfilling = true;
    try {
      const res = await api.cacheBackfill();
      toastStore.ok(res.message ?? "Backfill started");
    } catch (e) {
      toastStore.err(`Backfill failed: ${e}`);
    } finally {
      setTimeout(() => (backfilling = false), 2000);
    }
  }

  async function cancelOrder(id: string, symbol: string) {
    setBusy(id, true);
    try {
      await api.cancelOrder(id);
      toastStore.ok(`${symbol}: order cancelled`);
      await loadAll();
    } catch (e) {
      toastStore.err(`Cancel failed: ${e}`);
    } finally {
      setBusy(id, false);
    }
  }

  async function cancelAllOrders() {
    if (!confirm(`Cancel all ${orders.length} open orders?`)) return;
    try {
      await api.cancelAllOrders();
      toastStore.ok("All open orders cancelled");
      await loadAll();
    } catch (e) {
      toastStore.err(`Cancel all failed: ${e}`);
    }
  }

  let editingId = $state<string | null>(null);
  let editCfg = $state<{ label: string; api_url: string; api_key: string; api_secret: string }>({ label: "", api_url: "", api_key: "", api_secret: "" });

  function startEdit(cfg: (typeof configs)[number]) {
    editingId = cfg.id;
    editCfg = { label: cfg.label ?? "", api_url: cfg.api_url ?? "", api_key: "", api_secret: "" };
  }

  async function saveEdit() {
    if (!editingId) return;
    try {
      const body: Record<string, unknown> = { label: editCfg.label, api_url: editCfg.api_url };
      // Blank = keep whatever is stored. Alpaca (and most brokers) need BOTH
      // halves — a key without its secret authenticates nothing.
      const hasKey = !!editCfg.api_key.trim();
      const hasSecret = !!editCfg.api_secret.trim();
      if (hasKey !== hasSecret) {
        const which = hasKey ? "key without its secret" : "secret without its key";
        if (!confirm(`You entered a new ${which}. Brokers validate the pair together, so a mismatched half means every request fails with "unauthorized".

Save anyway?`)) return;
      }
      if (hasKey) body.api_key = editCfg.api_key.trim();
      if (hasSecret) body.api_secret = editCfg.api_secret.trim();
      await api.updateSetting(editingId, body);
      toastStore.ok("Provider updated");
      editingId = null;
      await loadAll();
    } catch (e) {
      toastStore.err(`Update failed: ${e}`);
    }
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 15_000);
    const unsub = wsStore.on("job_status", (msg) => {
      jobs = { ...jobs, ...(msg.data as JobStatusMap) };
    });
    return () => {
      clearInterval(poll);
      unsub();
    };
  });

  async function triggerJob(name: string) {
    setBusy(name, true);
    try {
      const res = await api.jobTrigger(name);
      if (res.ok) toastStore.ok(`${name}: started`);
      else toastStore.err(res.detail ?? `${name}: already running`);
      await loadAll();
    } catch (e) {
      toastStore.err(`${name}: trigger failed — ${e}`);
    } finally {
      setBusy(name, false);
    }
  }

  async function resetJob(name: string) {
    if (!confirm(`Reset '${name}' status to idle? This only clears the tracking flag — it doesn't stop a thread that's actually still running.`)) return;
    try {
      await api.jobReset(name);
      toastStore.ok(`${name}: reset to idle`);
      await loadAll();
    } catch (e) {
      toastStore.err(`${name}: reset failed — ${e}`);
    }
  }

  async function toggleActive(cfg: PlatformConfig) {
    setBusy(cfg.id, true);
    try {
      await api.settingsUpdate(cfg.id, { is_active: !cfg.is_active });
      toastStore.ok(`${cfg.label}: ${cfg.is_active ? "disabled" : "enabled"}`);
      await loadAll();
    } catch (e) {
      toastStore.err(`Update failed: ${e}`);
    } finally {
      setBusy(cfg.id, false);
    }
  }

  async function setDefault(cfg: PlatformConfig) {
    try {
      await api.settingsSetDefault(cfg.id);
      toastStore.ok(`${cfg.label} set as default for ${cfg.platform}`);
      await loadAll();
    } catch (e) {
      toastStore.err(`Failed: ${e}`);
    }
  }

  async function deleteConfig(cfg: PlatformConfig) {
    if (!confirm(`Delete "${cfg.label}"? This cannot be undone.`)) return;
    try {
      await api.settingsDelete(cfg.id);
      toastStore.ok(`${cfg.label} deleted`);
      await loadAll();
    } catch (e) {
      toastStore.err(`Delete failed: ${e}`);
    }
  }

  async function createConfig() {
    if (!newCfg.label.trim() || !newCfg.platform.trim()) {
      toastStore.err("Label and platform are required");
      return;
    }
    try {
      await api.settingsCreate(newCfg);
      toastStore.ok(`${newCfg.label} added`);
      newCfg = { label: "", platform: "llm", config_type: "api", api_key: "", api_url: "" };
      showAddForm = false;
      await loadAll();
    } catch (e) {
      toastStore.err(`Create failed: ${e}`);
    }
  }

  const fmtAgo = (iso: string | null) => {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
  const jobEntries = $derived(Object.entries(jobs));
</script>

<!--
  Platform mode and training-data integrity come FIRST. What JARVIS is
  permitted to do, and whether its own invariants are holding, outrank
  every job status below: a green scheduler running on corrupted evidence
  is worse than a red one.
-->
<TrainingIntegrity />


<div class="page-head">
  <h1>Ops</h1>
  <div class="sub">Job health/control and provider configuration — the engine room</div>
</div>

<div class="grid">
  <div class="span-4">
    <Panel title="LM Studio" dotColor={llmHealth?.ok ? "var(--good)" : "var(--bad)"} meta={llmHealth ? (llmHealth.ok ? "reachable" : "unreachable") : ""} status={feeds.status("llmHealth")}>
      {#if llmHealth}
        <div class="stat-list">
          <div class="stat"><span>Platform</span><b>{llmHealth.platform ?? "—"}</b></div>
          <div class="stat"><span>Model</span><b>{llmHealth.model ?? "—"}</b></div>
          {#if llmHealth.error}<div class="stat"><span>Error</span><b class="pl-down">{llmHealth.error}</b></div>{/if}
        </div>
      {:else}
        <StateNote status={feeds.status("llmHealth")} noun="LM Studio health" />
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="API Error Rate" dotColor={errorRate && errorRate.error_rate_pct > 5 ? "var(--bad)" : "var(--good)"} meta={errorRate ? `${errorRate.window_minutes}m window` : ""} status={feeds.status("errorRate")}>
      {#if errorRate}
        <div class="stat-list">
          <div class="stat"><span>Requests</span><b class="num">{errorRate.total_requests}</b></div>
          <div class="stat"><span>5xx Errors</span><b class="num {errorRate.error_count ? 'pl-down' : ''}">{errorRate.error_count}</b></div>
          <div class="stat"><span>Error Rate</span><b class="num {errorRate.error_rate_pct > 5 ? 'pl-down' : ''}">{errorRate.error_rate_pct}%</b></div>
        </div>
        {#if errorRate.top_error_paths.length}
          <div class="err-paths">
            {#each errorRate.top_error_paths as p (p.path)}
              <div class="err-path-row"><span>{p.path}</span><b class="num">{p.count}</b></div>
            {/each}
          </div>
        {/if}
      {:else}
        <StateNote status={feeds.status("errorRate")} noun="error-rate data" />
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="OHLCV Cache" meta={cacheStats ? `${cacheStats.db_size_mb} MB` : ""} status={feeds.status("cache")}>
      {#snippet children()}
        {#if cacheStats}
          <div class="stat-list">
            <div class="stat"><span>Total Bars</span><b class="num">{cacheStats.total_bars.toLocaleString()}</b></div>
            <div class="stat"><span>Symbols Cached</span><b class="num">{cacheStats.symbols_cached}</b></div>
            <div class="stat"><span>Latest Bar</span><b class="num">{cacheStats.latest_bar_ts?.slice(0, 16).replace("T", " ") || "—"}</b></div>
          </div>
        {:else}
          <StateNote status={feeds.status("cache")} noun="cache statistics" />
        {/if}
        <button class="btn tiny outline backfill-btn" disabled={backfilling} onclick={runBackfill}>
          {backfilling ? "Starting…" : "Backfill Now"}
        </button>
      {/snippet}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Event Platform" dotColor={platform && platform.queues.every((q) => q.dropped_total === 0) ? "var(--good)" : "var(--warn)"} meta={platform ? `${platform.store.events.toLocaleString()} events · ${mb(platform.store.file_bytes)} MB` : ""} status={feeds.status("platform")}>
      {#if platform}
        <div class="stat-list">
          {#each platform.books as b (b.stream)}
            <div class="stat">
              <span>{b.stream} <Pill label={b.valid ? "ok" : b.reason} tone={b.valid ? "good" : "bad"} /></span>
              <b class="num">{b.age_seconds < 10 ? "live" : `${b.age_seconds.toFixed(0)}s`}</b>
            </div>
          {/each}
          {#each bytesByKind as [kind, v] (kind)}
            <div class="stat"><span>{kind} /day</span><b class="num">{v.events.toLocaleString()} · {mb(v.bytes)} MB</b></div>
          {/each}
          {#each platform.queues.filter((q) => q.dropped_total > 0) as q (q.name)}
            <div class="stat"><span>{q.name} drops</span><b class="num pl-down">{q.dropped_total}</b></div>
          {/each}
        </div>
      {:else}
        <StateNote status={feeds.status("platform")} noun="event platform health" />
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Feed Parity" dotColor={parity && parity.pairs.every((p) => p.verdict === "parity") ? "var(--good)" : "var(--warn)"} meta={parity ? `${parity.symbol} · ${parity.window_min}m window` : ""} status={feeds.status("parity")}>
      {#if parity}
        {#if parity.pairs.length}
          <div class="stat-list">
            {#each parity.pairs as p (`${p.a}-${p.b}`)}
              <div class="stat">
                <span>{p.a} ↔ {p.b} <Pill label={p.verdict} tone={p.verdict === "parity" ? "good" : p.verdict === "divergent" ? "bad" : "neutral"} /></span>
                <b class="num">{p.median_bps.toFixed(1)} bps</b>
              </div>
            {/each}
          </div>
          <div class="parity-note">median mid-price gap; two venues measuring one market should agree within {parity.pairs[0]?.threshold_bps ?? 20} bps</div>
        {:else}
          <div class="empty">No overlapping venue data in window</div>
        {/if}
      {:else}
        <StateNote status={feeds.status("parity")} noun="feed parity" />
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Feature Corpus" meta={corpus ? `${Object.values(corpus.snapshots).reduce((a, b) => a + b, 0)} snapshots` : ""} status={feeds.status("corpus")}>
      {#if corpus}
        <div class="stat-list">
          {#each Object.entries(corpus.snapshots) as [k, n] (k)}
            <div class="stat"><span>{k}</span><b class="num">{n}</b></div>
          {/each}
          {#each labelRows as r (r.horizon)}
            <div class="stat">
              <span>{fmtHorizon(r.horizon)} labels</span>
              <b class="num">
                {r.resolved ?? 0} resolved{r.avg != null ? ` (${r.avg > 0 ? "+" : ""}${r.avg}%)` : ""} · {r.pending ?? 0} pend{r.abstained ? ` · ${r.abstained} abst` : ""}
              </b>
            </div>
          {/each}
        </div>
      {:else}
        <StateNote status={feeds.status("corpus")} noun="feature corpus" />
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel
      title="Wallet Flow (Helius)"
      dotColor={!wallet ? "var(--dim)" : !wallet.configured ? "var(--dim)" : wallet.events_stored ? "var(--good)" : "var(--warn)"}
      meta={wallet ? (wallet.configured ? `${wallet.wallets_watched} watched` : "not configured") : ""}
      status={feeds.status("wallet")}
    >
      {#if !wallet}
        <StateNote status={feeds.status("wallet")} noun="wallet collector status" />
      {:else}
        <div class="stat-list">
          <!-- Deliberately three states, not two. A collector that polls
               happily and stores nothing looks identical to a quiet chain
               unless the panel separates them. -->
          <div class="stat">
            <span>Status</span>
            <b>
              {#if !wallet.has_key}
                no API key
              {:else if !wallet.wallets_watched}
                no wallets watched
              {:else if wallet.events_stored}
                collecting
              {:else}
                configured · nothing stored yet
              {/if}
            </b>
          </div>
          <div class="stat"><span>Events stored</span><b class="num">{(wallet.events_stored ?? 0).toLocaleString()}</b></div>
          <div class="stat"><span>Parser</span><b>{wallet.parser}</b></div>
          <div class="stat"><span>Page limit</span><b class="num">{wallet.page_limit}</b></div>
          {#if wallet.store_error}
            <div class="stat"><span>Store</span><b class="bad">{wallet.store_error}</b></div>
          {/if}
          {#each wallet.top_symbols ?? [] as [sym, n] (sym)}
            <div class="stat">
              <span>{sym.length > 12 ? sym.slice(0, 6) + "…" + sym.slice(-4) : sym}</span>
              <b class="num">{n}</b>
            </div>
          {/each}
        </div>
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <!-- The single Helius door's own telemetry. lib/helius_client has
         recorded per-endpoint calls, errors and latency since it was
         written; until now reading it meant opening a Python prompt. -->
    <Panel
      title="Helius API"
      dotColor={!helius?.configured ? "var(--ink-faint)" : heliusOk ? "var(--good)" : "var(--bad)"}
      meta={helius?.configured ? `${heliusEndpoints.length} endpoints used` : ""}
      status={feeds.status("helius")}
    >
      {#if !helius}
        <StateNote status={feeds.status("helius")} noun="Helius client health" />
      {:else if !helius.configured}
        <div class="stat-list">
          <div class="stat"><span>Status</span><b>not configured</b></div>
          <div class="stat"><span>Detail</span><b>{helius.detail ?? "HELIUS_API_KEY not set"}</b></div>
        </div>
      {:else}
        <div class="stat-list">
          <div class="stat">
            <span>JSON-RPC</span>
            <b class={helius.health?.rpc?.ok ? "" : "bad"}>
              {helius.health?.rpc?.ok ? `ok · ${helius.health.rpc.ms}ms` : (helius.health?.rpc?.error ?? "unknown")}
            </b>
          </div>
          <div class="stat">
            <span>Wallet API</span>
            <b class={helius.health?.wallet_api?.ok ? "" : "bad"}>
              {helius.health?.wallet_api?.ok ? `ok · ${helius.health.wallet_api.ms}ms` : (helius.health?.wallet_api?.error ?? "unknown")}
            </b>
          </div>
          {#each heliusEndpoints as [name, m] (name)}
            <div class="stat">
              <span>{name}</span>
              <b class="num {m.errors ? 'bad' : ''}">
                {m.calls} calls{m.errors ? ` · ${m.errors} err` : ""}{m.ms_avg != null ? ` · ${Math.round(m.ms_avg)}ms` : ""}
              </b>
            </div>
          {/each}
          {#if !heliusEndpoints.length}
            <div class="stat"><span>Endpoints</span><b>none called since restart</b></div>
          {/if}
        </div>
      {/if}
    </Panel>
  </div>

  <div class="span-7">
    <Panel title="Jobs" meta="{jobEntries.filter(([, j]) => j.status === 'ok').length}/{jobEntries.length} ok" status={feeds.status("jobs")}>
      <div class="job-grid">
        {#each jobEntries as [name, job] (name)}
          <div class="job-card">
            <div class="jc-top">
              <span class="jc-name">{name}</span>
              <Pill
                label={job.status}
                tone={job.status === "ok" ? "good" : job.status === "error" ? "bad" : job.status === "running" ? "warm" : "neutral"}
              />
            </div>
            <div class="jc-last">last: {fmtAgo(job.last)}</div>
            {#if job.error}<div class="jc-error">{job.error}</div>{/if}
            <div class="jc-actions">
              <button class="btn tiny" disabled={busy.has(name)} onclick={() => triggerJob(name)}>Run Now</button>
              {#if job.status === "running"}
                <button class="btn tiny outline" onclick={() => resetJob(name)}>Reset</button>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    </Panel>
  </div>

  <div class="span-5">
    <Panel title="Provider Settings" meta="{configs.length} configured" status={feeds.status("configs")}>
      {#snippet children()}
        <button class="btn small primary" onclick={() => (showAddForm = !showAddForm)}>
          {showAddForm ? "Cancel" : "+ Add Config"}
        </button>

        {#if showAddForm}
          <div class="add-form">
            <input placeholder="Label" bind:value={newCfg.label} />
            <input placeholder="Platform (llm, alpaca, telegram, ...)" bind:value={newCfg.platform} />
            <input placeholder="API URL" bind:value={newCfg.api_url} />
            <input placeholder="API Key" type="password" bind:value={newCfg.api_key} />
            <button class="btn small primary" onclick={createConfig}>Save</button>
          </div>
        {/if}

        <div class="cfg-list">
          {#each configs as cfg (cfg.id)}
            <div class="cfg-row">
              <div class="cfg-main">
                <div class="cfg-label">
                  {cfg.label}
                  {#if cfg.is_default}<Pill label="default" tone="neutral" />{/if}
                  <Pill label={cfg.is_active ? "active" : "inactive"} tone={cfg.is_active ? "good" : "neutral"} />
                </div>
                <div class="cfg-meta">{cfg.platform} &middot; {cfg.has_api_key ? "key set" : "no key"}</div>
              </div>
              <div class="cfg-actions">
                <button class="btn tiny outline" onclick={() => (editingId === cfg.id ? (editingId = null) : startEdit(cfg))}>
                  {editingId === cfg.id ? "Cancel" : "Edit"}
                </button>
                <button class="btn tiny" disabled={busy.has(cfg.id)} onclick={() => toggleActive(cfg)}>
                  {cfg.is_active ? "Disable" : "Enable"}
                </button>
                {#if !cfg.is_default}
                  <button class="btn tiny outline" onclick={() => setDefault(cfg)}>Default</button>
                {/if}
                <button class="btn tiny ghost" onclick={() => deleteConfig(cfg)}>✕</button>
              </div>
            </div>
            {#if editingId === cfg.id}
              <div class="add-form edit-form">
                <input placeholder="Label" bind:value={editCfg.label} />
                <input placeholder="API URL" bind:value={editCfg.api_url} />
                <input placeholder="API Key (blank = keep current)" type="password" autocomplete="off" bind:value={editCfg.api_key} />
                <input placeholder="API Secret (blank = keep current)" type="password" autocomplete="off" bind:value={editCfg.api_secret} />
                <button class="btn small primary" onclick={saveEdit}>Save Changes</button>
              </div>
            {/if}
          {:else}
            <StateNote status={feeds.status("configs")} noun="provider configs" emptyText="No provider configs yet" />
          {/each}
        </div>
      {/snippet}
    </Panel>
  </div>

  <div class="span-12">
    <Panel title="Execution Criteria" meta="governs the broker account AND the paper book" status={feeds.status("execPrefs")}>
      {#if execPrefs}
        <div class="exec-grid">
          <label class="exec-field">
            <span>Min composite score (0–100)</span>
            <input type="number" min="0" max="100" step="1" bind:value={execPrefs.live_min_score} />
            <i>High-risk regime enforces a 75 floor regardless.</i>
          </label>
          <label class="exec-field">
            <span>Min R:R ratio (0 = off)</span>
            <input type="number" min="0" max="10" step="0.1" bind:value={execPrefs.live_min_rr} />
            <i>e.g. 2 = only take setups paying 2:1 or better.</i>
          </label>
          <label class="exec-field">
            <span>Min AI confidence % (0 = off)</span>
            <input type="number" min="0" max="100" step="1" bind:value={execPrefs.live_min_confidence} />
            <i>The LLM's own confidence on the signal.</i>
          </label>
          <button class="btn small" disabled={execSaving} onclick={saveExecPrefs}>{execSaving ? "Saving…" : "Save criteria"}</button>
        </div>
        <p class="exec-note">
          Two automatic books run side by side: <b>Auto Sim</b> takes EVERY approved signal unconditionally ($1k virtual each),
          while the <b>Alpaca account</b> only takes approved signals clearing all criteria above. Comparing the two tells you
          whether the criteria are earning their keep.
        </p>
      {:else}
        <StateNote status={feeds.status("execPrefs")} noun="execution criteria" />
      {/if}
    </Panel>
  </div>
  <div class="span-12">
    <TelegramWizard {configs} onSaved={loadAll} />
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
  }
  .span-4 {
    grid-column: span 4;
  }
  .span-5 {
    grid-column: span 5;
  }
  .span-7 {
    grid-column: span 7;
  }
  .exec-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    align-items: flex-end;
  }
  .exec-field {
    display: flex;
    flex-direction: column;
    gap: 5px;
    flex: 1 1 200px;
  }
  .exec-field span {
    font-size: 10.5px;
    letter-spacing: 0.05em;
    color: var(--ink-faint);
    font-weight: 600;
  }
  .exec-field input {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--ink);
    font: inherit;
    padding: 7px 10px;
  }
  .exec-field i {
    font-style: normal;
    font-size: 9.5px;
    color: var(--ink-faint);
  }
  .exec-note {
    font-size: 10.5px;
    color: var(--ink-dim);
    border-top: 1px solid var(--line);
    margin: 12px 0 0;
    padding-top: 10px;
    line-height: 1.5;
  }
  .span-12 {
    grid-column: span 12;
  }

  .stat-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .stat {
    display: flex;
    justify-content: space-between;
    font-size: 12.5px;
    color: var(--ink-dim);
  }
  .stat b {
    font-family: var(--mono);
    max-width: 60%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pl-down {
    color: var(--bad);
  }
  .err-paths {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .err-path-row {
    display: flex;
    justify-content: space-between;
    font-size: 10.5px;
    color: var(--ink-faint);
  }
  .err-path-row span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 70%;
  }
  .backfill-btn,


  .job-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }
  .job-card {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px;
  }
  .jc-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }
  .jc-name {
    font-size: 12.5px;
    font-weight: 600;
  }
  .jc-last {
    font-size: 10.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
  }
  .jc-error {
    font-size: 10px;
    color: var(--bad);
    margin-top: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .jc-actions {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }

  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 6px 10px;
    border-radius: 7px;
    font-size: 11px;
    cursor: pointer;
  }
  .btn.small {
    width: 100%;
    padding: 7px 10px;
    font-size: 12px;
    margin-bottom: 10px;
  }
  .btn.primary {
    background: rgba(124, 154, 255, 0.15);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .btn.tiny {
    padding: 4px 9px;
    font-size: 10.5px;
  }
  .btn.outline {
    background: transparent;
  }
  .btn.ghost {
    background: transparent;
    border-color: transparent;
    color: var(--ink-faint);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .add-form {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 14px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--line);
  }
  input {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 7px 9px;
    font-size: 12px;
  }

  .cfg-list {
    display: flex;
    flex-direction: column;
  }
  .cfg-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
    padding: 9px 0;
    border-bottom: 1px solid var(--line);
  }
  .cfg-row:last-child {
    border-bottom: none;
  }
  .cfg-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12.5px;
    font-weight: 600;
    flex-wrap: wrap;
  }
  .cfg-meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    margin-top: 3px;
  }
  .cfg-actions {
    display: flex;
    gap: 5px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .empty {
    padding: 20px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }

  .parity-note {
    margin-top: 8px;
    font-size: 11px;
    line-height: 1.45;
    color: var(--ink-faint);
  }

  @media (max-width: 1180px) {
    .span-4,
    .span-5,
    .span-7,
    .span-12 {
      grid-column: span 12;
    }
    .job-grid {
      grid-template-columns: repeat(2, 1fr);
    }
  }
</style>
