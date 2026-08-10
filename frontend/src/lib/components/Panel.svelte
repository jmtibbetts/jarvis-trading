<script lang="ts">
  import type { Snippet } from "svelte";

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
</script>

<div class="panel">
  <div class="panel-head">
    <div class="t">
      <span class="dot" style="background:{dotColor}; box-shadow: 0 0 8px {dotColor}"></span>
      {title}
    </div>
    {#if meta}<div class="meta">{meta}</div>{/if}
  </div>
  <div class="panel-body" class:no-pad={noPad}>
    {@render children()}
  </div>
</div>

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
  .meta {
    font-size: 10.5px;
    color: var(--ink-faint);
    font-family: var(--mono);
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
