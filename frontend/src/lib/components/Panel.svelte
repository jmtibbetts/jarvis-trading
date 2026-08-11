<script lang="ts">
  import type { Snippet } from "svelte";
  import { sectionStore, isPopout, popPanel, openPanelPopout } from "../stores/section.svelte";

  let {
    title,
    meta = "",
    dotColor = "var(--accent)",
    noPad = false,
    children,
  }: {
    title: string;
    meta?: string;
    dotColor?: string;
    noPad?: boolean;
    children: Snippet;
  } = $props();

  // Every panel is popout-able with zero per-callsite wiring: its id is the
  // slugified title. In a panel-popout window, only the matching panel
  // renders — everything else returns nothing.
  const slug = (t: string) =>
    t.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
  const myId = $derived(slug(title));
  const hidden = $derived(popPanel !== null && popPanel !== myId);
</script>

{#if !hidden}
  <div class="panel" class:solo={popPanel !== null}>
    <div class="panel-head">
      <div class="t">
        <span class="dot" style="background:{dotColor}; box-shadow: 0 0 8px {dotColor}"></span>
        {title}
      </div>
      <div class="right">
        {#if meta}<div class="meta">{meta}</div>{/if}
        {#if !isPopout}
          <button
            class="pop"
            title="Open “{title}” in its own window"
            aria-label="Open {title} in a new window"
            onclick={() => openPanelPopout(sectionStore.current, myId)}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M7 17L17 7M9 7h8v8" />
            </svg>
          </button>
        {/if}
      </div>
    </div>
    <div class="panel-body" class:no-pad={noPad}>
      {@render children()}
    </div>
  </div>
{/if}

<style>
  .panel {
    background: linear-gradient(180deg, var(--surface), var(--surface) 60%, var(--surface-raised));
    border: 1px solid var(--line);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    min-width: 0;
    height: 100%;
  }
  /* Solo (panel-popout) mode: the one visible panel fills its window. */
  .panel.solo {
    min-height: calc(100vh - 32px);
  }
  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
    flex: none;
  }
  .t {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    font-weight: 650;
    letter-spacing: 0.02em;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex: none;
  }
  .right {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pop {
    width: 18px;
    height: 18px;
    border-radius: 5px;
    display: grid;
    place-items: center;
    background: none;
    border: 1px solid transparent;
    color: var(--ink-faint);
    cursor: pointer;
    padding: 0;
    opacity: 0;
    transition: opacity 0.12s;
    flex: none;
  }
  .pop svg {
    width: 11px;
    height: 11px;
  }
  .panel-head:hover .pop {
    opacity: 1;
  }
  .pop:hover {
    color: var(--accent);
    border-color: var(--line-bright);
  }
  .panel-body {
    padding: 14px;
    min-height: 0;
    flex: 1;
  }
  .panel-body.no-pad {
    padding: 0;
  }
</style>
