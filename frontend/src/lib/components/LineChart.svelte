<script lang="ts">
  /**
   * A small multi-series line chart, inline SVG.
   *
   * Inline SVG rather than the canvas `Sparkline` next door because these
   * charts carry meaning that has to survive: axis labels, a zero line, a
   * shaded region for "this is the part that matters". Canvas would need
   * DPR handling and a redraw on every theme change; SVG inherits the
   * theme's custom properties for free and stays crisp at any zoom.
   *
   * Values may contain null for a missing observation. The line BREAKS at a
   * gap rather than interpolating across it — drawing a straight segment
   * over a hole invents data that was never measured, which on a yield
   * curve or a spread history is exactly the kind of quiet fiction this
   * codebase refuses elsewhere.
   */
  type Series = {
    label: string;
    values: (number | null)[];
    color?: string;
    /** Fill under the line. Use for a single-series chart only. */
    area?: boolean;
    dashed?: boolean;
  };

  let {
    series,
    xLabels = [],
    height = 150,
    yFormat = (v: number) => v.toFixed(2),
    yUnit = "",
    zeroLine = false,
    /** Shade the region below this y value — e.g. 0 for curve inversion. */
    shadeBelow = null,
    shadeColor = "var(--bad)",
    shadeLabel = "",
    showDots = false,
    /** Force the y-axis to include these values (e.g. 0). */
    includeY = [],
  }: {
    series: Series[];
    xLabels?: string[];
    height?: number;
    yFormat?: (v: number) => string;
    yUnit?: string;
    zeroLine?: boolean;
    shadeBelow?: number | null;
    shadeColor?: string;
    shadeLabel?: string;
    showDots?: boolean;
    includeY?: number[];
  } = $props();

  // A fixed viewBox with preserveAspectRatio="none" would stretch the
  // stroke; instead the box is wide and the SVG scales by width with a
  // fixed height, which keeps strokes even.
  const W = 600;
  const PAD = { l: 44, r: 10, t: 10, b: 20 };

  const all = $derived(
    series.flatMap((s) => s.values).filter((v): v is number => v != null)
      .concat(includeY),
  );
  const lo = $derived(all.length ? Math.min(...all) : 0);
  const hi = $derived(all.length ? Math.max(...all) : 1);
  // A flat series would collapse to a zero-height band and divide by zero.
  const span = $derived(hi - lo || Math.abs(hi) || 1);
  const yMin = $derived(lo - span * 0.08);
  const yMax = $derived(hi + span * 0.08);

  const n = $derived(Math.max(...series.map((s) => s.values.length), 1));
  const plotW = W - PAD.l - PAD.r;
  const plotH = $derived(height - PAD.t - PAD.b);

  const x = (i: number) => PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v: number) =>
    PAD.t + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;

  /** Path with gaps: a null starts a new subpath instead of bridging. */
  function path(values: (number | null)[]): string {
    let d = "";
    let pen = false;
    values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  }

  function areaPath(values: (number | null)[]): string {
    const pts = values.map((v, i) => ({ v, i })).filter((p) => p.v != null);
    if (pts.length < 2) return "";
    const base = y(Math.max(yMin, Math.min(...[0, yMax])));
    const head = pts.map((p, k) => `${k ? "L" : "M"}${x(p.i).toFixed(1)},${y(p.v as number).toFixed(1)}`).join(" ");
    return `${head} L${x(pts[pts.length - 1].i).toFixed(1)},${base.toFixed(1)} L${x(pts[0].i).toFixed(1)},${base.toFixed(1)} Z`;
  }

  // Four gridlines is enough to read a level without becoming graph paper.
  const ticks = $derived(
    Array.from({ length: 4 }, (_, k) => yMin + ((yMax - yMin) * k) / 3),
  );

  // Only label as many x ticks as will fit legibly.
  const xTickEvery = $derived(Math.max(1, Math.ceil(n / 8)));
  const shadeY = $derived(
    shadeBelow == null ? null : Math.min(plotH + PAD.t, Math.max(PAD.t, y(shadeBelow))),
  );
</script>

<div class="wrap">
  <svg viewBox="0 0 {W} {height}" style="height:{height}px" role="img"
       aria-label={series.map((s) => s.label).join(", ")}>
    <!-- horizontal gridlines + y labels -->
    {#each ticks as t}
      <line class="grid" x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
      <text class="ylab" x={PAD.l - 6} y={y(t) + 3} text-anchor="end">{yFormat(t)}</text>
    {/each}

    <!-- the region that carries the meaning, e.g. an inverted curve -->
    {#if shadeY != null}
      <rect class="shade" x={PAD.l} y={shadeY} width={plotW}
            height={Math.max(0, plotH + PAD.t - shadeY)}
            style="fill:{shadeColor}" />
      {#if shadeLabel}
        <text class="shadelab" x={PAD.l + 5} y={shadeY + 12} style="fill:{shadeColor}">{shadeLabel}</text>
      {/if}
    {/if}

    {#if zeroLine && yMin < 0 && yMax > 0}
      <line class="zero" x1={PAD.l} x2={W - PAD.r} y1={y(0)} y2={y(0)} />
    {/if}

    {#each series as s}
      {#if s.area}
        <path d={areaPath(s.values)} style="fill:{s.color ?? 'var(--accent)'}" class="area" />
      {/if}
      <path d={path(s.values)} style="stroke:{s.color ?? 'var(--accent)'}"
            class="line" class:dashed={s.dashed} />
      {#if showDots}
        {#each s.values as v, i}
          {#if v != null}
            <circle cx={x(i)} cy={y(v)} r="2.5" style="fill:{s.color ?? 'var(--accent)'}">
              <title>{xLabels[i] ?? i}: {yFormat(v)}{yUnit}</title>
            </circle>
          {/if}
        {/each}
      {/if}
    {/each}

    {#each xLabels as lab, i}
      {#if i % xTickEvery === 0}
        <text class="xlab" x={x(i)} y={height - 6} text-anchor="middle">{lab}</text>
      {/if}
    {/each}
  </svg>

  {#if series.length > 1}
    <div class="legend">
      {#each series as s}
        <span class="key">
          <i style="background:{s.color ?? 'var(--accent)'}"></i>{s.label}
        </span>
      {/each}
    </div>
  {/if}
</div>

<style>
  .wrap { width: 100%; }
  svg { width: 100%; display: block; overflow: visible; }
  .grid { stroke: var(--line); stroke-width: 1; opacity: 0.55; }
  .zero { stroke: var(--ink-faint); stroke-width: 1; stroke-dasharray: 3 3; }
  .line { fill: none; stroke-width: 1.75; stroke-linejoin: round; stroke-linecap: round; }
  .line.dashed { stroke-dasharray: 4 3; }
  .area { opacity: 0.13; }
  .shade { opacity: 0.10; }
  .ylab, .xlab { font-family: var(--mono); font-size: 9px; fill: var(--ink-faint); }
  .shadelab { font-family: var(--mono); font-size: 8.5px; opacity: 0.9; }
  .legend {
    display: flex; flex-wrap: wrap; gap: 12px;
    margin-top: 6px; font-size: 10.5px; color: var(--ink-dim);
  }
  .key { display: inline-flex; align-items: center; gap: 5px; }
  .key i { width: 9px; height: 2.5px; border-radius: 2px; display: inline-block; }
</style>
