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

  // Helius wallet SHADOW intelligence. Read-only evidence: classified
  // economic events, deterministic theses or truthful refusals, and forward
  // outcomes where a price actually exists. No order is ever submitted.
  let shadow = $state<any | null>(null);
  let shadowEvents = $state<any | null>(null);
  let shadowTheses = $state<any | null>(null);
  let reclassing = $state(false);

  // The bounded intelligence cycle that runs at the end of every wallet
  // poll: swap enrichment, wallet alpha, scoring, prices, classification,
  // theses and outcomes. Four sections because they fail INDEPENDENTLY and
  // the desk has to be able to say which one is holding everything up.
  let intel = $state<any | null>(null);
  let cycling = $state(false);

  async function loadAll() {
    // NO third argument. `load(key, fn, opts?)` takes `{ keepLast }` — the
    // old signature took the caller's previous value, and this call site
    // still passed its own `$state` there. Two things followed: the opts
    // object was nonsense, and worse, reading those six variables inside a
    // function called from an `$effect` that then WRITES all six made the
    // effect re-trigger itself. That is the measured 34-requests-in-10s
    // storm — fixed inside FeedTracker, still live here.
    const [w, d, s, b, p, h, sh, se, st, ic] = await Promise.all([
      feeds.load("wallets", () => api.raw<any>("/onchain/wallets?limit=60")),
      feeds.load("discovery", () => api.raw<any>("/onchain/discovery/status")),
      feeds.load("surge", () => api.raw<any>("/onchain/surge?limit=15")),
      feeds.load("book", () => api.dexBook()),
      feeds.load("protocols", () => api.raw<any>("/onchain/protocols")),
      feeds.load("helius", () => api.raw<any>("/helius/health")),
      feeds.load("shadow", () => api.raw<any>("/onchain/shadow/summary")),
      feeds.load("shadowEvents", () => api.raw<any>("/onchain/shadow/events?limit=40")),
      feeds.load("shadowTheses", () => api.raw<any>("/onchain/shadow/theses?limit=25")),
      feeds.load("intelCycle", () => api.raw<any>("/onchain/intel/cycle")),
    ]);
    wallets = w; discovery = d; surge = s; book = b; protocols = p; helius = h;
    shadow = sh; shadowEvents = se; shadowTheses = st; intel = ic;
  }

  async function runCycle() {
    if (cycling) return;
    cycling = true;
    try {
      const r = await api.rawPost<any>("/onchain/intel/cycle/run");
      toastStore.ok(
        `Cycle ${r.result} in ${r.duration_seconds}s — ` +
        `${r.signatures_enriched ?? "—"} enriched, ` +
        `${r.price_snapshots ?? "—"} prices, ` +
        `${r.theses_created ?? "—"} theses`,
      );
      await loadAll();
    } catch (e: any) {
      toastStore.err(String(e?.message ?? e));
    } finally {
      cycling = false;
    }
  }

  async function reclassify() {
    if (reclassing) return;
    reclassing = true;
    try {
      const r = await api.rawPost<any>("/onchain/shadow/process");
      toastStore.ok(
        `Re-classified ${r.events} events into ${r.clusters} observations — ` +
        `${r.eligible} eligible, ${r.refused} refused`,
      );
      await loadAll();
    } catch (e) {
      toastStore.err(`Re-classification failed: ${e}`);
    } finally {
      reclassing = false;
    }
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
    <KpiTile label="DEX Equity" value={usd(book?.equity_executable_usd)} period="executable — what the book could realise" />
    <KpiTile label="Observations"
             value={shadow ? num(shadow.observations?.market_observations, 0) : "—"}
             period="market events, after leg + copy suppression" />
    <KpiTile label="Shadow Theses"
             value={shadow ? String(shadow.eligible ?? 0) : "—"}
             period="eligible — no order submitted" />
    <KpiTile label="Refused"
             value={shadow ? num(shadow.refused, 0) : "—"}
             period="evidence missing, reasons kept" />
  </div>

  <!-- ── HELIUS WALLET SHADOW INTELLIGENCE ───────────────────────────────
       Read-only. Every panel below distinguishes "we looked and found
       nothing" from "we could not look", and labels every thesis as
       SHADOW so nothing here can be mistaken for an order. -->
  <div class="grid">
    <Panel title="Helius Wallet Polling" status={feeds.status("shadow")}
           meta={shadow?.polling?.enabled ? "read-only · on a timer" : "disabled"}>
      {#if shadow?.polling}
        {@const p = shadow.polling}
        <div class="stat-list">
          <div class="stat"><span>Polling</span>
            <b><Pill label={p.enabled ? (p.running ? "ENABLED · RUNNING" : "ENABLED · IDLE") : "DISABLED"}
                     tone={p.enabled && p.running ? "good" : p.enabled ? "warm" : "neutral"} /></b></div>
          <div class="stat"><span>Interval</span><b class="num">{p.interval_seconds}s</b></div>
          <div class="stat"><span>Watched wallets</span>
            <b class="num">{p.subsystem?.wallets_watched ?? "—"}</b></div>
          <div class="stat"><span>Last completed</span>
            <b class="num">{p.last_completed_at ? new Date(p.last_completed_at).toLocaleTimeString() : "—"}</b></div>
          <div class="stat"><span>Next run</span>
            <b class="num">{p.next_run_at && p.next_run_at !== "imminent"
              ? new Date(p.next_run_at).toLocaleTimeString() : (p.next_run_at ?? "—")}</b></div>
          <div class="stat"><span>Last result</span>
            <b><Pill label={p.last_result ?? "—"}
                     tone={p.last_result === "POLL_OK" ? "good"
                           : p.last_result === "POLL_FAILED" ? "bad" : "neutral"} /></b></div>
          <!-- MISSING IS NOT ZERO: a pass that could not look reports null. -->
          <div class="stat"><span>Observed / inserted / deduped</span>
            <b class="num">{p.observed ?? "—"} / {p.inserted ?? "—"} / {p.deduplicated ?? "—"}</b></div>
          <div class="stat"><span>Provider calls</span><b class="num">{p.provider_calls ?? "—"}</b></div>
          <div class="stat"><span>Polls completed</span><b class="num">{p.polls_completed}</b></div>
          <div class="stat"><span>Overlaps refused</span><b class="num">{p.polls_refused_overlapping}</b></div>
        </div>
        {#if p.last_error}
          <div class="degraded" style="margin-top:10px">
            <em>Last bounded error</em> — {p.last_error}
          </div>
        {/if}
        <p class="note">
          JARVIS POLLS; nothing listens. No webhook, no queue, no inbound
          host. Counts read “—” when a pass did not look: a quiet chain and
          an unreachable provider are different facts.
        </p>
      {:else}
        <StateNote status={feeds.status("shadow")} noun="Helius polling status" />
      {/if}
    </Panel>

    <Panel title="Intelligence Cycle" status={feeds.status("intelCycle")}
           meta="one bounded pass per wallet poll">
      {#if intel?.cycle}
        {@const c = intel.cycle}
        <div class="stat-list">
          <div class="stat"><span>Cycle</span>
            <b><Pill label={c.enabled ? (c.running ? "RUNNING" : "ENABLED · IDLE") : "DISABLED"}
                     tone={c.enabled && c.running ? "good" : c.enabled ? "warm" : "neutral"} /></b></div>
          <div class="stat"><span>Last result</span>
            <b><Pill label={c.last_result ?? "NOT YET RUN"}
                     tone={c.last_result === "CYCLE_OK" ? "good"
                           : c.last_result === "CYCLE_PARTIAL" ? "warm"
                           : c.last_result === "CYCLE_FAILED" ? "bad" : "neutral"} /></b></div>
          <div class="stat"><span>Current stage</span>
            <b class="num">{c.current_stage ?? "—"}</b></div>
          <div class="stat"><span>Started / completed</span>
            <b class="num">{c.last_started_at ? new Date(c.last_started_at).toLocaleTimeString() : "—"}
              / {c.last_completed_at ? new Date(c.last_completed_at).toLocaleTimeString() : "—"}</b></div>
          <div class="stat"><span>Duration</span>
            <b class="num">{c.last_duration_seconds ?? "—"}s</b></div>
          <div class="stat"><span>Next cycle</span>
            <b class="num">{c.next_cycle_at && c.next_cycle_at !== "imminent"
              ? new Date(c.next_cycle_at).toLocaleTimeString() : (c.next_cycle_at ?? "—")}</b></div>
          <div class="stat"><span>Transfers collected</span>
            <b class="num">{c.transfers_collected ?? "—"}</b></div>
          <div class="stat"><span>Signatures considered / answered</span>
            <b class="num">{c.signatures_considered ?? "—"} / {c.signatures_answered ?? "—"}</b></div>
          <div class="stat"><span>— established as trades</span>
            <b class="num">{c.signatures_enriched ?? "—"}</b></div>
          <div class="stat"><span>— proven NOT a trade</span>
            <b class="num">{c.signatures_refused_non_trading ?? "—"}</b></div>
          <div class="stat"><span>Enrichment failures</span>
            <b class="num">{c.enrichment_failures ?? "—"}</b></div>
          <div class="stat"><span>Wallets rescored</span>
            <b class="num">{c.wallets_rescored ?? "—"}</b></div>
          <div class="stat"><span>Price snapshots</span>
            <b class="num">{c.price_snapshots ?? "—"}</b></div>
          <div class="stat"><span>Events processed</span>
            <b class="num">{c.events_processed ?? "—"}</b></div>
          <div class="stat"><span>Reclassified / superseded</span>
            <b class="num">{c.events_reclassified ?? "—"} / {c.events_superseded ?? "—"}</b></div>
          <div class="stat"><span>Theses created</span>
            <b class="num">{c.theses_created ?? "—"}</b></div>
          <div class="stat"><span>Outcomes resolved</span>
            <b class="num">{c.outcomes_resolved ?? "—"}</b></div>
          <div class="stat"><span>Cycles completed / failed</span>
            <b class="num">{c.cycles_completed} / {c.cycles_failed}</b></div>
        </div>
        {#if c.stages && Object.keys(c.stages).length}
          <div class="sub">Stages</div>
          <div class="chips">
            {#each (c.stage_order ?? []) as s2}
              {@const st2 = c.stages[s2]}
              <span class="chip"><b>{st2?.state ?? "—"}</b><em>{s2}</em></span>
            {/each}
          </div>
        {/if}
        {#if c.last_error}
          <div class="degraded" style="margin-top:10px">
            <em>Bounded stage error</em> — {c.last_error}
          </div>
        {/if}
        <p class="note">
          NOT A SCHEDULER. The cycle owns no timer: it runs at the END of
          each wallet poll, so the next cycle is the next poll. A stage that
          fails is recorded and the rest still run — an unreachable price
          provider must not stop outcome resolution that needs no provider.
          A “—” means that stage did not look.
        </p>
        <button class="btn small outline" disabled={cycling} onclick={runCycle}>
          {cycling ? "Running…" : "Run one cycle now (diagnostic)"}
        </button>
      {:else}
        <StateNote status={feeds.status("intelCycle")} noun="intelligence cycle status" />
      {/if}
    </Panel>

    <Panel title="Swap Evidence" status={feeds.status("intelCycle")}
           meta="full-transaction balance deltas">
      {#if intel?.swap_evidence?.state === "MEASURED"}
        {@const s3 = intel.swap_evidence}
        <div class="stat-list">
          <div class="stat"><span>Pending enrichment</span>
            <b class="num">{s3.pending_candidates ?? "—"}</b></div>
          <div class="stat"><span>Enriched</span>
            <b class="num">{s3.by_state?.ENRICHED ?? 0}</b></div>
          <div class="stat"><span>Partial (unvaluable swap)</span>
            <b class="num">{s3.by_state?.PARTIAL ?? 0}</b></div>
          <div class="stat"><span>Retryable failures</span>
            <b class="num">{s3.by_state?.RETRYABLE_FAILURE ?? 0}</b></div>
          <div class="stat"><span>Permanently unresolved</span>
            <b class="num">{s3.by_state?.PERMANENTLY_UNRESOLVED ?? 0}</b></div>
          <div class="stat"><span>Refused — not a trade</span>
            <b class="num">{s3.by_state?.REFUSED_NON_TRADING ?? 0}</b></div>
          <div class="stat"><span>Classified buys / sells</span>
            <b class="num">{s3.classified_buys} / {s3.classified_sells}</b></div>
          <div class="stat"><span>Budget — signatures / calls</span>
            <b class="num">{s3.budget_signatures} / {s3.budget_calls}</b></div>
          <div class="stat"><span>Max attempts</span>
            <b class="num">{s3.max_attempts}</b></div>
        </div>
        <p class="note">
          A FAILED TRANSACTION IS NOT A TRADE. The transfers feed reports the
          attempted movements and no error, so only the transaction’s own
          pre/post balances can tell a completed swap from one that reverted,
          a token inflow with no payment from a purchase, or an LP deposit
          from a sale. Each of those lands as REFUSED — a stored answer, not
          an absence, so the signature is never bought twice.
        </p>
      {:else}
        <StateNote status={feeds.status("intelCycle")} noun="swap evidence" />
      {/if}
    </Panel>

    <Panel title="Wallet-Score Coverage" status={feeds.status("intelCycle")}
           meta="an unproven wallet is not a neutral one">
      {#if intel?.wallet_scoring?.state === "MEASURED"}
        {@const w2 = intel.wallet_scoring}
        <div class="stat-list">
          <div class="stat"><span>Registry wallets</span>
            <b class="num">{num(w2.registry_wallets, 0)}</b></div>
          <div class="stat"><span>Watched wallets</span>
            <b class="num">{num(w2.watched_wallets, 0)}</b></div>
          <div class="stat"><span>Wallets scored</span>
            <b class="num">{num(w2.scored, 0)}</b></div>
          <div class="stat"><span>Score coverage</span>
            <b class="num">{w2.coverage_pct ?? "—"}%</b></div>
          <div class="stat"><span>Insufficient evidence</span>
            <b class="num">{num(w2.insufficient_evidence, 0)}</b></div>
          <div class="stat"><span>With resolved samples</span>
            <b class="num">{num(w2.with_resolved_samples, 0)}</b></div>
          <div class="stat"><span>Never analysed</span>
            <b class="num">{num(w2.never_analysed, 0)}</b></div>
          <div class="stat"><span>Provider failures</span>
            <b class="num">{num(w2.failed, 0)}</b></div>
          <div class="stat"><span>Round trips required</span>
            <b class="num">{w2.min_trades_for_score}</b></div>
          <div class="stat"><span>Score version</span>
            <b class="num">{w2.score_version}</b></div>
          <div class="stat"><span>Last scoring update</span>
            <b class="num">{w2.last_scoring_update
              ? new Date(w2.last_scoring_update).toLocaleString() : "—"}</b></div>
        </div>
        {#if w2.by_measurability_reason && Object.keys(w2.by_measurability_reason).length}
          <div class="sub">Why not measurable</div>
          <div class="chips">
            {#each Object.entries(w2.by_measurability_reason) as [r2, n2]}
              <span class="chip"><b>{n2}</b><em>{r2}</em></span>
            {/each}
          </div>
        {/if}
        <p class="note">
          BOOTSTRAPPED FROM {w2.bootstrap_population}, not from JARVIS
          theses. A wallet’s own entries and what the token did next are one
          population; how a JARVIS thesis derived from that wallet performed
          is a different one, measured separately and never fed back. That
          separation is what stops “needs a score to make a thesis, needs a
          thesis to get a score”. An unscored wallet stays UNKNOWN — it is
          never given a neutral score to get an event through the gate.
        </p>
      {:else}
        <StateNote status={feeds.status("intelCycle")} noun="wallet-score coverage" />
      {/if}
    </Panel>

    <Panel title="Price Coverage" status={feeds.status("intelCycle")}
           meta="exact mints · missing stays missing">
      {#if intel?.price_coverage?.state === "MEASURED"}
        {@const pc = intel.price_coverage}
        <div class="stat-list">
          <div class="stat"><span>Event mints</span>
            <b class="num">{num(pc.event_mints, 0)}</b></div>
          <div class="stat"><span>Priced mints</span>
            <b class="num">{num(pc.priced_mints, 0)}</b></div>
          <div class="stat"><span>Fresh (within {pc.fresh_window_seconds}s)</span>
            <b class="num">{num(pc.fresh_mints, 0)}</b></div>
          <div class="stat"><span>Stale mints</span>
            <b class="num">{num(pc.stale_mints, 0)}</b></div>
          <div class="stat"><span>Unpriced mints</span>
            <b class="num">{num(pc.unpriced_mints, 0)}</b></div>
          <div class="stat"><span>Pending collection</span>
            <b class="num">{pc.pending_mints ?? "—"}</b></div>
          <div class="stat"><span>Due checkpoints</span>
            <b class="num">{num(pc.due_checkpoints, 0)}</b></div>
          <div class="stat"><span>Resolved checkpoints</span>
            <b class="num">{num(pc.resolved_checkpoints, 0)}</b></div>
          <div class="stat"><span>Unresolved checkpoints</span>
            <b class="num">{num(pc.unresolved_checkpoints, 0)}</b></div>
          <div class="stat"><span>Snapshot rows</span>
            <b class="num">{num(pc.snapshot_rows, 0)}</b></div>
          <div class="stat"><span>Last collection</span>
            <b class="num">{pc.last_snapshot_at
              ? new Date(pc.last_snapshot_at).toLocaleString() : "—"}</b></div>
        </div>
        {#if intel?.quote_series?.series?.length}
          <div class="sub">Quote assets</div>
          <div class="chips">
            {#each intel.quote_series.series as qs}
              <span class="chip"><b>{qs.state}</b><em>{qs.symbol}
                {qs.age_hours != null ? `· ${qs.age_hours}h old` : ""}</em></span>
            {/each}
          </div>
        {/if}
        <p class="note">
          A TICKER IS NOT A MINT and today’s price is not the price then.
          Snapshots are requested for exact mints — due checkpoints first,
          then new event references — and a mint the provider does not cover
          produces NO ROW rather than a zero. The quote series above is
          separate and it gates everything: a SOL-quoted round trip cannot be
          valued at all while SOL itself is stale, which is why no wallet
          could be scored while that series sat 39.8 hours behind.
        </p>
      {:else}
        <StateNote status={feeds.status("intelCycle")} noun="price coverage" />
      {/if}
    </Panel>

    <Panel title="Wallet Intelligence — Classification" status={feeds.status("shadow")}
           meta="transfers → economic events → market observations">
      {#if shadow?.observations}
        {@const o = shadow.observations}
        <div class="stat-list">
          <div class="stat"><span>Transfer legs stored</span><b class="num">{num(o.transfer_legs, 0)}</b></div>
          <div class="stat"><span>Signatures (economic events)</span><b class="num">{num(o.signatures, 0)}</b></div>
          <div class="stat"><span>Market observations</span><b class="num">{num(o.market_observations, 0)}</b></div>
          <div class="stat"><span>Legs per observation</span><b class="num">{num(o.legs_per_observation, 2)}</b></div>
          <div class="stat"><span>Signatures per observation</span><b class="num">{num(o.signatures_per_observation, 2)}</b></div>
        </div>
        <div class="sub">By classification</div>
        <div class="chips">
          {#each shadow.by_classification ?? [] as c2}
            <span class="chip"><b>{num(c2.count, 0)}</b><em>{c2.classification}</em></span>
          {/each}
        </div>
        <div class="sub">Event types</div>
        <div class="chips">
          {#each (shadow.by_event_type ?? []).slice(0, 8) as t}
            <span class="chip"><b>{num(t.count, 0)}</b><em>{t.event_type}</em></span>
          {/each}
        </div>
        <p class="note">
          MULTIPLE LEGS ARE NOT MULTIPLE VOTES. A routed swap arrives as
          seven or eight transfers; wallets acting on the same token inside
          {shadow.policy?.cluster_window_seconds ?? 900}s collapse again.
          Both ratios above are that suppression, measured.
        </p>
        <button class="btn small outline" disabled={reclassing} onclick={reclassify}>
          {reclassing ? "Re-classifying…" : "Re-classify stored observations"}
        </button>
      {:else}
        <StateNote status={feeds.status("shadow")} noun="classification summary" />
      {/if}
    </Panel>

    <Panel title="Shadow Theses" status={feeds.status("shadowTheses")}
           meta="deterministic eligibility · measured, never acted on">
      <div class="shadowbar">SHADOW INTELLIGENCE — NO ORDER SUBMITTED</div>
      {#if shadowTheses?.theses?.length}
        <table class="tbl">
          <thead><tr>
            <th>Token</th><th>Dir</th><th class="num">Ref price</th>
            <th class="num">Notional</th><th>Wallets</th><th>Outcomes</th>
          </tr></thead>
          <tbody>
            {#each shadowTheses.theses as t}
              <tr>
                <td class="sym" title={t.mint ?? ""}>{t.symbol ?? t.mint_abbrev}</td>
                <td><Pill label={t.direction ?? "—"}
                          tone={t.direction === "BUY" ? "good" : "bad"} /></td>
                <td class="num">{t.reference_price_usd == null ? "UNKNOWN"
                  : `$${Number(t.reference_price_usd).toPrecision(4)}`}</td>
                <td class="num">{usd(t.notional_usd)}</td>
                <td class="num">{t.wallet_count}</td>
                <td>
                  <div class="assets">
                    {#each t.outcomes ?? [] as o}
                      <span class="leg" class:unres={o.status !== "RESOLVED"}>
                        {o.horizon}
                        <em>{o.status === "RESOLVED"
                          ? `${Number(o.net_return_pct).toFixed(1)}% net`
                          : "UNRESOLVED"}</em>
                      </span>
                    {/each}
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else if shadow}
        <!-- NOT an error, and not a blank panel. Every event was examined
             and refused, and the reasons are in the next panel. -->
        <div class="degraded">
          <em>No eligible thesis</em> — all
          {num(shadow.refused, 0)} market observations were examined and
          refused. This is the gate working, not a failure: the reasons are
          listed under Refusals, and nothing was fabricated to fill this
          table.
        </div>
      {:else}
        <StateNote status={feeds.status("shadowTheses")} noun="shadow theses" />
      {/if}
      <p class="note">
        Shadow means shadow. These are measurements of what watching these
        wallets would have been worth — never permission to act, and never
        pooled with JARVIS execution, manual operator or virtual-book results.
      </p>
    </Panel>

    <Panel title="Refusals" status={feeds.status("shadow")}
           meta="why an observation did not become a thesis">
      {#if shadow?.by_refusal_reason?.length}
        <table class="tbl">
          <thead><tr><th>Reason</th><th class="num">Count</th></tr></thead>
          <tbody>
            {#each shadow.by_refusal_reason as r}
              <tr>
                <td class="sym">{r.reason}</td>
                <td class="num"><b>{num(r.count, 0)}</b></td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="note">
          Refusals are kept, not discarded — they measure classification
          quality and name exactly which evidence is missing. UNKNOWN wallet
          quality is a refusal, not a neutral score: an unproven wallet does
          not get the benefit of the doubt.
        </p>
      {:else if shadow}
        <StateNote status={feeds.status("shadow")} noun="refusals"
                   emptyText="No refusals recorded — every observation passed the gate." />
      {:else}
        <StateNote status={feeds.status("shadow")} noun="refusals" />
      {/if}
    </Panel>

    <Panel title="Forward Outcomes" status={feeds.status("shadow")}
           meta="gross AND net of an assumed round trip">
      {#if shadow && Object.keys(shadow.horizons ?? {}).length}
        <table class="tbl">
          <thead><tr>
            <th>Horizon</th><th class="num">Resolved</th>
            <th class="num">Unresolved</th><th class="num">Gross</th>
            <th class="num">Net</th><th>Sample</th>
          </tr></thead>
          <tbody>
            {#each Object.entries(shadow.horizons) as [h, cell]}
              {@const v = cell as any}
              <tr>
                <td class="sym">{h}</td>
                <td class="num">{v.resolved}</td>
                <td class="num">{v.unresolved}</td>
                <td class="num">{v.gross_return_pct == null ? "—" : `${v.gross_return_pct}%`}</td>
                <td class="num">{v.net_return_pct == null ? "—" : `${v.net_return_pct}%`}</td>
                <td>
                  <Pill label={v.sample_sufficient ? "sufficient" : "too few"}
                        tone={v.sample_sufficient ? "good" : "warm"} />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else if shadow}
        <div class="degraded">
          <em>No forward outcomes yet</em> — checkpoints are created when a
          thesis is eligible, and there are
          {num(shadow.eligible, 0)}. UNRESOLVED is never counted as a loss.
        </div>
      {:else}
        <StateNote status={feeds.status("shadow")} noun="forward outcomes" />
      {/if}
      <p class="note">
        Returns are shown before AND after an assumed
        {shadow?.estimated_round_trip_cost_pct ?? 3}% on-chain round trip.
        Observed profit before costs is not edge, and no expectancy is stated
        below {shadow?.min_sample_for_expectancy ?? 20} resolved observations.
      </p>
    </Panel>

    <Panel title="Recent Classified Activity" status={feeds.status("shadowEvents")}
           meta="real Helius observations — safe labels only">
      {#if shadowEvents?.events?.length}
        <!-- Seven columns do not fit a 557px panel at this breakpoint.
             The table scrolls inside its own container rather than pushing
             the page sideways — measured, not assumed. -->
        <div class="tblwrap">
        <table class="tbl">
          <thead><tr>
            <th>Time</th><th>Type</th><th>Token</th><th class="num">Amount</th>
            <th>Wallets</th><th>Legs</th><th>Verdict</th>
          </tr></thead>
          <tbody>
            {#each shadowEvents.events.slice(0, 25) as e}
              <tr>
                <td class="num small">{e.event_time
                  ? new Date(e.event_time).toLocaleString(undefined,
                      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
                  : "—"}</td>
                <td class="sym small">{e.event_type}</td>
                <!-- Abbreviated by default. The backend nulls `symbol` when
                     it is really the mint, so this can never widen into a
                     full 44-character address. -->
                <td class="sym small" title={e.classification_reason ?? ""}>
                  {e.symbol ?? e.mint_abbrev ?? "UNKNOWN"}</td>
                <td class="num">{e.subject_amount == null ? "UNKNOWN" : num(e.subject_amount, 2)}</td>
                <td class="num">{e.wallet_count || "—"}</td>
                <td class="num">{e.leg_count}</td>
                <td>
                  <Pill label={e.state === "ELIGIBLE" ? "ELIGIBLE" : (e.refusal_reason ?? "REFUSED")}
                        tone={e.state === "ELIGIBLE" ? "good" : "neutral"} />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        </div>
        <p class="note">
          Wallets appear as counts and safe labels; full addresses and mints
          are never rendered by default. A transfer with no paired swap leg
          stays UNKNOWN_TRANSFER rather than being called a trade.
        </p>
      {:else}
        <StateNote status={feeds.status("shadowEvents")} noun="classified events"
                   emptyText="No classified events yet — run a re-classification pass." />
      {/if}
    </Panel>
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
  /* The one thing that must never be missed on this panel. Worded, not
     colour alone — a shadow thesis read as an order is the worst possible
     misreading of this screen. */
  /* Wide content scrolls in its own box; the page never scrolls sideways. */
  .tblwrap { overflow-x: auto; }
  .shadowbar { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
               color: var(--accent); border: 1px solid var(--accent-dim);
               background: color-mix(in srgb, var(--accent) 8%, transparent);
               border-radius: var(--radius-sm); padding: 5px 9px; margin-bottom: 9px;
               text-align: center; font-weight: 600; }
</style>
