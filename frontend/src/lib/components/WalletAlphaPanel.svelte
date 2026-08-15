<script lang="ts">
  // Wallet Alpha — the Helius intelligence that existed with no way to see it.
  //
  // `lib/wallet_intel.py` has been computing whale scores, exchange flow,
  // funder clusters and coordination since Phase 7, and `lib/token_pricing.py`
  // has been resolving USD value for it. Neither had a route or a surface, so
  // the desk's answer to "is anything moving on-chain?" was to open a Python
  // prompt. A measurement nobody can see does not change any decision.
  //
  // Two things this panel refuses to do:
  //   §116 — it never implies these records feed the majors book. The
  //          boundary is printed, not assumed.
  //   §117 — it never shows a wallet count without the independent cluster
  //          count beside it. Three addresses controlled by one actor are
  //          one opinion, and a panel that prints "3 wallets" is helping
  //          manufacture the consensus it claims to detect.
  import Panel from "./Panel.svelte";
  import Pill from "./Pill.svelte";
  import StateNote from "./StateNote.svelte";
  import { api, type WalletIntel } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();

  let intel = $state<WalletIntel | null>(null);
  let running = $state(false);

  // Explicitly NOT polled. Each run makes bounded live Helius calls
  // (transfers per wallet, one batched identity, one funded-by per wallet);
  // putting that on a 30s timer would spend the plan's quota on a tab
  // nobody is reading.
  async function run() {
    running = true;
    intel = await feeds.load("intel", () => api.walletIntel(100));
    running = false;
  }

  const short = (a: string | null | undefined, n = 4) =>
    !a ? "—" : a.length <= n * 2 + 1 ? a : `${a.slice(0, n)}…${a.slice(-n)}`;
  const usd = (v: number | null | undefined) =>
    v == null ? null : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  const qty = (v: number) =>
    v.toLocaleString(undefined, { maximumFractionDigits: v < 1 ? 6 : 2 });
  const when = (ts: number | null) =>
    ts == null ? "—" : new Date(ts * 1000).toLocaleString();

  const notConfigured = $derived(intel != null && intel.configured === false);
</script>

<Panel
  title="Wallet Alpha (Helius)"
  meta={intel?.configured
    ? `${intel.transfers ?? 0} transfers · ${intel.wallets_queried ?? 0} wallets`
    : ""}
  status={feeds.status("intel")}
>
  <div class="bar">
    <button class="btn tiny" disabled={running} onclick={run}>
      {running ? "Reading chain…" : intel ? "Re-run" : "Run analysis"}
    </button>
    <span class="hint">
      Live Helius calls, bounded — deliberately on a button, not a poll.
    </span>
  </div>

  {#if !intel && !running}
    <StateNote
      status={feeds.status("intel")}
      noun="wallet intelligence"
      emptyText="Not run yet — the analysis spends live API calls, so it waits to be asked."
    />
  {:else if notConfigured}
    <!-- A configuration state, not a failure, and the two are worth telling
         apart: an empty watchlist is one env var away from working. -->
    <div class="note warn">
      <b>Not configured.</b>
      {intel?.detail}
      {#if intel?.has_key}
        Set <code>HELIUS_WATCH_WALLETS</code> to a comma-separated list of
        Solana addresses to follow.
      {:else}
        Set <code>HELIUS_API_KEY</code>.
      {/if}
    </div>
  {:else if intel}
    {#if intel.errors?.length}
      <div class="note bad">
        {#each intel.errors as e (e)}<div>{e}</div>{/each}
      </div>
    {/if}

    {#if intel.wallets_truncated}
      <div class="note warn">
        Only {intel.wallets_queried} of {intel.wallets_watched} watched wallets
        were queried — the rest were dropped to bound API spend. Their activity
        is NOT in the numbers below.
      </div>
    {/if}

    <!-- §117, front and centre. Both numbers or neither. -->
    {#if intel.independence}
      <div class="independence">
        <div class="ind-fig">
          <span class="ind-n">{intel.independence.raw_wallets}</span>
          <span class="ind-l">raw wallets</span>
        </div>
        <span class="ind-arrow">→</span>
        <div class="ind-fig strong">
          <span class="ind-n">{intel.independence.independent_clusters}</span>
          <span class="ind-l">independent clusters</span>
        </div>
        {#if intel.independence.collapsed}
          <span class="ind-note">
            {intel.independence.collapsed} collapsed as related — that many
            fewer independent opinions than addresses
          </span>
        {:else}
          <span class="ind-note">nothing collapsed: no shared-funder evidence among these wallets</span>
        {/if}
      </div>
    {/if}

    {#if intel.pricing}
      <div class="pricing">
        <b>USD coverage</b>
        <span class="pr-main">{intel.pricing.priced_pct}%</span>
        <span class="muted">
          {intel.pricing.priced} of {intel.pricing.total} priced ·
          {intel.pricing.unpriced} abstained
        </span>
        {#each Object.entries(intel.pricing.by_source) as [src, n] (src)}
          <Pill label="{src} {n}" tone="neutral" />
        {/each}
        <span class="muted small">
          Unpriced transfers are excluded from USD sizing rather than valued at
          a guess — the whale floor needs a real number to measure against.
        </span>
      </div>
    {/if}

    {#if intel.coordination}
      <div class="coord" class:live={intel.coordination.score > 0}>
        <div class="coord-head">
          <b>Coordination</b>
          <span class="coord-score">{intel.coordination.score}</span>
          {#if intel.coordination.independent_clusters != null}
            <span class="muted">
              {intel.coordination.independent_clusters} independent clusters
              ({intel.coordination.raw_wallets} wallets)
            </span>
          {/if}
        </div>
        {#each intel.coordination.reasons as r (r)}
          <div class="muted small">{r}</div>
        {/each}
      </div>
    {/if}

    {#if intel.whales?.length}
      <h4>Largest moves <span class="muted">— absolute floor and per-wallet anomaly, scored separately</span></h4>
      <table class="tbl">
        <thead>
          <tr><th>Score</th><th>Wallet</th><th>Dir</th><th>Token</th><th class="r">Amount</th><th class="r">USD</th><th>Why</th></tr>
        </thead>
        <tbody>
          {#each intel.whales.slice(0, 12) as w (w.signature + w.mint + w.counterparty)}
            <tr>
              <td><Pill label={String(w.whale.score)} tone={w.whale.is_whale ? "warm" : "neutral"} /></td>
              <td class="mono" title={w.wallet}>{short(w.wallet)}</td>
              <td><Pill label={w.direction} tone={w.direction === "in" ? "good" : "bad"} /></td>
              <td class="mono" title={w.mint ?? ""}>{short(w.symbol, 5)}</td>
              <td class="r mono">{qty(w.amount)}</td>
              <td class="r mono">{usd(w.usd_value) ?? "unpriced"}</td>
              <td class="why">{w.whale.reasons.join(" · ")}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    {#if intel.exchange_flows?.length}
      <h4>Exchange flow <span class="muted">— a deposit is a precondition for selling, not proof of it</span></h4>
      <table class="tbl">
        <thead>
          <tr><th>Flow</th><th>Exchange</th><th>Wallet</th><th>Token</th><th class="r">Amount</th><th>When</th></tr>
        </thead>
        <tbody>
          {#each intel.exchange_flows.slice(0, 10) as f (f.signature + f.exchange_address)}
            <tr title={f.implication}>
              <td>
                <Pill
                  label={f.flow === "exchange_inflow" ? "deposit" : "withdrawal"}
                  tone={f.flow === "exchange_inflow" ? "warm" : "neutral"}
                />
              </td>
              <td>{f.exchange}</td>
              <td class="mono" title={f.wallet}>{short(f.wallet)}</td>
              <td class="mono">{short(f.symbol, 5)}</td>
              <td class="r mono">{qty(f.amount)}</td>
              <td class="mono small">{when(f.timestamp)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    {#if intel.clusters?.length}
      <h4>Funder clusters</h4>
      {#each intel.clusters as c (c.funder)}
        <div class="cluster">
          <div class="cl-head">
            <Pill
              label="conf {c.confidence}"
              tone={c.confidence >= 0.4 ? "warm" : "neutral"}
            />
            <span class="mono">{c.funder_name || short(c.funder, 6)}</span>
            <span class="muted">{c.funder_type ?? "unknown"} · {c.size} wallets</span>
            {#if c.is_infrastructure_funder}
              <Pill label="infrastructure" tone="neutral" />
            {/if}
          </div>
          {#each c.reasons as r (r)}<div class="muted small">{r}</div>{/each}
        </div>
      {/each}
    {/if}

    {#if intel.boundary_note}
      <div class="boundary">{intel.boundary_note}</div>
    {/if}
  {/if}
</Panel>

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }
  .hint,
  .muted {
    color: var(--ink-faint);
    font-size: 11px;
  }
  .small {
    font-size: 10.5px;
  }
  .note {
    padding: 8px 10px;
    border-radius: var(--radius-sm);
    font-size: 11.5px;
    margin-bottom: 10px;
    line-height: 1.5;
  }
  .note.warn {
    border: 1px solid color-mix(in srgb, var(--warm) 32%, transparent);
    background: color-mix(in srgb, var(--warm) 8%, transparent);
    color: var(--ink-dim);
  }
  .note.bad {
    border: 1px solid color-mix(in srgb, var(--bad) 32%, transparent);
    background: color-mix(in srgb, var(--bad) 8%, transparent);
    color: var(--ink-dim);
  }
  code {
    font-family: var(--mono);
    font-size: 10.5px;
    background: var(--surface-raised);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .independence {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    margin-bottom: 10px;
  }
  .ind-fig {
    display: flex;
    flex-direction: column;
    line-height: 1.1;
  }
  .ind-n {
    font-family: var(--mono);
    font-size: 20px;
    color: var(--ink-dim);
  }
  .ind-fig.strong .ind-n {
    color: var(--accent);
  }
  .ind-l {
    font-size: 9.5px;
    color: var(--ink-faint);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .ind-arrow {
    color: var(--ink-faint);
  }
  .ind-note {
    font-size: 11px;
    color: var(--ink-faint);
    flex: 1;
    min-width: 200px;
  }
  .pricing {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 10px;
    font-size: 11.5px;
  }
  .pr-main {
    font-family: var(--mono);
    font-size: 15px;
    color: var(--good);
  }
  .coord {
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    margin-bottom: 12px;
  }
  .coord.live {
    border-color: color-mix(in srgb, var(--warm) 40%, transparent);
  }
  .coord-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .coord-score {
    font-family: var(--mono);
    font-size: 16px;
    color: var(--warm);
  }
  h4 {
    margin: 14px 0 6px;
    font-size: 11.5px;
    font-weight: 650;
  }
  .tbl {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }
  .tbl th {
    text-align: left;
    color: var(--ink-faint);
    font-weight: 500;
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 4px 6px;
    border-bottom: 1px solid var(--line);
  }
  .tbl td {
    padding: 5px 6px;
    border-bottom: 1px solid color-mix(in srgb, var(--line) 50%, transparent);
    vertical-align: top;
  }
  .r {
    text-align: right;
  }
  .mono {
    font-family: var(--mono);
  }
  .why {
    color: var(--ink-faint);
    font-size: 10px;
    max-width: 280px;
  }
  .cluster {
    padding: 6px 0;
    border-bottom: 1px solid color-mix(in srgb, var(--line) 50%, transparent);
  }
  .cl-head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 11.5px;
  }
  .boundary {
    margin-top: 12px;
    padding: 8px 10px;
    border-left: 2px solid var(--accent-dim);
    color: var(--ink-faint);
    font-size: 10.5px;
    line-height: 1.5;
  }
</style>
