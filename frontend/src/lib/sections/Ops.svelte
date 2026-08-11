<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import TelegramWizard from "../components/TelegramWizard.svelte";
  import { api, type JobStatusMap, type PlatformConfig, type ConfigCreate, type LlmHealth, type CacheStats, type ErrorRateSummary } from "../api";
  import { toastStore } from "../stores/toast.svelte";
  import { wsStore } from "../stores/ws.svelte";

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

  function setBusy(key: string, v: boolean) {
    const next = new Set(busy);
    v ? next.add(key) : next.delete(key);
    busy = next;
  }

  async function loadAll() {
    jobs = await api.jobStatus().catch(() => ({}));
    configs = await api.settingsList().catch(() => []);
    llmHealth = await api.llmHealth().catch(() => null);
    cacheStats = await api.cacheStats().catch(() => null);
    orders = await api.alpacaOrders().catch(() => []);
    errorRate = await api.errorRate(15).catch(() => null);
  }

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
  let editCfg = $state<{ label: string; api_url: string; api_key: string }>({ label: "", api_url: "", api_key: "" });

  function startEdit(cfg: (typeof configs)[number]) {
    editingId = cfg.id;
    editCfg = { label: cfg.label ?? "", api_url: cfg.api_url ?? "", api_key: "" };
  }

  async function saveEdit() {
    if (!editingId) return;
    try {
      const body: Record<string, unknown> = { label: editCfg.label, api_url: editCfg.api_url };
      if (editCfg.api_key.trim()) body.api_key = editCfg.api_key.trim(); // blank = keep existing key
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

<div class="page-head">
  <h1>Ops</h1>
  <div class="sub">Job health/control and provider configuration — the engine room</div>
</div>

<div class="grid">
  <div class="span-4">
    <Panel title="LM Studio" dotColor={llmHealth?.ok ? "var(--good)" : "var(--bad)"} meta={llmHealth?.ok ? "reachable" : "unreachable"}>
      {#if llmHealth}
        <div class="stat-list">
          <div class="stat"><span>Platform</span><b>{llmHealth.platform ?? "—"}</b></div>
          <div class="stat"><span>Model</span><b>{llmHealth.model ?? "—"}</b></div>
          {#if llmHealth.error}<div class="stat"><span>Error</span><b class="pl-down">{llmHealth.error}</b></div>{/if}
        </div>
      {:else}
        <div class="empty">Checking…</div>
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="API Error Rate" dotColor={errorRate && errorRate.error_rate_pct > 5 ? "var(--bad)" : "var(--good)"} meta={errorRate ? `${errorRate.window_minutes}m window` : ""}>
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
        <div class="empty">Loading…</div>
      {/if}
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="OHLCV Cache" meta={cacheStats ? `${cacheStats.db_size_mb} MB` : ""}>
      {#snippet children()}
        {#if cacheStats}
          <div class="stat-list">
            <div class="stat"><span>Total Bars</span><b class="num">{cacheStats.total_bars.toLocaleString()}</b></div>
            <div class="stat"><span>Symbols Cached</span><b class="num">{cacheStats.symbols_cached}</b></div>
            <div class="stat"><span>Latest Bar</span><b class="num">{cacheStats.latest_bar_ts?.slice(0, 16).replace("T", " ") || "—"}</b></div>
          </div>
        {:else}
          <div class="empty">Loading…</div>
        {/if}
        <button class="btn tiny outline backfill-btn" disabled={backfilling} onclick={runBackfill}>
          {backfilling ? "Starting…" : "Backfill Now"}
        </button>
      {/snippet}
    </Panel>
  </div>

  <div class="span-7">
    <Panel title="Jobs" meta="{jobEntries.filter(([, j]) => j.status === 'ok').length}/{jobEntries.length} ok">
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
    <Panel title="Provider Settings" meta="{configs.length} configured">
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
                <input placeholder="API Key (leave blank to keep current)" type="password" bind:value={editCfg.api_key} />
                <button class="btn small primary" onclick={saveEdit}>Save Changes</button>
              </div>
            {/if}
          {:else}
            <div class="empty">No provider configs yet</div>
          {/each}
        </div>
      {/snippet}
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
    align-items: start;
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
