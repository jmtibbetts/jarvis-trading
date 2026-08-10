<script lang="ts">
  import type { Threat } from "../api";

  let { threats }: { threats: Threat[] } = $props();

  let canvas: HTMLCanvasElement;
  let wrap: HTMLDivElement;
  let raf = 0;

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
    withCoords.forEach((t, i) => {
      const x = ((t.longitude! + 180) / 360) * w;
      const y = ((90 - t.latitude!) / 180) * h;
      const pulse = 3 + 2 * Math.abs(Math.sin(Date.now() / 700 + i));
      const color = SEV_COLOR[t.severity] ?? SEV_COLOR.Medium;
      ctx.beginPath();
      ctx.arc(x, y, pulse + 5, 0, Math.PI * 2);
      ctx.fillStyle = color + "22";
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.shadowColor = color;
      ctx.shadowBlur = 10;
      ctx.fill();
      ctx.shadowBlur = 0;
    });

    raf = requestAnimationFrame(draw);
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
  <canvas bind:this={canvas}></canvas>
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
  }
</style>
