<script lang="ts">
  import { api, type OnChainContext } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import StateNote from "./StateNote.svelte";

  const feeds = new FeedTracker();

  let ctx = $state<OnChainContext | null>(null);
  let loading = $state(true);

  // The bare `catch { failed = true }` this replaces collapsed every failure
  // into one sentence, "On-chain context unavailable", which also covered
  // the genuinely-empty case. Coin Metrics being down, the key being absent
  // and the dataset being empty are three different situations.
  async function load() {
    loading = true;
    ctx = await feeds.load("onchain", () => api.onChainContext());
    loading = false;
  }

  $effect(() => {
    load();
  });

  // MVRV's LEVEL is close to meaningless across assets — 2.4 is euphoric
  // for one and ordinary for another — so the gauge is the percentile
  // against the asset's own trailing 2 years. Bands are labels for that
  // percentile, not valuation calls.
  function band(p: number | null | undefined) {
    if (p == null) return { label: "—", tone: "dim" };
    if (p >= 90) return { label: "cycle highs", tone: "hot" };
    if (p >= 70) return { label: "elevated", tone: "warm" };
    if (p >= 30) return { label: "mid-range", tone: "dim" };
    if (p >= 10) return { label: "subdued", tone: "cool" };
    return { label: "cycle lows", tone: "cool" };
  }

  const fmtNum = (n: number | null | undefined) =>
    n == null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: 0 });
</script>

{#if loading && !ctx}
  <div class="empty">Loading on-chain context…</div>
{:else if !ctx}
  <StateNote status={feeds.status("onchain")} noun="on-chain context" />
{:else}
  <div class="oc-grid">
    {#each ctx.assets as a (a.symbol)}
      <div class="oc-card" class:oc-degraded={a.state !== "fresh"}>
        <div class="oc-head">
          <b>{a.symbol}</b>
          <span class="oc-state oc-{a.state}">
            {a.state === "fresh"
              ? `as of ${a.as_of}`
              : a.state === "stale"
                ? `stale · ${a.mvrv_age_days}d`
                : a.state === "never_synced"
                  ? "never synced"
                  : "error"}
          </span>
        </div>

        {#if a.state === "never_synced" || a.state === "error"}
          <div class="empty small">{a.detail}</div>
        {:else}
          <div class="oc-metric">
            <span class="oc-label">MVRV</span>
            <b class="num oc-value">{a.mvrv?.toFixed(3) ?? "—"}</b>
            <span class="oc-band {band(a.mvrv_pctile_2y).tone}">{band(a.mvrv_pctile_2y).label}</span>
          </div>
          <!-- The gauge: where this sits in its OWN two years, not against
               some cross-asset threshold. -->
          <div class="oc-gauge">
            <div class="oc-track">
              {#if a.mvrv_pctile_2y != null}
                <div class="oc-marker" style="left:{Math.min(100, Math.max(0, a.mvrv_pctile_2y))}%"></div>
              {/if}
            </div>
            <div class="oc-scale">
              <span>2y low</span>
              <span class="num">{a.mvrv_pctile_2y != null ? `${a.mvrv_pctile_2y.toFixed(1)}th pctile` : "insufficient history"}</span>
              <span>2y high</span>
            </div>
          </div>

          <div class="oc-metric">
            <span class="oc-label">Active addresses</span>
            <b class="num oc-value">{fmtNum(a.active_addresses)}</b>
            <span class="oc-band dim">
              {a.active_addr_pctile_2y != null ? `${a.active_addr_pctile_2y.toFixed(0)}th pctile` : "—"}
            </span>
          </div>

          <div class="oc-foot">
            {#if a.state === "stale"}
              <span class="oc-warn">⚠ {a.detail}</span>
            {:else if a.joined}
              <span class="dim">joined to new candidates · {a.observations} observations</span>
            {:else}
              <span class="oc-warn">not joined to candidates</span>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>
  <p class="oc-note dim">{ctx.note}</p>
{/if}

<style>
  .oc-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
  }
  .oc-card {
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 12px 14px;
  }
  .oc-degraded {
    opacity: 0.75;
  }
  .oc-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 10px;
    font-size: 12.5px;
  }
  .oc-state {
    font-size: 10.5px;
    color: var(--dim);
  }
  .oc-stale,
  .oc-never_synced,
  .oc-error {
    color: var(--warm);
  }
  .oc-metric {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 6px;
  }
  .oc-label {
    flex: 1;
    font-size: 11px;
    color: var(--dim);
  }
  .oc-value {
    font-size: 14px;
  }
  .oc-band {
    font-size: 10.5px;
    min-width: 74px;
    text-align: right;
  }
  .oc-gauge {
    margin: 8px 0 14px;
  }
  .oc-track {
    position: relative;
    height: 4px;
    border-radius: 2px;
    background: linear-gradient(90deg, var(--good), var(--line), var(--warm));
  }
  .oc-marker {
    position: absolute;
    top: -3px;
    width: 2px;
    height: 10px;
    background: var(--text);
    transform: translateX(-1px);
  }
  .oc-scale {
    display: flex;
    justify-content: space-between;
    margin-top: 5px;
    font-size: 10px;
    color: var(--dim);
  }
  .oc-foot {
    font-size: 10.5px;
  }
  .oc-warn {
    color: var(--warm);
  }
  .oc-note {
    margin: 12px 0 0;
    font-size: 10.5px;
  }
  .hot {
    color: var(--bad);
  }
  .warm {
    color: var(--warm);
  }
  .cool {
    color: var(--good);
  }
  .dim {
    color: var(--dim);
  }
  .empty {
    color: var(--dim);
    font-size: 12px;
    padding: 10px 0;
  }
  .small {
    font-size: 11px;
  }
</style>
