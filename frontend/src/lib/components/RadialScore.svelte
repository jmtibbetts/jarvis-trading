<script lang="ts">
  let { score, size = 40 }: { score: number; size?: number } = $props();

  const color = $derived(score >= 75 ? "var(--good)" : score >= 60 ? "var(--warm)" : "var(--bad)");
  const r = $derived(size / 2 - 4);
  const circ = $derived(2 * Math.PI * r);
  const offset = $derived(circ - (Math.min(100, Math.max(0, score)) / 100) * circ);
</script>

<div class="ring" style="width:{size}px;height:{size}px">
  <svg width={size} height={size}>
    <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line-bright)" stroke-width="3" />
    <circle
      cx={size / 2}
      cy={size / 2}
      r={r}
      fill="none"
      stroke={color}
      stroke-width="3"
      stroke-linecap="round"
      stroke-dasharray={circ}
      stroke-dashoffset={offset}
      transform="rotate(-90 {size / 2} {size / 2})"
      style="filter: drop-shadow(0 0 3px {color})"
    />
  </svg>
  <span class="n num" style="color:{color}">{score}</span>
</div>

<style>
  .ring {
    position: relative;
    display: grid;
    place-items: center;
  }
  .ring svg {
    position: absolute;
    inset: 0;
  }
  .n {
    font-size: 11px;
    font-weight: 700;
    position: relative;
  }
</style>
