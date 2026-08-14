<script lang="ts">
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
  import { api, type AnalogSummary, type ChartPayload } from "../api";
  import Pill from "../components/Pill.svelte";

  let container: HTMLDivElement;
  let chart: IChartApi | null = null;
  let candleSeries: ISeriesApi<"Candlestick"> | null = null;
  let volumeSeries: ISeriesApi<"Histogram"> | null = null;
  let markersApi: ISeriesMarkersPluginApi<Time> | null = null;
  // Price lines accumulate on the series unless explicitly removed —
  // without this ledger, switching symbols would keep drawing the OLD
  // symbol's entry/stop at its absolute price on the new chart.
  let activeLines: IPriceLine[] = [];

  let symbol = $state(localStorage.getItem("jarvis.chart.symbol") ?? "BTC/USD");
  let timeframe = $state(localStorage.getItem("jarvis.chart.tf") ?? "1H");
  let payload = $state<ChartPayload | null>(null);
  let symbolInput = $state("");
  let available = $state<{ symbol: string; timeframes: string[] }[]>([]);
  let loading = $state(false);
  let showSignals = $state(true);

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
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
  }

  function render() {
    if (!chart || !candleSeries || !volumeSeries || !payload) return;
    candleSeries.setData(payload.bars as never[]);
    volumeSeries.setData(
      payload.bars.map((b) => ({ time: b.time, value: b.volume })) as never[],
    );

    // Open positions: entry/stop/target as price lines — the trade as it
    // actually sits on the chart, initial stop dashed if it has trailed.
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

    // Signal markers: arrows at generation time, direction-colored.
    // v5 moved markers to a plugin; one instance is reused across loads.
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

  let analogs = $state<AnalogSummary | null>(null);

  async function load() {
    loading = true;
    payload = await api.marketChart(symbol, timeframe).catch(() => null);
    loading = false;
    localStorage.setItem("jarvis.chart.symbol", symbol);
    localStorage.setItem("jarvis.chart.tf", timeframe);
    render();
    analogs = await api.marketAnalogs(symbol, timeframe).catch(() => null);
  }

  function jumpTo(time: string) {
    // Center the chart on an analog's moment so the rhyme can be SEEN.
    if (!chart) return;
    const t = Math.floor(new Date(time.replace(" ", "T") + (time.includes("+") ? "" : "Z")).getTime() / 1000);
    chart.timeScale().setVisibleRange({
      from: (t - 96 * 3600) as never,
      to: (t + 96 * 3600) as never,
    });
  }

  $effect(() => {
    symbol;
    timeframe;
    showSignals;
    if (chart) load();
  });

  $effect(() => {
    buildChart();
    api.chartSymbols().then((r) => (available = r.symbols)).catch(() => {});
    load();
    return () => chart?.remove();
  });

  function pickSymbol(e: Event) {
    e.preventDefault();
    const v = symbolInput.trim().toUpperCase();
    if (v) symbol = v;
    symbolInput = "";
  }
</script>

<div class="charts">
  <div class="bar">
    <form onsubmit={pickSymbol}>
      <input
        list="chart-symbols"
        placeholder={symbol}
        bind:value={symbolInput}
        aria-label="Symbol"
      />
      <datalist id="chart-symbols">
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
    <label class="toggle">
      <input type="checkbox" bind:checked={showSignals} /> signals
    </label>
    <div class="meta">
      {#if payload}
        <Pill tone="neutral">{payload.symbol}</Pill>
        <span class="muted">{payload.bar_count.toLocaleString()} bars</span>
        {#if payload.positions.length}
          <Pill tone="info">{payload.positions.length} open</Pill>
        {/if}
        {#if loading}<span class="muted">…</span>{/if}
      {/if}
    </div>
  </div>
  <div class="surface" bind:this={container}></div>
  {#if payload && !payload.bar_count}
    <p class="empty">no cached bars for {payload.symbol} @ {payload.timeframe} — the picker lists what the cache can draw</p>
  {/if}
  {#if analogs}
    <div class="analogs">
      <div class="ahead">
        <b>analogs</b>
        <span class="muted">
          {analogs.analogs.length} most similar non-overlapping moments of
          {analogs.candidates_searched.toLocaleString()} searched — history, not prediction
        </span>
        {#each Object.entries(analogs.forward_summary) as [h, s]}
          <span class="fwd">
            +{h.replace("fwd_", "").replace("b", "")} bars:
            <b class:pos={s.median_pct > 0} class:neg={s.median_pct < 0}>{s.median_pct}%</b>
            <span class="muted">({s.up_rate}% up, n={s.n})</span>
          </span>
        {/each}
      </div>
      <div class="alist">
        {#each analogs.analogs as a}
          <button class="chip" onclick={() => jumpTo(a.time)} title="jump chart to this moment">
            {String(a.time).slice(0, 10)}
            <span class:pos={Number(a["fwd_96b_pct"]) > 0} class:neg={Number(a["fwd_96b_pct"]) < 0}>
              {a["fwd_96b_pct"]}%
            </span>
          </button>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .charts {
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 12px 16px 16px;
    gap: 10px;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .bar input[list] {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--ink);
    border-radius: 8px;
    padding: 6px 10px;
    width: 150px;
    font-size: 13px;
  }
  .tfs {
    display: flex;
    gap: 4px;
  }
  .tfs button {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--ink-faint);
    border-radius: 7px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
  }
  .tfs button.on {
    color: var(--accent);
    border-color: var(--accent-dim);
    background: rgba(124, 154, 255, 0.08);
  }
  .toggle {
    font-size: 12px;
    color: var(--ink-faint);
    display: flex;
    gap: 5px;
    align-items: center;
  }
  .meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }
  .muted {
    color: var(--ink-faint);
    font-size: 12px;
  }
  .surface {
    flex: 1;
    min-height: 0;
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
  }
  .empty {
    color: var(--ink-faint);
    font-size: 12.5px;
    margin: 0;
  }
  .analogs {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .ahead {
    display: flex;
    gap: 14px;
    align-items: baseline;
    flex-wrap: wrap;
    font-size: 12.5px;
  }
  .fwd {
    font-variant-numeric: tabular-nums;
  }
  .alist {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .chip {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--ink-faint);
    border-radius: 7px;
    padding: 3px 8px;
    font-size: 11.5px;
    cursor: pointer;
    display: flex;
    gap: 6px;
    font-variant-numeric: tabular-nums;
  }
  .chip:hover {
    border-color: var(--accent-dim);
    color: var(--ink);
  }
  .pos { color: var(--good); }
  .neg { color: var(--bad); }
  .muted {
    color: var(--ink-faint);
  }
</style>
