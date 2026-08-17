<script lang="ts">
  /**
   * P27 — the edge–cost matrix: strategy × product × timeframe × venue.
   *
   * The column that earns this panel is LIMITING. A losing cell loses money
   * two ways, and they call for opposite responses:
   *
   *   EDGE  the setup does not work. Cheaper routing would not save it.
   *   COST  the setup DOES work and the round trip eats it — a routing
   *         result, not a verdict on the setup.
   *
   * A blended P&L reports both as "this lost money" and retires the second
   * one, which throws away a working thesis over a routing decision.
   *
   * Sorted worst-first among cells that have a verdict: a cell whose real
   * edge is being consumed is the most actionable row on the page, and it
   * would sit at the bottom of an alphabetical list forever.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { api, type EdgeCostMatrix } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();
  let m = $state<EdgeCostMatrix | null>(null);
  let days = $state(180);
  let only = $state<string>("");

  $effect(() => {
    const d = days;
    feeds.load("matrix", () => api.edgeCostMatrix(d)).then((r) => (m = r));
  });

  const shown = $derived(
    (m?.cells ?? []).filter((c) => !only || c.limiting === only),
  );

  const r = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "" : ""}${v.toFixed(3)}R`);

  const limitTone = (l: string): "bad" | "warm" | "good" | "neutral" =>
    l === "COST" ? "warm" : l === "EDGE" ? "bad" : l === "NONE" ? "good" : "neutral";

  const LIMIT_MEANS: Record<string, string> = {
    COST: "real gross edge, consumed by the round trip — a routing result",
    EDGE: "no gross edge to protect; cheaper routing would not save it",
    EVIDENCE: "too few closed trades to say anything",
    NONE: "clears the bar",
  };
</script>

<Panel
  title="Edge vs Cost"
  status={feeds.status("matrix")}
  meta={m ? `${m.cells_total} cells · ${m.rows_considered.toLocaleString()} closed trades` : "—"}
>
  <div class="controls">
    <div class="seg">
      {#each [90, 180, 365] as d (d)}
        <button class:on={days === d} onclick={() => (days = d)}>{d}d</button>
      {/each}
    </div>
    {#if m}
      <div class="seg">
        <button class:on={only === ""} onclick={() => (only = "")}>all</button>
        {#each ["COST", "EDGE", "EVIDENCE", "NONE"] as l (l)}
          {#if m.by_limiting_factor[l]}
            <button class:on={only === l} onclick={() => (only = l)} title={LIMIT_MEANS[l]}>
              {l.toLowerCase()} {m.by_limiting_factor[l]}
            </button>
          {/if}
        {/each}
      </div>
    {/if}
  </div>

  {#if !m}
    <StateNote status={feeds.status("matrix")} noun="edge–cost matrix" />
  {:else if !m.cells_total}
    <StateNote
      status={feeds.status("matrix")}
      noun="closed outcomes"
      emptyText="No closed trades in this window — the matrix has nothing to measure yet."
    />
  {:else}
    {#if m.errors.length}
      <div class="errs">
        {#each m.errors as e (e)}<div>{e}</div>{/each}
      </div>
    {/if}

    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>Strategy</th><th>Product</th><th>TF</th><th>Venue</th>
            <th class="n">n</th><th>Evidence</th>
            <th class="n">Gross</th><th class="n">Cost</th><th class="n">Net</th>
            <th class="n">Edge/cost</th><th>Limiting</th>
          </tr>
        </thead>
        <tbody>
          {#each shown as c (c.strategy + c.product + c.timeframe + c.venue)}
            <tr class:dim={c.limiting === "EVIDENCE"}>
              <td class="mono">{c.strategy}</td>
              <td class="mono">{c.product}</td>
              <td class="mono">{c.timeframe}</td>
              <td class="mono">{c.venue}</td>
              <td class="n mono">{c.n.toLocaleString()}</td>
              <td>
                <!-- 7,740 samples reads as overwhelming; 7,740 REPLAYED
                     samples and zero live fills is a different claim. -->
                <span
                  class="ev"
                  class:replay={c.evidence === "REPLAY_ONLY"}
                  title={c.evidence === "REPLAY_ONLY"
                    ? "every row is a replayed fill — perfect execution assumed, systematically optimistic"
                    : `${c.n_live} live / ${c.n_replay} replayed`}
                >
                  {c.evidence === "REPLAY_ONLY" ? "replay" : c.evidence === "MIXED" ? `${c.n_live} live` : "live"}
                </span>
              </td>
              <td class="n mono" class:pos={(c.gross_r ?? 0) > 0} class:neg={(c.gross_r ?? 0) < 0}>
                {r(c.gross_r)}
              </td>
              <td class="n mono" title={c.cost_r_p90 != null ? `p90 ${c.cost_r_p90.toFixed(3)}R · ${c.cost_basis}` : ""}>
                {r(c.cost_r_median)}
              </td>
              <td class="n mono" class:pos={(c.net_r ?? 0) > 0} class:neg={(c.net_r ?? 0) < 0}>
                {r(c.net_r)}
              </td>
              <td class="n mono dim">{c.edge_cost_ratio == null ? "—" : `${c.edge_cost_ratio}×`}</td>
              <td>
                <Pill tone={limitTone(c.limiting)} label={c.limiting} />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    {#if m.truncated}
      <p class="why">{m.truncated} further cells not shown.</p>
    {/if}

    <!--
      The venue comparison. It takes TWO arms to answer "bad signal or bad
      venue", and with one on file compare_venues still returns a lesson
      whose text contradicts the COST cells above — so it is reported as
      not-yet-answerable rather than as a finding.
    -->
    <div class="venue">
      <div class="vhead">
        <b>Bad signal, or bad venue?</b>
        {#if m.venues.comparable}
          <Pill tone={m.venues.lesson === "BAD_VENUE" ? "warm" : "neutral"} label={m.venues.lesson} />
        {:else}
          <Pill tone="neutral" label="NOT COMPARABLE" />
        {/if}
      </div>
      <p>
        {m.venues.comparable ? m.venues.detail : m.venues.not_comparable_reason}
      </p>
    </div>

    <p class="why">{m.note}</p>
  {/if}
</Panel>

<style>
  .controls { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
  .seg { display: flex; gap: 2px; }
  .seg button {
    background: none; border: 1px solid transparent; color: var(--ink-faint);
    border-radius: 6px; padding: 2px 9px; font-size: 11px; cursor: pointer;
    font-family: var(--mono);
  }
  .seg button.on {
    color: var(--accent); border-color: var(--line-bright); background: var(--surface-raised);
  }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th {
    text-align: left; padding: 5px 8px; color: var(--ink-faint);
    font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
    border-bottom: 1px solid var(--line); white-space: nowrap;
  }
  td { padding: 5px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  tr.dim td { opacity: 0.6; }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  .mono { font-family: var(--mono); font-size: 11.5px; }
  .dim { color: var(--ink-faint); }
  .pos { color: var(--good); }
  .neg { color: var(--bad); }
  .ev { font-size: 10px; font-family: var(--mono); color: var(--ink-faint); }
  .ev.replay { color: var(--warm, #d9a441); }
  .errs {
    border: 1px solid color-mix(in srgb, var(--bad) 35%, transparent);
    background: color-mix(in srgb, var(--bad) 8%, transparent);
    border-radius: 6px; padding: 7px 10px; margin-bottom: 10px;
    font-size: 11.5px; color: var(--muted);
  }
  .venue {
    margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line);
  }
  .vhead { display: flex; align-items: center; gap: 9px; font-size: 12.5px; }
  .venue p { margin: 5px 0 0; font-size: 11.5px; color: var(--ink-faint); line-height: 1.5; max-width: 86ch; }
  .why { font-size: 11px; color: var(--ink-faint); line-height: 1.55; margin: 10px 0 0; max-width: 86ch; }
</style>
