<script lang="ts" generics="T">
  /**
   * Fixed-row-height windowed list.
   *
   * The scanner can return 200 signals, each of which would otherwise be a
   * live DOM row with its own event handlers, and every filter keystroke
   * rebuilds all of them. Rendering only the rows in view keeps that flat:
   * the browser holds ~30 rows regardless of whether the list is 40 long or
   * 4,000.
   *
   * FIXED HEIGHT IS A REQUIREMENT, NOT A SHORTCUT. Windowing needs to know
   * where row N sits without having measured rows 0..N-1. Variable heights
   * need measurement and a running offset table, and a wrong guess makes the
   * scrollbar lie and the list jump under the cursor while you read it. The
   * table rows this drives are uniform by construction; anything that is not
   * should render normally rather than pretend.
   */
  import type { Snippet } from "svelte";

  let {
    items,
    rowHeight,
    height = "58vh",
    overscan = 8,
    row,
    empty = undefined,
  }: {
    items: T[];
    /** Exact px height of one row. Must match the row markup's real height. */
    rowHeight: number;
    /** CSS height of the scroll viewport. */
    height?: string;
    /** Rows rendered beyond each edge, so a fast scroll does not show gaps. */
    overscan?: number;
    row: Snippet<[T, number]>;
    empty?: Snippet;
  } = $props();

  let scrollTop = $state(0);
  let viewportH = $state(0);

  const total = $derived(items.length * rowHeight);
  const first = $derived(
    Math.max(0, Math.floor(scrollTop / rowHeight) - overscan),
  );
  const visibleCount = $derived(
    Math.ceil((viewportH || 600) / rowHeight) + overscan * 2,
  );
  const slice = $derived(items.slice(first, first + visibleCount));
  // The spacer above holds the un-rendered rows' height so the scrollbar
  // describes the whole list rather than the window.
  const offset = $derived(first * rowHeight);
</script>

<div
  class="vl"
  style="height:{height}"
  onscroll={(e) => (scrollTop = (e.currentTarget as HTMLDivElement).scrollTop)}
  bind:clientHeight={viewportH}
>
  {#if !items.length}
    <div class="vl-empty">{#if empty}{@render empty()}{/if}</div>
  {:else}
    <div class="vl-sizer" style="height:{total}px">
      <div class="vl-window" style="transform: translateY({offset}px)">
        {#each slice as item, i (first + i)}
          <div class="vl-row" style="height:{rowHeight}px">
            {@render row(item, first + i)}
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .vl {
    overflow: auto;
    position: relative;
    min-height: 0;
  }
  .vl-sizer {
    position: relative;
    width: 100%;
  }
  .vl-window {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    will-change: transform;
  }
  .vl-row {
    box-sizing: border-box;
    overflow: hidden;
  }
  .vl-empty {
    padding: 10px 2px;
  }
</style>
