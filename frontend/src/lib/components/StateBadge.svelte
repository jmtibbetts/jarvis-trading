<script lang="ts">
  import { stateLabel, stateColor, ageText, type FeedStatus } from "../dataState.svelte";

  let { status, compact = false }: { status: FeedStatus | null | undefined; compact?: boolean } =
    $props();

  // READY is the boring case and does not earn pixels on a dense workstation
  // layout — a badge on every healthy panel is noise that trains the operator
  // to stop reading badges. EMPTY is shown, because "genuinely nothing" is a
  // real answer the operator should be able to tell apart from a failure.
  const visible = $derived(status != null && status.state !== "ready");

  // Re-render the age while a feed is stale, so "showing data from 40s ago"
  // does not sit frozen at the age it had when the request failed.
  let tick = $state(0);
  $effect(() => {
    if (status?.state !== "stale") return;
    const t = setInterval(() => (tick += 1), 5_000);
    return () => clearInterval(t);
  });

  const age = $derived.by(() => {
    void tick;
    return status?.state === "stale" && status.lastGoodAt ? ageText(status.lastGoodAt) : null;
  });
</script>

{#if visible && status}
  <span
    class="badge"
    class:compact
    style="--c: {stateColor(status.state)}"
    title={status.detail || stateLabel(status.state)}
  >
    <span class="dot"></span>
    <span class="label">{stateLabel(status.state)}</span>
    {#if age}<span class="age">· {age}</span>{/if}
  </span>
{/if}

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.04em;
    color: var(--c);
    border: 1px solid color-mix(in srgb, var(--c) 35%, transparent);
    background: color-mix(in srgb, var(--c) 10%, transparent);
    border-radius: 4px;
    padding: 2px 6px;
    white-space: nowrap;
    flex: none;
    cursor: help;
  }
  .badge.compact .label {
    display: none;
  }
  .badge.compact {
    padding: 2px;
  }
  .dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--c);
    flex: none;
  }
  .age {
    color: color-mix(in srgb, var(--c) 70%, var(--ink-faint));
  }
</style>
