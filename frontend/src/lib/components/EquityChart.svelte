<script lang="ts">
  import type { EquityPoint } from "../api";

  let {
    points,
    rangeHours = 24 * 7,
    onRange,
  }: {
    points: EquityPoint[];
    rangeHours?: number;
    onRange?: (hours: number) => void;
  } = $props();

  let canvas: HTMLCanvasElement;
  let wrap: HTMLDivElement;
  let hoverIdx = $state<number | null>(null);
  let showDrawdown = $state(true);

  const RANGES = [
    { label: "24H", hours: 24 },
    { label: "7D", hours: 24 * 7 },
    { label: "30D", hours: 24 * 30 },
    { label: "ALL", hours: 24 * 365 },
  ];

  // Room for axis labels — the chart previously had none.
  const PAD_L = 8;
  const PAD_R = 64; // y labels sit right, beside the live endpoint
  const PAD_T = 10;
  const PAD_B = 22; // x time labels

  const fmtUsd = (v: number) => "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  const fmtUsdExact = (v: number) =>
    "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  /** Ticks land on human numbers (1/2/2.5/5 x 10^n), not raw min/max. */
  function niceTicks(min: number, max: number, count: number): number[] {
    if (!(max > min)) return [min];
    const raw = (max - min) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const first = Math.ceil(min / step) * step;
    const out: number[] = [];
    for (let v = first; v <= max + step * 0.001; v += step) out.push(v);
    return out;
  }

  /** Intraday windows show clock time; multi-day windows show the date, so a
   * 7d curve doesn't repeat "00:00" six times. */
  function fmtTime(iso: string, spanHours: number): string {
    const d = new Date(iso);
    if (spanHours <= 36) return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
  }

  // ── Derived analytics shown alongside the curve ──────────────────────────
  const first = $derived(points.length ? points[0] : null);
  const last = $derived(points.length ? points[points.length - 1] : null);
  const changePct = $derived(
    first && last && first.equity ? ((last.equity - first.equity) / first.equity) * 100 : null,
  );
  const peak = $derived(
    points.length ? points.reduce((a, b) => (b.equity > a.equity ? b : a)) : null,
  );
  const trough = $derived(
    points.length ? points.reduce((a, b) => (b.equity < a.equity ? b : a)) : null,
  );
  /** Running peak per point — the high-water mark the drawdown shading uses. */
  const runningPeaks = $derived.by(() => {
    let hi = -Infinity;
    return points.map((p) => (hi = Math.max(hi, p.equity)));
  });
  const maxDrawdownPct = $derived.by(() => {
    let worst = 0;
    points.forEach((p, i) => {
      const hi = runningPeaks[i];
      if (hi > 0) worst = Math.min(worst, ((p.equity - hi) / hi) * 100);
    });
    return worst;
  });
  const hovered = $derived(hoverIdx != null ? points[hoverIdx] : null);
  const hoveredDd = $derived.by(() => {
    if (hoverIdx == null || !points[hoverIdx]) return null;
    const hi = runningPeaks[hoverIdx];
    return hi > 0 ? ((points[hoverIdx].equity - hi) / hi) * 100 : null;
  });

  function draw() {
    if (!canvas || !wrap) return;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    if (points.length < 2) {
      ctx.fillStyle = "#465268";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText("Not enough equity history yet", 12, h / 2);
      return;
    }

    const plotW = Math.max(10, w - PAD_L - PAD_R);
    const plotH = Math.max(10, h - PAD_T - PAD_B);
    const values = points.map((p) => p.equity);
    const startVal = values[0];
    const rawMin = Math.min(...values, startVal);
    const rawMax = Math.max(...values, startVal);
    const span = rawMax - rawMin || Math.max(1, rawMax * 0.01);
    const min = rawMin - span * 0.1;
    const max = rawMax + span * 0.1;

    const xAt = (i: number) => PAD_L + (i / (points.length - 1)) * plotW;
    const yAt = (v: number) => PAD_T + (1 - (v - min) / (max - min)) * plotH;

    const up = (last?.equity ?? 0) >= startVal;
    const accent = up ? "#3ddc97" : "#ff5c72";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "middle";

    // ── Y axis ───────────────────────────────────────────────────────────
    for (const v of niceTicks(min, max, 4)) {
      const y = yAt(v);
      ctx.strokeStyle = "rgba(124,154,255,.08)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD_L, y);
      ctx.lineTo(PAD_L + plotW, y);
      ctx.stroke();
      ctx.fillStyle = "#8792a8";
      ctx.textAlign = "left";
      ctx.fillText(fmtUsd(v), PAD_L + plotW + 7, y);
    }

    // ── X axis ───────────────────────────────────────────────────────────
    const spanHours =
      (new Date(points[points.length - 1].time).getTime() - new Date(points[0].time).getTime()) / 3.6e6;
    const labelCount = Math.max(2, Math.min(7, Math.floor(plotW / 85)));
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i <= labelCount; i++) {
      const idx = Math.round((i / labelCount) * (points.length - 1));
      const x = xAt(idx);
      ctx.strokeStyle = "rgba(124,154,255,.05)";
      ctx.beginPath();
      ctx.moveTo(x, PAD_T);
      ctx.lineTo(x, PAD_T + plotH);
      ctx.stroke();
      ctx.fillStyle = "#8792a8";
      ctx.fillText(fmtTime(points[idx].time, spanHours), x, PAD_T + plotH + 6);
    }

    // ── Drawdown shading: the gap between the high-water mark and equity ──
    if (showDrawdown) {
      ctx.beginPath();
      points.forEach((_, i) => (i === 0 ? ctx.moveTo(xAt(i), yAt(runningPeaks[i])) : ctx.lineTo(xAt(i), yAt(runningPeaks[i]))));
      for (let i = points.length - 1; i >= 0; i--) ctx.lineTo(xAt(i), yAt(points[i].equity));
      ctx.closePath();
      ctx.fillStyle = "rgba(255,92,114,.10)";
      ctx.fill();
      // high-water mark line
      ctx.beginPath();
      points.forEach((_, i) => (i === 0 ? ctx.moveTo(xAt(i), yAt(runningPeaks[i])) : ctx.lineTo(xAt(i), yAt(runningPeaks[i]))));
      ctx.strokeStyle = "rgba(255,92,114,.35)";
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // ── Starting-equity baseline ─────────────────────────────────────────
    const baseY = yAt(startVal);
    ctx.strokeStyle = "rgba(135,146,168,.45)";
    ctx.setLineDash([2, 4]);
    ctx.beginPath();
    ctx.moveTo(PAD_L, baseY);
    ctx.lineTo(PAD_L + plotW, baseY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#8792a8";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText("start", PAD_L + 3, baseY - 2);

    // ── Area + line ──────────────────────────────────────────────────────
    const grad = ctx.createLinearGradient(0, PAD_T, 0, PAD_T + plotH);
    grad.addColorStop(0, accent + "33");
    grad.addColorStop(1, accent + "00");
    ctx.beginPath();
    ctx.moveTo(xAt(0), PAD_T + plotH);
    points.forEach((p, i) => ctx.lineTo(xAt(i), yAt(p.equity)));
    ctx.lineTo(xAt(points.length - 1), PAD_T + plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    points.forEach((p, i) => (i === 0 ? ctx.moveTo(xAt(i), yAt(p.equity)) : ctx.lineTo(xAt(i), yAt(p.equity))));
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // ── Peak / trough markers ────────────────────────────────────────────
    const markPoint = (p: EquityPoint | null, color: string, label: string, above: boolean) => {
      if (!p) return;
      const i = points.indexOf(p);
      if (i < 0) return;
      const x = xAt(i);
      const y = yAt(p.equity);
      ctx.beginPath();
      ctx.arc(x, y, 2.6, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.fillStyle = color;
      ctx.textAlign = x > PAD_L + plotW * 0.85 ? "right" : "left";
      ctx.textBaseline = above ? "bottom" : "top";
      ctx.fillText(`${label} ${fmtUsd(p.equity)}`, x + (x > PAD_L + plotW * 0.85 ? -5 : 5), y + (above ? -5 : 5));
    };
    markPoint(peak, "#3ddc97", "peak", true);
    markPoint(trough, "#ff5c72", "low", false);

    // ── Live endpoint ────────────────────────────────────────────────────
    const lastX = xAt(points.length - 1);
    const lastY = yAt(points[points.length - 1].equity);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3.4, 0, Math.PI * 2);
    ctx.fillStyle = accent;
    ctx.shadowColor = accent;
    ctx.shadowBlur = 10;
    ctx.fill();
    ctx.shadowBlur = 0;

    // ── Hover crosshair ──────────────────────────────────────────────────
    if (hoverIdx != null && hoverIdx >= 0 && hoverIdx < points.length) {
      const hx = xAt(hoverIdx);
      const hy = yAt(points[hoverIdx].equity);
      ctx.strokeStyle = "rgba(124,154,255,.4)";
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hx, PAD_T);
      ctx.lineTo(hx, PAD_T + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(hx, hy, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#fff";
      ctx.fill();
    }
  }

  function onMove(e: MouseEvent) {
    if (!wrap || points.length < 2) return;
    const rect = wrap.getBoundingClientRect();
    const plotW = Math.max(10, rect.width - PAD_L - PAD_R);
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - PAD_L) / plotW));
    hoverIdx = Math.round(frac * (points.length - 1));
  }

  $effect(() => {
    points;
    hoverIdx;
    showDrawdown;
    draw();
  });

  $effect(() => {
    if (!wrap) return;
    const ro = new ResizeObserver(draw);
    ro.observe(wrap);
    return () => ro.disconnect();
  });
</script>

<div class="eq-head">
  {#if last}
    <span class="eq-now num">{fmtUsdExact(last.equity)}</span>
    {#if changePct != null}
      <span class="eq-chg num {changePct >= 0 ? 'pl-up' : 'pl-down'}">
        {changePct >= 0 ? "+" : ""}{changePct.toFixed(2)}%
      </span>
    {/if}
    <span class="eq-stat num"><i>peak</i> {peak ? fmtUsd(peak.equity) : "—"}</span>
    <span class="eq-stat num"><i>low</i> {trough ? fmtUsd(trough.equity) : "—"}</span>
    <span class="eq-stat num"><i>max DD</i> <span class="pl-down">{maxDrawdownPct.toFixed(2)}%</span></span>
  {/if}
  <div class="eq-controls">
    <button
      class="eq-toggle"
      class:on={showDrawdown}
      title="Shade the gap between equity and its running high-water mark"
      onclick={() => (showDrawdown = !showDrawdown)}
    >drawdown</button>
    {#if onRange}
      <div class="eq-ranges">
        {#each RANGES as r (r.hours)}
          <button class="eq-range" class:on={rangeHours === r.hours} onclick={() => onRange?.(r.hours)}>{r.label}</button>
        {/each}
      </div>
    {/if}
  </div>
</div>

<div class="eq-hover-line num">
  {#if hovered}
    {new Date(hovered.time).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
    &middot; <b>{fmtUsdExact(hovered.equity)}</b>
    &middot; <span class="dim">cash</span> {fmtUsd(hovered.cash)}
    &middot; <span class="dim">positions</span> {Math.round(hovered.position_count ?? 0)}
    {#if hovered.unrealized_pl != null}
      &middot; <span class="dim">unreal.</span> <span class={hovered.unrealized_pl >= 0 ? "pl-up" : "pl-down"}>{fmtUsd(hovered.unrealized_pl)}</span>
    {/if}
    {#if hoveredDd != null && hoveredDd < -0.001}
      &middot; <span class="pl-down">{hoveredDd.toFixed(2)}% off peak</span>
    {/if}
  {:else}
    <span class="dim">hover the chart for exact equity, cash, positions, and drawdown at any point</span>
  {/if}
</div>

<div
  class="wrap"
  bind:this={wrap}
  role="img"
  aria-label="Portfolio equity over time"
  onmousemove={onMove}
  onmouseleave={() => (hoverIdx = null)}
>
  <canvas bind:this={canvas}></canvas>
</div>

<style>
  .eq-head {
    display: flex;
    align-items: baseline;
    gap: 14px;
    flex-wrap: wrap;
    margin-bottom: 2px;
  }
  .eq-now {
    font-size: 18px;
    font-weight: 650;
  }
  .eq-chg {
    font-size: 12px;
    font-weight: 600;
  }
  .eq-stat {
    font-size: 10.5px;
    color: var(--ink-dim);
  }
  .eq-stat i {
    font-style: normal;
    color: var(--ink-faint);
    letter-spacing: 0.05em;
  }
  .eq-controls {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .eq-toggle,
  .eq-range {
    background: none;
    border: 1px solid var(--line);
    color: var(--ink-faint);
    font: inherit;
    font-size: 9.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 3px 7px;
    border-radius: 6px;
    cursor: pointer;
  }
  .eq-toggle.on,
  .eq-range.on {
    border-color: var(--accent);
    color: var(--ink);
    background: rgba(124, 154, 255, 0.12);
  }
  .eq-ranges {
    display: flex;
    gap: 3px;
  }
  .eq-hover-line {
    font-size: 10.5px;
    color: var(--ink-dim);
    min-height: 15px;
    margin-bottom: 4px;
  }
  .wrap {
    position: relative;
    height: 196px;
    width: 100%;
  }
  canvas {
    position: absolute;
    inset: 0;
  }
</style>
