<script lang="ts">
  import type { Snippet } from "svelte";

  let {
    label,
    tone = "neutral",
    children,
  }: {
    label?: string;
    tone?: "good" | "bad" | "warm" | "critical" | "neutral" | "info";
    children?: Snippet;
  } = $props();
</script>

<!--
  `children` is accepted as well as `label` because four call sites were
  already written that way — <Pill tone="neutral">{symbol}</Pill> — and with
  no snippet to render, the content was silently dropped and the pill
  rendered as an empty 12px box. Typecheck had been flagging it the whole
  time; it was being counted as a known-error baseline rather than read.
-->
<span class="pill {tone}">{#if children}{@render children()}{:else}{label}{/if}</span>

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
  /* `neutral` is accent-blue rather than grey — misnamed, but renaming it
     would touch every call site. `info` is the name two of them reached for
     wanting exactly this, so it is an explicit alias rather than a third
     shade nobody asked for. Without it those pills matched no rule at all
     and rendered unstyled. */
  .neutral,
  .info {
    background: rgba(124, 154, 255, 0.12);
    color: var(--accent);
  }
</style>
