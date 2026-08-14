<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import Pill from "../components/Pill.svelte";
  import { api, type MorningBrief } from "../api";

  let brief = $state<MorningBrief | null>(null);
  let windowHours = $state(24);
  let loading = $state(false);

  async function load() {
    loading = true;
    brief = await api.morningBrief(windowHours).catch(() => null);
    loading = false;
  }

  $effect(() => {
    windowHours;
    load();
  });

  const fmt = (v: number | null | undefined, digits = 2) =>
    v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: digits });

  // Arm rows in a fixed, meaningful order — TRADE first because it's the
  // arm that decides capital.
  const armOrder = ["TRADE", "TENTATIVE", "NO_TRADE"];
  let arms = $derived(
    brief
      ? armOrder
          .filter((a) => brief!.gate_experiment.arms[a])
          .map((a) => ({
            name: a,
            ...brief!.gate_experiment.arms[a],
            fresh: brief!.gate_experiment.resolved_in_window[a] ?? null,
          }))
      : [],
  );
</script>

<div class="brief">
  <div class="head">
    <div>
      <h1>Morning Brief</h1>
      <p class="sub">
        what moved in the last
        <select bind:value={windowHours}>
          <option value={12}>12h</option>
          <option value={24}>24h</option>
          <option value={48}>48h</option>
          <option value={168}>7d</option>
        </select>
        — assembled from the desk's own measurements
      </p>
    </div>
    {#if brief}
      <div class="releases">
        {#each brief.releases_today as r}
          <Pill tone="info">{r}</Pill>
        {:else}
          <Pill tone="neutral">no scheduled releases today</Pill>
        {/each}
      </div>
    {/if}
  </div>

  {#if loading && !brief}
    <p class="muted">assembling…</p>
  {:else if brief}
    <div class="grid">
      <Panel title="Gate Experiment" meta="arms judged by the same resolver — compare within timeframe, mind effective n">
        <table>
          <thead>
            <tr><th>arm</th><th>candidates</th><th>resolved</th><th>resolved in window</th><th>win</th><th>avg P&L</th></tr>
          </thead>
          <tbody>
            {#each arms as a}
              <tr>
                <td class="arm-{a.name.toLowerCase()}">{a.name}</td>
                <td>{a.candidates}</td>
                <td>{a.resolved}</td>
                <td>{a.fresh?.resolved ?? 0}</td>
                <td>{a.fresh ? `${a.fresh.win_rate}%` : "—"}</td>
                <td class:neg={(a.fresh?.avg_pnl_pct ?? 0) < 0}
                    class:pos={(a.fresh?.avg_pnl_pct ?? 0) > 0}>
                  {a.fresh ? `${a.fresh.avg_pnl_pct}%` : "—"}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if brief.gate_experiment.new_trade_picks.length}
          <p class="note">new TRADE picks: {brief.gate_experiment.new_trade_picks.join(", ")}</p>
        {/if}
      </Panel>

      <Panel title="Book" meta="paper — the account the desk actually runs">
        <div class="tiles">
          <KpiTile label="open positions" value={String(brief.book.open_positions)} />
          <KpiTile label="unrealized" value={fmt(brief.book.open_unrealized_pnl)} />
          <KpiTile label="closed ({windowHours}h)" value={String(brief.book.closed_in_window)} />
          <KpiTile label="realized ({windowHours}h)" value={fmt(brief.book.realized_pnl_window)} />
        </div>
      </Panel>

      <Panel title="Learning Corpus" meta="clock snapshots + independent-horizon labels">
        <div class="tiles">
          <KpiTile label="snapshots taken" value={String(brief.corpus.snapshots_taken)} />
          <KpiTile label="labels due today" value={String(brief.corpus.labels_due_today)} />
          <KpiTile
            label="ablation coverage"
            value={`${brief.corpus.ablation_coverage.with_context}/${brief.corpus.ablation_coverage.resolved_total}`}
          />
        </div>
        {#if brief.corpus.labels_moved.length}
          <p class="note">
            labels moved:
            {brief.corpus.labels_moved
              .map((l) => `${l.horizon_min >= 1440 ? "1d" : l.horizon_min >= 240 ? "4h" : "1h"} ${l.status} ×${l.n}`)
              .join(" · ")}
          </p>
        {/if}
      </Panel>

      <Panel title="Positioning Extremes" meta="3-year percentile tails, from released COT only">
        {#if brief.positioning_extremes.length}
          <ul class="extremes">
            {#each brief.positioning_extremes as e}
              <li>
                <span class="sym">{e.instrument}</span>
                <span class="sector">({e.sector})</span>
                <span class:pos={e.spec_pctile_3y >= 90} class:neg={e.spec_pctile_3y <= 10}>
                  {e.spec_pctile_3y} pctile
                </span>
                <span class="muted">spec net {fmt(e.spec_net, 0)}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="muted">nothing at the tails</p>
        {/if}
      </Panel>

      <Panel title="Data Platform" meta="events ingested in window, by kind">
        <div class="kinds">
          {#each Object.entries(brief.platform.events_by_kind) as [kind, n]}
            <span class="kind">{kind} <b>{n.toLocaleString()}</b></span>
          {/each}
        </div>
        <p class="note">{brief.book.new_signals.toLocaleString()} signal writes in window</p>
      </Panel>
    </div>
  {:else}
    <p class="muted">brief unavailable — is the API up?</p>
  {/if}
</div>

<style>
  .brief {
    padding: 20px 24px;
    overflow-y: auto;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 18px;
  }
  h1 {
    font-size: 20px;
    margin: 0 0 4px;
  }
  .sub {
    color: var(--ink-faint);
    font-size: 12.5px;
    margin: 0;
  }
  .sub select {
    background: var(--surface-raised);
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 1px 4px;
  }
  .releases {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 14px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;
    font-variant-numeric: tabular-nums;
  }
  th {
    text-align: left;
    color: var(--ink-faint);
    font-weight: 500;
    padding: 4px 8px;
    border-bottom: 1px solid var(--line);
  }
  td {
    padding: 5px 8px;
    border-bottom: 1px solid var(--line);
  }
  .arm-trade { color: var(--good); font-weight: 600; }
  .arm-no_trade { color: var(--ink-faint); }
  .arm-tentative { color: var(--warn); }
  .pos { color: var(--good); }
  .neg { color: var(--bad); }
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 10px;
  }
  .note {
    margin: 10px 0 0;
    font-size: 12px;
    color: var(--ink-faint);
  }
  .extremes {
    list-style: none;
    margin: 0;
    padding: 0;
    font-size: 13px;
  }
  .extremes li {
    display: flex;
    gap: 8px;
    align-items: baseline;
    padding: 4px 0;
    border-bottom: 1px solid var(--line);
  }
  .sym { font-weight: 600; text-transform: capitalize; }
  .sector, .muted { color: var(--ink-faint); font-size: 12px; }
  .kinds {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .kind {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 3px 9px;
    font-size: 12px;
    color: var(--ink-faint);
  }
  .kind b { color: var(--ink); }
</style>
