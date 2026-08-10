<script lang="ts">
  import { api, type SignalAnalysis, type TfAnalysis } from "../api";
  import Pill from "./Pill.svelte";
  import CandleChart from "./CandleChart.svelte";
  import { toastStore } from "../stores/toast.svelte";

  let { signalId, onClose }: { signalId: string; onClose: () => void } = $props();

  let data = $state<SignalAnalysis | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let activeTf = $state<string>("4H");

  async function load() {
    loading = true;
    error = null;
    try {
      data = await api.signalAnalysis(signalId);
      // land on the signal's own timeframe if it has candles, else the first tf with data
      const sigTf = data.signal.timeframe;
      if (sigTf && data.candles[sigTf]?.length) {
        activeTf = sigTf;
      } else {
        const firstGood = data.timeframes.find((tf) => data!.candles[tf]?.length);
        if (firstGood) activeTf = firstGood;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    load();
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }

  const biasTone = (b: string | undefined) => (b === "bullish" ? "good" : b === "bearish" ? "bad" : "neutral");

  async function copySummary() {
    if (!data) return;
    const s = data.signal;
    const c = data.confluence;
    const text = [
      `${s.asset_symbol} — ${s.direction} (${s.timeframe})`,
      `Entry ${s.entry_price} | Target ${s.target_price} | Stop ${s.stop_loss}`,
      `Composite score ${s.composite_score ?? s.confidence} | Confluence ${c.score}% (${c.label})`,
      s.reasoning ? `Reasoning: ${s.reasoning}` : "",
    ]
      .filter(Boolean)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toastStore.ok("Copied summary to clipboard");
    } catch {
      toastStore.err("Clipboard access denied");
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="scrim" onclick={onClose} role="presentation">
  <div
    class="modal"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
    role="dialog"
    aria-modal="true"
    tabindex="-1"
  >
    {#if loading}
      <div class="state">Loading analysis…</div>
    {:else if error}
      <div class="state err">Failed to load: {error}</div>
    {:else if data}
      {@const s = data.signal}
      {@const c = data.confluence}
      <div class="head">
        <div class="head-main">
          <span class="sym">{s.asset_symbol}</span>
          <Pill label={s.direction} tone={s.direction.toLowerCase().includes("short") ? "bad" : "good"} />
          {#if s.paper_mode}<Pill label="paper" tone="warm" />{/if}
          <span class="head-meta">{s.timeframe} &middot; {s.signal_source}</span>
        </div>
        <div class="head-actions">
          <button class="btn tiny outline" onclick={copySummary}>Copy Summary</button>
          <button class="btn tiny ghost" onclick={onClose}>✕</button>
        </div>
      </div>

      <div class="overview">
        <div class="ov-stat"><span>Entry</span><b class="num">{s.entry_price}</b></div>
        <div class="ov-stat"><span>Target</span><b class="num pl-up">{s.target_price}</b></div>
        <div class="ov-stat"><span>Stop</span><b class="num pl-down">{s.stop_loss}</b></div>
        <div class="ov-stat"><span>R:R</span><b class="num">{s.rr_ratio != null ? `${s.rr_ratio}:1` : "—"}</b></div>
        <div class="ov-stat"><span>Score</span><b class="num">{Math.round(s.composite_score ?? s.confidence ?? 0)}</b></div>
        <div class="ov-stat"><span>Confluence</span><b class="num">{c.score}% ({c.label})</b></div>
      </div>

      <div class="tf-strip">
        {#each data.timeframes as tf (tf)}
          <button
            class="tf-btn"
            class:on={activeTf === tf}
            class:has-error={data.ta[tf]?.error}
            disabled={!data.candles[tf]?.length}
            onclick={() => (activeTf = tf)}
          >
            {tf}
          </button>
        {/each}
      </div>

      <CandleChart
        candles={data.candles[activeTf] ?? []}
        entry={s.entry_price}
        target={s.target_price}
        stop={s.stop_loss}
      />

      {#if c.risk_flags.length}
        <div class="risk-flags">
          {#each c.risk_flags as flag (flag)}
            <div class="risk-flag">⚠ {flag}</div>
          {/each}
        </div>
      {/if}

      <div class="ta-grid">
        {#each data.timeframes as tf (tf)}
          {@const d = data.ta[tf]}
          <div class="ta-panel" class:unavailable={!d || d.error}>
            <div class="ta-panel-head">
              <b>{tf}</b>
              {#if d && !d.error}
                <Pill label={d.bias ?? "neutral"} tone={biasTone(d.bias)} />
              {/if}
            </div>
            {#if d && !d.error}
              <div class="metric"><span>RSI</span><b class="num">{d.rsi ?? "—"}</b></div>
              <div class="metric"><span>ATR%</span><b class="num">{d.atr?.pct ?? "—"}</b></div>
              <div class="metric"><span>MACD</span><b class="num">{d.macd?.trend ?? "—"}</b></div>
              <div class="metric"><span>BB</span><b class="num">{d.bollinger_bands?.position ?? "—"}</b></div>
              <div class="metric"><span>Vol</span><b class="num">{d.volume?.surge ? "surge" : d.volume?.dry ? "dry" : "normal"}</b></div>
            {:else}
              <div class="ta-unavailable">no data</div>
            {/if}
          </div>
        {/each}
      </div>

      {#if s.reasoning}
        <div class="reasoning">
          <div class="section-label">Reasoning</div>
          <p>{s.reasoning}</p>
          {#if s.key_risks}<p class="key-risks"><b>Key risks:</b> {s.key_risks}</p>{/if}
          {#if s.invalidation}<p class="invalidation"><b>Invalidation:</b> {s.invalidation}</p>{/if}
        </div>
      {/if}

      <div class="context-grid">
        <div>
          <div class="section-label">Related threats ({data.threats.length})</div>
          <div class="context-list">
            {#each data.threats.slice(0, 6) as t (t.id)}
              <div class="context-row">
                <Pill label={t.severity} tone={t.severity === "Critical" ? "critical" : t.severity === "High" ? "warm" : "neutral"} />
                <span>{t.title}</span>
              </div>
            {:else}
              <div class="context-empty">None linked</div>
            {/each}
          </div>
        </div>
        <div>
          <div class="section-label">Related news ({data.news.length})</div>
          <div class="context-list">
            {#each data.news.slice(0, 6) as n (n.id)}
              <div class="context-row">
                <Pill label={n.sentiment ?? "neutral"} tone={n.sentiment === "positive" ? "good" : n.sentiment === "negative" ? "bad" : "neutral"} />
                <span>{n.title}</span>
              </div>
            {:else}
              <div class="context-empty">None linked</div>
            {/each}
          </div>
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: rgba(5, 7, 10, 0.7);
    backdrop-filter: blur(2px);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 40px 20px;
    z-index: 200;
    overflow-y: auto;
  }
  .modal {
    background: var(--surface);
    border: 1px solid var(--line-bright);
    border-radius: 14px;
    width: min(900px, 100%);
    padding: 20px 22px 26px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }
  .state {
    padding: 60px 0;
    text-align: center;
    color: var(--ink-faint);
  }
  .state.err {
    color: var(--bad);
  }

  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
    flex-wrap: wrap;
    gap: 10px;
  }
  .head-main {
    display: flex;
    align-items: center;
    gap: 9px;
    flex-wrap: wrap;
  }
  .sym {
    font-size: 18px;
    font-weight: 700;
  }
  .head-meta {
    font-size: 11.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
  }
  .head-actions {
    display: flex;
    gap: 6px;
  }
  .btn {
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    color: var(--ink);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .btn.tiny {
    padding: 4px 9px;
    font-size: 11px;
  }
  .btn.outline {
    background: transparent;
    border-color: var(--accent);
    color: var(--accent);
  }
  .btn.ghost {
    background: transparent;
    border-color: transparent;
    color: var(--ink-faint);
  }

  .overview {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    padding: 10px 0;
    margin-bottom: 12px;
  }
  .ov-stat {
    display: flex;
    flex-direction: column;
    gap: 3px;
    text-align: center;
    border-right: 1px solid var(--line);
  }
  .ov-stat:last-child {
    border-right: none;
  }
  .ov-stat span {
    font-size: 9.5px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .ov-stat b {
    font-size: 14px;
  }
  .pl-up {
    color: var(--good);
  }
  .pl-down {
    color: var(--bad);
  }

  .tf-strip {
    display: flex;
    gap: 5px;
    overflow-x: auto;
    margin-bottom: 8px;
  }
  .tf-btn {
    flex: none;
    background: none;
    border: 1px solid var(--line-bright);
    color: var(--ink-faint);
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 11px;
    cursor: pointer;
  }
  .tf-btn.on {
    background: var(--accent);
    color: var(--bg);
    border-color: var(--accent);
    font-weight: 700;
  }
  .tf-btn.has-error:not(.on) {
    color: var(--bad);
  }
  .tf-btn:disabled {
    opacity: 0.35;
    cursor: default;
  }

  .risk-flags {
    margin-top: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .risk-flag {
    font-size: 11.5px;
    color: var(--warm);
    background: rgba(255, 180, 84, 0.08);
    border: 1px solid rgba(255, 180, 84, 0.25);
    border-radius: 6px;
    padding: 5px 9px;
  }

  .ta-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 16px;
  }
  .ta-panel {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
    min-width: 0;
  }
  .ta-panel.unavailable {
    opacity: 0.5;
  }
  .ta-panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
    font-size: 12px;
  }
  .metric {
    display: flex;
    justify-content: space-between;
    font-size: 10.5px;
    color: var(--ink-dim);
    padding: 1px 0;
  }
  .ta-unavailable {
    font-size: 10.5px;
    color: var(--ink-faint);
    text-align: center;
    padding: 6px 0;
  }

  .reasoning {
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid var(--line);
  }
  .reasoning p {
    font-size: 12.5px;
    color: var(--ink-dim);
    line-height: 1.6;
    margin: 0 0 8px;
  }
  .section-label {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-faint);
    margin-bottom: 8px;
    font-weight: 700;
  }

  .context-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid var(--line);
  }
  .context-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .context-row {
    display: flex;
    align-items: flex-start;
    gap: 7px;
    font-size: 11.5px;
    color: var(--ink-dim);
  }
  .context-empty {
    font-size: 11.5px;
    color: var(--ink-faint);
  }

  @media (max-width: 700px) {
    .overview {
      grid-template-columns: repeat(3, 1fr);
    }
    .ov-stat:nth-child(3) {
      border-right: none;
    }
    .ta-grid {
      grid-template-columns: 1fr;
    }
    .context-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
