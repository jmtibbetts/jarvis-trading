<script lang="ts">
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import Pill from "../components/Pill.svelte";
  import StateNote from "../components/StateNote.svelte";
  import { api, type MorningBrief } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();

  let brief = $state<MorningBrief | null>(null);
  let windowHours = $state(24);
  let loading = $state(false);

  async function load() {
    loading = true;
    brief = await feeds.load("brief", () => api.morningBrief(windowHours));
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
      <Panel title="Market Pulse" meta="last close vs prior — the desk's core instruments">
        <div class="pulse">
          {#each brief.market_pulse as p}
            <div class="pt">
              <span class="sym">{p.symbol.replace("/USD", "").replace("=X", "").replace("=F", "")}</span>
              <span class:pos={p.change_pct > 0} class:neg={p.change_pct < 0}>
                {p.change_pct > 0 ? "+" : ""}{p.change_pct}%
              </span>
            </div>
          {:else}
            <p class="muted">no pulse data cached yet</p>
          {/each}
        </div>
      </Panel>

      <Panel title="Analogs" meta="what followed the most similar past moments — history, not prediction">
        {#each brief.analog_reads as a}
          <div class="analog-row">
            <b>{a.symbol}</b>
            <span class="muted">n={a.n_analogs} of {a.candidates_searched.toLocaleString()} searched</span>
            <span>4h: <b class:pos={(a.fwd_4h_median_pct ?? 0) > 0} class:neg={(a.fwd_4h_median_pct ?? 0) < 0}>{a.fwd_4h_median_pct}%</b> ({a.fwd_4h_up_rate}% up)</span>
            <span>1d: <b class:pos={(a.fwd_1d_median_pct ?? 0) > 0} class:neg={(a.fwd_1d_median_pct ?? 0) < 0}>{a.fwd_1d_median_pct}%</b> ({a.fwd_1d_up_rate}% up)</span>
          </div>
        {:else}
          <p class="muted">corpus too thin for analogs yet</p>
        {/each}
      </Panel>

      <Panel title="Perp State" meta="OKX funding / OI / account skew — the crowd's current lean">
        <table>
          <thead><tr><th>coin</th><th>funding 8h</th><th>OI</th><th>L/S accts</th></tr></thead>
          <tbody>
            {#each brief.derivatives_now as d}
              <tr>
                <td><b>{d.symbol}</b></td>
                <td class:neg={(d.funding_rate_8h ?? 0) < 0}>{d.funding_rate_8h == null ? "—" : (d.funding_rate_8h * 100).toFixed(4) + "%"}</td>
                <td>{d.oi_usd == null ? "—" : "$" + (d.oi_usd / 1e9).toFixed(2) + "B"}</td>
                <td>{d.long_short_ratio?.toFixed(2) ?? "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </Panel>

      <Panel title="Positioning" meta="COT spec percentile (3y) + curve, every tracked market">
        <table>
          <thead><tr><th>market</th><th>pctile</th><th>net</th><th>curve</th><th>roll/yr</th></tr></thead>
          <tbody>
            {#each brief.positioning as p}
              <tr>
                <td class="cap">{p.instrument}</td>
                <td class:pos={(p.spec_pctile_3y ?? 50) >= 90} class:neg={(p.spec_pctile_3y ?? 50) <= 10}>
                  {p.spec_pctile_3y ?? "—"}
                </td>
                <td>{p.spec_net?.toLocaleString() ?? "—"}</td>
                <td>{p.curve ?? "—"}</td>
                <td>{p.roll_pct == null ? "—" : p.roll_pct + "%"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </Panel>

      <Panel title="Threat → Price Pressure" meta="rule-mapped hypotheses from active threats — the map stays in Intelligence">
        {#if brief.threat_transmission.length}
          <ul class="tw">
            {#each brief.threat_transmission as t}
              <li>
                <span class="sym">{t.instrument}</span>
                <span class="press press-{t.pressure}">{t.pressure}</span>
                <span class="muted">{t.rule} [{t.severity}]</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="muted">no active threats map to tracked instruments</p>
        {/if}
      </Panel>

      <Panel title="Incubator" meta="{brief.incubator.counts?.incubating ?? 0} coins building history toward the {brief.incubator.graduation_bars_1h}-bar graduation">
        {#if brief.incubator.incubating?.length}
          <ul class="inc">
            {#each brief.incubator.incubating as c}
              <li>
                <span class="sym">{c.symbol}</span>
                <span class="muted">{c.age_days}d old · {c.bars_1h} bars</span>
                <span class="prog"><span class="fill" style="width:{c.progress_pct}%"></span></span>
                <span>{c.progress_pct}%</span>
              </li>
            {/each}
          </ul>
          {#if brief.incubator.counts?.coverage_gaps}
            <p class="note">{brief.incubator.counts.coverage_gaps} older coins have thin history (backfill gaps, not new listings)</p>
          {/if}
        {:else}
          <p class="muted">nothing incubating — every tracked coin has graduated</p>
        {/if}
      </Panel>

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

      <Panel title="Alerts" meta="raised in window, by severity">
        <div class="kinds">
          {#each Object.entries(brief.alerts) as [sev, n]}
            <span class="kind" class:crit={sev === "CRITICAL"}>{sev} <b>{n}</b></span>
          {:else}
            <p class="muted">quiet window</p>
          {/each}
        </div>
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
    <!-- Was a guess dressed as a diagnosis ("is the API up?"). The status
         knows whether the server answered, what it said, and how many times
         in a row it has failed. -->
    <StateNote status={feeds.status("brief")} noun="morning brief" />
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
  .kind.crit { border-color: var(--bad); color: var(--bad); }
  .pulse {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
    gap: 8px;
  }
  .pt {
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }
  .analog-row {
    display: flex;
    gap: 12px;
    align-items: baseline;
    flex-wrap: wrap;
    padding: 6px 0;
    border-bottom: 1px solid var(--line);
    font-size: 13px;
  }
  .tw, .inc {
    list-style: none;
    margin: 0;
    padding: 0;
    font-size: 13px;
  }
  .tw li, .inc li {
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid var(--line);
  }
  .press {
    font-size: 11px;
    padding: 1px 8px;
    border-radius: 6px;
    border: 1px solid var(--line);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .press-up { color: var(--good); border-color: var(--good); }
  .press-down { color: var(--bad); border-color: var(--bad); }
  .press-vol { color: var(--warn); border-color: var(--warn); }
  .prog {
    flex: 1;
    max-width: 120px;
    height: 5px;
    border-radius: 3px;
    background: var(--surface-raised);
    border: 1px solid var(--line);
    overflow: hidden;
  }
  .prog .fill {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .cap { text-transform: capitalize; }
</style>
