# Execution Pass B — audit of every economic exit path

Prerequisite for canonical exits: enumerate every route that can reduce or
close a `PaperPosition`, so the canonical executor covers all of them rather
than most of them. An exit route that bypasses the canonical executor is a
defect, and the only way to know they are all covered is to list them first.

## The finding

**Every economic exit on the paper book funnels through exactly two
functions**, and both already refuse canonical positions:

    lib/paper_engine.close_paper_position          -> _refuse_legacy_close()
    lib/paper_engine.partial_close_paper_position  -> _refuse_legacy_close()

That is a better starting position than the entry work had. There is no
scattered set of hand-rolled close paths to chase down; there is one legacy
door, it is already locked against canonical positions, and Pass B builds the
canonical door beside it.

**Consequence to state plainly: canonical positions currently cannot be
closed at all.** That is the intended fail-closed state, not an outage — the
guard exists precisely so a venue-book position cannot settle at a mark.

**THE GUARD IS PERMANENT.** Pass B does not remove or weaken
`_refuse_legacy_close()`. It makes canonical callers BYPASS the legacy leaf by
routing them to `canonical_exit`; the legacy leaf keeps refusing anything with
a canonical venue-book fill. If a canonical or hybrid position ever reaches it
again by accident, it must still refuse. Building the canonical door is what
unblocks canonical positions — the lock on the legacy door stays on.

## Caller matrix

| # | caller | trigger | price passed as fill | canonical-aware |
|---|---|---|---|---|
| 1 | `paper_engine.mark_to_market:1760` | stop / target / liquidation | `price` (mark) | via guard |
| 2 | `paper_engine:1931` | book reset | `price` (mark) | via guard |
| 3 | `paper_trading:519` | scale-out TP1 | `current_price` | via guard (partial) |
| 4 | `paper_trading:607` | risk guard | `current_price` | via guard |
| 5 | `paper_trading:648` | tier exit | `current_price` | via guard |
| 6 | `paper_trading:779` | tier exit | `current_price` | via guard |
| 7 | `paper_trading:786` | AI EXIT | `current_price` | via guard |
| 8 | `telegram_bot:823` | manual Telegram close | supplied price | via guard |
| 9 | `app/routers/trading.py:1171` | manual API close | `close_price` | via guard |
| 10 | `app/routers/trading.py:1248` | manual flatten | `px` | via guard |

**All ten pass a MARK as the settlement price.** Every one is a
mark-becomes-fill path. That is the single defect Pass B exists to remove:
these ten become *triggers* that submit an intent, and the fill comes back
from `ExecutionResult`.

## Out of scope, deliberately

`lib/auto_simulator.py` also closes positions and mutates `realized_pnl` and
portfolio cash directly (lines ~229-237). It operates on `AutoSimPosition` /
`AutoSimPortfolio` — **a separate book with its own economy**, not the paper
book. It is not reachable from the paper exit paths above and is not part of
Pass B. Changing it would be a different phase with its own accounting proof.

## What Pass B must therefore build

1. `lib/canonical_exit.py`, analogous to `canonical_entry`: frozen identity
   from `execution_provenance`, exit-quantity authorization, `OrderPlan` with
   `intent=EXIT` and `reduce_only`, submission through `execution_venue`,
   fee from the ACTUAL filled leg, carry over the ACTUAL interval, atomic
   settlement.
2. Persisted `SettlementLeg` and `PositionSettlement` (currently in-memory
   only in `lib/paper_settlement.py`).
3. The ten callers above rerouted: canonical positions to `canonical_exit`,
   legacy positions unchanged.
4. Execution-side inversion — long closes SELL into the bid, short closes BUY
   from the ask. Passing the POSITION side into `execute_market` would open
   more exposure instead of closing it.
5. One `RealizedOutcome` per position, from settlement truth, with partial
   legs producing no learning vote.

## Constraints carried from earlier phases

- Legacy positions keep legacy economics; the 667 operator rows are not
  migrated and their deferred round-trip fee is not retroactively split.
- Hybrid provenance (canonical fill, incomplete metadata) fails closed on
  BOTH paths — it is not permission to pick whichever accounting works.
- Exact-contract identity applies to execution exactly as Phase B proved it
  for evidence: a frozen `PBTCUCZ50` must meet a `PBTCUCZ50` book, and a
  NULL or different contract refuses rather than settles.
- `EVIDENCE_ONLY` must remain structurally unable to settle anything.

---

# AFTER — Pass B caller routing (2026-08-18)

The finding above is preserved unchanged. This section records what the ten
callers do NOW.

Every one of them goes through `lib/exit_dispatch.py`, which classifies the
position ONCE and routes it. No caller reaches `close_paper_position` or
`partial_close_paper_position` directly any more — proved from the code by
`tests/test_pass_b_exit_routing.py::NoProductionCallerTouchesTheLegacyLeafTests`,
an AST sweep over `lib/`, `app/` and `jobs/`, not by this document.

## The three routes

| Route | Condition | Behaviour |
|---|---|---|
| `LEGACY` | no canonical fill AND no settlement header | the legacy leaf, unchanged: caller mark IS the fill, deferred round-trip fee, legacy learning |
| `CANONICAL` | canonical fill AND `is_canonical` AND a valid current B1 header | `canonical_exit` → B2A settlement → B2C learning |
| `HYBRID` | any mixed state | `HYBRID_POSITION_EXIT_REFUSED`, nothing mutates |

There is **no fallback**. A canonical exit that cannot execute — stale book,
halted market, drifted contract, unavailable fee or carry — leaves the
position OPEN. "Canonical failed, settle at the mark" would reintroduce the
exact defect this pass removed, so it does not exist, not even on exception.

## Caller matrix

| # | Caller | Canonical reason | Caller price role | Fill authority | Learning |
|---|---|---|---|---|---|
| 1 | `mark_to_market` stop | `STOP_EXIT` | decision reference; threshold passed separately as `trigger_price` | exact venue book, after the stop is re-confirmed on the executable side | final → applied |
| 1b | `mark_to_market` target | `TARGET_EXIT` | reference | exact book, target re-confirmed | final → applied |
| 1c | `mark_to_market` margin call | `MARGIN_CALL` | reference | exact book; the condition is re-derived from it at the SAME `MARGIN_CALL_THRESHOLD` | final → applied |
| 2 | `soft_reset_paper_portfolio` | `ADMINISTRATIVE_RESET` | reference | exact book | **SKIPPED_POLICY** — never strategy evidence |
| 3 | paper TP1 scale-out | `SCALE_OUT` | reference | exact book | none (a partial never votes) |
| 4 | paper risk guard | `VOLUNTARY_EXIT` | reference | exact book | final → applied |
| 5 | paper tier exit | `VOLUNTARY_EXIT` | reference | exact book | final → applied |
| 6 | paper tier exit (2nd) | `VOLUNTARY_EXIT` | reference | exact book | final → applied |
| 7 | paper AI exit | `VOLUNTARY_EXIT` | reference | exact book | final → applied |
| 8 | Telegram manual close | `VOLUNTARY_EXIT` | reference | exact book | final → applied |
| 9 | API manual close | `VOLUNTARY_EXIT` | reference | exact book | final → applied |
| 10 | API flatten | `VOLUNTARY_EXIT` | reference | exact book | final → applied |

Each caller's original string survives in exit provenance as
`caller_reason`, beside `caller_source`, so `VOLUNTARY_EXIT`/`ai_exit` stays
distinguishable from `VOLUNTARY_EXIT`/`telegram_manual`. The canonical
vocabulary is never widened to hold UI spelling, and an unmapped string
REFUSES rather than being guessed into it.

## What changed about triggers

**A caller mark is evidence. The contract's own book is authority.**

An automated price trigger — stop, target, margin call, forced liquidation —
is re-confirmed against the frozen instrument's executable side before any
order exists:

    LONG  exits by SELLING  → judged on the BID
    SHORT exits by BUYING   → judged on the ASK

A midpoint or a cross-market print that touches the level while the
executable side has not is not an executable trigger, and returns
`EXIT_TRIGGER_NOT_CONFIRMED` with the threshold, the caller reference and
both sides of the book. `mark_to_market` reports those under
`trigger_refused`, never as a close with a null P&L.

Discretionary exits — manual, AI, flatten, reset — are INSTRUCTIONS, not
price claims, and are never second-guessed this way.

`trigger_price` means the THRESHOLD. It is a third fact beside the decision
reference and the actual fill, and the ledger stores all three separately.

**This matters more than it looks.** The legacy mark arithmetic in
`mark_to_market` is unit-blind: it prices 26 PBTC CONTRACTS as 26 coins, so
almost any adverse mark makes it declare a margin call. That arithmetic is
still fine for its remaining job — display, and deciding a position is worth
investigating — precisely because it can no longer settle anything.

## What changed about administrative reset

`soft_reset_paper_portfolio` may close positions through the dispatcher, but
it may only RESEED capital when zero open positions remain. If any position
refuses — a stale book, an unavailable carry — it returns `RESET_INCOMPLETE`
with the successes, the failures and what is still open, and the portfolio
row is left exactly as it was.

Closing N positions against N live markets is not one transaction and this
does not pretend otherwise: positions that closed stay closed, which is
honest history, and a later reset may finish the job. The invariant is
capital safety, stated plainly: **never fresh cash beside old open
exposure.**

`API flatten` follows the same principle without the reseed: it verifies the
final open count and reports `flattened`, `refused` and `remaining_open`
rather than claiming success from attempt counts.

## What did NOT change

- **`_refuse_legacy_close()` is still permanent.** The dispatcher bypasses
  the legacy leaf for canonical positions; the leaf still refuses them if
  anything ever sends one its way. Defense in depth, pinned by
  `TheLegacyGuardIsStillPermanentTests`.
- **Legacy economics.** A legacy position's caller price is still its fill —
  proved by a control test, without which the canonical mark-poison proof
  would be vacuous.
- **`lib/auto_simulator.py`** is a different ORM economy and remains out of
  scope.
- **`EVIDENCE_ONLY`** still cannot settle anything on either route.
