<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import RadialScore from "../components/RadialScore.svelte";
  import SignalAnalysisModal from "../components/SignalAnalysisModal.svelte";
  import { api, type Signal, type AnalyzeResult, type ScannerStatus, type TradingPreference } from "../api";
  import { toastStore } from "../stores/toast.svelte";
  import { downloadCsv } from "../csv";

  // ── trade horizon preference ────────────────────────────────────────
  const HORIZON_MODES: ["scalp" | "longer" | "all", string][] = [
    ["scalp", "Scalp"],
    ["longer", "Longer"],
    ["all", "Both"],
  ];
  let preference = $state<TradingPreference | null>(null);
  async function loadPreference() {
    preference = await api.tradingPreference().catch(() => null);
  }
  async function setTradeMode(mode: "scalp" | "longer" | "all") {
    try {
      preference = await api.setTradeMode(mode);
      toastStore.ok(`Trade horizon set to ${mode}`);
    } catch (e) {
      toastStore.err(`Failed to update trade horizon: ${e}`);
    }
  }

  // ── signal analysis modal ───────────────────────────────────────────
  let analysisSignalId = $state<string | null>(null);

  // ── signal list ──────────────────────────────────────────────────────
  let signals = $state<Signal[]>([]);
  let statusFilter = $state("Active");
  let classFilter = $state("");
  let busyIds = $state<Set<string>>(new Set());

  async function loadSignals() {
    try {
      signals = await api.signals(statusFilter || undefined, 200);
    } catch (e) {
      toastStore.err(`Failed to load signals: ${e}`);
    }
  }

  $effect(() => {
    statusFilter;
    loadSignals();
  });

  $effect(() => {
    loadPreference();
  });

  const filteredSignals = $derived(
    classFilter ? signals.filter((s) => (s.asset_class ?? "").toLowerCase() === classFilter.toLowerCase()) : signals,
  );

  // ── saved filter presets (localStorage — this is view state, not data the
  // backend needs to know about) ──────────────────────────────────────────
  type FilterPreset = { name: string; status: string; klass: string };
  const PRESETS_KEY = "jarvis.signalFilterPresets";
  let presets = $state<FilterPreset[]>(JSON.parse(localStorage.getItem(PRESETS_KEY) || "[]"));

  function savePreset() {
    const name = window.prompt("Name this filter preset:", `${statusFilter || "All"} / ${classFilter || "All"}`);
    if (!name) return;
    presets = [...presets.filter((p) => p.name !== name), { name, status: statusFilter, klass: classFilter }];
    localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
    toastStore.ok(`Preset "${name}" saved`);
  }

  function applyPreset(p: FilterPreset) {
    statusFilter = p.status;
    classFilter = p.klass;
  }

  function deletePreset(name: string) {
    presets = presets.filter((p) => p.name !== name);
    localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
  }

  function exportSignalsCsv() {
    downloadCsv(
      "signals",
      ["symbol", "direction", "status", "asset_class", "entry", "target", "stop", "confidence", "rr_ratio", "signal_source", "generated_at"],
      filteredSignals.map((s) => [s.asset_symbol, s.direction, s.status, s.asset_class ?? "", s.entry_price, s.target_price, s.stop_loss, s.confidence, s.rr_ratio, s.signal_source, s.generated_at]),
    );
  }

  function setBusy(id: string, busy: boolean) {
    const next = new Set(busyIds);
    busy ? next.add(id) : next.delete(id);
    busyIds = next;
  }

  async function doAction(sig: Signal, action: "approve" | "reject" | "execute" | "paper" | "delete") {
    setBusy(sig.id, true);
    try {
      if (action === "approve") await api.approveSignal(sig.id);
      else if (action === "reject") await api.rejectSignal(sig.id);
      else if (action === "execute") await api.executeSignal(sig.id);
      else if (action === "paper") await api.paperExecuteSignal(sig.id, sig.paper_direction ?? "Long");
      else if (action === "delete") await api.deleteSignal(sig.id);
      toastStore.ok(`${sig.asset_symbol}: ${action} done`);
      await loadSignals();
    } catch (e) {
      toastStore.err(`${sig.asset_symbol}: ${action} failed — ${e}`);
    } finally {
      setBusy(sig.id, false);
    }
  }

  async function clearExpired() {
    try {
      const res = await api.clearExpiredSignals();
      toastStore.ok(`Cleared ${res.cleared ?? 0} expired signal(s)`);
      await loadSignals();
    } catch (e) {
      toastStore.err(`Clear expired failed: ${e}`);
    }
  }

  // ── pending queue bulk actions ──────────────────────────────────────
  async function approveAllPending() {
    if (!confirm(`Force-execute all pending signals now, ahead of the 9:30 AM ET auto-execute?`)) return;
    try {
      const res = await api.approveAllSignals();
      toastStore.ok(`Approved ${res.approved ?? "all"} pending signal(s)`);
      await loadSignals();
    } catch (e) {
      toastStore.err(`Approve all failed: ${e}`);
    }
  }

  async function rejectAllPending() {
    if (!confirm(`Reject all pending signals? They will not auto-execute.`)) return;
    try {
      const res = await api.rejectAllSignals();
      toastStore.ok(`Rejected ${res.rejected ?? "all"} pending signal(s)`);
      await loadSignals();
    } catch (e) {
      toastStore.err(`Reject all failed: ${e}`);
    }
  }

  async function cancelAllOpenOrders() {
    if (!confirm(`Cancel every open Alpaca order?`)) return;
    try {
      await api.cancelAllOrders();
      toastStore.ok("All open orders cancelled");
    } catch (e) {
      toastStore.err(`Cancel all orders failed: ${e}`);
    }
  }

  async function saveGeneratedSignal() {
    if (!analyzeResult?.signal || analyzeResult.signal.error) return;
    try {
      await api.saveSignal({ ...analyzeResult.signal, asset_symbol: analyzeResult.symbol });
      toastStore.ok(`${analyzeResult.symbol}: signal saved`);
      await loadSignals();
    } catch (e) {
      toastStore.err(`Save failed: ${e}`);
    }
  }

  // ── scanner ──────────────────────────────────────────────────────────
  const SCANNER_MODES: { key: "pre_market" | "intraday" | "crypto" | "futures"; label: string }[] = [
    { key: "pre_market", label: "Pre-Market" },
    { key: "intraday", label: "Intraday" },
    { key: "crypto", label: "Crypto 24/7" },
    { key: "futures", label: "Futures/Forex" },
  ];
  let scannerStatus = $state<ScannerStatus | null>(null);
  let scannerRunning = $state<Set<string>>(new Set());

  async function loadScannerStatus() {
    scannerStatus = await api.scannerStatus().catch(() => null);
  }

  async function runScanner(mode: (typeof SCANNER_MODES)[number]["key"]) {
    const next = new Set(scannerRunning);
    next.add(mode);
    scannerRunning = next;
    try {
      await api.runScanner(mode);
      toastStore.ok(`Scanner [${mode}] started`);
      setTimeout(loadScannerStatus, 1500);
    } catch (e) {
      toastStore.err(`Scanner [${mode}] failed to start: ${e}`);
    } finally {
      setTimeout(() => {
        const n = new Set(scannerRunning);
        n.delete(mode);
        scannerRunning = n;
      }, 2000);
    }
  }

  // ── manual analysis ──────────────────────────────────────────────────
  const ALL_TFS = ["1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D"];
  let analyzeSymbol = $state("");
  let selectedTfs = $state<Set<string>>(new Set(["1H", "4H", "1D"]));
  let generateSignal = $state(false);
  let analyzing = $state(false);
  let analyzeResult = $state<AnalyzeResult | null>(null);

  function toggleTf(tf: string) {
    const next = new Set(selectedTfs);
    next.has(tf) ? next.delete(tf) : next.add(tf);
    selectedTfs = next;
  }

  async function runAnalyze() {
    if (!analyzeSymbol.trim()) {
      toastStore.err("Enter a symbol first");
      return;
    }
    analyzing = true;
    analyzeResult = null;
    try {
      analyzeResult = await api.analyze(analyzeSymbol.trim().toUpperCase(), [...selectedTfs], generateSignal);
    } catch (e) {
      toastStore.err(`Analysis failed: ${e}`);
    } finally {
      analyzing = false;
    }
  }

  $effect(() => {
    loadScannerStatus();
    const poll = setInterval(loadScannerStatus, 15_000);
    return () => clearInterval(poll);
  });

  function isPending(s: Signal) {
    return s.status === "PendingApproval";
  }
</script>

<div class="page-head">
  <div>
    <h1>Signals &amp; Scanner</h1>
    <div class="sub">Signal review, manual analysis, and the opportunity scanner in one workspace</div>
  </div>
  <div class="horizon-toggle" title="Which timeframes the LLM is allowed to propose signals for">
    {#each HORIZON_MODES as [mode, label] (mode)}
      <button class="h-btn" class:on={preference?.trade_mode === mode} onclick={() => setTradeMode(mode)}>{label}</button>
    {/each}
  </div>
</div>

{#if analysisSignalId}
  <SignalAnalysisModal signalId={analysisSignalId} onClose={() => (analysisSignalId = null)} />
{/if}

<div class="grid">
  <div class="span-4">
    <Panel title="Manual Analysis" meta={analyzing ? "running…" : ""}>
      <div class="field">
        <label for="an-sym">Symbol</label>
        <input id="an-sym" bind:value={analyzeSymbol} placeholder="NVDA, BTC/USD..." />
      </div>
      <div class="field">
        <span class="flabel">Timeframes</span>
        <div class="tf-row">
          {#each ALL_TFS as tf (tf)}
            <label class="tf-chip" class:on={selectedTfs.has(tf)}>
              <input type="checkbox" checked={selectedTfs.has(tf)} onchange={() => toggleTf(tf)} />
              {tf}
            </label>
          {/each}
        </div>
      </div>
      <label class="check-row">
        <input type="checkbox" bind:checked={generateSignal} />
        Generate LLM signal
      </label>
      <button class="btn primary" onclick={runAnalyze} disabled={analyzing}>
        {analyzing ? "Analyzing…" : "Analyze"}
      </button>

      {#if analyzeResult}
        <div class="an-result">
          {#each Object.entries(analyzeResult.ta) as [tf, d] (tf)}
            {#if d && !d.error}
              <div class="an-tf">
                <div class="an-tf-head">
                  <b>{tf}</b>
                  <Pill label={d.bias ?? "neutral"} tone={d.bias === "bullish" ? "good" : d.bias === "bearish" ? "bad" : "neutral"} />
                </div>
                <div class="an-metrics">
                  <span>RSI <b class="num">{d.rsi ?? "—"}</b></span>
                  <span>ATR% <b class="num">{d.atr?.pct ?? "—"}</b></span>
                  <span>EMA21 <b class="num">{d.emas?.ema21 ?? "—"}</b></span>
                </div>
              </div>
            {/if}
          {/each}
          {#if analyzeResult.signal && !analyzeResult.signal.error}
            <div class="an-signal">
              <div class="an-signal-head">
                Generated signal
                <button class="btn tiny outline save-btn" onclick={saveGeneratedSignal}>Save to Signals</button>
              </div>
              <div class="num">
                {analyzeResult.signal.direction} @ {analyzeResult.signal.entry_price} → {analyzeResult.signal.target_price}
                / stop {analyzeResult.signal.stop_loss}
              </div>
            </div>
          {/if}
        </div>
      {/if}
    </Panel>
  </div>

  <div class="span-8">
    <Panel title="Opportunity Scanner" meta="4 lanes — crypto runs 24/7">
      <div class="scanner-row">
        {#each SCANNER_MODES as mode (mode.key)}
          <div class="scanner-card">
            <div class="sc-top">
              <b>{mode.label}</b>
              <span
                class="status-dot"
                class:ok={scannerStatus?.scanner?.[mode.key]?.status === "ok"}
                class:running={scannerRunning.has(mode.key) || scannerStatus?.scanner?.[mode.key]?.status === "running"}
                class:idle={scannerStatus?.scanner?.[mode.key]?.status === "idle"}
              ></span>
            </div>
            <button class="btn small" onclick={() => runScanner(mode.key)} disabled={scannerRunning.has(mode.key)}>
              {scannerRunning.has(mode.key) ? "Starting…" : "Run Now"}
            </button>
          </div>
        {/each}
      </div>
    </Panel>
  </div>

  <div class="span-12">
    <Panel title="Signals" meta="{filteredSignals.length} shown">
      {#snippet children()}
        <div class="filters">
          <select bind:value={statusFilter}>
            <option value="">All Status</option>
            <option value="Active">Active</option>
            <option value="PendingApproval">Pending Approval</option>
            <option value="Executed">Executed</option>
            <option value="Closed">Closed</option>
            <option value="Expired">Expired</option>
          </select>
          <select bind:value={classFilter}>
            <option value="">All Classes</option>
            <option value="Equity">Equity</option>
            <option value="Crypto">Crypto</option>
            <option value="Futures">Futures</option>
          </select>
          <button class="btn small outline" onclick={clearExpired}>Clear Expired</button>
          <button class="btn small outline" onclick={savePreset}>Save Preset</button>
          <button class="btn small outline" onclick={exportSignalsCsv}>Export CSV</button>
        </div>

        {#if presets.length}
          <div class="presets">
            {#each presets as p (p.name)}
              <span class="preset-chip">
                <button class="preset-apply" onclick={() => applyPreset(p)}>{p.name}</button>
                <button class="preset-del" onclick={() => deletePreset(p.name)}>✕</button>
              </span>
            {/each}
          </div>
        {/if}

        {#if statusFilter === "PendingApproval"}
          <div class="pending-banner">
            <span>These auto-execute at <b>9:30 AM ET</b> if left untouched. Reject any you don't want, or force them through now.</span>
            <div class="pending-actions">
              <button class="btn tiny good" onclick={approveAllPending}>Force Execute All</button>
              <button class="btn tiny bad" onclick={rejectAllPending}>Reject All</button>
              <button class="btn tiny outline" onclick={cancelAllOpenOrders}>Cancel All Open Orders</button>
            </div>
          </div>
        {/if}

        <div class="sig-table">
          {#each filteredSignals as sig (sig.id)}
            <div
              class="sig-row clickable"
              onclick={() => (analysisSignalId = sig.id)}
              onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (analysisSignalId = sig.id)}
              role="button"
              tabindex="0"
            >
              <RadialScore score={Math.round(sig.composite_score ?? sig.confidence ?? 0)} size={34} />
              <div class="sr-main">
                <div class="sr-sym">
                  {sig.asset_symbol}
                  <Pill label={sig.direction} tone={sig.direction.toLowerCase().includes("short") ? "bad" : "good"} />
                  {#if sig.paper_mode}<Pill label="paper" tone="warm" />{/if}
                  <Pill label={sig.status} tone="neutral" />
                </div>
                <div class="sr-meta num">
                  entry {sig.entry_price ?? "—"} → {sig.target_price ?? "—"} / stop {sig.stop_loss ?? "—"} &middot; {sig.timeframe ?? "—"}
                  &middot; {sig.signal_source}
                </div>
              </div>
              <div class="sr-actions" role="group" aria-label="Signal actions">
                {#if isPending(sig) && !sig.paper_mode}
                  <button class="btn tiny good" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "approve"); }}>Approve</button>
                  <button class="btn tiny bad" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "reject"); }}>Deny</button>
                {:else if sig.status === "Active" && !sig.paper_mode}
                  <button class="btn tiny" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "execute"); }}>Execute</button>
                {/if}
                {#if sig.paper_mode && (sig.status === "Active" || sig.status === "PendingApproval")}
                  <button class="btn tiny outline" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "paper"); }}>Paper Trade</button>
                {/if}
                <button class="btn tiny ghost" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "delete"); }}>✕</button>
              </div>
            </div>
          {:else}
            <div class="empty">No signals match these filters</div>
          {/each}
        </div>
      {/snippet}
    </Panel>
  </div>
</div>

<style>
  .page-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 16px;
  }
  .page-head h1 {
    font-size: 19px;
    margin: 0 0 4px;
    font-weight: 650;
  }
  .horizon-toggle {
    display: flex;
    gap: 4px;
    border: 1px solid var(--line-bright);
    border-radius: 8px;
    padding: 3px;
  }
  .h-btn {
    background: none;
    border: none;
    color: var(--ink-faint);
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .h-btn.on {
    background: var(--accent);
    color: var(--bg);
    font-weight: 700;
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
  .span-8 {
    grid-column: span 8;
  }
  .span-12 {
    grid-column: span 12;
  }

  .field {
    margin-bottom: 12px;
  }
  .field label,
  .flabel {
    display: block;
    font-size: 11px;
    color: var(--ink-dim);
    margin-bottom: 5px;
  }
  input:not([type]),
  select {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    color: var(--ink);
    padding: 7px 9px;
    font-size: 12.5px;
  }
  .tf-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .tf-chip {
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--line-bright);
    color: var(--ink-dim);
    cursor: pointer;
    user-select: none;
  }
  .tf-chip input {
    display: none;
  }
  .tf-chip.on {
    color: var(--accent);
    border-color: var(--accent);
    background: rgba(124, 154, 255, 0.1);
  }
  .check-row {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
    color: var(--ink-dim);
    margin-bottom: 12px;
    cursor: pointer;
  }

  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 8px 12px;
    border-radius: 7px;
    font-size: 12px;
    cursor: pointer;
    width: 100%;
  }
  .btn.primary {
    background: rgba(124, 154, 255, 0.15);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .btn.small {
    width: auto;
    padding: 6px 10px;
  }
  .btn.outline {
    background: transparent;
  }
  .btn.tiny {
    width: auto;
    padding: 4px 9px;
    font-size: 11px;
  }
  .btn.tiny.good {
    border-color: var(--good);
    color: var(--good);
  }
  .btn.tiny.bad {
    border-color: var(--bad);
    color: var(--bad);
  }
  .btn.tiny.outline {
    background: transparent;
    border-color: var(--accent);
    color: var(--accent);
  }
  .btn.tiny.ghost {
    background: transparent;
    border-color: transparent;
    color: var(--ink-faint);
  }

  .an-result {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .an-tf-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
    font-size: 12px;
  }
  .an-metrics {
    display: flex;
    gap: 12px;
    font-size: 11px;
    color: var(--ink-dim);
  }
  .an-signal {
    background: rgba(61, 220, 151, 0.06);
    border: 1px solid rgba(61, 220, 151, 0.25);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 11.5px;
  }
  .an-signal-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: var(--good);
    font-weight: 600;
    margin-bottom: 4px;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .save-btn {
    text-transform: none;
    letter-spacing: normal;
  }

  .pending-banner {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    background: rgba(255, 180, 84, 0.06);
    border: 1px solid rgba(255, 180, 84, 0.25);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 12px;
    font-size: 11.5px;
    color: var(--ink-dim);
  }
  .pending-actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  .scanner-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
  }
  .scanner-card {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px;
  }
  .sc-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    font-size: 12.5px;
  }
  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--ink-faint);
  }
  .status-dot.ok {
    background: var(--good);
    box-shadow: 0 0 6px var(--good);
  }
  .status-dot.running {
    background: var(--warm);
    box-shadow: 0 0 6px var(--warm);
  }

  .filters {
    display: flex;
    gap: 10px;
    margin-bottom: 12px;
  }
  .filters select {
    width: auto;
  }

  .presets {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 12px;
  }
  .preset-chip {
    display: inline-flex;
    align-items: center;
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    border-radius: 6px;
    overflow: hidden;
  }
  .preset-apply {
    background: none;
    border: none;
    color: var(--ink-dim);
    padding: 5px 8px;
    font-size: 11px;
    cursor: pointer;
  }
  .preset-apply:hover {
    color: var(--accent);
  }
  .preset-del {
    background: none;
    border: none;
    border-left: 1px solid var(--line);
    color: var(--ink-faint);
    padding: 5px 7px;
    font-size: 10px;
    cursor: pointer;
  }

  .sig-table {
    display: flex;
    flex-direction: column;
  }
  .sig-row {
    display: grid;
    grid-template-columns: 34px 1fr auto;
    gap: 12px;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
  }
  .sig-row:last-child {
    border-bottom: none;
  }
  .sig-row.clickable {
    cursor: pointer;
    border-radius: 6px;
    margin: 0 -8px;
    padding: 10px 8px;
  }
  .sig-row.clickable:hover {
    background: rgba(124, 154, 255, 0.05);
  }
  .sr-sym {
    display: flex;
    align-items: center;
    gap: 7px;
    font-weight: 650;
    font-size: 13px;
    flex-wrap: wrap;
  }
  .sr-meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    margin-top: 3px;
  }
  .sr-actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .empty {
    padding: 24px 0;
    text-align: center;
    color: var(--ink-faint);
    font-size: 12px;
  }

  @media (max-width: 1180px) {
    .span-4,
    .span-8 {
      grid-column: span 12;
    }
    .scanner-row {
      grid-template-columns: repeat(2, 1fr);
    }
  }
</style>
