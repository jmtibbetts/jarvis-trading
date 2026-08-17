<script lang="ts">
  /**
   * Platform mode, training-data integrity, and what each venue can
   * actually execute.
   *
   * THREE THINGS THAT MUST NOT LOOK LIKE FAULTS.
   *
   * LIVE EXECUTION DISABLED is the current platform STATE, not a broken
   * connection. An operator who sees a red "unavailable" badge will try to
   * repair it; one who sees a declared training mode will not.
   *
   * A FAILING INVARIANT is not a failing feed. Every defect this programme
   * found was confident and wrong — a leveraged short marked as a long,
   * futures P&L missing its multiplier, a liquidity failure booked as a
   * perfect exit. None of them threw. These checks are the remedy made
   * visible, so each one carries WHY IT MATTERS: a red light nobody
   * understands gets ignored, and then disabled.
   *
   * UI_ONLY IS NOT UNSUPPORTED. Kraken Pro shows a human 11,000 US
   * equities while the API Center documents no stock trading contract.
   * That difference belongs on screen rather than in a developer's memory,
   * and it is a THIRD state — observable, not executable.
   *
   * `healthy` deliberately requires that every check actually RAN. A check
   * that could not run reports UNAVAILABLE and blocks a clean verdict,
   * because zero violations and no ability to look are different claims.
   */
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { api, type IntegrityPanel, type PlatformMode,
           type VenueCapabilities } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();

  let mode = $state<PlatformMode | null>(null);
  let panel = $state<IntegrityPanel | null>(null);
  let venues = $state<VenueCapabilities | null>(null);
  let expanded = $state<Set<string>>(new Set());

  async function load() {
    mode = await feeds.load("mode", () => api.platformMode());
    panel = await feeds.load("integrity", () => api.integrity());
    venues = await feeds.load("venues", () => api.venueCapabilities());
  }

  $effect(() => { load(); });

  function toggle(key: string) {
    const next = new Set(expanded);
    next.has(key) ? next.delete(key) : next.add(key);
    expanded = next;
  }

  const tone = (status: string, severity: string) =>
    status === "OK" ? "good"
    : status === "UNAVAILABLE" ? "neutral"
    : severity === "CRITICAL" ? "bad" : "warm";

  const capTone = (s: string) =>
    s === "DOCUMENTED" || s === "DISCOVERED" ? "good"
    : s === "UI_ONLY" ? "warm"
    : s === "UNSUPPORTED" ? "bad" : "neutral";
</script>

<div class="ti">
  <!-- Mode: a STATE, rendered as configuration rather than a fault. -->
  <Panel title="Platform Mode" status={feeds.status("mode")}
         meta={mode?.mode ?? "—"}>
    {#if mode}
      <div class="mode" class:training={!mode.live_execution_allowed}>
        <strong>{mode.detail}</strong>
        <p>
          {#if mode.virtual_only}
            Every eligible thesis routes to a virtual venue. Market and
            account data still flow — reading a broker is not trading.
          {:else}
            Real order submission is permitted in this mode.
          {/if}
        </p>
        <div class="modes">
          {#each mode.modes_available as m}
            <Pill tone={m === mode.mode ? "info" : "neutral"} label={m} />
          {/each}
        </div>
      </div>
    {:else}
      <StateNote status={feeds.status("mode")} noun="Platform mode" />
    {/if}
  </Panel>

  <!-- Integrity: checks that can genuinely go red. -->
  <Panel title="Training Data Integrity" status={feeds.status("integrity")}
         meta={panel ? `${panel.violations}/${panel.total} failing` : "—"}>
    {#if panel}
      <div class="verdict"
           class:bad={panel.critical > 0}
           class:warn={panel.violations > 0 && panel.critical === 0}
           class:ok={panel.healthy}>
        {panel.verdict}
      </div>
      {#if panel.unavailable > 0 || panel.errors > 0}
        <p class="hint">
          {panel.unavailable + panel.errors} check(s) could not run. A check
          that could not run is not a check that passed, so the verdict
          cannot be clean.
        </p>
      {/if}

      <div class="checks">
        {#each panel.checks as c (c.key)}
          <div class="check" class:failing={c.status === "VIOLATION"}>
            <button class="head" onclick={() => toggle(c.key)}>
              <Pill tone={tone(c.status, c.severity)} label={c.status} />
              <span class="title">{c.title}</span>
              <span class="count num">
                {c.status === "UNAVAILABLE" ? "—" : c.count}
                {#if c.scanned}<em>/ {c.scanned}</em>{/if}
              </span>
            </button>
            {#if expanded.has(c.key)}
              <div class="body">
                {#if c.detail}<p class="detail">{c.detail}</p>{/if}
                {#if c.why_it_matters}
                  <p class="why"><strong>Why it matters:</strong> {c.why_it_matters}</p>
                {/if}
                {#if c.examples?.length}
                  <ul class="examples">
                    {#each c.examples as ex}
                      <li class="mono">{JSON.stringify(ex)}</li>
                    {/each}
                  </ul>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <StateNote status={feeds.status("integrity")} noun="Integrity checks" />
    {/if}
  </Panel>

  <!-- Venues: three states, not two. -->
  <Panel title="Venue Capability" status={feeds.status("venues")}
         meta="what can actually be executed">
    {#if venues?.venues}
      <p class="hint">{venues.note}</p>
      <div class="tablewrap">
        <table>
          <thead>
            <tr><th>Venue</th><th>Product</th><th>Status</th>
                <th>API</th><th>Data</th><th>Note</th></tr>
          </thead>
          <tbody>
            {#each Object.entries(venues.venues) as [venue, caps]}
              {#each caps as c}
                <tr>
                  <td class="mono">{venue}</td>
                  <td class="mono">{c.product}</td>
                  <td><Pill tone={capTone(c.status)} label={c.status} /></td>
                  <td class="mono dim">{c.api_surface ?? "—"}</td>
                  <td class="mono dim">{c.data_entitlement}</td>
                  <td class="reason">{c.reason ?? ""}</td>
                </tr>
              {/each}
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <StateNote status={feeds.status("venues")} noun="Venue capability" />
    {/if}
  </Panel>
</div>

<style>
  .ti { display: flex; flex-direction: column; gap: 14px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 10px; }

  .mode {
    padding: 12px 14px; border-radius: 5px;
    border: 1px solid var(--border, #222a35); background: var(--bg-elev, #11151c);
  }
  /* Training mode is DECLARED, not degraded — deliberately not red. */
  .mode.training {
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
    background: color-mix(in srgb, var(--accent) 8%, transparent);
  }
  .mode strong {
    display: block; font-size: 13px; letter-spacing: .05em;
    text-transform: uppercase;
  }
  .mode p { margin: 6px 0 10px; font-size: 12.5px; color: var(--muted); }
  .modes { display: flex; gap: 5px; flex-wrap: wrap; }

  .verdict {
    font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    padding: 8px 11px; border-radius: 4px; margin-bottom: 10px;
    border: 1px solid var(--border, #222a35);
  }
  .verdict.ok { color: var(--good, #4ec9a0); border-color: color-mix(in srgb, var(--good, #4ec9a0) 40%, transparent); }
  .verdict.warn { color: var(--warn-text, #e0b070); border-color: color-mix(in srgb, var(--warn-text, #e0b070) 40%, transparent); }
  .verdict.bad { color: var(--bad, #e06c75); border-color: color-mix(in srgb, var(--bad, #e06c75) 45%, transparent); }

  .checks { display: flex; flex-direction: column; gap: 5px; }
  .check { border: 1px solid var(--border, #222a35); border-radius: 4px; }
  .check.failing { border-color: color-mix(in srgb, var(--bad, #e06c75) 35%, transparent); }
  .head {
    display: flex; align-items: center; gap: 9px; width: 100%;
    padding: 7px 10px; background: none; border: none; cursor: pointer;
    color: inherit; font: inherit; text-align: left;
  }
  .head:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
  .title { flex: 1; font-size: 12.5px; }
  .count { font-variant-numeric: tabular-nums; font-size: 12px; }
  .count em { color: var(--muted); font-style: normal; font-size: 11px; }
  .body {
    padding: 2px 12px 10px 12px; border-top: 1px solid var(--border, #222a35);
  }
  .detail { margin: 8px 0 4px; font-size: 12.5px; }
  .why { margin: 4px 0; font-size: 12px; color: var(--muted); }
  .examples { margin: 6px 0 0; padding-left: 16px; }
  .examples li { font-size: 11.5px; color: var(--muted); }

  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; padding: 6px 9px; color: var(--muted);
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
    border-bottom: 1px solid var(--border, #222a35); white-space: nowrap;
  }
  td { padding: 6px 9px; border-bottom: 1px solid var(--border, #222a35); }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
  .dim { color: var(--muted); }
  .reason { color: var(--muted); font-size: 11.5px; max-width: 380px; }
</style>
