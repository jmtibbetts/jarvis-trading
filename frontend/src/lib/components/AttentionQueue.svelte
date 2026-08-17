<script lang="ts">
  /**
   * The operations room's lead panel: what needs the operator right now.
   *
   * THE TEST THIS IS BUILT AGAINST. If the operator had twenty seconds
   * before stepping away from the desk, could this tell them whether
   * anything requires intervention? If instead it makes them read thirteen
   * research panels to find out, it has failed.
   *
   * A SUMMARY, NEVER AN AUTHORITY. Every row deep-links to the page that
   * owns the truth — Accounts & Exposure for positions, Signals for the
   * book of setups, Ops for system and integrity. When a row and its
   * authority disagree, the authority is right.
   *
   * `degraded` is rendered as loudly as the queue itself and deliberately
   * ABOVE it. An empty list means "nothing needs you", which is exactly
   * what a silently-failing producer would fabricate — so a queue that
   * could not be fully assembled must never be able to read as all-clear.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { sectionStore, type SectionId } from "../stores/section.svelte";
  import { linkStore } from "../stores/link.svelte";
  import { attentionStore } from "../stores/attention.svelte";

  let { compact = false }: { compact?: boolean } = $props();

  // One shared fetch — see stores/attention.svelte.ts. This panel and the
  // At-Risk panel are two views of the same answer and must never be able
  // to show different counts.
  $effect(() => attentionStore.subscribe());

  const q = $derived(attentionStore.queue);
  let only = $state("");

  const shown = $derived(
    (q?.items ?? []).filter((i) => !only || i.priority === only),
  );

  const tone = (p: string): "critical" | "bad" | "warm" | "neutral" =>
    p === "CRITICAL" ? "critical" : p === "HIGH" ? "bad"
      : p === "MEDIUM" ? "warm" : "neutral";

  const age = (m: number | null) => {
    if (m == null) return "";
    if (m < 60) return `${Math.round(m)}m`;
    if (m < 1440) return `${Math.round(m / 60)}h`;
    return `${Math.round(m / 1440)}d`;
  };

  function go(item: { deep_link: string | null; symbol: string | null }) {
    // Linking the symbol first means the destination lands ON the thing
    // that raised the item rather than on a page the operator then has to
    // search. Same mechanism the rest of the app uses, so a popout on
    // another monitor follows too.
    if (item.symbol) linkStore.link(item.symbol);
    const id = (item.deep_link ?? "").replace("#", "") as SectionId;
    if (id) sectionStore.go(id);
  }

  const PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
</script>

<Panel
  title="Attention Queue"
  dotColor={q?.by_priority?.CRITICAL ? "var(--critical)" : "var(--accent)"}
  status={attentionStore.status}
  meta={q ? `${q.total} open · ${q.producers_run}/${q.producers_total} checks ran` : "—"}
>
  <!--
    ABOVE the queue, always. A degraded producer means a whole category
    cannot appear, and an operator reading an empty list underneath a
    hidden warning would conclude the opposite of the truth.
  -->
  {#if q && !q.complete}
    <div class="degraded">
      <b>{q.degraded.length} check{q.degraded.length === 1 ? "" : "s"} could not run —
        this list is not a complete answer.</b>
      {#each q.degraded as d (d.producer)}
        <div class="drow"><code>{d.producer}</code> {d.means}</div>
      {/each}
    </div>
  {/if}

  {#if q && q.total}
    <div class="filters">
      <button class:on={only === ""} onclick={() => (only = "")}>
        all {q.total}
      </button>
      {#each PRIORITIES as p (p)}
        {#if q.by_priority[p]}
          <button class:on={only === p} class={p.toLowerCase()} onclick={() => (only = p)}>
            {p.toLowerCase()} {q.by_priority[p]}
          </button>
        {/if}
      {/each}
    </div>
  {/if}

  {#if !q}
    <StateNote status={attentionStore.status} noun="attention queue" />
  {:else if !q.total}
    <div class="clear">
      <b>Nothing requires intervention.</b>
      <span>All {q.producers_total} checks ran and none of them raised.</span>
    </div>
  {:else}
    <ul class="items" class:compact>
      {#each shown as i (i.id)}
        <li class={i.priority.toLowerCase()}>
          <button class="row" onclick={() => go(i)} title={i.deep_link ? `Open ${i.deep_link}` : ""}>
            <span class="lead">
              <Pill tone={tone(i.priority)} label={i.priority} />
              <span class="cat">{i.category.replaceAll("_", " ").toLowerCase()}</span>
              {#if i.age_minutes != null}<span class="age">{age(i.age_minutes)}</span>{/if}
            </span>
            <span class="body">
              <b>{i.title}</b>
              <span class="reason">{i.reason}</span>
              {#if i.suggested_action}
                <span class="action">→ {i.suggested_action}</span>
              {/if}
            </span>
            <span class="state">
              {#if i.current_state}<span>{i.current_state}</span>{/if}
              <span class="src">{i.source}</span>
            </span>
          </button>
        </li>
      {/each}
    </ul>
    {#if q.truncated}
      <p class="why">{q.truncated} further items not shown.</p>
    {/if}
    <p class="why">{q.note}</p>
  {/if}
</Panel>

<style>
  .degraded {
    border: 1px solid color-mix(in srgb, var(--bad) 45%, transparent);
    background: color-mix(in srgb, var(--bad) 10%, transparent);
    border-radius: 7px;
    padding: 8px 10px;
    margin-bottom: 10px;
    font-size: 11.5px;
  }
  .degraded b { display: block; margin-bottom: 4px; color: var(--bad); }
  .drow { color: var(--ink-faint); font-size: 10.5px; line-height: 1.45; }
  .drow code { color: var(--ink); font-family: var(--mono); }

  .filters { display: flex; gap: 3px; flex-wrap: wrap; margin-bottom: 9px; }
  .filters button {
    background: none; border: 1px solid transparent; color: var(--ink-faint);
    border-radius: 6px; padding: 2px 9px; font-size: 10.5px; cursor: pointer;
    font-family: var(--mono);
  }
  .filters button.on {
    color: var(--ink); border-color: var(--line-bright); background: var(--surface-raised);
  }
  .filters button.critical.on { color: var(--critical, #ff5c72); }
  .filters button.high.on { color: var(--bad); }

  .clear {
    display: flex; flex-direction: column; gap: 3px;
    padding: 14px 4px; font-size: 13px;
  }
  .clear b { color: var(--good); }
  .clear span { font-size: 11.5px; color: var(--ink-faint); }

  .items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .items li { border-bottom: 1px solid var(--line); }
  .items li:last-child { border-bottom: none; }
  /* A left rule carries the urgency, so priority is legible from across
     the desk without reading the pill. */
  .items li.critical { box-shadow: inset 3px 0 0 var(--critical, #ff5c72); }
  .items li.high { box-shadow: inset 3px 0 0 var(--bad); }
  .items li.medium { box-shadow: inset 3px 0 0 var(--warm, #d9a441); }

  .row {
    display: grid;
    grid-template-columns: 128px minmax(0, 1fr) 150px;
    gap: 10px;
    width: 100%;
    align-items: start;
    background: none;
    border: none;
    text-align: left;
    padding: 9px 10px;
    cursor: pointer;
    color: inherit;
    font: inherit;
  }
  .row:hover { background: var(--surface-raised); }
  @media (max-width: 900px) {
    .row { grid-template-columns: 1fr; gap: 4px; }
  }
  .lead { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .cat { font-size: 9.5px; color: var(--ink-faint); letter-spacing: .04em; }
  .age { font-size: 9.5px; color: var(--ink-faint); font-family: var(--mono); }
  .body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .body b { font-size: 12.5px; }
  .reason { font-size: 11px; color: var(--ink-faint); line-height: 1.45; }
  .action { font-size: 11px; color: var(--accent); }
  .state {
    display: flex; flex-direction: column; gap: 2px; text-align: right;
    font-size: 10.5px; color: var(--ink-faint); font-family: var(--mono);
  }
  .src { opacity: .7; font-size: 9.5px; }
  .items.compact .reason, .items.compact .action { display: none; }
  .why { font-size: 10.5px; color: var(--ink-faint); line-height: 1.5; margin: 9px 0 0; max-width: 84ch; }
</style>
