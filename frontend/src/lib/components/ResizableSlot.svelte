<script lang="ts">
  /**
   * A grid cell the operator can resize, persisted per slot.
   *
   * The 12-column grid already decides layout; this lets the operator move
   * a panel's share of it without a redesign. Width snaps to COLUMNS rather
   * than to pixels — a panel at 7.3 columns would break the alignment every
   * other row depends on, and a dashboard whose columns do not line up is
   * harder to read than one with the wrong proportions.
   *
   * Height is optional and off by default: most panels size to their
   * content, and forcing a height on one that does not need it creates dead
   * space. A slot that opts in gets a bottom handle too.
   *
   * DOUBLE-CLICK A HANDLE TO RESET. Discoverable enough via the tooltip,
   * and it means a bad drag is never something the operator has to fix by
   * clearing storage.
   *
   * Deliberately not drag-to-reorder. Reordering panels changes what the
   * page MEANS — the Command Center's rows are ranked by urgency, and a
   * layout where the attention queue can end up below the market movers is
   * not the page that was designed.
   */
  import type { Snippet } from "svelte";

  let {
    id,
    span = 6,
    minSpan = 3,
    maxSpan = 12,
    height = 0,
    minHeight = 180,
    children,
  }: {
    /** Persist key. Stable across sessions; changing it resets the slot. */
    id: string;
    span?: number;
    minSpan?: number;
    maxSpan?: number;
    /** 0 = size to content, which is the right default for most panels. */
    height?: number;
    minHeight?: number;
    children: Snippet;
  } = $props();

  const keyFor = (slot: string) => `jarvis.layout.${slot}`;

  function restore(): { span: number; height: number } {
    try {
      const raw = JSON.parse(localStorage.getItem(keyFor(id)) || "null");
      if (raw && typeof raw === "object") {
        return {
          // Clamped on READ, not just on write: the defaults can change in
          // a later build, and a stored 4 against a new minSpan of 6 would
          // otherwise pin the slot below its own minimum forever.
          span: Math.min(maxSpan, Math.max(minSpan, Number(raw.span) || span)),
          height: Math.max(0, Number(raw.height) || height),
        };
      }
    } catch {
      /* corrupt layout state must not take the page down */
    }
    return { span, height };
  }

  const initial = restore();
  let curSpan = $state(initial.span);
  let curHeight = $state(initial.height);
  let dragging = $state<"" | "w" | "h">("");

  function persist() {
    try {
      localStorage.setItem(keyFor(id), JSON.stringify({ span: curSpan, height: curHeight }));
    } catch {
      /* best effort */
    }
  }

  let el: HTMLDivElement;

  function startWidth(e: PointerEvent) {
    e.preventDefault();
    // Column width is derived from THIS slot's own geometry rather than
    // read off the grid: the slot knows its width and its span, and the
    // ratio is the same number without needing a reference to the parent.
    const rect = el.getBoundingClientRect();
    const perCol = rect.width / curSpan;
    const startX = e.clientX;
    const startSpan = curSpan;
    dragging = "w";
    const move = (ev: PointerEvent) => {
      const delta = Math.round((ev.clientX - startX) / perCol);
      curSpan = Math.min(maxSpan, Math.max(minSpan, startSpan + delta));
    };
    const up = () => {
      dragging = "";
      persist();
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function startHeight(e: PointerEvent) {
    e.preventDefault();
    const startY = e.clientY;
    const startH = curHeight || el.getBoundingClientRect().height;
    dragging = "h";
    const move = (ev: PointerEvent) => {
      curHeight = Math.max(minHeight, Math.round(startH + (ev.clientY - startY)));
    };
    const up = () => {
      dragging = "";
      persist();
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function reset() {
    curSpan = span;
    curHeight = height;
    persist();
  }

  // Keyboard equivalent, so the layout is not mouse-only.
  function onKey(e: KeyboardEvent) {
    if (e.key === "ArrowLeft") curSpan = Math.max(minSpan, curSpan - 1);
    else if (e.key === "ArrowRight") curSpan = Math.min(maxSpan, curSpan + 1);
    else return;
    e.preventDefault();
    persist();
  }
</script>

<div
  class="slot"
  class:dragging={dragging !== ""}
  bind:this={el}
  style="grid-column: span {curSpan};{curHeight ? ` --slot-h:${curHeight}px;` : ''}"
  class:fixed-h={curHeight > 0}
>
  {@render children()}

  <button
    type="button"
    class="grip w"
    aria-label="Resize width — currently {curSpan} of {maxSpan} columns. Arrow keys adjust."
    title="Drag to resize ({curSpan}/{maxSpan} columns) · double-click to reset · ← → with focus"
    onpointerdown={startWidth}
    ondblclick={reset}
    onkeydown={onKey}
  ></button>

  {#if height > 0}
    <button
      type="button"
      class="grip h"
      aria-label="Resize height"
      title="Drag to resize height · double-click to reset"
      onpointerdown={startHeight}
      ondblclick={reset}
    ></button>
  {/if}
</div>

<style>
  .slot {
    position: relative;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .slot.fixed-h {
    height: var(--slot-h);
  }
  .slot.fixed-h > :global(.panel) {
    min-height: 0;
    overflow: auto;
  }

  /* Handles are invisible until the slot is hovered — the rail, the panels
     and the numbers are what the page is for, and a permanent grid of drag
     bars would compete with all of them. */
  .grip {
    position: absolute;
    padding: 0;
    border: none;
    background: none;
    opacity: 0;
    transition: opacity 0.12s;
    z-index: 3;
  }
  .grip.w {
    top: 12px;
    bottom: 12px;
    right: -8px;
    width: 10px;
    cursor: col-resize;
    border-radius: 4px;
  }
  .grip.h {
    left: 12px;
    right: 12px;
    bottom: -8px;
    height: 10px;
    cursor: row-resize;
    border-radius: 4px;
  }
  .slot:hover .grip,
  .grip:focus-visible {
    opacity: 1;
    background: var(--line);
  }
  .grip:hover,
  .grip:focus-visible {
    background: var(--accent);
    opacity: 1;
  }
  .slot.dragging .grip {
    opacity: 1;
    background: var(--accent);
  }
  /* A drag that leaves the handle must not select half the page. */
  .slot.dragging {
    user-select: none;
  }
  @media (max-width: 900px) {
    /* One column on small screens: resizing a stack does nothing useful. */
    .grip {
      display: none;
    }
  }
</style>
