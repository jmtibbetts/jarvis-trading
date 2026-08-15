<script lang="ts">
  /**
   * What actually followed the most similar past moments — as a DISTRIBUTION.
   *
   * The analog endpoint returns every individual outcome (12 analogs x 3
   * horizons of `fwd_Nb_pct`) and the page was reducing all of it to three
   * median numbers in a sentence. A median alone is the most misleading
   * summary available here: "+0.9% at 96 bars" reads like a forecast, when
   * the same twelve samples span -2.2% to +2.5%. The spread IS the finding,
   * and §125 asks for the primitive that shows it.
   *
   * Form: dot strip per horizon with an IQR band and a median tick — chosen
   * over a bar of medians because bars encode a single magnitude and hide
   * the sample, which is the opposite of what this data is for.
   *
   * COLOR NOTE (measured, not assumed). Sign is encoded by POSITION against
   * the zero line; colour is redundant reinforcement. The palette validator
   * rates this desk's --good/--bad pair at CVD ΔE 9.1 deutan (target ≥ 8)
   * and 35.8 normal, contrast ≥ 3:1 — it passes every check except the
   * dark-mode lightness band, where both tokens sit brighter than 0.48–0.67.
   * A band-compliant green/red drops CVD separation to ΔE 6.0, and the
   * fully-compliant alternative is blue/red (ΔE 23.4), which would make this
   * the only surface on the desk where up is not green. Keeping the domain
   * convention, with position carrying the encoding, is the deliberate call.
   */
  import type { AnalogSummary } from "../api";

  let { analogs }: { analogs: AnalogSummary } = $props();

  type Row = {
    key: string;
    label: string;
    points: number[];
    median: number;
    q1: number;
    q3: number;
    upRate: number;
    n: number;
  };

  const HORIZON_LABEL = (k: string) => `+${k.replace("fwd_", "").replace("b", "")} bars`;

  const rows = $derived.by<Row[]>(() => {
    const out: Row[] = [];
    for (const [key, s] of Object.entries(analogs.forward_summary ?? {})) {
      // Every analog's own outcome for this horizon, not just the summary.
      const points = (analogs.analogs ?? [])
        .map((a) => a[`${key}_pct`])
        .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
      out.push({
        key,
        label: HORIZON_LABEL(key),
        points,
        median: s.median_pct,
        q1: s.iqr_pct?.[0] ?? s.median_pct,
        q3: s.iqr_pct?.[1] ?? s.median_pct,
        upRate: s.up_rate,
        n: s.n,
      });
    }
    // Shortest horizon first — the reader is scanning outward in time.
    return out.sort((a, b) => Number(a.key.match(/\d+/)?.[0] ?? 0) - Number(b.key.match(/\d+/)?.[0] ?? 0));
  });

  // ONE shared scale across horizons. Independent scales per row would make a
  // ±0.5% spread and a ±2.5% spread look identical, which is the whole point
  // being made here.
  const domain = $derived.by(() => {
    const all = rows.flatMap((r) => [...r.points, r.q1, r.q3]);
    if (!all.length) return { lo: -1, hi: 1 };
    const lo = Math.min(...all), hi = Math.max(...all);
    const pad = Math.max(0.15, (hi - lo) * 0.12);
    // Zero must be inside the domain — it is the reference the reader judges
    // every point against.
    return { lo: Math.min(lo - pad, 0), hi: Math.max(hi + pad, 0) };
  });

  const W = 100; // viewBox units; the SVG scales to its container
  const x = (v: number) =>
    ((v - domain.lo) / (domain.hi - domain.lo || 1)) * W;

  const ROW_H = 46;
  const height = $derived(rows.length * ROW_H + 22);

  let hover = $state<{ label: string; v: number; cx: number; cy: number } | null>(null);

  const fmt = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
</script>

{#if rows.length}
  <div class="wrap">
    <!-- Explicit pixel height. With preserveAspectRatio="none" and
         `height: auto`, the browser derives height from the viewBox ratio —
         a 100x160 box at 437px wide rendered 700px tall. The x axis is meant
         to stretch; the rows are not. -->
    <svg
      viewBox="0 0 {W} {height}"
      preserveAspectRatio="none"
      style="height: {height}px"
      role="img"
      aria-label="Distribution of forward returns after each analog moment, by horizon"
    >
      <!-- Zero reference, drawn under the marks. -->
      <line class="zero" x1={x(0)} x2={x(0)} y1="0" y2={rows.length * ROW_H} />

      {#each rows as r, i (r.key)}
        {@const cy = i * ROW_H + ROW_H / 2}
        <!-- Interquartile band: where the middle half of history landed. -->
        <rect
          class="iqr"
          x={Math.min(x(r.q1), x(r.q3))}
          y={cy - 9}
          width={Math.max(0.4, Math.abs(x(r.q3) - x(r.q1)))}
          height="18"
          rx="1.5"
        />
        {#each r.points as p, j (j)}
          <circle
            class="pt"
            class:pos={p > 0}
            class:neg={p < 0}
            cx={x(p)}
            cy={cy}
            r="2.6"
            role="presentation"
            onmouseenter={() => (hover = { label: r.label, v: p, cx: x(p), cy })}
            onmouseleave={() => (hover = null)}
          />
        {/each}
        <!-- Median last so it is never buried under a dot. -->
        <line class="median" x1={x(r.median)} x2={x(r.median)} y1={cy - 11} y2={cy + 11} />
      {/each}
    </svg>

    <!-- Labels live in HTML, not SVG: they stay at real font sizes instead of
         being stretched by preserveAspectRatio="none". -->
    <div class="labels" style="--row-h: {ROW_H}px">
      {#each rows as r (r.key)}
        <div class="lab">
          <span class="lab-h">{r.label}</span>
          <span class="lab-m" class:pos={r.median > 0} class:neg={r.median < 0}>{fmt(r.median)}</span>
          <span class="lab-n">{r.upRate.toFixed(0)}% up · n={r.n}</span>
        </div>
      {/each}
    </div>

    {#if hover}
      <div class="tip" style="left: {hover.cx}%; top: {(hover.cy / height) * 100}%">
        {hover.label}: {fmt(hover.v)}
      </div>
    {/if}
  </div>

  <div class="axis">
    <span>{fmt(domain.lo)}</span>
    <span class="axis-zero">0</span>
    <span>{fmt(domain.hi)}</span>
  </div>

  <p class="foot">
    Each dot is one analog's actual forward return; the bar is the
    interquartile range and the tick is the median. Same scale on every row —
    a wide row is a wide outcome, not a different zoom. History, not
    prediction: {analogs.candidates_searched.toLocaleString()} candidate
    moments searched, {rows[0]?.n ?? 0} kept.
  </p>
{/if}

<style>
  .wrap {
    position: relative;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
    align-items: stretch;
  }
  svg {
    width: 100%;
    overflow: visible;
  }
  .zero {
    stroke: var(--line-bright);
    stroke-width: 0.3;
    vector-effect: non-scaling-stroke;
  }
  .iqr {
    fill: color-mix(in srgb, var(--accent) 16%, transparent);
    stroke: color-mix(in srgb, var(--accent) 34%, transparent);
    stroke-width: 0.2;
    vector-effect: non-scaling-stroke;
  }
  .pt {
    fill: var(--ink-dim);
    /* A 2px surface ring so overlapping samples stay countable — with 12
       points on one row, coincident dots otherwise read as one. */
    stroke: var(--surface);
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
    cursor: crosshair;
  }
  .pt.pos {
    fill: var(--good);
  }
  .pt.neg {
    fill: var(--bad);
  }
  .pt:hover {
    stroke: var(--ink);
  }
  .median {
    stroke: var(--ink);
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }
  .labels {
    display: flex;
    flex-direction: column;
    min-width: 128px;
  }
  .lab {
    height: var(--row-h);
    display: flex;
    flex-direction: column;
    justify-content: center;
    line-height: 1.25;
  }
  .lab-h {
    font-size: 10.5px;
    color: var(--ink-dim);
  }
  .lab-m {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink);
  }
  .lab-m.pos {
    color: var(--good);
  }
  .lab-m.neg {
    color: var(--bad);
  }
  .lab-n {
    font-size: 9.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
  }
  .tip {
    position: absolute;
    transform: translate(-50%, -160%);
    background: var(--surface-raised);
    border: 1px solid var(--line-bright);
    border-radius: 4px;
    padding: 3px 7px;
    font-family: var(--mono);
    font-size: 10.5px;
    color: var(--ink);
    pointer-events: none;
    white-space: nowrap;
    z-index: 2;
  }
  .axis {
    display: flex;
    justify-content: space-between;
    font-family: var(--mono);
    font-size: 9.5px;
    color: var(--ink-faint);
    margin-top: 2px;
    padding-right: 138px;
  }
  .axis-zero {
    color: var(--ink-dim);
  }
  .foot {
    margin: 8px 0 0;
    font-size: 10.5px;
    color: var(--ink-faint);
    line-height: 1.5;
  }
</style>
