<script lang="ts">
  import Sparkline from "./Sparkline.svelte";

  let {
    label,
    value,
    trend = "neutral",
    spark = [],
    sparkColor,
    period = "",
  }: {
    label: string;
    value: string;
    trend?: "up" | "down" | "neutral";
    spark?: number[];
    sparkColor?: string;
    period?: string;
  } = $props();

  const resolvedSparkColor = $derived(sparkColor ?? (trend === "down" ? "var(--bad)" : "var(--accent)"));
</script>

<div class="kpi">
  <div class="eyebrow-row">
    <span class="eyebrow">{label}</span>
    {#if period}<span class="period num">{period}</span>{/if}
  </div>
  <div class="val" class:up={trend === "up"} class:down={trend === "down"}>{value}</div>
  {#if spark.length > 1}
    <div class="spark-wrap"><Sparkline points={spark} color={resolvedSparkColor} /></div>
  {/if}
</div>

<style>
  .kpi {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 13px 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    position: relative;
    overflow: hidden;
  }
  .eyebrow-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .period {
    font-size: 9px;
    color: var(--ink-dim);
  }
  .val {
    font-size: 21px;
    font-weight: 650;
    letter-spacing: -0.01em;
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
  }
  .val.up {
    color: var(--good);
  }
  .val.down {
    color: var(--bad);
  }
  .spark-wrap {
    position: absolute;
    right: 0;
    bottom: 0;
    opacity: 0.55;
  }
</style>
