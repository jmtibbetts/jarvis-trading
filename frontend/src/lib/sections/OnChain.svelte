<script lang="ts">
  /**
   * The on-chain desk — the surface for work that had none.
   *
   * Six backend modules were built and tested with no route and no panel:
   * the wallet registry, autonomous discovery, the token surge engine, the
   * virtual DEX book, native staking and Kamino lending. Intelligence that
   * cannot be seen changes no decision, which is the same objection §4
   * makes about a silent failure.
   *
   * The rule this screen follows everywhere: SAY WHERE EACH NUMBER CAME
   * FROM. On-chain values carry very different weights — an obligation
   * decoded by the canonical Kamino layout is VERIFIED, a health factor
   * derived from it is CALCULATED, and forced-sale exposure is ESTIMATED.
   * Rendering them at equal confidence is how a model gets read as a
   * measurement.
   */
  import Panel from "../components/Panel.svelte";
  import Pill from "../components/Pill.svelte";
  import StateNote from "../components/StateNote.svelte";
  import KpiTile from "../components/KpiTile.svelte";
  import { api } from "../api";
  import { FeedTracker } from "../dataState.svelte";
  import { toastStore } from "../stores/toast.svelte";
  import DexExchange from "../components/DexExchange.svelte";
  import LiquidationStress from "../components/LiquidationStress.svelte";

  const feeds = new FeedTracker();

  let wallets = $state<any | null>(null);
  let discovery = $state<any | null>(null);
  let surge = $state<any | null>(null);
  let book = $state<any | null>(null);
  let protocols = $state<any | null>(null);
  let helius = $state<any | null>(null);
  let scanning = $state(false);
  let riskScan = $state<any | null>(null);
  let riskBusy = $state(false);

  async function loadAll() {
    // NO third argument. `load(key, fn, opts?)` takes `{ keepLast }` — the
    // old signature took the caller's previous value, and this call site
    // still passed its own `$state` there. Two things followed: the opts
    // object was nonsense, and worse, reading those six variables inside a
    // function called from an `$effect` that then WRITES all six made the
    // effect re-trigger itself. That is the measured 34-requests-in-10s
    // storm — fixed inside FeedTracker, still live here.
    const [w, d, s, b, p, h] = await Promise.all([
      feeds.load("wallets", () => api.raw<any>("/onchain/wallets?limit=60")),
      feeds.load("discovery", () => api.raw<any>("/onchain/discovery/status")),
      feeds.load("surge", () => api.raw<any>("/onchain/surge?limit=15")),
      feeds.load("book", () => api.dexBook()),
      feeds.load("protocols", () => api.raw<any>("/onchain/protocols")),
      feeds.load("helius", () => api.raw<any>("/helius/health")),
    ]);
    wallets = w; discovery = d; surge = s; book = b; protocols = p; helius = h;
  }

  $effect(() => {
    loadAll();
    const poll = setInterval(loadAll, 60_000);
    return () => clearInterval(poll);
  });

  async function runDiscovery() {
    if (scanning) return;
    scanning = true;
    try {
      const r = await api.raw<any>("/onchain/discovery/run?max_tokens=5");
      toastStore.ok(
        `Discovery: ${r.tokens_scanned} tokens, ${r.owners_seen} owners, ` +
        `${r.candidates_created} new candidates, ${r.excluded} excluded`,
      );
      await loadAll();
    } catch (e) {
      toastStore.err(`Discovery failed: ${e}`);
    } finally {
      scanning = false;
    }
  }

  async function runRiskScan() {
    if (riskBusy) return;
    riskBusy = true;
    try {
      riskScan = await api.raw<any>("/onchain/lending/risk/scan?limit_scanned=4000");
    } catch (e) {
      toastStore.err(`Lending scan failed: ${e}`);
    } finally {
      riskBusy = false;
    }
  }

  const c = $derived(discovery?.counts ?? wallets?.counts ?? {});
  const riskTone = (s: string) =>
    s === "CRITICAL" || s === "LIQUIDATION_IN_PROGRESS" ? "critical"
    : s === "HIGH" ? "bad" : s === "ELEVATED" ? "warm" : "good";
  const num = (v: any, d = 2) =>
    v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
  const usd = (v: any) => (v == null ? "—" : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  const short = (a: string) => (a ? `${a.slice(0, 4)}…${a.slice(-4)}` : "—");
</script>

<div class="oc">
  <div class="kpis">
    <KpiTile label="Candidates" value={String(c.candidates ?? "—")} period="discovered + seeded" />
    <KpiTile label="Excluded" value={String(c.excluded_entities ?? "—")} period="exchanges, pools, PDAs" />
    <KpiTile label="Smart Money" value={String(c.smart_money ?? 0)} period="none until measured" />
    <KpiTile label="DEX Equity" value={usd(book?.equity_usd)} period="virtual on-chain book" />
  </div>

  <div class="grid">
    <Panel title="Wallet Discovery" status={feeds.status("discovery")}
           meta={discovery?.enabled ? "autonomous — token activity to candidates" : "disabled"}>
      {#if discovery}
        <div class="stat-list">
          <div class="stat"><span>Discovery</span>
            <b><Pill label={discovery.enabled ? "enabled" : "disabled"}
                     tone={discovery.enabled ? "good" : "neutral"} /></b></div>
          <div class="stat"><span>Last pass</span>
            <b class="num">{discovery.last_run ? new Date(discovery.last_run).toLocaleTimeString() : "never"}</b></div>
          {#each Object.entries(discovery.by_source ?? {}) as [src, n]}
            <div class="stat"><span>via {src}</span><b class="num">{n}</b></div>
          {/each}
        </div>
        <p class="note">
          Starts from TOKEN ACTIVITY, not from a wallet list — two RPC calls per
          token: largest accounts, then their owners. Infrastructure is recorded
          as excluded rather than dropped, so the next pass recognises the same
          exchange instead of paying to classify it again.
        </p>
        <button class="btn small outline" disabled={scanning} onclick={runDiscovery}>
          {scanning ? "Scanning…" : "Run a pass now"}
        </button>
      {:else}
        <StateNote status={feeds.status("discovery")} noun="discovery status" />
      {/if}
    </Panel>

    <Panel title="Helius" status={feeds.status("helius")}
           meta={helius?.configured ? "connected" : "not configured"}>
      {#if helius}
        <div class="stat-list">
          <div class="stat"><span>Key</span>
            <b><Pill label={helius.configured ? "configured" : "missing"}
                     tone={helius.configured ? "good" : "bad"} /></b></div>
          {#each Object.entries(helius.metrics ?? {}).slice(0, 6) as [ep, m]}
            {@const mm = m as any}
            <div class="stat">
              <span>{ep}</span>
              <b class="num">{mm.calls} calls
                {#if mm.errors}<span class="pl-down"> · {mm.errors} err</span>{/if}
                {#if mm.total_ms && mm.calls}<span class="dim"> · {Math.round(mm.total_ms / mm.calls)}ms</span>{/if}
              </b>
            </div>
          {/each}
        </div>
      {:else}
        <StateNote status={feeds.status("helius")} noun="Helius health" />
      {/if}
    </Panel>

    <Panel title="Token Surge Scanner" status={feeds.status("surge")}
           meta="acceleration vs each token's own baseline — not size">
      {#if surge?.tokens?.length}
        <table class="tbl">
          <thead><tr>
            <th>Token</th><th class="num">Surge</th><th>Bias</th>
            <th class="num">5m vol</th><th class="num">Buys/Sells</th><th>Baseline</th>
          </tr></thead>
          <tbody>
            {#each surge.tokens.slice(0, 12) as t}
              <tr>
                <td class="sym">{(t.symbol ?? "").slice(0, 18)}</td>
                <td class="num"><b>{num(t.surge_score, 1)}</b></td>
                <td><Pill label={t.bias}
                          tone={t.bias === "bullish" ? "good" : t.bias === "bearish" ? "bad" : "neutral"} /></td>
                <td class="num">{usd(t.volume_m5)}</td>
                <td class="num">{t.buys_m5}/{t.sells_m5}</td>
                <td class="dim small">{t.baseline_quality}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="note">
          A token doing $2M every day is not news; one that went from $5k to
          $500k in an hour is. <b>baseline_quality</b> says whether the score
          came from measured history or — for a token too new to have any — an
          absolute-activity estimate, capped until a baseline exists.
        </p>
      {:else if surge?.errors?.length}
        <!-- "Nothing found" and "could not look" are different answers and
             only one of them is about the market. The upstream said 429;
             saying "no candidates" here would be the §4 failure this whole
             screen exists to avoid. -->
        <div class="degraded">
          <b>Could not scan.</b> The market source refused the request, so this
          is <em>unknown</em>, not empty:
          <ul>{#each surge.errors as e}<li>{e}</li>{/each}</ul>
        </div>
      {:else}
        <StateNote status={feeds.status("surge")} noun="surge candidates" />
      {/if}
    </Panel>

    <Panel title="Wallet Registry" status={feeds.status("wallets")}
           meta="{wallets?.wallets?.length ?? 0} shown · scores null until measured">
      {#if wallets?.wallets?.length}
        <table class="tbl">
          <thead><tr>
            <th>Wallet</th><th>Status</th><th>Entity</th><th>Source</th>
            <th class="num">Smart</th><th class="num">Alpha</th><th class="num">Copy</th>
          </tr></thead>
          <tbody>
            {#each wallets.wallets.slice(0, 25) as w}
              <tr>
                <td class="sym" title={w.address}>{short(w.address)}{#if w.pinned}<span class="pin" title="pinned seed">📌</span>{/if}</td>
                <td><Pill label={w.status} tone={w.status === "EXCLUDED_ENTITY" ? "neutral" : "warm"} /></td>
                <td class="dim small">{w.entity_name ?? w.entity_type ?? "—"}</td>
                <td class="dim small">{w.source}</td>
                <td class="num">{w.smart_money_score ?? "—"}</td>
                <td class="num">{w.alpha_score ?? "—"}</td>
                <td class="num">{w.copy_score ?? "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="note">{wallets.note}</p>
      {:else}
        <StateNote status={feeds.status("wallets")} noun="wallets" />
      {/if}
    </Panel>

    <Panel title="Kamino Liquidation Risk" meta="canonical decode · scan on demand">
      <button class="btn small outline" disabled={riskBusy} onclick={runRiskScan}>
        {riskBusy ? "Scanning obligations…" : "Scan lending positions"}
      </button>
      {#if riskScan}
        <div class="stat-list">
          <div class="stat"><span>Scanned</span><b class="num">{riskScan.scanned?.toLocaleString()}</b></div>
          <div class="stat"><span>With debt</span><b class="num">{riskScan.with_debt?.toLocaleString()}</b></div>
          <div class="stat"><span>Tracked</span><b class="num">{riskScan.tracked}</b></div>
          <div class="stat"><span>Debt at risk</span><b class="num">{usd(riskScan.at_risk_usd)}</b></div>
        </div>
        {#if riskScan.positions?.length}
          <table class="tbl">
            <thead><tr>
              <th>Owner</th><th>Collateral</th><th>Debt</th>
              <th class="num">Value</th><th class="num">Health</th><th>Risk</th>
            </tr></thead>
            <tbody>
              {#each riskScan.positions.slice(0, 12) as p}
                <tr>
                  <td class="sym" title={p.owner}>{short(p.owner)}</td>
                  <!-- Assets are NAMED now: reserve decoding resolves each
                       leg to its mint, decimals and oracle price. -->
                  <td class="assets">
                    {#each p.assets?.deposits ?? [] as d}
                      <span class="leg" class:unres={!d.resolved}>
                        {d.symbol ?? "UNRESOLVED"}
                        {#if d.resolved}<em>{num(d.amount, 4)}</em>{/if}
                      </span>
                    {:else}<span class="dim">—</span>{/each}
                  </td>
                  <td class="assets">
                    {#each p.assets?.borrows ?? [] as b}
                      <span class="leg" class:unres={!b.resolved}>
                        {b.symbol ?? "UNRESOLVED"}
                        {#if b.resolved}<em>{num(b.amount, 4)}</em>{/if}
                      </span>
                    {:else}<span class="dim">—</span>{/each}
                  </td>
                  <td class="num">{usd(p.collateral_value_usd)}</td>
                  <td class="num">{num(p.health_factor, 3)}</td>
                  <td><Pill label={p.risk_state} tone={riskTone(p.risk_state)} /></td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}

        {#if riskScan.by_asset?.by_family}
          <div class="sub">Exposure by correlated family</div>
          <table class="tbl">
            <thead><tr><th>Family</th><th class="num">Collateral</th><th class="num">Debt</th></tr></thead>
            <tbody>
              {#each Object.entries(riskScan.by_asset.by_family) as [fam, v]}
                {@const vv = v as any}
                <tr><td class="sym">{fam}</td>
                    <td class="num">{usd(vv.collateral_usd)}</td>
                    <td class="num">{usd(vv.debt_usd)}</td></tr>
              {/each}
            </tbody>
          </table>
          <p class="note">{riskScan.by_asset.note}</p>
        {/if}

        {#if riskScan.stress?.SOL_FAMILY?.ladder?.length}
          {@const L = riskScan.stress.SOL_FAMILY}
          <div class="sub">
            SOL-family price stress — {L.positions_considered} positions exposed
            {#if L.already_liquidatable}· {L.already_liquidatable} already liquidatable{/if}
          </div>
          <table class="tbl">
            <thead><tr>
              <th class="num">Shock</th><th class="num">Newly liquidatable</th>
              <th class="num">New debt</th><th class="num">Cumulative</th>
            </tr></thead>
            <tbody>
              {#each L.ladder as r}
                <tr class:hit={r.newly_liquidatable > 0}>
                  <td class="num">−{r.shock_pct}%</td>
                  <td class="num">{r.newly_liquidatable}</td>
                  <td class="num">{usd(r.newly_liquidatable_debt_usd)}</td>
                  <td class="num">{usd(r.cumulative_liquidatable_debt_usd)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <p class="note"><b>Scenario, not a forecast.</b> {L.basis}</p>
        {/if}
        <div class="prov">
          <b>Provenance.</b>
          Position values <span class="v">VERIFIED</span> — canonical Kamino layout,
          cross-checked against the official SDK and the public API.
          Health <span class="cx">CALCULATED</span> from Kamino's own rule.
          Forced-sale <span class="es">ESTIMATED</span> — debt value, not a market-impact model.
          Asset identity <span class="un">UNAVAILABLE</span> — reserve decoding not yet ported,
          so deposits and borrows are counted but not named.
        </div>
      {/if}
    </Panel>

    <Panel title="Protocol Registry" status={feeds.status("protocols")}
           meta="{protocols?.programs?.length ?? 0} verified on-chain">
      {#if protocols?.programs?.length}
        <div class="chips">
          {#each protocols.programs as p}
            <span class="chip" title="{p.program_id}">{p.name}<em>{p.category}</em></span>
          {/each}
        </div>
        <div class="chips lst">
          {#each protocols.lst_mints as m}
            <span class="chip lstc" title={m.mint}>{m.symbol}<em>{m.provider} · still SOL</em></span>
          {/each}
        </div>
        <p class="note">{protocols.note}</p>
      {:else}
        <StateNote status={feeds.status("protocols")} noun="protocol registry" />
      {/if}
    </Panel>
  </div>

  <!--
    The virtual DEX exchange, full width because it is a working surface
    rather than a readout. It replaces the read-only "Virtual DEX Book"
    panel that used to sit in the grid above — two surfaces for one book
    is the same duplication this whole pass exists to remove.
  -->
  <div class="exchange">
    <h2 class="sect">Virtual DEX Exchange</h2>
    <p class="note">
      AMM-priced against real pool depth. No leverage — a constant-product
      pool does not lend — and no short side, because you cannot borrow from
      one. Size is bounded by POOL DEPTH before equity: $25,000 into a
      $50,000 pool is 49.9% price impact, half the stake gone on entry
      before the trade is even wrong.
    </p>
    <DexExchange />
  </div>

  <!--
    The Kamino sweep and the stress matrix. Both engines existed with no
    route and no panel; §2's "stress matrix and sweep panels" is this.
  -->
  <div class="exchange">
    <h2 class="sect">Lending Risk — Sweep &amp; Stress</h2>
    <p class="note">
      The book ranked by significance rather than size, and the liquidation
      boundary on three independent axes. Certainty is not flat here: a
      decoded position is VERIFIED, a health factor CALCULATED, and carry,
      depeg and cascade MODELLED — the panel says which is which.
    </p>
    <LiquidationStress />
  </div>
</div>

<style>
  .oc { padding: 16px 20px; overflow-y: auto; }
  .exchange { margin-top: 20px; }
  .sect {
    font-size: 13px; text-transform: uppercase; letter-spacing: .09em;
    color: var(--muted); margin: 0 0 4px; font-weight: 600;
  }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); gap: 12px; align-items: start; }
  .stat-list { display: flex; flex-direction: column; gap: 5px; }
  .stat { display: flex; justify-content: space-between; align-items: center; gap: 12px; font-size: 12px; }
  .stat span { color: var(--ink-dim); }
  .tbl { width: 100%; border-collapse: collapse; font-size: 11.5px; margin-top: 8px; }
  .tbl th { text-align: left; color: var(--ink-faint); font-weight: 500; font-size: 10px;
            text-transform: uppercase; letter-spacing: 0.04em; padding: 4px 6px; border-bottom: 1px solid var(--line); }
  .tbl td { padding: 4px 6px; border-bottom: 1px solid var(--line); }
  .tbl .num, .num { text-align: right; font-family: var(--mono); }
  .sym { font-family: var(--mono); }
  .small { font-size: 10.5px; }
  .dim { color: var(--ink-dim); }
  .pin { margin-left: 4px; font-size: 9px; }
  .note { font-size: 11px; color: var(--ink-dim); line-height: 1.5; margin: 10px 0 0; }
  .btn.small { margin-top: 10px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .chip { display: inline-flex; flex-direction: column; gap: 1px; font-size: 10.5px;
          border: 1px solid var(--line-bright); border-radius: var(--radius-sm); padding: 3px 7px; }
  .chip em { font-style: normal; font-size: 9px; color: var(--ink-faint); }
  .chip.lstc { border-color: var(--accent-dim); }
  .chips.lst { margin-top: 8px; }
  /* Provenance is colour-coded AND worded — never colour alone. */
  .prov { margin-top: 10px; font-size: 11px; color: var(--ink-dim); line-height: 1.6;
          border-top: 1px solid var(--line); padding-top: 8px; }
  .prov .v { color: var(--good); font-family: var(--mono); font-size: 10px; }
  .prov .cx { color: var(--accent); font-family: var(--mono); font-size: 10px; }
  .prov .es { color: var(--warm); font-family: var(--mono); font-size: 10px; }
  .prov .un { color: var(--ink-faint); font-family: var(--mono); font-size: 10px; }
  .degraded { font-size: 11.5px; color: var(--warm); line-height: 1.6;
              border: 1px solid color-mix(in srgb, var(--warm) 30%, transparent);
              background: color-mix(in srgb, var(--warm) 7%, transparent);
              border-radius: var(--radius-sm); padding: 9px 11px; }
  .degraded ul { margin: 5px 0 0; padding-left: 18px; color: var(--ink-dim); }
  .degraded em { font-style: normal; color: var(--warm); }
  .sub { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
         color: var(--ink-faint); margin: 12px 0 2px; }
  .assets { display: flex; flex-wrap: wrap; gap: 4px; }
  .leg { display: inline-flex; align-items: baseline; gap: 3px; font-size: 10.5px;
         border: 1px solid var(--line-bright); border-radius: 3px; padding: 1px 5px; }
  .leg em { font-style: normal; font-family: var(--mono); color: var(--ink-dim); font-size: 9.5px; }
  .leg.unres { border-color: var(--warm); color: var(--warm); }
  tr.hit td { background: color-mix(in srgb, var(--bad) 8%, transparent); }
</style>
