<script lang="ts">
  import { api, type DexDiscovery } from "../api";

  // On-demand only: a pass hits GeckoTerminal and DEX Screener live across
  // several networks and can take many seconds. Firing it on every render
  // of the crypto desk would hammer two keyless public APIs for data that
  // nothing downstream consumes automatically.
  let res = $state<DexDiscovery | null>(null);
  let running = $state(false);
  let error = $state<string | null>(null);
  let ranAt = $state<string | null>(null);

  async function run() {
    running = true;
    error = null;
    try {
      res = await api.dexDiscovery(true);
      ranAt = new Date().toLocaleTimeString();
    } catch (e) {
      error = String(e);
    } finally {
      running = false;
    }
  }

  const fmtUsd = (n: number | null) =>
    n == null
      ? "—"
      : n >= 1e9
        ? `$${(n / 1e9).toFixed(2)}B`
        : n >= 1e6
          ? `$${(n / 1e6).toFixed(1)}M`
          : n >= 1e3
            ? `$${(n / 1e3).toFixed(0)}k`
            : `$${n.toFixed(0)}`;

  const fmtAge = (h: number | null) =>
    h == null ? "—" : h < 48 ? `${h.toFixed(0)}h` : `${(h / 24).toFixed(1)}d`;

  // The survivor rate IS the headline. A filter whose product is the
  // rejection deserves to show how much it rejected.
  const survivalPct = $derived(
    res && res.scanned ? (res.survivors.length / res.scanned) * 100 : null,
  );
</script>

<div class="dx-toolbar">
  <button class="btn small" disabled={running} onclick={run}>
    {running ? "Scanning…" : res ? "Re-scan" : "Run discovery"}
  </button>
  {#if res}
    <span class="dim">
      {res.scanned} screened · {res.survivors.length} survived
      {#if survivalPct != null}({survivalPct.toFixed(1)}%){/if}
      {#if ranAt} · {ranAt}{/if}
    </span>
  {:else if !running}
    <span class="dim">keyless · GeckoTerminal + DEX Screener · live call</span>
  {/if}
</div>

{#if error}
  <div class="dx-warn">Discovery failed: {error}</div>
{:else if !res}
  {#if !running}
    <div class="empty">Not scanned this session.</div>
  {/if}
{:else}
  {#if res.degraded}
    <!-- "scanned 0, survivors 0" reads as "nothing qualified" when it may
         mean "we never looked". The distinction is the panel's job. -->
    <div class="dx-warn">
      ⚠ Partial fetch — this pass could not reach every source, so the counts
      below are a floor, not a census.
      <div class="dx-errs">{res.fetch_errors.join(" · ")}</div>
    </div>
  {/if}

  {#if res.survivors.length}
    <div class="dx-scroll">
      <table class="dx-table">
        <thead>
          <tr>
            <th>Pool</th><th>Net</th><th class="r">Liquidity</th>
            <th class="r">Vol 24h</th><th class="r">Txns</th>
            <th class="r">Buyers</th><th class="r">Age</th><th class="r">FDV</th>
          </tr>
        </thead>
        <tbody>
          {#each res.survivors as s (s.pool_address)}
            <tr>
              <td>
                {#if s.ds_url}
                  <a href={s.ds_url} target="_blank" rel="noopener noreferrer">{s.name}</a>
                {:else}
                  {s.name}
                {/if}
                {#if s.source_disagreement}
                  <div class="dx-disagree" title={s.source_disagreement}>⚠ sources disagree</div>
                {/if}
              </td>
              <td class="dim">{s.ds_dex ?? s.network}</td>
              <td class="r num">{fmtUsd(s.liquidity_usd)}</td>
              <td class="r num">{fmtUsd(s.volume_24h_usd)}</td>
              <td class="r num">{s.txns_24h.toLocaleString()}</td>
              <td class="r num">{s.buyers_24h.toLocaleString()}</td>
              <td class="r num">{fmtAge(s.age_hours)}</td>
              <td class="r num">{fmtUsd(s.fdv_usd)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <div class="empty">
      Nothing cleared the floors this pass — {res.rejected} rejected of {res.scanned}.
    </div>
  {/if}

  {#if Object.keys(res.rejection_reasons).length}
    <div class="dx-reasons">
      <div class="dx-reasons-head dim">Why {res.rejected} were rejected — the shape of the noise</div>
      {#each Object.entries(res.rejection_reasons) as [tag, n] (tag)}
        <div class="dx-reason">
          <span>{tag.replace(/_/g, " ")}</span>
          <div class="dx-track">
            <div class="dx-fill" style="width:{Math.min(100, (n / res.scanned) * 100)}%"></div>
          </div>
          <b class="num">{n}</b>
        </div>
      {/each}
    </div>
  {/if}

  <p class="dx-note dim">{res.note}</p>
{/if}

<style>
  .dx-toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
    font-size: 11px;
  }
  .dx-scroll {
    overflow-x: auto;
  }
  .dx-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11.5px;
  }
  .dx-table th {
    text-align: left;
    font-weight: 500;
    color: var(--dim);
    padding: 4px 8px 6px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  .dx-table td {
    padding: 5px 8px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  .dx-table .r {
    text-align: right;
  }
  .dx-disagree {
    font-size: 10px;
    color: var(--warm);
  }
  .dx-warn {
    margin-bottom: 10px;
    font-size: 11px;
    color: var(--warm);
  }
  .dx-errs {
    margin-top: 3px;
    font-size: 10px;
    color: var(--dim);
  }
  .dx-reasons {
    margin-top: 14px;
  }
  .dx-reasons-head {
    font-size: 10.5px;
    margin-bottom: 6px;
  }
  .dx-reason {
    display: grid;
    grid-template-columns: 150px 1fr 34px;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    margin-bottom: 3px;
  }
  .dx-track {
    height: 4px;
    border-radius: 2px;
    background: var(--line);
  }
  .dx-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--dim);
  }
  .dx-reason b {
    text-align: right;
  }
  .dx-note {
    margin: 12px 0 0;
    font-size: 10.5px;
    line-height: 1.45;
  }
  .empty {
    color: var(--dim);
    font-size: 12px;
    padding: 10px 0;
  }
  .dim {
    color: var(--dim);
  }
</style>
