<script lang="ts">
  // Phase 0: structural shell only, proving the design system renders
  // correctly end-to-end. Real data wiring (equity curve, threat map,
  // live signal/position feeds over the WS channel) is Phase 1.
  import Panel from "../components/Panel.svelte";
  import KpiTile from "../components/KpiTile.svelte";

  const kpis = [
    { label: "Win Rate", value: "—", period: "30d" },
    { label: "Sharpe", value: "—" },
    { label: "Max Drawdown", value: "—" },
    { label: "Profit Factor", value: "—" },
    { label: "Open Positions", value: "—" },
    { label: "Avg Hold", value: "—" },
  ];
</script>

<div class="page-head">
  <div>
    <h1>Command Center</h1>
    <div class="sub">Phase 0 shell — live data wiring lands in Phase 1</div>
  </div>
</div>

<div class="grid">
  <div class="kpis">
    {#each kpis as kpi (kpi.label)}
      <KpiTile label={kpi.label} value={kpi.value} period={kpi.period} />
    {/each}
  </div>

  <div class="span-8">
    <Panel title="Equity Curve — Live vs Paper" meta="pending Phase 1">
      <div class="placeholder">Chart lands here in Phase 1</div>
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Threat Intelligence" dotColor="var(--critical)" meta="pending Phase 1" noPad>
      <div class="placeholder">World map lands here in Phase 1</div>
    </Panel>
  </div>

  <div class="span-5">
    <Panel title="Active Signals" meta="pending Phase 1">
      <div class="placeholder">Signal feed lands here in Phase 1</div>
    </Panel>
  </div>

  <div class="span-4">
    <Panel title="Open Positions" meta="pending Phase 1">
      <div class="placeholder">Positions table lands here in Phase 1</div>
    </Panel>
  </div>

  <div class="span-3">
    <Panel title="Job Health" meta="pending Phase 1">
      <div class="placeholder">Job status lands here in Phase 1</div>
    </Panel>
  </div>
</div>

<style>
  .page-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 16px;
  }
  .page-head h1 {
    font-size: 19px;
    margin: 0;
    font-weight: 650;
    letter-spacing: -0.01em;
  }
  .sub {
    font-size: 12px;
    color: var(--ink-faint);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
  }
  .kpis {
    grid-column: span 12;
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 14px;
  }
  .span-8 {
    grid-column: span 8;
  }
  .span-5 {
    grid-column: span 5;
  }
  .span-4 {
    grid-column: span 4;
  }
  .span-3 {
    grid-column: span 3;
  }
  .placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 140px;
    color: var(--ink-faint);
    font-size: 12px;
    border: 1px dashed var(--line);
    border-radius: 8px;
  }
  @media (max-width: 1180px) {
    .kpis {
      grid-template-columns: repeat(3, 1fr);
    }
    .span-8,
    .span-5,
    .span-4,
    .span-3 {
      grid-column: span 12;
    }
  }
</style>
