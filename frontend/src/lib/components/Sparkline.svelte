<script lang="ts">
  let {
    points,
    color = "var(--accent)",
    width = 90,
    height = 34,
  }: { points: number[]; color?: string; width?: number; height?: number } = $props();

  let canvas: HTMLCanvasElement;

  function draw() {
    if (!canvas || points.length < 2) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    // Resolve CSS custom properties (e.g. "var(--good)") to a concrete color
    // string canvas can composite with alpha suffixes below.
    const resolved = color.startsWith("var(")
      ? getComputedStyle(document.documentElement).getPropertyValue(color.slice(4, -1)).trim()
      : color;

    const min = Math.min(...points);
    const max = Math.max(...points);
    const pad = 3;
    const xs = points.map((_, i) => pad + i * ((width - pad * 2) / (points.length - 1)));
    const ys = points.map((v) => height - pad - ((v - min) / (max - min || 1)) * (height - pad * 2));

    const grad = ctx.createLinearGradient(0, 0, 0, height);
    grad.addColorStop(0, resolved + "55");
    grad.addColorStop(1, resolved + "00");
    ctx.beginPath();
    ctx.moveTo(xs[0], height);
    xs.forEach((x, i) => ctx.lineTo(x, ys[i]));
    ctx.lineTo(xs[xs.length - 1], height);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    xs.forEach((x, i) => (i === 0 ? ctx.moveTo(x, ys[i]) : ctx.lineTo(x, ys[i])));
    ctx.strokeStyle = resolved;
    ctx.lineWidth = 1.4;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(xs[xs.length - 1], ys[ys.length - 1], 2, 0, Math.PI * 2);
    ctx.fillStyle = resolved;
    ctx.fill();
  }

  $effect(() => {
    points;
    color;
    draw();
  });
</script>

<canvas bind:this={canvas}></canvas>
