<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import RadialScore from "../components/RadialScore.svelte";
  import SignalAnalysisModal from "../components/SignalAnalysisModal.svelte";
  import { api, type Signal, type AnalyzeResult, type ScannerStatus, type TradingPreference, type VerifyResult } from "../api";
  import { toastStore } from "../stores/toast.svelte";
  import { downloadCsv } from "../csv";
  import { linkStore } from "../stores/link.svelte";

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
  let verifyResults = $state<Record<string, VerifyResult>>({});

  async function doReverse(sig: Signal) {
    const p = verifyResults[sig.id]?.reversal_proposal;
    if (!p) return;
    const ok = confirm(
      `Flip ${sig.asset_symbol} from ${sig.direction} to ${p.direction}?

` +
      `entry ${p.entry_price}
stop ${p.stop_loss}
target ${p.target_price}  (R:R ${p.rr_ratio}:1)

` +
      `${p.basis}

${p.warning}

The original signal will be superseded. Levels are recomputed server-side at submit.`,
    );
    if (!ok) return;
    setBusy(sig.id, true);
    try {
      const res = await api.reverseSignal(sig.id);
      toastStore.ok(`${sig.asset_symbol}: flipped to ${res.proposal.direction} @ ${res.proposal.entry_price}`);
      await loadSignals();
    } catch (e) {
      toastStore.err(`Reverse failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(sig.id, false);
    }
  }

  async function doVerify(sig: Signal, deep = false) {
    setBusy(sig.id, true);
    if (deep) toastStore.ok(`${sig.asset_symbol}: deep verify — fresh TA + web news + LLM, ~30-90s`);
    try {
      let res = await api.verifySignal(sig.id, false, deep);
      verifyResults = { ...verifyResults, [sig.id]: res };
      if (res.verdict === "STALE_ENTRY" && res.suggested_update) {
        const u = res.suggested_update;
        const ok = confirm(
          `${sig.asset_symbol}: price moved ${res.drift_pct}% from entry.
` +
          `Re-anchor levels?
  entry ${u.entry_price}
  stop ${u.stop_loss}
  target ${u.target_price}
` +
          `(${u.basis})`,
        );
        if (ok) {
          res = await api.verifySignal(sig.id, true);
          verifyResults = { ...verifyResults, [sig.id]: res };
          toastStore.ok(`${sig.asset_symbol}: levels re-anchored`);
          await loadSignals();
        }
      } else if (res.verdict === "INVALIDATED") {
        toastStore.err(`${sig.asset_symbol}: setup invalidated at current price`);
      } else if (res.verdict === "CONFIRMED") {
        toastStore.ok(`${sig.asset_symbol}: levels still valid (${res.price_asof})`);
      }
      const a = res.llm_assessment;
      if (a && a.assessment !== "UNAVAILABLE") {
        toastStore.ok(`${sig.asset_symbol} AI second opinion: ${a.assessment}${a.confidence != null ? ` (${a.confidence}%)` : ""}`);
      }
    } catch (e) {
      toastStore.err(`Verify failed: ${e}`);
    } finally {
      setBusy(sig.id, false);
    }
  }

  // Market color coding: left edge + tint per asset class, while the TOP
  // border stays green/red for long/short. Classes beyond the big three
  // fall into "other" (futures palette) rather than inventing more hues.
  const marketClass = (ac: string | null) => {
    const c = (ac ?? "").toLowerCase();
    if (c.includes("crypto")) return "mkt-crypto";
    if (c.includes("equity") || c.includes("stock") || c.includes("etf")) return "mkt-equity";
    return "mkt-other";
  };

  // Expected hold time per chart timeframe — the user-facing answer to
  // "how long is this trade supposed to take?"
  const HOLD_BY_TF: Record<string, string> = {
    "1m": "<30 min", "3m": "<30 min", "5m": "<1 hr",
    "15m": "1-4 hr", "30m": "2-8 hr", "1H": "4-24 hr",
    "2H": "1-3 days", "4H": "1-5 days", "1D": "1-4 weeks",
  };
  const holdEstimate = (tf: string | null) => HOLD_BY_TF[tf ?? ""] ?? "varies";

  const verdictTone = (v: string) => (v === "CONFIRMED" ? "good" : v === "INVALIDATED" ? "bad" : v === "STALE_ENTRY" ? "warm" : "neutral");

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

  // Sort options for the card grid. "newest" uses generated_at; score falls
  // back to confidence for signals generated before composite scoring existed.
  type SortKey = "newest" | "oldest" | "confidence" | "score" | "rr" | "symbol";
  let sortBy = $state<SortKey>("newest");
  const SORTS: [SortKey, string][] = [
    ["newest", "Newest first"], ["oldest", "Oldest first"],
    ["confidence", "Confidence %"], ["score", "Composite score"],
    ["rr", "R:R ratio"], ["symbol", "Symbol A-Z"],
  ];

  const filteredSignals = $derived.by(() => {
    let list = classFilter
      ? signals.filter((s) => (s.asset_class ?? "").toLowerCase() === classFilter.toLowerCase())
      : signals;
    if (linkStore.symbol) {
      list = list.filter((s) => s.asset_symbol === linkStore.symbol);
    }
    const by: Record<SortKey, (a: Signal, b: Signal) => number> = {
      newest: (a, b) => (b.generated_at ?? "").localeCompare(a.generated_at ?? ""),
      oldest: (a, b) => (a.generated_at ?? "").localeCompare(b.generated_at ?? ""),
      confidence: (a, b) => (b.confidence ?? 0) - (a.confidence ?? 0),
      score: (a, b) => (b.composite_score ?? b.confidence ?? 0) - (a.composite_score ?? a.confidence ?? 0),
      rr: (a, b) => (b.rr_ratio ?? 0) - (a.rr_ratio ?? 0),
      symbol: (a, b) => (a.asset_symbol ?? "").localeCompare(b.asset_symbol ?? ""),
    };
    return [...list].sort(by[sortBy]);
  });

  const fmtAge = (iso: string | null | undefined) => {
    if (!iso) return "—";
    const sec = (Date.now() - new Date(iso).getTime()) / 1000;
    if (sec < 3600) return `${Math.max(1, Math.floor(sec / 60))}m`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
    return `${Math.floor(sec / 86400)}d`;
  };

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
                <button
                  class="btn tiny outline save-btn"
                  disabled={!!analyzeResult.signal.bias_conflict}
                  title={analyzeResult.signal.bias_conflict ? "Blocked: direction conflicts with the chart" : "Save this signal"}
                  onclick={saveGeneratedSignal}
                >Save to Signals</button>
              </div>
              <div class="gen-pills">
                <Pill
                  label={String(analyzeResult.signal.direction ?? "?")}
                  tone={String(analyzeResult.signal.direction ?? "").toLowerCase().includes("short") ? "bad" : "good"}
                />
                <Pill label={String(analyzeResult.signal.horizon ?? "?")} tone="neutral" />
                <span class="num dim">{analyzeResult.signal.timeframe ?? "—"} chart · expect {analyzeResult.signal.hold_estimate ?? "?"}</span>
                <span class="num"><span class="dim">confidence</span> {Math.round(Number(analyzeResult.signal.confidence ?? 0))}%</span>
              </div>
              <div class="num">
                entry {analyzeResult.signal.entry_price} → target {analyzeResult.signal.target_price}
                / stop {analyzeResult.signal.stop_loss}
              </div>
              {#if analyzeResult.signal.bias_conflict}
                <div class="gen-conflict">
                  ⚠ Direction conflicts with the chart: {analyzeResult.signal.bias_conflict}. Treat with caution.
                </div>
              {/if}
              {#if analyzeResult.signal.bias_summary}
                <div class="dim num gen-bias">
                  timeframe biases: {analyzeResult.signal.bias_summary.bullish} bullish / {analyzeResult.signal.bias_summary.bearish} bearish of {analyzeResult.signal.bias_summary.total}
                </div>
              {/if}
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
          <select bind:value={sortBy} title="Sort cards">
            {#each SORTS as [key, label] (key)}
              <option value={key}>{label}</option>
            {/each}
          </select>
          <button class="btn small outline" onclick={clearExpired}>Clear Expired</button>
          <button class="btn small outline" onclick={savePreset}>Save Preset</button>
          <button class="btn small outline" onclick={exportSignalsCsv}>Export CSV</button>
          {#if linkStore.symbol}
            <span class="link-chip" title="Linked symbol filter — set by clicking a symbol in any window">
              🔗 {linkStore.symbol}
              <button class="link-clear" onclick={() => linkStore.clear()}>✕</button>
            </span>
          {/if}
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

        <div class="sig-cards">
          {#each filteredSignals as sig (sig.id)}
            <div
              class="sig-card {marketClass(sig.asset_class)}"
              class:short={sig.direction.toLowerCase().includes("short")}
              onclick={() => (analysisSignalId = sig.id)}
              onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (analysisSignalId = sig.id)}
              role="button"
              tabindex="0"
            >
              <div class="sc-head">
                <button
                  class="sc-sym"
                  title="Link {sig.asset_symbol} across windows"
                  onclick={(e) => { e.stopPropagation(); linkStore.link(sig.asset_symbol); }}
                >{sig.asset_symbol}</button>
                <span class="sc-right">
                  <span class="sc-mkt">{(sig.asset_class ?? "?").slice(0, 6)}</span>
                  <span class="sc-age num" title={sig.generated_at}>{fmtAge(sig.generated_at)}</span>
                </span>
              </div>
              <div class="sc-pills">
                <Pill label={sig.direction} tone={sig.direction.toLowerCase().includes("short") ? "bad" : "good"} />
                {#if sig.paper_mode}<Pill label="paper" tone="warm" />{/if}
                <Pill label={sig.status} tone="neutral" />
                {#if verifyResults[sig.id]}
                  <Pill label={verifyResults[sig.id].verdict.replaceAll("_", " ").toLowerCase()} tone={verdictTone(verifyResults[sig.id].verdict)} />
                {/if}
              </div>
              {#if verifyResults[sig.id]}
                {@const vr = verifyResults[sig.id]}
                <div class="verify-box">
                  <div class="vb-head">
                    <b>{vr.verdict.replaceAll("_", " ")}</b>
                    <span class="num dim">checked @ {vr.current_price ?? "—"} ({vr.price_asof ?? "?"}){vr.drift_pct != null ? ` · ${vr.drift_pct}% from entry` : ""}</span>
                  </div>
                  {#if vr.llm_assessment}
                    {@const a = vr.llm_assessment}
                    {#if a.assessment !== "UNAVAILABLE"}
                      <div class="vb-ai {a.assessment === "AGREE" ? 'vb-good' : a.assessment === "DISAGREE" ? 'vb-bad' : ''}">
                        AI second opinion: <b>{a.assessment}</b>{a.confidence != null ? ` (${a.confidence}%)` : ""}
                      </div>
                      {#if a.reasoning}<div class="vb-reason dim">{a.reasoning}</div>{/if}
                      {#if a.key_change && a.key_change !== "nothing material"}<div class="vb-reason">Changed: {a.key_change}</div>{/if}
                      {#if a.context_used}
                        <div class="vb-ctx dim">
                          context: {Object.entries(a.context_used).filter(([, v]) => v).map(([k]) => k.replaceAll("_", " ")).join(", ") || "none"}
                        </div>
                      {/if}
                      {#if vr.reversal_proposal}
                        {@const rp = vr.reversal_proposal}
                        <div class="vb-rev">
                          <div class="vb-rev-head">
                            Suggested play: <b>{rp.direction}</b>
                            <span class="num dim">entry {rp.entry_price} · stop {rp.stop_loss} · target {rp.target_price} · R:R {rp.rr_ratio}:1</span>
                          </div>
                          <div class="vb-reason dim">{rp.basis}</div>
                          <div class="vb-reason vb-warn">{rp.warning}</div>
                          <button
                            class="btn tiny"
                            disabled={busyIds.has(sig.id)}
                            onclick={(e) => { e.stopPropagation(); doReverse(sig); }}
                          >Flip to {rp.direction}</button>
                        </div>
                      {/if}
                    {:else}
                      <div class="vb-reason dim">{a.reasoning}</div>
                    {/if}
                  {/if}
                </div>
              {/if}
              <div class="sc-score">
                <RadialScore score={Math.round(sig.composite_score ?? sig.confidence ?? 0)} size={46} />
                <div class="sc-score-meta">
                  <div class="num"><span class="dim">conf</span> {Math.round(sig.confidence ?? 0)}%</div>
                  <div class="num"><span class="dim">R:R</span> {sig.rr_ratio != null ? `${sig.rr_ratio}:1` : "—"}</div>
                  <div class="num"><span class="dim">tf</span> {sig.timeframe ?? "—"}</div>
                  <div class="num"><span class="dim">hold</span> {holdEstimate(sig.timeframe)}</div>
                </div>
              </div>
              <div class="sc-levels num">
                <div><span class="dim">entry</span> {sig.entry_price ?? "—"}</div>
                <div><span class="dim">target</span> {sig.target_price ?? "—"}</div>
                <div><span class="dim">stop</span> {sig.stop_loss ?? "—"}</div>
              </div>
              <div class="sc-src dim">{sig.signal_source}</div>
              <div class="sc-actions" role="group" aria-label="Signal actions">
                {#if isPending(sig) && !sig.paper_mode}
                  <button class="btn tiny good" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "approve"); }}>Approve</button>
                  <button class="btn tiny bad" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "reject"); }}>Deny</button>
                {:else if sig.status === "Active" && !sig.paper_mode}
                  <button class="btn tiny" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "execute"); }}>Execute</button>
                {/if}
                {#if sig.paper_mode && (sig.status === "Active" || sig.status === "PendingApproval")}
                  <button class="btn tiny outline" disabled={busyIds.has(sig.id)} onclick={(e) => { e.stopPropagation(); doAction(sig, "paper"); }}>Paper</button>
                {/if}
                <button
                  class="btn tiny outline"
                  title="Double-check this setup against fresh data"
                  disabled={busyIds.has(sig.id)}
                  onclick={(e) => { e.stopPropagation(); doVerify(sig); }}
                >Verify</button>
                <button
                  class="btn tiny outline"
                  title="Deep verify: fresh multi-timeframe TA + Massive market data + web news, all fed to the LLM for a second opinion (slower, ~30-90s)"
                  disabled={busyIds.has(sig.id)}
                  onclick={(e) => { e.stopPropagation(); doVerify(sig, true); }}
                >Deep</button>
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

  .gen-pills {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin: 6px 0;
  }
  .gen-conflict {
    margin-top: 6px;
    font-size: 11px;
    padding: 6px 9px;
    border: 1px solid rgba(255, 180, 84, 0.35);
    border-radius: var(--radius-sm);
    background: rgba(255, 180, 84, 0.08);
    color: var(--warm);
  }
  .gen-bias {
    margin-top: 4px;
    font-size: 10px;
  }
  .link-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    padding: 4px 9px;
    border: 1px solid var(--accent-dim);
    border-radius: var(--radius-sm);
    background: rgba(124, 154, 255, 0.1);
    color: var(--accent);
  }
  .link-clear {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-size: 10px;
    padding: 0;
  }
  .vb-rev {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--line-bright);
  }
  .vb-rev-head {
    display: flex;
    gap: 8px;
    align-items: baseline;
    flex-wrap: wrap;
    font-size: 11px;
    margin-bottom: 3px;
  }
  .vb-warn {
    color: var(--warm);
  }
  .vb-rev .btn {
    margin-top: 6px;
  }
  .verify-box {
    grid-column: 1 / -1;
    border: 1px solid var(--line-bright);
    border-radius: var(--radius-sm);
    background: rgba(124, 154, 255, 0.05);
    padding: 8px 10px;
    margin-top: 8px;
    font-size: 11px;
  }
  .vb-head {
    display: flex;
    gap: 10px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  .vb-ai {
    margin-top: 4px;
  }
  .vb-good {
    color: var(--good);
  }
  .vb-bad {
    color: var(--bad);
  }
  .vb-reason {
    margin-top: 3px;
    line-height: 1.45;
    font-size: 10.5px;
  }
  .vb-ctx {
    margin-top: 4px;
    font-size: 9.5px;
    letter-spacing: 0.04em;
  }
  .ai-opinion {
    font-size: 10px;
    margin-top: 3px;
    line-height: 1.35;
  }
  .sig-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(215px, 1fr));
    gap: var(--space-sm);
  }
  .sig-card {
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding: var(--space-md);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: var(--surface-raised);
    cursor: pointer;
    border-top: 2px solid var(--good);
  }
  .sig-card.short {
    border-top-color: var(--bad);
  }
  /* Market identity: left edge + faint wash. Long/short stays on the top edge. */
  .sig-card.mkt-crypto {
    border-left: 3px solid #f7931a;
    background: linear-gradient(90deg, rgba(247, 147, 26, 0.05), var(--surface-raised) 40%);
  }
  .sig-card.mkt-equity {
    border-left: 3px solid var(--accent);
    background: linear-gradient(90deg, rgba(124, 154, 255, 0.05), var(--surface-raised) 40%);
  }
  .sig-card.mkt-other {
    border-left: 3px solid #b48cff;
    background: linear-gradient(90deg, rgba(180, 140, 255, 0.05), var(--surface-raised) 40%);
  }
  .sc-right {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .sc-mkt {
    font-size: 8.5px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .mkt-crypto .sc-mkt { color: #f7931a; }
  .mkt-equity .sc-mkt { color: var(--accent); }
  .mkt-other .sc-mkt { color: #b48cff; }
  .sig-card:hover {
    border-color: var(--line-bright);
    border-top-color: inherit;
  }
  .sig-card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .sc-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .sc-sym {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    font-weight: 700;
    font-size: 14.5px;
    color: var(--ink);
    cursor: pointer;
  }
  .sc-sym:hover {
    color: var(--accent);
    text-decoration: underline;
  }
  .sc-age {
    font-size: 10px;
    color: var(--ink-faint);
  }
  .sc-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .sc-score {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .sc-score-meta {
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: 10.5px;
  }
  .sc-levels {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    border-top: 1px dashed var(--line);
    padding-top: 7px;
  }
  .sc-src {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .sc-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: auto;
  }
  .dim {
    color: var(--ink-faint);
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

  .sig-row:last-child {
    border-bottom: none;
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
