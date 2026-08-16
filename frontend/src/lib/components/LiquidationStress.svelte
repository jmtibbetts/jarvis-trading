<script lang="ts">
  /**
   * The Kamino book, and what it would take to break a position.
   *
   * `lib/kamino_sweep` and `lib/liquidation_matrix` were both built and
   * tested with no route and no panel. This is their surface.
   *
   * TWO RULES THIS PANEL FOLLOWS.
   *
   * 1. CERTAINTY IS NOT FLAT. A position value decoded by the canonical
   *    Kamino layout is VERIFIED. A health factor derived from it is
   *    CALCULATED. Future carry, a depeg and a forced-sale cascade are
   *    MODELLED. Rendering them at equal weight is how a model gets read
   *    as a measurement, so the provenance travels with the numbers.
   *
   * 2. THE BOUNDARY MOVES, AND ON THREE INDEPENDENT AXES. "Distance to
   *    liquidation" as one number answers the question badly. The grid is
   *    SOL shock x LST/SOL depeg because that stays readable; the STABLE
   *    depeg is a selector that picks which slice you are looking at,
   *    rather than a fourth dimension nobody can read.
   *
   * The stable axis is the one that did not exist at all. Debt was summed
   * at face value and grown only by interest, so borrowed USDC was modelled
   * as debt that stays exactly $1.00 forever — and the genuinely dangerous
   * scenario is collateral falling WHILE the debt gets more expensive.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { api, type KaminoSweep, type ObligationStress } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import { toastStore } from "../stores/toast.svelte";

  const feeds = new FeedTracker();

  let sweep = $state<KaminoSweep | null>(null);
  let stress = $state<ObligationStress | null>(null);
  let selected = $state<string | null>(null);
  let stableDepeg = $state(0);
  let days = $state(0);
  let sweeping = $state(false);
  let stressing = $state(false);

  // On-demand: the sweep decodes the whole Kamino program, and the stress
  // read fetches an account. Neither belongs on a poll.
  async function runSweep() {
    if (sweeping) return;
    sweeping = true;
    try {
      sweep = await feeds.load("sweep", () => api.kaminoSweep(40));
      if (sweep?.detail) toastStore.push(sweep.detail, "neutral");
    } catch (e: any) {
      toastStore.err(`Sweep failed: ${e?.message ?? e}`);
    } finally {
      sweeping = false;
    }
  }

  async function loadStress(obligation: string) {
    if (stressing) return;
    stressing = true;
    selected = obligation;
    try {
      stress = await feeds.load("stress", () =>
        api.obligationStress(obligation, { days, stableDepegPct: stableDepeg }));
      if (stress && !stress.available && stress.reason) {
        toastStore.push(stress.reason, "bad");
      }
    } catch (e: any) {
      stress = null;
      toastStore.err(`Stress read failed: ${e?.message ?? e}`);
    } finally {
      stressing = false;
    }
  }

  // Re-read when either axis control moves, but only once a position is
  // chosen — the controls are meaningless without one.
  async function reload() {
    if (selected) await loadStress(selected);
  }

  const usd = (v: number | null | undefined, dp = 0) =>
    v === null || v === undefined ? "—"
    : `$${v.toLocaleString(undefined, { maximumFractionDigits: dp })}`;
  const pct = (v: number | null | undefined, dp = 2) =>
    v === null || v === undefined ? "—" : `${v.toFixed(dp)}%`;
  const hf = (v: number | null | undefined) =>
    v === null || v === undefined ? "—" : v.toFixed(3);
  const short = (a: string | null | undefined) =>
    a ? `${a.slice(0, 4)}…${a.slice(-4)}` : "—";

  /** Health-factor colour. 1.0 is the liquidation line, not a soft warning. */
  function hfTone(v: number | null | undefined) {
    if (v === null || v === undefined) return "";
    if (v <= 1.0) return "liq";
    if (v <= 1.15) return "crit";
    if (v <= 1.4) return "warn";
    return "ok";
  }
</script>

<div class="ls">
  <Panel title="Kamino Book Sweep" status={feeds.status("sweep")}
         meta={sweep ? `${sweep.positions} obligations · ${sweep.ranked.length} ranked` : "on demand"}>
    <p class="hint">
      Ranked by what <strong>matters</strong> — size, proximity to the line and
      wallet quality — not by what is biggest. A $42M position 2% from
      liquidation owned by a measured wallet is a different event from the
      same position owned by an unknown one.
    </p>
    <button onclick={runSweep} disabled={sweeping}>
      {sweeping ? "Sweeping the program…" : "Sweep the book"}
    </button>

    {#if sweep?.bands?.length}
      <div class="tablewrap bands">
        <table>
          <thead>
            <tr><th>Collateral ≥</th><th>Positions</th><th>Collateral</th>
                <th>Debt</th><th>Within 5% of liq.</th><th>Debt at risk</th></tr>
          </thead>
          <tbody>
            {#each sweep.bands as b (b.min_collateral_usd)}
              <tr>
                <td class="n">{usd(b.min_collateral_usd)}</td>
                <td class="n">{b.positions.toLocaleString()}</td>
                <td class="n">{usd(b.collateral_usd)}</td>
                <td class="n">{usd(b.debt_usd)}</td>
                <td class="n" class:warn={b.within_5pct_of_liquidation > 0}>
                  {b.within_5pct_of_liquidation}</td>
                <td class="n">{usd(b.debt_within_5pct_usd)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if sweep?.ranked?.length}
      <div class="tablewrap">
        <table>
          <thead>
            <tr><th>Obligation</th><th>Owner</th><th>Collateral</th><th>Debt</th>
                <th>Health</th><th>To liq.</th><th>Significance</th><th></th></tr>
          </thead>
          <tbody>
            {#each sweep.ranked as p (p.obligation)}
              <tr class:sel={selected === p.obligation}>
                <td class="mono">{short(p.obligation)}</td>
                <td class="mono">
                  {short(p.owner)}
                  <!-- The join is reported even when empty: discovery finds
                       wallets by TOKEN ACTIVITY and this finds them by
                       BORROWING, which are different populations. -->
                  {#if p.wallet_status}
                    <Pill tone="info" label={p.wallet_status} />
                  {/if}
                </td>
                <td class="n">{usd(p.collateral_value_usd)}</td>
                <td class="n">{usd(p.debt_value_usd)}</td>
                <td class="n {hfTone(p.health_factor)}">{hf(p.health_factor)}</td>
                <td class="n">{pct(p.distance_to_liquidation_pct)}</td>
                <td class="n">{p.significance_score?.toFixed(1) ?? "—"}</td>
                <td><button class="sm" onclick={() => loadStress(p.obligation)}
                            disabled={stressing}>Stress</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else if sweep}
      <p class="empty">{sweep.detail ?? "No obligations above the debt floor."}</p>
    {:else}
      <StateNote status={feeds.status("sweep")} noun="Kamino sweep"
                 emptyText="Not swept yet." />
    {/if}
  </Panel>

  <Panel title="Liquidation Stress Matrix" status={feeds.status("stress")}
         meta={selected ? short(selected) : "select a position"}>
    {#if !selected}
      <p class="empty">Pick a position above to see where its boundary sits.</p>
    {:else if stress?.available}
      <div class="head">
        <div class="stat"><span>Health now</span>
          <b class="n {hfTone(stress.current_health_factor)}">{hf(stress.current_health_factor)}</b></div>
        <div class="stat"><span>SOL fall to liquidation</span>
          <b class="n">{stress.static_sol_liquidation_pct === null
            ? "survives −99%" : pct(stress.static_sol_liquidation_pct)}</b></div>
        <div class="stat"><span>At selected depeg</span>
          <b class="n">{stress.boundary_at_selected_depeg === null
            ? "survives −99%" : pct(stress.boundary_at_selected_depeg)}</b></div>
      </div>

      <div class="controls">
        <label>Stable depeg
          <select bind:value={stableDepeg} onchange={reload}>
            {#each stress.stable_shocks_available ?? [0] as s}
              <option value={s}>{s > 0 ? "+" : ""}{s}%</option>
            {/each}
          </select>
        </label>
        <label>Horizon
          <select bind:value={days} onchange={reload}>
            <option value={0}>now</option>
            <option value={1}>1 day</option>
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
        </label>
        <span class="sign">
          positive = the stable trades ABOVE par — adverse for a borrower
        </span>
      </div>

      {#if stress.matrix}
        <div class="tablewrap">
          <table class="matrix">
            <thead>
              <tr>
                <th>SOL \ LST depeg</th>
                {#each stress.matrix.depeg_shocks as d}<th class="n">−{d}%</th>{/each}
              </tr>
            </thead>
            <tbody>
              {#each stress.matrix.rows as row (row.sol_shock_pct)}
                <tr>
                  <th class="rowh">−{row.sol_shock_pct}%</th>
                  {#each row.cells as c}
                    <td class="n cell {hfTone(c.health_factor)}"
                        class:liqcell={c.liquidatable}>{hf(c.health_factor)}</td>
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}

      {#if stress.stable_axis?.cells?.length}
        <h4>Debt repricing — the axis that did not exist</h4>
        <p class="hint">
          Debt used to be summed at face value and grown only by interest, so
          borrowed USDC was modelled as debt that stays exactly $1.00 forever.
          The dangerous case is collateral falling <em>while</em> the debt gets
          more expensive.
        </p>
        <div class="tablewrap">
          <table>
            <thead>
              <tr><th>Stable vs par</th><th>Debt</th><th>Health</th>
                  <th>SOL fall to liquidation</th></tr>
            </thead>
            <tbody>
              {#each stress.stable_axis.cells as c (c.stable_depeg_pct)}
                <tr class:sel={c.stable_depeg_pct === stableDepeg}>
                  <td class="n">{c.stable_depeg_pct > 0 ? "+" : ""}{c.stable_depeg_pct}%</td>
                  <td class="n">{usd(c.debt_value_usd)}</td>
                  <td class="n {hfTone(c.health_factor)}">{hf(c.health_factor)}</td>
                  <td class="n">{c.boundary_sol_pct === null
                    ? "survives −99%" : `−${c.boundary_sol_pct}%`}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <p class="basis">{stress.stable_axis.basis}</p>
      {/if}

      {#if stress.legs?.length}
        <h4>Collateral legs</h4>
        <div class="tablewrap">
          <table>
            <thead>
              <tr><th>Asset</th><th>Value</th><th>Threshold</th>
                  <th>Identified by</th><th>Takes</th></tr>
            </thead>
            <tbody>
              {#each stress.legs as l}
                <tr>
                  <td>{l.symbol ?? short(l.mint)}</td>
                  <td class="n">{usd(l.value_usd)}</td>
                  <!-- THE protocol parameter, per reserve, never averaged. -->
                  <td class="n">{l.liquidation_threshold_pct ?? "—"}%</td>
                  <td class="mono dim">{l.identified_by}</td>
                  <td class="tags">
                    {#if l.took_sol_shock}<Pill tone="warm" label="SOL" />{/if}
                    {#if l.took_depeg}<Pill tone="warm" label="LST depeg" />{/if}
                    {#if l.took_stable_depeg}<Pill tone="info" label="stable" />{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}

      {#if stress.provenance}
        <div class="prov">
          {#each Object.entries(stress.provenance) as [k, v]}
            <div class="prow"><span>{k.replace(/_/g, " ")}</span><b>{v}</b></div>
          {/each}
        </div>
      {/if}
    {:else if stress}
      <p class="empty">{stress.reason ?? "This position could not be stressed."}</p>
    {:else}
      <StateNote status={feeds.status("stress")} noun="Stress matrix" />
    {/if}
  </Panel>
</div>

<style>
  .ls { display: flex; flex-direction: column; gap: 14px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
  button {
    background: var(--bg-elev, #11151c); color: var(--text, #e6e9ef);
    border: 1px solid var(--border, #222a35); border-radius: 4px;
    padding: 7px 12px; font-size: 12px; cursor: pointer; font-family: inherit;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.sm { padding: 3px 8px; font-size: 11px; }
  .tablewrap { overflow-x: auto; margin-top: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; padding: 6px 9px; color: var(--muted);
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
    border-bottom: 1px solid var(--border, #222a35); white-space: nowrap;
  }
  td { padding: 6px 9px; border-bottom: 1px solid var(--border, #222a35); }
  td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  tr.sel { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
  .dim { color: var(--muted); }
  .empty { color: var(--muted); font-size: 13px; margin: 10px 0 0; }
  .head { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 12px; }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat span {
    font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--muted);
  }
  .stat b { font-size: 17px; font-variant-numeric: tabular-nums; }
  .controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
  .controls label {
    display: flex; align-items: center; gap: 6px; font-size: 11px;
    text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
  }
  select {
    background: var(--bg-elev, #11151c); color: var(--text, #e6e9ef);
    border: 1px solid var(--border, #222a35); border-radius: 4px;
    padding: 4px 7px; font-size: 12px; font-family: inherit;
  }
  .sign { font-size: 11px; color: var(--muted); }
  table.matrix th.rowh {
    text-align: right; font-size: 11px; text-transform: none;
    letter-spacing: 0; color: var(--text, #e6e9ef);
  }
  td.cell { font-variant-numeric: tabular-nums; }
  /* 1.0 is the liquidation line, not a soft warning. */
  .ok { color: var(--good, #4ec9a0); }
  .warn { color: var(--warn-text, #e0b070); }
  .crit { color: #e8945a; }
  .liq { color: var(--bad, #e06c75); font-weight: 600; }
  td.liqcell { background: color-mix(in srgb, var(--bad, #e06c75) 16%, transparent); }
  h4 {
    margin: 18px 0 4px; font-size: 11px; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); font-weight: 600;
  }
  .basis { font-size: 11px; color: var(--muted); margin: 6px 0 0; }
  .tags { display: flex; gap: 4px; flex-wrap: wrap; }
  .prov {
    margin-top: 14px; padding-top: 10px;
    border-top: 1px solid var(--border, #222a35);
  }
  .prow {
    display: flex; justify-content: space-between; gap: 12px;
    font-size: 11px; padding: 2px 0;
  }
  .prow span { color: var(--muted); text-transform: capitalize; }
</style>
