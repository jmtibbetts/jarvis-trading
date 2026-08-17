<script lang="ts">
  /**
   * The POSITION_RISK slice of the attention queue, on its own.
   *
   * NOT a second measurement. It reads the same shared queue the Attention
   * Queue panel reads, so the two can never show different counts — a page
   * whose panels disagree about how many positions are in trouble is worse
   * than a page with one fewer panel.
   *
   * It exists separately because position risk is the one category an
   * operator scans for by reflex, and making them filter a mixed list to
   * find it costs the seconds this page is supposed to save.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { attentionStore } from "../stores/attention.svelte";
  import { sectionStore } from "../stores/section.svelte";
  import { linkStore } from "../stores/link.svelte";

  $effect(() => attentionStore.subscribe());

  const items = $derived(attentionStore.inCategory("POSITION_RISK"));
  const q = $derived(attentionStore.queue);

  // Whether the position-risk producers actually RAN. "No positions at
  // risk" and "we could not check" must not render the same, and this is
  // the panel where that difference is most expensive.
  const blind = $derived(
    (q?.degraded ?? []).filter(
      (d) => d.producer === "positions_near_stop" || d.producer === "concentration",
    ),
  );

  function open(symbol: string | null) {
    if (symbol) linkStore.link(symbol);
    sectionStore.go("positions");
  }
</script>

<Panel
  title="At-Risk Positions"
  dotColor={items.length ? "var(--bad)" : "var(--accent)"}
  status={attentionStore.status}
  meta={items.length ? `${items.length} need a decision` : "buffer + concentration"}
>
  {#if blind.length}
    <div class="blind">
      <b>Position risk could not be fully checked.</b>
      {#each blind as d (d.producer)}
        <div><code>{d.producer}</code> — {d.error}</div>
      {/each}
      <span>An empty list below does not mean you are safe.</span>
    </div>
  {/if}

  {#if !q}
    <StateNote status={attentionStore.status} noun="position risk" />
  {:else if !items.length}
    <p class="ok">
      No position is inside its stop buffer and no book is over its
      concentration cap.
    </p>
  {:else}
    <ul>
      {#each items as i (i.id)}
        <li>
          <button onclick={() => open(i.symbol)}>
            <Pill tone={i.priority === "CRITICAL" ? "critical" : "bad"} label={i.priority} />
            <span class="main">
              <b>{i.title}</b>
              <span class="why">{i.reason}</span>
            </span>
            <span class="state">{i.current_state ?? ""}</span>
          </button>
        </li>
      {/each}
    </ul>
    <p class="note">
      Measured against the stop AS PLACED, never the trailed stop — by
      mid-trade the live stop often sits at breakeven, and a buffer measured
      from there describes the trail rather than the risk taken.
    </p>
  {/if}
</Panel>

<style>
  .blind {
    border: 1px solid color-mix(in srgb, var(--bad) 45%, transparent);
    background: color-mix(in srgb, var(--bad) 10%, transparent);
    border-radius: 7px; padding: 8px 10px; margin-bottom: 9px; font-size: 11px;
  }
  .blind b { display: block; color: var(--bad); margin-bottom: 3px; }
  .blind div { color: var(--ink-faint); font-size: 10.5px; }
  .blind code { font-family: var(--mono); color: var(--ink); }
  .blind span { display: block; margin-top: 4px; color: var(--bad); font-size: 10.5px; }
  .ok { font-size: 12px; color: var(--ink-faint); line-height: 1.5; margin: 4px 0 0; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { border-bottom: 1px solid var(--line); }
  li:last-child { border-bottom: none; }
  li button {
    display: grid; grid-template-columns: 74px minmax(0, 1fr) auto; gap: 9px;
    width: 100%; align-items: start; text-align: left;
    background: none; border: none; color: inherit; font: inherit;
    padding: 8px 6px; cursor: pointer;
  }
  li button:hover { background: var(--surface-raised); }
  .main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .main b { font-size: 12px; }
  .why { font-size: 10.5px; color: var(--ink-faint); line-height: 1.4; }
  .state { font-size: 10px; color: var(--ink-faint); font-family: var(--mono); white-space: nowrap; }
  .note { font-size: 10.5px; color: var(--ink-faint); line-height: 1.5; margin: 9px 0 0; }
</style>
