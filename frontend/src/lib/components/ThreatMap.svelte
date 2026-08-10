<script lang="ts">
  import type { Threat } from "../api";

  let { threats }: { threats: Threat[] } = $props();

  let canvas: HTMLCanvasElement;
  let wrap: HTMLDivElement = $state()!;
  let raf = 0;
  let dotPositions: { threat: Threat; x: number; y: number }[] = [];
  let selected = $state<{ threat: Threat; x: number; y: number } | null>(null);

  // Coarse landmass mask, read as a stylized world map — not real geo data,
  // just enough for the dot-matrix continents to be recognizable.
  const rows = [
    "............................................................",
    "..........XXXXXXX.......XXXXXXXXXXXXXXXXXXXXXXXX..........",
    ".......XXXXXXXXXXXX.....XXXXXXXXXXXXXXXXXXXXXXXXXXXX.......",
    "......XXXXXXXXXXXXXX....XXXXXXXXXXXXX.XXXXXXXXXXXXXXXX.....",
    ".....XXXXXXXXXXXXXXX...XXXXXXX...XXXX...XXXXXXXXXXXXXX.....",
    "......XXXXXXXXXXXXX....XXXXX......XX.....XXXXXXXXXXXXX.....",
    "........XXXXXXXXX........XX.......XX......XXXXXXXXXX.......",
    ".........XXXXXX..........XX.......XXX........XXXXXX........",
    "..........XXXX...........XXX.......XXX.......XXXX..........",
    "...........XX............XXXX.......XXX.......XX...........",
    "...........XX.............XXX........XX.......XX...........",
    "...........X..............XX.........XX........X...........",
    "..........XX...............X.........XX.......XXX..........",
    ".........XXX................X.........X........XX..........",
    "..........X.................X.........X........X...........",
    "............................................................",
  ];

  const SEV_COLOR: Record<string, string> = {
    Critical: "#ff3864",
    High: "#ffb454",
    Medium: "#7c9aff",
    Low: "#3ddc97",
  };

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

    const cols = rows[0].length;
    const rcount = rows.length;
    const cw = w / cols;
    const ch = h / rcount;
    ctx.fillStyle = "rgba(124,154,255,.16)";
    for (let r = 0; r < rcount; r++) {
      for (let c = 0; c < cols; c++) {
        if (rows[r][c] === "X") {
          ctx.beginPath();
          ctx.arc(c * cw + cw / 2, r * ch + ch / 2, Math.min(cw, ch) * 0.22, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    const withCoords = threats.filter((t) => t.latitude != null && t.longitude != null);
    dotPositions = [];
    withCoords.forEach((t, i) => {
      const x = ((t.longitude! + 180) / 360) * w;
      const y = ((90 - t.latitude!) / 180) * h;
      dotPositions.push({ threat: t, x, y });
      const isSelected = selected?.threat.id === t.id;
      const pulse = 3 + 2 * Math.abs(Math.sin(Date.now() / 700 + i));
      const color = SEV_COLOR[t.severity] ?? SEV_COLOR.Medium;
      ctx.beginPath();
      ctx.arc(x, y, pulse + 5, 0, Math.PI * 2);
      ctx.fillStyle = color + "22";
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x, y, isSelected ? 4.5 : 3, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.shadowColor = color;
      ctx.shadowBlur = isSelected ? 16 : 10;
      ctx.fill();
      ctx.shadowBlur = 0;
      if (isSelected) {
        ctx.beginPath();
        ctx.arc(x, y, 8, 0, Math.PI * 2);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    });

    raf = requestAnimationFrame(draw);
  }

  function onCanvasClick(e: MouseEvent) {
    const rect = canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    let closest: { threat: Threat; x: number; y: number } | null = null;
    let closestDist = Infinity;
    for (const dot of dotPositions) {
      const d = Math.hypot(dot.x - cx, dot.y - cy);
      if (d < closestDist) { closestDist = d; closest = dot; }
    }
    selected = closest && closestDist <= 12 ? closest : null;
  }

  $effect(() => {
    threats;
    if (raf) cancelAnimationFrame(raf);
    draw();
    return () => cancelAnimationFrame(raf);
  });

  $effect(() => {
    if (!wrap) return;
    const ro = new ResizeObserver(() => {});
    ro.observe(wrap);
    return () => ro.disconnect();
  });
</script>

<div class="wrap" bind:this={wrap}>
  <canvas bind:this={canvas} onclick={onCanvasClick}></canvas>
  {#if selected}
    <div
      class="tooltip"
      style="left:{Math.min(Math.max(selected.x, 90), (wrap?.clientWidth ?? 300) - 90)}px; top:{Math.min(selected.y + 14, (wrap?.clientHeight ?? 236) - 70)}px"
    >
      <button class="tooltip-close" onclick={() => (selected = null)}>✕</button>
      <div class="tooltip-sev" style="color:{SEV_COLOR[selected.threat.severity] ?? SEV_COLOR.Medium}">{selected.threat.severity}</div>
      <div class="tooltip-title">{selected.threat.title}</div>
      <div class="tooltip-meta">{selected.threat.country || selected.threat.region || "Global"}</div>
    </div>
  {/if}
</div>

<style>
  .wrap {
    position: relative;
    height: 236px;
    width: 100%;
  }
  canvas {
    position: absolute;
    inset: 0;
    cursor: pointer;
  }
  .tooltip {
    position: absolute;
    transform: translateX(-50%);
    width: 180px;
    background: var(--surface-raised, #12161d);
    border: 1px solid var(--line-bright, #2a3140);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 11px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    z-index: 5;
  }
  .tooltip-close {
    position: absolute;
    top: 4px;
    right: 6px;
    background: none;
    border: none;
    color: var(--ink-faint, #7a8296);
    cursor: pointer;
    font-size: 10px;
    padding: 2px;
  }
  .tooltip-sev {
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    margin-bottom: 3px;
  }
  .tooltip-title {
    color: var(--ink, #e8ebf2);
    line-height: 1.4;
    margin-bottom: 3px;
  }
  .tooltip-meta {
    color: var(--ink-faint, #7a8296);
    font-size: 10px;
  }
</style>
