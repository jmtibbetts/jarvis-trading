<script lang="ts">
  let {
    label,
    tone = "neutral",
  }: { label: string; tone?: "good" | "bad" | "warm" | "critical" | "neutral" } = $props();
</script>

<span class="pill {tone}">{label}</span>

<style>
  .pill {
    font-size: 9px;
    padding: 2px 6px;
    border-radius: 4px;
    letter-spacing: 0.04em;
    font-weight: 700;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .good {
    background: rgba(61, 220, 151, 0.12);
    color: var(--good);
  }
  .bad {
    background: rgba(255, 92, 114, 0.12);
    color: var(--bad);
  }
  .warm {
    background: rgba(255, 180, 84, 0.12);
    color: var(--warm);
  }
  .critical {
    background: rgba(255, 56, 100, 0.12);
    color: var(--critical);
  }
  .neutral {
    background: rgba(124, 154, 255, 0.12);
    color: var(--accent);
  }
</style>
