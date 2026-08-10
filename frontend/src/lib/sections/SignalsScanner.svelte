<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import RadialScore from "../components/RadialScore.svelte";
  import { api, type Signal, type AnalyzeResult, type ScannerStatus } from "../api";
  import { toastStore } from "../stores/toast.svelte";

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

  const filteredSignals = $derived(
    classFilter ? signals.filter((s) => (s.asset_class ?? "").toLowerCase() === classFilter.toLowerCase()) : signals,
  );

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
  <h1>Signals &amp; Scanner</h1>
  <div class="sub">Signal review, manual analysis, and the opportunity scanner in one workspace</div>
</div>

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
              <div class="an-signal-head">Generated signal</div>
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
        </div>

        <div class="sig-table">
          {#each filteredSignals as sig (sig.id)}
            <div class="sig-row">
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
              <div class="sr-actions">
                {#if isPending(sig) && !sig.paper_mode}
                  <button class="btn tiny good" disabled={busyIds.has(sig.id)} onclick={() => doAction(sig, "approve")}>Approve</button>
                  <button class="btn tiny bad" disabled={busyIds.has(sig.id)} onclick={() => doAction(sig, "reject")}>Deny</button>
                {:else if sig.status === "Active" && !sig.paper_mode}
                  <button class="btn tiny" disabled={busyIds.has(sig.id)} onclick={() => doAction(sig, "execute")}>Execute</button>
                {/if}
                {#if sig.paper_mode && (sig.status === "Active" || sig.status === "PendingApproval")}
                  <button class="btn tiny outline" disabled={busyIds.has(sig.id)} onclick={() => doAction(sig, "paper")}>Paper Trade</button>
                {/if}
                <button class="btn tiny ghost" disabled={busyIds.has(sig.id)} onclick={() => doAction(sig, "delete")}>✕</button>
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
    color: var(--good);
    font-weight: 600;
    margin-bottom: 4px;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
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
