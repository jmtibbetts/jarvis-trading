# Plan status — what the documents in this repository still describe

Several planning documents predate the Virtual CEX / Virtual DEX
architecture. They were accurate when written and some are now describing a
system that no longer exists. **Documentation that describes an obsolete
architecture is not neutral** — it teaches a fresh install the wrong model
and sends a future reader looking for code that was deliberately removed.

Status vocabulary:

| | |
|---|---|
| **DONE** | implemented and pinned by tests |
| **PARTIAL** | some items landed, the rest still open |
| **OPEN** | not started |
| **SUPERSEDED** | the plan describes an architecture that has been replaced |

Last reconciled against `main` with 2,687 tests passing.

---

## Planning documents

| Document | Status | Notes |
|---|---|---|
| `HARDENING_PLAN.md` | **PARTIAL** | The gate experiment, cost modelling and R-multiple provenance landed. Duplicate-evidence elimination and the selection-bias dashboard remain open. |
| `UI_AUDIT.md` | **PARTIAL** | P0/P1 complete and accurate. Written before the Virtual CEX work, so its navigation model (Live / Paper / Auto Sim) is superseded — see below. |
| `JARVIS_MASTER_UI_UX_HELIUS_WALLET_ALPHA_PROMPT.md` | **PARTIAL** | Wallet intelligence, token surge and the on-chain desk are done. The §130 Daily Brief command centre and §131 Crypto Desk IA remain open. |
| `JARVIS_CLAUDE_IMPLEMENTATION_PLAN*.md` | **SUPERSEDED** | Live-first sequencing. Routing by `paper_mode`, "shorts and leverage go to paper", live execution as the default destination — all replaced. |
| `UPGRADE_PLAN.md` | **PARTIAL** | Cost-aware filtering and `min_viable_stop_pct` landed and were then corrected (FX was priced as equity; unknown futures fell back to an equity multiplier). |
| `tradingupgradep1.md` / `p2` | **SUPERSEDED** | Pre-dates the canonical instrument model and the execution boundary. |
| `DATA_PLATFORM_PLAN.md` | **PARTIAL** | Feature snapshots and independent-horizon labels landed. The three clocks (event / ingestion / processing) are partially done. |
| `JARVIS_NPU_CLAUDE.md` / `NPU_update.md` | **OPEN** | The NPU is built and measured but wired to one route. It cannot address the LLM load problem it was framed against. |

---

## Claims that are no longer true

These appeared in documentation or code comments and have been corrected.
They are listed because each one misled a reader at some point, and a
future reader deserves to know the correction happened.

**"Shorts and leverage go to paper; ordinary longs go live."**
Superseded. Routing is not decided by direction. In `VIRTUAL_ONLY` every
eligible thesis routes to a virtual venue, and a live adapter is
unreachable regardless of the product. See `lib/platform_mode.py`.

**"Discovery costs roughly two Helius RPC calls per token."**
Was in the scheduler comment. Deep discovery can call
`getTokenLargestAccounts`, `getMultipleAccounts`, `getSignaturesForAddress`,
many `getTransaction`, plus classification and identity.

**"Empty `HELIUS_WATCH_WALLETS` means the collector stays inert."**
Superseded. `wallet_registry` is the runtime wallet universe; the
environment variable is seed input only, and empty is the normal
configuration.

**"The paper engine is a parallel book for shorts and leverage."**
It is becoming the Virtual CEX — the primary training exchange for every
CEX-tradable product, not a side channel for the trades Alpaca refused.

**"Real vs Paper performance comparison."**
The meaningful comparison is now **Agent vs Shadow on the same thesis**,
which measures policy value rather than two unrelated books. See
`lib/trade_thesis.py`.

**Kraken is "an important crypto data source".**
Understated. Kraken Pro is the primary real-world target venue, including
75+ crypto and TradFi futures across CME, CBOT, NYMEX and COMEX.

---

## Configuration that documented an architecture nothing performed

`.env.example` has been corrected, but the class of error is worth
recording because it recurs:

- `HELIUS_PAGE_LIMIT` allowed up to 1000 against an API that serves 100.
- `HELIUS_BACKFILL_LIMIT=500` advertised a depth the collector never
  fetched — it read one page and stopped.
- `HELIUS_WATCH_WALLETS` was documented as the wallet universe long after
  the registry replaced it.

A setting that describes analysis nothing performs is worse than a missing
one: it is believed.

---

## Known open items carried forward

- **`6J=F`** (CME Japanese Yen) has no verified contract spec and is
  correctly refused by the instrument layer, behind 140 existing signals.
  The spec must be verified against the exchange rather than inferred.
- **8,281 of 21,129 trade outcomes** predate signal linkage. They stay
  `LEGACY_UNATTRIBUTED` and must not be fuzzy-matched to strategies.
- **GitHub Actions are pinned to mutable major-version tags.** Pinning to
  verified commit SHAs is the correct hardening step and was deliberately
  not done from memory — a guessed digest breaks every build.
- **Kraken equity/ETF is `UI_ONLY`.** Whether an API contract exists must
  be established by probe, not by inference from the Pro interface.
- **Token→token wallet swaps carry `notional_usd = NULL`.** They are real
  swaps that cannot yet be valued, and must not be counted as
  zero-dollar trades.
