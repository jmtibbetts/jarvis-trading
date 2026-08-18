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
