<script lang="ts">
  /**
   * The charts surface: ONE chart with its analysis panels, or FOUR at once.
   *
   * §140.5 — four independent instruments on screen together, for manual
   * trading and for reading a produced signal against its peers. Each pane
   * owns its symbol, timeframe, data and load state (see ChartPane), so the
   * grid is four real charts rather than one chart drawn four times.
   *
   * The analysis panels (bar-return distribution, analog outcomes) stay with
   * the SINGLE view. At four-up there is no room to read them, and showing
   * one pane's analysis under a grid of four would leave the operator
   * guessing which pane it described.
   */
  import type { IChartApi } from "lightweight-charts";
  import { api, type AnalogSummary, type ChartPayload } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import ChartPane from "../components/ChartPane.svelte";
  import StateNote from "../components/StateNote.svelte";
  import AnalogDistribution from "../components/AnalogDistribution.svelte";
  import ReturnHistogram from "../components/ReturnHistogram.svelte";

  const feeds = new FeedTracker();

  type Layout = "single" | "grid";
  let layout = $state<Layout>(
    (localStorage.getItem("jarvis.chart.layout") as Layout) ?? "single",
  );
  $effect(() => localStorage.setItem("jarvis.chart.layout", layout));

  let available = $state<{ symbol: string; timeframes: string[] }[]>([]);
  let showSignals = $state(true);

  // Only the single view's pane reports back — the analysis panels below it
  // describe that one instrument.
  let payload = $state<ChartPayload | null>(null);
  let analogs = $state<AnalogSummary | null>(null);
  let mainChart: IChartApi | null = null;

  $effect(() => {
    feeds.load("symbols", () => api.chartSymbols()).then((r) => {
      if (r) available = r.symbols;
    });
  });

  async function onMainPayload(p: ChartPayload | null) {
    payload = p;
    analogs = p
      ? await feeds.load("analogs", () => api.marketAnalogs(p.symbol, p.timeframe), { keepLast: false })
      : null;
  }

  function jumpTo(time: string) {
    // Centre the chart on an analog's moment so the rhyme can be SEEN.
    if (!mainChart) return;
    const t = Math.floor(
      new Date(time.replace(" ", "T") + (time.includes("+") ? "" : "Z")).getTime() / 1000,
    );
    mainChart.timeScale().setVisibleRange({
      from: (t - 96 * 3600) as never,
      to: (t + 96 * 3600) as never,
    });
  }

  // Four sensible starting instruments rather than four copies of BTC. Each
  // pane overrides and persists its own choice from here.
  const GRID_DEFAULTS = ["BTC/USD", "ETH/USD", "SOL/USD", "SPY"];
</script>

<div class="charts">
  <div class="topbar">
    <div class="layouts">
      <button class:on={layout === "single"} onclick={() => (layout = "single")}>
        1 chart
      </button>
      <button class:on={layout === "grid"} onclick={() => (layout = "grid")}>
        4 charts
      </button>
    </div>
    <label class="toggle">
      <input type="checkbox" bind:checked={showSignals} /> signals
    </label>
    <span class="hint">
      {layout === "grid"
        ? "Four independent charts — each keeps its own symbol and timeframe."
        : "Bar-return and analog distributions sit under the chart."}
    </span>
  </div>

  {#if layout === "single"}
    <div class="single">
      <ChartPane
        paneId="main"
        {available}
        {showSignals}
        height="420px"
        onPayload={onMainPayload}
        onChart={(c) => (mainChart = c)}
      />
    </div>

    <div class="lower">
      <div class="lower-col">
        <div class="sub-head">
          <b>Bar returns</b>
          <span class="muted">
            how this instrument actually moves, from the
            {payload?.bar_count.toLocaleString() ?? 0} bars on screen
          </span>
        </div>
        {#if payload && payload.bars.length > 2}
          <ReturnHistogram bars={payload.bars} timeframe={payload.timeframe} />
        {:else}
          <StateNote status={feeds.status("chart")} noun="bars" emptyText="No bars to measure" />
        {/if}
      </div>

      <div class="lower-col">
        <div class="sub-head">
          <b>Analog outcomes</b>
          <span class="muted">
            {analogs?.analogs.length ?? 0} most similar non-overlapping moments — every
            one of them, not just the median
          </span>
        </div>
        {#if analogs}
          <AnalogDistribution {analogs} />
          <div class="alist">
            {#each analogs.analogs as a}
              <button class="chip" onclick={() => jumpTo(String(a.time))} title="jump chart to this moment">
                {String(a.time).slice(0, 10)}
                <span class:pos={Number(a["fwd_96b_pct"]) > 0} class:neg={Number(a["fwd_96b_pct"]) < 0}>
                  {a["fwd_96b_pct"]}%
                </span>
              </button>
            {/each}
          </div>
        {:else}
          <StateNote status={feeds.status("analogs")} noun="analogs" />
        {/if}
      </div>
    </div>
  {:else}
    <div class="grid">
      {#each GRID_DEFAULTS as sym, i (i)}
        <ChartPane
          paneId={`g${i}`}
          defaultSymbol={sym}
          {available}
          {showSignals}
          height="260px"
        />
      {/each}
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
    overflow-y: auto;
  }
  .topbar {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    flex: none;
  }
  .layouts {
    display: flex;
    gap: 2px;
  }
  .layouts button {
    background: none;
    border: 1px solid transparent;
    color: var(--ink-faint);
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 11.5px;
    cursor: pointer;
  }
  .layouts button.on {
    color: var(--accent);
    border-color: var(--line-bright);
    background: var(--surface-raised);
  }
  .toggle {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 11.5px;
    color: var(--ink-dim);
    cursor: pointer;
  }
  .hint {
    font-size: 10.5px;
    color: var(--ink-faint);
  }
  .single {
    flex: none;
  }
  /* Two rows of two. Each pane fixes its own chart height, so the grid does
     not depend on the section having a resolved height — which is what made
     an earlier flex version collapse to zero. */
  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    flex: none;
  }
  @media (max-width: 1100px) {
    .grid {
      grid-template-columns: 1fr;
    }
  }
  .lower {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr);
    gap: 10px;
    flex: none;
  }
  @media (max-width: 1100px) {
    .lower {
      grid-template-columns: 1fr;
    }
  }
  .lower-col {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .sub-head {
    display: flex;
    gap: 10px;
    align-items: baseline;
    flex-wrap: wrap;
    font-size: 12.5px;
  }
  .muted {
    color: var(--ink-faint);
    font-size: 10.5px;
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
    font-family: var(--mono);
  }
  .chip:hover {
    border-color: var(--line-bright);
    color: var(--ink);
  }
  .pos {
    color: var(--good);
  }
  .neg {
    color: var(--bad);
  }
</style>
