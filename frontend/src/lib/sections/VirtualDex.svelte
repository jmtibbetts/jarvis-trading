<script lang="ts">
  /**
   * The Virtual DEX — a first-class trading surface, not a panel buried
   * under on-chain analytics.
   *
   * WHY IT MOVED. The swap engine lived at the bottom of the On-Chain
   * Desk, below wallet intelligence and liquidation research. That put a
   * TRADING venue inside a RESEARCH page, which is backwards: On-Chain is
   * where you decide what is interesting, and this is where you act on it.
   *
   * WHY IT IS SEPARATE FROM THE VIRTUAL CEX. A pool is not an order book.
   * There is no bid/ask to cross, no borrow, no leverage a venue will
   * extend you; size is bounded by what the pool can absorb, and the
   * binding cost is price impact rather than spread. Running DEX trades
   * through broker mechanics produces a book whose every number is
   * plausible and wrong.
   *
   * They converge at the RealizedOutcome layer — same units, same cost
   * accounting, same learning contract — which is the only place they
   * should look alike.
   */
  import Panel from "../components/Panel.svelte";
  import DexExchange from "../components/DexExchange.svelte";
  import DexDiscoveryPanel from "../components/DexDiscoveryPanel.svelte";
  import Pill from "../components/Pill.svelte";
  import StateNote from "../components/StateNote.svelte";
  import { api, type PlatformMode } from "../api";
  import { FeedTracker } from "../dataState.svelte";

  const feeds = new FeedTracker();
  let mode = $state<PlatformMode | null>(null);

  $effect(() => {
    feeds.load("mode", () => api.platformMode()).then((m) => (mode = m));
  });
</script>

<div class="page">
  <header class="page-head">
    <h1>Virtual DEX</h1>
    <p>
      On-chain execution — AMM pools, routed swaps, price impact and gas.
      Sized by what the pool can absorb, not by account equity alone.
    </p>
    {#if mode}
      <div class="mode-strip" class:training={!mode.live_execution_allowed}>
        <Pill tone={mode.virtual_only ? "info" : "warm"} label={mode.mode} />
        <span>{mode.detail}</span>
      </div>
    {/if}
  </header>

  <!--
    Discovery first: you cannot swap what you have not found. This is the
    same surge result the sampler persists and the autotrader reads — one
    engine, not a second definition.
  -->
  <DexDiscoveryPanel />

  <DexExchange />

  <Panel title="How this venue differs" meta="read once">
    <div class="notes">
      <div>
        <b>Size is bounded by the pool, not your equity.</b>
        Measured on live data: $25,000 into a $50,000 pool is 49.9% price
        impact — half the stake gone on entry, before the trade is even
        wrong. Account risk is an additional constraint, never the only one.
      </div>
      <div>
        <b>Depth certainty changes the size.</b>
        A pool whose reserves were read from the chain is VERIFIED. One
        estimated from total liquidity is ASSUMED_BALANCED_POOL. A
        concentrated-liquidity pool is a MODELLED_ESTIMATE, and its local
        depth may be nothing like half the total in either direction — so
        it sizes to 30% and weights predicted impact 2.5x.
      </div>
      <div>
        <b>Gas is paid in SOL, separately.</b>
        The pool returns exactly what the curve says; the network fee leaves
        the wallet balance. A wallet holding tokens with no SOL cannot
        transact at all, and that is a real state this book can reach.
      </div>
      <div>
        <b>An unpriceable exit is a risk event.</b>
        If no route can price a close, the position stays open as
        EXIT_PENDING_NO_LIQUIDITY. It is never booked at the mid — a
        simulator that rewards illiquidity teaches the desk to seek it.
      </div>
    </div>
  </Panel>
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 14px; }
  .page-head h1 {
    margin: 0; font-size: 19px; letter-spacing: -.01em;
  }
  .page-head p {
    margin: 4px 0 0; font-size: 12.5px; color: var(--muted); max-width: 74ch;
  }
  .mode-strip {
    display: flex; align-items: center; gap: 9px; margin-top: 9px;
    padding: 6px 10px; border-radius: 4px; font-size: 12px;
    border: 1px solid var(--border, #222a35);
  }
  .mode-strip.training {
    border-color: color-mix(in srgb, var(--accent) 40%, transparent);
    background: color-mix(in srgb, var(--accent) 7%, transparent);
  }
  .mode-strip span { color: var(--muted); }
  .notes { display: flex; flex-direction: column; gap: 10px; }
  .notes div { font-size: 12.5px; color: var(--muted); line-height: 1.55; }
  .notes b { color: var(--text, #e6e9ef); display: block; margin-bottom: 2px; }
</style>
