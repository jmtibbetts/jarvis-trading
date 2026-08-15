<script lang="ts">
  /**
   * One self-contained chart: its own symbol, timeframe, data and load state.
   *
   * Extracted from Charts.svelte so the page can run FOUR of them at once
   * (§140.5). Everything that used to be module-level state in that section —
   * the chart handle, the series, the price-line ledger, the markers plugin —
   * is per-instance here, because four panes sharing any of it would have
   * them drawing on each other.
   *
   * Each pane persists its own selection under its own key, so a four-up
   * layout survives a reload as the four instruments the operator chose
   * rather than four copies of the last one.
   */
  import {
    createChart,
    createSeriesMarkers,
    CandlestickSeries,
    HistogramSeries,
    type IChartApi,
    type IPriceLine,
    type ISeriesApi,
    type ISeriesMarkersPluginApi,
    type Time,
  } from "lightweight-charts";
  import { untrack } from "svelte";
  import { api, type ChartPayload } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";

  let {
    paneId,
    defaultSymbol = "BTC/USD",
    defaultTimeframe = "1H",
    available = [],
    height = "100%",
    showSignals = true,
    onPayload = undefined,
    onChart = undefined,
  }: {
    paneId: string;
    defaultSymbol?: string;
    defaultTimeframe?: string;
    available?: { symbol: string; timeframes: string[] }[];
    height?: string;
    showSignals?: boolean;
    /** Lets the page draw its own panels from this pane's data. */
    onPayload?: (p: ChartPayload | null) => void;
    /** Exposes the chart handle so the page can pan it (analog jump). */
    onChart?: (c: IChartApi | null) => void;
  } = $props();

  const feeds = new FeedTracker();
  const KEY = (k: string) => `jarvis.chart.${paneId}.${k}`;

  let container: HTMLDivElement;
  let chart: IChartApi | null = null;
  let candleSeries: ISeriesApi<"Candlestick"> | null = null;
  let volumeSeries: ISeriesApi<"Histogram"> | null = null;
  let markersApi: ISeriesMarkersPluginApi<Time> | null = null;
  // Price lines accumulate on the series unless explicitly removed — without
  // this ledger, switching symbols keeps drawing the OLD symbol's entry/stop
  // at its absolute price on the new chart.
  let activeLines: IPriceLine[] = [];

  // `untrack` states the intent Svelte warns about: these props are SEEDS.
  // Once the pane exists it owns its symbol — a parent re-render must not
  // yank a chart back to the default the operator just changed away from.
  let symbol = $state(localStorage.getItem(KEY("symbol")) ?? untrack(() => defaultSymbol));
  let timeframe = $state(localStorage.getItem(KEY("tf")) ?? untrack(() => defaultTimeframe));
  let payload = $state<ChartPayload | null>(null);
  let symbolInput = $state("");
  let loading = $state(false);

  const TFS = ["15m", "1H", "4H", "1D"];

  function css(name: string): string {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function buildChart() {
    if (!container) return;
    chart = createChart(container, {
      layout: {
        background: { color: "transparent" },
        textColor: css("--ink-faint") || "#8b96a8",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(120,135,160,0.08)" },
        horzLines: { color: "rgba(120,135,160,0.08)" },
      },
      rightPriceScale: { borderColor: "rgba(120,135,160,0.2)" },
      timeScale: { borderColor: "rgba(120,135,160,0.2)", timeVisible: true },
      crosshair: { mode: 0 },
      autoSize: true,
    });
    candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: css("--good") || "#3fbf7f",
      downColor: css("--bad") || "#e05c6a",
      wickUpColor: css("--good") || "#3fbf7f",
      wickDownColor: css("--bad") || "#e05c6a",
      borderVisible: false,
    });
    volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      color: "rgba(120,135,160,0.35)",
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    onChart?.(chart);
  }

  function render() {
    if (!chart || !candleSeries || !volumeSeries || !payload) return;
    candleSeries.setData(payload.bars as never[]);
    volumeSeries.setData(
      payload.bars.map((b) => ({ time: b.time, value: b.volume })) as never[],
    );

    for (const line of activeLines) candleSeries.removePriceLine(line);
    activeLines = [];
    const addLine = (price: number, color: string, title: string, dashed = false) =>
      activeLines.push(
        candleSeries!.createPriceLine({
          price, color, lineWidth: 1, title,
          ...(dashed ? { lineStyle: 2 } : {}),
        }),
      );
    for (const p of payload.positions) {
      if (p.entry_price) addLine(p.entry_price, css("--accent") || "#7c9aff", `entry ${p.direction}`);
      if (p.stop_loss) addLine(p.stop_loss, css("--bad") || "#e05c6a", "stop");
      if (p.initial_stop_loss && p.initial_stop_loss !== p.stop_loss)
        addLine(p.initial_stop_loss, css("--bad") || "#e05c6a", "initial stop", true);
      if (p.target_price) addLine(p.target_price, css("--good") || "#3fbf7f", "target");
    }

    markersApi ??= createSeriesMarkers(candleSeries, []);
    if (showSignals && payload.bars.length) {
      const first = payload.bars[0].time;
      const markers = payload.signals
        .map((s) => {
          const t = Math.floor(new Date(s.generated_at + (s.generated_at.endsWith("Z") ? "" : "Z")).getTime() / 1000);
          return { s, t };
        })
        .filter(({ t }) => t >= first)
        .map(({ s, t }) => ({
          time: t as Time,
          position: ((s.direction || "").toLowerCase().startsWith("short") ? "aboveBar" : "belowBar") as never,
          color: (s.direction || "").toLowerCase().startsWith("short") ? css("--bad") || "#e05c6a" : css("--good") || "#3fbf7f",
          shape: ((s.direction || "").toLowerCase().startsWith("short") ? "arrowDown" : "arrowUp") as never,
          text: s.timeframe ?? "",
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));
      markersApi.setMarkers(markers);
    } else {
      markersApi.setMarkers([]);
    }
    chart.timeScale().fitContent();
  }

  async function load() {
    loading = true;
    // keepLast: false — a chart still labelled BTC while drawing the previous
    // symbol's candles misinforms in a way an empty chart does not.
    payload = await feeds.load("chart", () => api.marketChart(symbol, timeframe), { keepLast: false });
    loading = false;
    localStorage.setItem(KEY("symbol"), symbol);
    localStorage.setItem(KEY("tf"), timeframe);
    render();
    onPayload?.(payload);
  }

  $effect(() => {
    symbol;
    timeframe;
    showSignals;
    if (chart) load();
  });

  $effect(() => {
    buildChart();
    load();
    return () => {
      chart?.remove();
      chart = null;
      onChart?.(null);
    };
  });

  function pickSymbol(e: Event) {
    e.preventDefault();
    const v = symbolInput.trim().toUpperCase();
    if (v) symbol = v;
    symbolInput = "";
  }

  export function currentSymbol() {
    return symbol;
  }
</script>

<div class="pane" style="--pane-h: {height}">
  <div class="bar">
    <form onsubmit={pickSymbol}>
      <input
        list="chart-symbols-{paneId}"
        placeholder={symbol}
        bind:value={symbolInput}
        aria-label="Symbol for pane {paneId}"
      />
      <datalist id="chart-symbols-{paneId}">
        {#each available as a (a.symbol)}
          <option value={a.symbol}>{a.timeframes.join(" ")}</option>
        {/each}
      </datalist>
    </form>
    <div class="tfs">
      {#each TFS as tf}
        <button class:on={timeframe === tf} onclick={() => (timeframe = tf)}>{tf}</button>
      {/each}
    </div>
    <div class="meta">
      {#if payload}
        <Pill tone="neutral" label={payload.symbol} />
        <span class="muted">{payload.bar_count.toLocaleString()} bars</span>
        {#if payload.positions.length}
          <Pill tone="info" label="{payload.positions.length} open" />
        {/if}
      {/if}
      {#if loading}<span class="muted">…</span>{/if}
    </div>
  </div>

  <div class="surface" bind:this={container}></div>

  {#if payload && !payload.bar_count}
    <p class="empty">no cached bars for {payload.symbol} @ {payload.timeframe}</p>
  {:else if !payload && !loading}
    <StateNote status={feeds.status("chart")} noun="chart data" />
  {/if}
</div>

<style>
  .pane {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
    min-height: 0;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 8px 10px;
    background: var(--surface);
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    flex: none;
  }
  input {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--ink);
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11.5px;
    width: 108px;
    font-family: var(--mono);
  }
  input:focus {
    outline: none;
    border-color: var(--accent-dim);
  }
  .tfs {
    display: flex;
    gap: 2px;
  }
  .tfs button {
    background: none;
    border: 1px solid transparent;
    color: var(--ink-faint);
    border-radius: 5px;
    padding: 2px 7px;
    font-size: 10.5px;
    cursor: pointer;
    font-family: var(--mono);
  }
  .tfs button.on {
    color: var(--accent);
    border-color: var(--line-bright);
    background: var(--surface-raised);
  }
  .meta {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
    min-width: 0;
  }
  .muted {
    color: var(--ink-faint);
    font-size: 10.5px;
    font-family: var(--mono);
  }
  .surface {
    flex: 1;
    min-height: var(--pane-h);
    height: var(--pane-h);
  }
  .empty {
    color: var(--ink-faint);
    font-size: 11px;
    margin: 0;
  }
</style>
