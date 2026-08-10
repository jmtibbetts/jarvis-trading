<script lang="ts">
  import type { Candle } from "../api";

  let {
    candles,
    entry,
    target,
    stop,
  }: { candles: Candle[]; entry?: number | null; target?: number | null; stop?: number | null } = $props();

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

    if (!candles || candles.length < 2) {
      ctx.fillStyle = "#465268";
      ctx.font = "12px sans-serif";
      ctx.fillText("No candle data for this timeframe", 12, h / 2);
      return;
    }

    const padTop = 10;
    const padBottom = 10;
    const padLeft = 8;
    const padRight = 56;
    const chartW = w - padLeft - padRight;
    const chartH = h - padTop - padBottom;

    const refLevels = [entry, target, stop].filter((v): v is number => v != null && v > 0);
    const highs = candles.map((c) => c.high).concat(refLevels);
    const lows = candles.map((c) => c.low).concat(refLevels);
    const max = Math.max(...highs);
    const min = Math.min(...lows);
    const range = max - min || 1;

    const y = (price: number) => padTop + chartH - ((price - min) / range) * chartH;
    const n = candles.length;
    const slot = chartW / n;
    const bodyW = Math.max(1.5, slot * 0.6);

    // grid + price labels (5 lines)
    ctx.strokeStyle = "rgba(124,154,255,.06)";
    ctx.fillStyle = "#7188a0";
    ctx.font = "10px ui-monospace, monospace";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const price = min + (range * i) / 4;
      const py = y(price);
      ctx.beginPath();
      ctx.moveTo(padLeft, py);
      ctx.lineTo(w - padRight, py);
      ctx.stroke();
      ctx.fillText(price < 1 ? price.toFixed(6) : price.toFixed(2), w - padRight + 6, py + 3);
    }

    candles.forEach((c, i) => {
      const x = padLeft + i * slot + slot / 2;
      const up = c.close >= c.open;
      const color = up ? "#3ddc97" : "#ff5c72";
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      // wick
      ctx.beginPath();
      ctx.moveTo(x, y(c.high));
      ctx.lineTo(x, y(c.low));
      ctx.lineWidth = 1;
      ctx.stroke();
      // body
      const yOpen = y(c.open);
      const yClose = y(c.close);
      const top = Math.min(yOpen, yClose);
      const bh = Math.max(1, Math.abs(yClose - yOpen));
      ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
    });

    // entry/target/stop reference lines, dashed, labeled at the right edge
    const refs: [number | null | undefined, string, string][] = [
      [entry, "#7c9aff", "entry"],
      [target, "#3ddc97", "target"],
      [stop, "#ff5c72", "stop"],
    ];
    ctx.font = "9.5px ui-monospace, monospace";
    for (const [price, color, label] of refs) {
      if (price == null || price <= 0) continue;
      const py = y(price);
      ctx.strokeStyle = color;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(padLeft, py);
      ctx.lineTo(w - padRight, py);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.fillText(label, padLeft + 2, py - 3);
    }
  }

  $effect(() => {
    candles;
    entry;
    target;
    stop;
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
    height: 280px;
    width: 100%;
  }
  canvas {
    position: absolute;
    inset: 0;
  }
</style>
