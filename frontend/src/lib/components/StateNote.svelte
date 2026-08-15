<script lang="ts">
  import { stateColor, stateLabel, ageText, type FeedStatus } from "../dataState.svelte";

  let {
    status,
    /** What this panel would have shown. Used for the EMPTY sentence. */
    noun = "data",
    /** Override the EMPTY line where the domain has a better sentence. */
    emptyText = "",
  }: { status: FeedStatus | null | undefined; noun?: string; emptyText?: string } = $props();

  // §64: "No liquidations observed" and "Liquidation feed unavailable" must
  // remain distinct. This is the body-level half of that — the header badge
  // names the state, this says what it means for the panel in front of you.
  //
  // Written to be a drop-in for the bare `<div class="empty">No X</div>` this
  // codebase used everywhere: the genuinely-empty case still renders as plain
  // muted text with no box, so replacing one does not change how a healthy
  // panel looks. Only the states that were previously indistinguishable from
  // empty get the callout treatment.
  const line = $derived.by(() => {
    if (!status) return "";
    switch (status.state) {
      case "loading":
        return `Loading ${noun}…`;
      case "ready":
      case "empty":
        return emptyText || `No ${noun} — the query succeeded and returned nothing.`;
      case "not_configured":
        return status.detail || `${noun} is not configured — credentials required.`;
      case "unsupported":
        return status.detail || `${noun} is not available on this deployment.`;
      case "degraded":
        return status.detail || `${noun} unavailable — the upstream provider returned nothing.`;
      case "stale":
        return status.lastGoodAt
          ? `Showing ${noun} from ${ageText(status.lastGoodAt)} — the latest refresh failed.`
          : `Refresh failed — ${noun} may be out of date.`;
      case "error":
        return status.detail || `Could not load ${noun}.`;
      default:
        return "";
    }
  });
</script>

{#if status && line}
  {#if status.state === "ready" || status.state === "empty" || status.state === "loading"}
    <div class="plain">{line}</div>
  {:else}
    <div class="note" style="--c: {stateColor(status.state)}">
      <span class="tag">{stateLabel(status.state)}</span>
      <span class="msg">{line}</span>
      {#if status.failures > 1}
        <span class="rep">· {status.failures} consecutive failures</span>
      {/if}
    </div>
  {/if}
{/if}

<style>
  .plain {
    color: var(--ink-faint);
    font-size: 12px;
    padding: 10px 2px;
  }
  .note {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid color-mix(in srgb, var(--c) 30%, transparent);
    background: color-mix(in srgb, var(--c) 7%, transparent);
    border-radius: var(--radius-sm);
    font-size: 11.5px;
    color: var(--ink-dim);
    line-height: 1.5;
  }
  .tag {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.05em;
    color: var(--c);
    flex: none;
  }
  .msg {
    min-width: 0;
  }
  .rep {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-faint);
  }
</style>
