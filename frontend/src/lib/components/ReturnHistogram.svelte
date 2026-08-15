<script lang="ts">
  /**
   * The distribution of per-bar returns for whatever is on the chart.
   *
   * Every bar needed for this was already in the browser — the candle series
   * has been drawing open/close for months and nothing ever asked what the
   * shape of those moves is. That shape is the input to every stop decision
   * on the desk: a 1% stop on an instrument whose typical bar is 0.1% is a
   * different trade from the same stop on one whose typical bar is 0.9%, and
   * the sizing bug fixed alongside this came straight out of stops set
   * without that context.
   *
   * Computed client-side from bars already loaded — no endpoint, no refetch.
   *
   * Form: histogram, because the question is "how are these distributed",
   * and a single hue with a diverging split at zero, because sign is the
   * only categorical distinction present. Bars are positioned by return, so
   * as in AnalogDistribution the colour is redundant with position.
   */
  import type { ChartBar } from "../api";

  let { bars, timeframe }: { bars: ChartBar[]; timeframe: string } = $props();

  const BIN_COUNT = 31; // odd, so one bin straddles zero rather than splitting it

  const stats = $derived.by(() => {
    const rets: number[] = [];
    for (const b of bars) {
      if (!b.open || !Number.isFinite(b.open) || !Number.isFinite(b.close)) continue;
      rets.push(((b.close - b.open) / b.open) * 100);
    }
    if (rets.length < 3) return null;

    const sorted = [...rets].sort((a, b) => a - b);
    const q = (p: number) => sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
    // Clip the axis to the 1st–99th percentile so a single gap candle cannot
    // squash every ordinary bar into the middle column. The outliers are still
    // COUNTED — they land in the end bins — they just do not set the scale.
    const lo = Math.min(q(0.01), 0);
    const hi = Math.max(q(0.99), 0);
    const span = hi - lo || 1;
    const width = span / BIN_COUNT;

    const bins = Array.from({ length: BIN_COUNT }, (_, i) => ({
      from: lo + i * width,
      to: lo + (i + 1) * width,
      count: 0,
    }));
    for (const r of rets) {
      const idx = Math.max(0, Math.min(BIN_COUNT - 1, Math.floor((r - lo) / width)));
      bins[idx].count++;
    }

    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length);
    const typical = q(0.5);
    // The number a stop is actually competing with: how far a bar moves in
    // absolute terms, most of the time.
    const absSorted = rets.map(Math.abs).sort((a, b) => a - b);
    const absP80 = absSorted[Math.floor(0.8 * absSorted.length)];

    return {
      bins,
      lo,
      hi,
      n: rets.length,
      mean,
      sd,
      typical,
      absP80,
      upRate: (rets.filter((r) => r > 0).length / rets.length) * 100,
      max: Math.max(...bins.map((b) => b.count)),
    };
  });

  let hover = $state<{ i: number; from: number; to: number; count: number } | null>(null);
  // `(-0.001).toFixed(2)` is "-0.00", which renders as a negative zero and
  // reads as a small loss. Snap anything that rounds to nothing back to 0.
  const fmt = (v: number) => {
    const r = Number(v.toFixed(2));
    return `${r > 0 ? "+" : ""}${r === 0 ? "0.00" : r.toFixed(2)}%`;
  };
</script>

{#if stats}
  <div class="hist" role="img" aria-label="Distribution of per-bar percentage returns">
    {#each stats.bins as b, i (i)}
      {@const mid = (b.from + b.to) / 2}
      <div
        class="col"
        class:pos={mid > 0}
        class:neg={mid < 0}
        class:on={hover?.i === i}
        style="height: {Math.max(2, (b.count / stats.max) * 100)}%"
        onmouseenter={() => (hover = { i, ...b })}
        onmouseleave={() => (hover = null)}
        role="presentation"
      ></div>
    {/each}
  </div>

  <div class="axis">
    <span>{fmt(stats.lo)}</span>
    <span class="zero">0</span>
    <span>{fmt(stats.hi)}</span>
  </div>

  <div class="tipline">
    {#if hover}
      <span class="tip">
        {fmt(hover.from)} to {fmt(hover.to)} · {hover.count} bar{hover.count === 1 ? "" : "s"}
      </span>
    {:else}
      <span class="muted">hover a column for its range and count</span>
    {/if}
  </div>

  <div class="stats">
    <div><span>bars</span><b>{stats.n.toLocaleString()}</b></div>
    <div><span>median</span><b class:pos={stats.typical > 0} class:neg={stats.typical < 0}>{fmt(stats.typical)}</b></div>
    <!-- "SD", not "σ": the label row is uppercased by CSS, which turns a
         lowercase sigma into Σ — summation, not standard deviation. -->
    <div><span>sd per bar</span><b>{stats.sd.toFixed(2)}%</b></div>
    <div>
      <span title="80% of {timeframe} bars move less than this, in either direction">
        80% move &lt;
      </span>
      <b>{stats.absP80.toFixed(2)}%</b>
    </div>
    <div><span>up bars</span><b>{stats.upRate.toFixed(0)}%</b></div>
  </div>

  <p class="foot">
    A stop inside the 80% band will be hit by ordinary {timeframe} noise rather
    than by the thesis failing. Axis clipped to the 1st–99th percentile so one
    gap cannot flatten the rest; outliers are still counted in the end columns.
  </p>
{/if}

<style>
  .hist {
    display: flex;
    align-items: flex-end;
    gap: 1px;
    height: 120px;
    padding: 0 1px;
  }
  .col {
    flex: 1;
    min-width: 0;
    background: var(--ink-faint);
    /* 4px rounded data-end, anchored to the baseline. */
    border-radius: 3px 3px 0 0;
    transition: filter 0.1s;
    cursor: crosshair;
  }
  .col.pos {
    background: color-mix(in srgb, var(--good) 72%, transparent);
  }
  .col.neg {
    background: color-mix(in srgb, var(--bad) 72%, transparent);
  }
  .col.on {
    filter: brightness(1.5);
  }
  .axis {
    display: flex;
    justify-content: space-between;
    font-family: var(--mono);
    font-size: 9.5px;
    color: var(--ink-faint);
    margin-top: 3px;
  }
  .zero {
    color: var(--ink-dim);
  }
  .tipline {
    height: 16px;
    margin-top: 2px;
    font-family: var(--mono);
    font-size: 10px;
  }
  .tip {
    color: var(--ink);
  }
  .muted {
    color: var(--ink-faint);
    font-size: 10px;
  }
  .stats {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 18px;
    margin-top: 6px;
  }
  .stats div {
    display: flex;
    flex-direction: column;
    line-height: 1.2;
  }
  .stats span {
    font-size: 9.5px;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .stats b {
    font-family: var(--mono);
    font-size: 12.5px;
    color: var(--ink);
    font-weight: 600;
  }
  .pos {
    color: var(--good);
  }
  .neg {
    color: var(--bad);
  }
  .foot {
    margin: 8px 0 0;
    font-size: 10.5px;
    color: var(--ink-faint);
    line-height: 1.5;
  }
</style>
