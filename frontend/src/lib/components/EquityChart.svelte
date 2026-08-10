<script lang="ts">
  import type { EquityPoint } from "../api";

  let { points }: { points: EquityPoint[] } = $props();

  let canvas: HTMLCanvasElement;
  let wrap: HTMLDivElement;

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

    ctx.strokeStyle = "rgba(124,154,255,.06)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 5; i++) {
      const y = (h / 5) * i;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    if (points.length < 2) {
      ctx.fillStyle = "#465268";
      ctx.font = "12px sans-serif";
      ctx.fillText("Not enough equity history yet", 12, h / 2);
      return;
    }

    const values = points.map((p) => p.equity);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const pad = 8;
    const xs = points.map((_, i) => i * (w / (points.length - 1)));
    const ys = values.map((v) => h - pad - ((v - min) / (max - min || 1)) * (h - pad * 2));

    const accent = "#7c9aff";
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, accent + "33");
    grad.addColorStop(1, accent + "00");
    ctx.beginPath();
    ctx.moveTo(0, h);
    xs.forEach((x, i) => ctx.lineTo(x, ys[i]));
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    xs.forEach((x, i) => (i === 0 ? ctx.moveTo(x, ys[i]) : ctx.lineTo(x, ys[i])));
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.7;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(xs[xs.length - 1], ys[ys.length - 1], 3.2, 0, Math.PI * 2);
    ctx.fillStyle = accent;
    ctx.shadowColor = accent;
    ctx.shadowBlur = 10;
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  $effect(() => {
    points;
    draw();
  });

  $effect(() => {
    if (!wrap) return;
    const ro = new ResizeObserver(draw);
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
