<script lang="ts">
  /**
   * The Shadow Lab — the control arm, not a third book.
   *
   * Auto Sim used to be presented as another account alongside Live and
   * Paper, which invited the wrong question: "how is Auto Sim doing?"
   * Its actual job is to answer a much better one:
   *
   *     DID THE AI'S DECISION ACTUALLY ADD VALUE?
   *
   * The Agent book is what JARVIS chose. The Shadow book follows a
   * standardised policy over the SAME theses using the SAME execution
   * arithmetic. The difference between them is the only clean measure of
   * whether the entry review, the sizing and the AI management are
   * earning their place.
   *
   * THE SAMPLE RULE, and the reason this page exists at all: two arms
   * acting on one market event is ONE observation with two policy results.
   * Counting it as two makes a market that moved once vote twice, shrinks
   * every confidence interval by root two for free, and does so in the
   * flattering direction where nobody looks. `market_samples` counts
   * DISTINCT THESES; `arm_results` counts rows. They are shown side by
   * side so they can never be confused again.
   */
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import StateNote from "../components/StateNote.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import { api, type AutoSimSummary } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();
  let autosim = $state<AutoSimSummary | null>(null);

  $effect(() => {
    feeds.load("autosim", () => api.autoSimSummary()).then((a) => (autosim = a));
  });

  const usd = (v: number | null | undefined) =>
    v == null ? "—" : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
</script>

<div class="page">
  <header class="page-head">
    <h1>Shadow Lab</h1>
    <p>
      The control arm. Same theses, same execution arithmetic, a
      standardised policy — so the difference between it and the Agent book
      measures the AI's decisions rather than the market's direction.
    </p>
  </header>

  <div class="kpis">
    <KpiTile label="Control equity" value={autosim ? usd(autosim.summary?.equity) : "—"} />
    <KpiTile label="Open positions" value={autosim ? String(autosim.positions?.length ?? 0) : "—"} />
    <KpiTile label="Realized P&L" value={autosim ? usd(autosim.summary?.realized_pnl) : "—"} />
    <KpiTile label="Unrealized" value={autosim ? usd(autosim.summary?.unrealized_pnl) : "—"} />
  </div>

  <Panel title="What this book is for" meta="control arm">
    <div class="explain">
      <div class="row">
        <b>Agent</b>
        <span>What JARVIS actually chose — its entry review, its sizing, its
        AI position management.</span>
      </div>
      <div class="row">
        <b>Shadow</b>
        <span>What would have happened if every otherwise-valid signal were
        followed under standardised rules.</span>
      </div>
      <div class="row">
        <b>Delta</b>
        <span><code>delta_net_r = agent_net_r − shadow_net_r</code>, per
        shared thesis. 500 theses give 500 PAIRED differences, which is a
        stronger analysis than pretending to 1,000 independent trades —
        pairing removes the market's own variance and leaves policy value.</span>
      </div>
    </div>
    <p class="rule">
      The Shadow book may have a different POLICY. It must never have
      different instrument arithmetic, fee maths, fill semantics or P&L
      units — those are what make the comparison meaningful rather than
      two unrelated books drifting apart.
    </p>
  </Panel>

  <Panel title="Edge decomposition" meta="selection · execution · management">
    <div class="pending">
      <p>
        Paired outcomes are being collected. This reports
        <b>selection</b> (did it take the right theses?),
        <b>execution</b> (given the same thesis, did it fill better?) and
        <b>management</b> (given the same entry, did it exit better?) —
        separately, because a single blended P&L answers none of them.
      </p>
      <p class="dim">
        Positive rate is reported beside the mean: one large win among
        nineteen small losses has a positive mean and is not skill. The
        verdict is expressible in both directions — AGENT_SUBTRACTS_VALUE
        is a first-class result, or the experiment is decoration.
      </p>
      <p class="dim">
        Requires theses evaluated by BOTH arms. A thesis one arm never saw
        says nothing about relative skill, and including it would measure
        coverage while claiming to measure edge.
      </p>
    </div>
  </Panel>

  {#if autosim?.positions?.length}
    <Panel title="Control positions" status={feeds.status("autosim")}
           meta={`${autosim.positions.length} open`}>
      <div class="tablewrap">
        <table>
          <thead>
            <tr><th>Symbol</th><th>Side</th><th class="n">Qty</th>
                <th class="n">Entry</th><th class="n">Current</th>
                <th class="n">P&L</th></tr>
          </thead>
          <tbody>
            {#each autosim.positions as p (p.id ?? p.symbol)}
              <tr>
                <td class="mono">{p.symbol}</td>
                <td><Pill tone={(p.direction ?? "").toLowerCase().includes("short") ? "bad" : "good"}
                      label={p.direction ?? "?"} /></td>
                <td class="n">{p.qty}</td>
                <td class="n">{p.entry_price}</td>
                <td class="n">{p.current_price}</td>
                <td class="n" class:pl-up={(p.unrealized_pnl ?? 0) > 0}
                    class:pl-down={(p.unrealized_pnl ?? 0) < 0}>
                  {usd(p.unrealized_pnl)}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </Panel>
  {:else}
    <Panel title="Control positions" status={feeds.status("autosim")}>
      <StateNote status={feeds.status("autosim")} noun="Control book"
                 emptyText="No open control positions." />
    </Panel>
  {/if}
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 14px; }
  .page-head h1 { margin: 0; font-size: 19px; letter-spacing: -.01em; }
  .page-head p { margin: 4px 0 0; font-size: 12.5px; color: var(--muted); max-width: 74ch; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
  .explain { display: flex; flex-direction: column; gap: 9px; }
  .row { display: grid; grid-template-columns: 90px 1fr; gap: 12px; font-size: 12.5px; }
  .row b { color: var(--text, #e6e9ef); }
  .row span { color: var(--muted); line-height: 1.55; }
  .row code {
    font-family: ui-monospace, Consolas, monospace; font-size: 11.5px;
    background: var(--bg-elev, #11151c); padding: 1px 5px; border-radius: 3px;
  }
  .rule {
    margin: 12px 0 0; padding-top: 10px; font-size: 12px; color: var(--muted);
    border-top: 1px solid var(--border, #222a35); line-height: 1.55;
  }
  .pending p { margin: 0 0 8px; font-size: 12.5px; line-height: 1.55; }
  .pending .dim { color: var(--muted); }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; padding: 6px 9px; color: var(--muted); font-size: 10.5px;
       text-transform: uppercase; letter-spacing: .07em;
       border-bottom: 1px solid var(--border, #222a35); }
  td { padding: 6px 9px; border-bottom: 1px solid var(--border, #222a35); }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
</style>
