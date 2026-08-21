# JARVIS TRADING — canonical handoff

**HISTORICAL PROMPTS AND OLD REPORTS ARE NOT CURRENT-STATE AUTHORITY.**

**MEASURED CURRENT REPO/RUNTIME STATE OVERRIDES STALE DOCUMENTATION —
including this file.** If a claim here disagrees with the machine, the
machine is right and this file is a bug. Verify before you rely.

This is a CURRENT-STATE document. It is not a diary, a changelog, a
transcript, or an archive of old prompts. Everything historical lives in
`git log`. Keep it short enough to seed a fresh session.

*Last measured: 2026-08-21, at code SHA `acca096`.*

---

## 1. Runtime and repository

    project        JARVIS TRADING (not EIDOS — do not rename)
    runtime        WSL2 Ubuntu 24.04 ONLY
    active repo    /home/nullcode/jarvis-trading
    interpreter    .venv/bin/python  (never bare python3, never Windows Python)
    remote         origin -> github.com/jmtibbetts/jarvis-trading
    code SHA       acca096 (the wallet-intelligence cycle completes itself)

**Never work in the Windows checkout at `C:\jarvis-trading-ai-python`.**
Never push from it, never run `run.ps1` / `stop.ps1` / `setup.ps1`, never
treat `/mnt/c` as the active repo, never run Windows Python or Node against
this project.

It is **not** an inert copy, and calling it one gets the danger backwards.
Measured 2026-08-20: it sits on the SAME lineage, roughly nine commits
behind (its HEAD `05a3865` is a genuine ancestor of WSL `main`). Because it
is merely behind rather than divergent, it fast-forwards, builds and mostly
tests fine — which is exactly what makes mistaking it for the runtime easy.
Two concrete hazards:

- ~12 tests fail there for PLATFORM reasons alone (shell guards, path/AST
  checks). They are green on Linux. **Never quote a Windows run as a
  baseline.**
- It has its OWN stale `data/`. `conftest.py` redirects the database only
  under pytest, so a bare `python -c` importing `app.database` opens that
  tree's real `data/jarvis.db` and runs `create_all` on it. Use pytest, or
  open read-only: `sqlite3.connect("file:...?mode=ro", uri=True)`.

> **Trap, hit on 2026-08-20.** When driving WSL from a Windows shell as
> `wsl.exe -d Ubuntu-24.04 -- bash -c '...'`, `$(...)` and `$VAR` inside
> those single quotes can be expanded by the OUTER Windows shell first — so
> `$(git rev-parse HEAD)` runs against the DEAD WINDOWS REPO and silently
> reports its SHA as though it were the runtime's. It produced a plausible
> wrong SHA that only the reflog disproved. Put no shell substitution in the
> command string; let the commands run plainly inside WSL and read their
> stdout.

Canonical process control — use these, do not hand-roll a launch command:

    scripts/start_jarvis.sh     scheduler OFF by default; declares the modes
    scripts/stop_jarvis.sh      identity-checked SIGTERM, then escalation
    scripts/status_jarvis.sh    read-only; reads posture from /proc/<pid>/environ

`start_jarvis.sh` establishes `VIRTUAL_ONLY` / `FULL_VIRTUAL` explicitly and
sets `JARVIS_DISABLE_SCHEDULER=1` unless `--with-scheduler` is passed.

### Verifying which code the server actually loaded

`lib/build_identity.py` captures the commit ONCE at import.
`GET /api/system/version` reports:

    backend_commit               the LOADED code. Immutable for the process.
    loaded_backend_commit        the same value, named unambiguously
    repository_head_commit       live git HEAD; may legitimately differ
    code_matches_repository_head comparison, or null if either is unknown

A difference is NORMAL after a documentation-only commit and a genuine
mismatch after a code-bearing one.

> Until 2026-08-20 `backend_commit` shelled out to `git rev-parse HEAD` per
> request, so it reported the REPOSITORY rather than the loaded code — and
> told a deploy check that a server running `ae5bab9` was running `ac77450`.
> If you are ever unsure, the behavioural check still works: call an endpoint
> whose shape changed in the commit you care about, or compare
> `ps -o lstart= -p <pid>` against the commit time.

> **THE SAME DEFECT SURVIVED THAT FIX AND WAS CLOSED AGAIN AT `027d124`.**
> The constant was captured at MODULE import — and the only consumer imported
> `lib/build_identity` INSIDE the `/system/version` handler, so nothing
> imported it until a request arrived. It was therefore stamped at the FIRST
> REQUEST, and any commit made in between became the reported loaded build.
> Measured: a process started 11:04:51 reported a commit authored 11:08:48 as
> its own. `main.py` now imports the module at module scope; that import is
> load-bearing, not tidy-uppable, and `tests/test_build_identity_capture_time.py`
> pins it. Proven behaviourally: with the repo moved underneath a running
> server, `loaded` held while `repository_head_commit` moved and
> `code_matches_repository_head` went False.
>
> A related trap that cost 42 red tests: **`import main` inside a test runs
> `load_dotenv()`** and injects the operator's real `.env` into the whole
> pytest session, so tests scheduled afterwards see `VENUE_30D_VOLUME_USD`
> and friends. Load the entrypoint in a SUBPROCESS.
>
> **RESIDUAL GAP: a server started from a DIRTY tree names the wrong SHA.**
> `GIT_HEAD_AT_IMPORT` reads HEAD, and HEAD does not know about uncommitted
> changes — so a process launched mid-edit reports the LAST COMMIT while
> running code that is not in any commit. Hit on 2026-08-20: a restart
> before committing reported `f66cc28` while serving routes that only
> existed in the working tree. Not a lie the code can detect today. **Start
> the runtime from a COMMITTED tree** when the reported identity matters,
> and check `git status --porcelain` if it does not look right.

The SPA fallback answers any unmatched path with `index.html` and HTTP
**200**, so a missing route looks like success until JSON parsing fails.
Verify the JSON and the expected key, never the status code alone.

## 2. Posture — currently in force

    JARVIS_PLATFORM_MODE           VIRTUAL_ONLY
    JARVIS_RUNTIME_MODE            FULL_VIRTUAL   (explicit, not defaulted)
    JARVIS_DISABLE_SCHEDULER       1  — no trading jobs, no signal execution
    Helius wallet polling          ON, independently gated (see 10c)
    external broker connector      OFF
    external account management    OFF

Runtime mode fails OPEN (silence means `FULL_VIRTUAL`), which is why the
launcher declares it out loud; platform mode fails CLOSED to `VIRTUAL_ONLY`.
`/api/health` and `/api/system/version` both report all five fields.

Do not enable the scheduler, real brokerage, external account management, or
place any real transaction without explicit operator approval.

    REAL market data + REAL analysis + VIRTUAL execution + FAKE money
    + REAL learning from forward observation

**THE GOLDEN RULE: the bot must never make money because the simulator is
wrong.** Nearly every defect in this system's history violated it.

## 3. Canonical epoch and databases

    ENGINE_EPOCH   2026-08-18-canonical-lifecycle-v1
    PRIOR_EPOCHS   2026-08-13, 2026-08-17-venue-book

Do not change the epoch. Active stores, all ext4, none Windows-backed
(`app.database` refuses Windows-backed persistence at the engine boundary):

    data/jarvis.db              canonical operational DB
    data/forward_evidence.db    forward evidence
    data/events.db              raw event store
    data/ohlcv_cache.db         OHLCV cache

**Tests never touch the operator DB.** `app.database._resolve_db_path`
raises unconditionally if `JARVIS_UNDER_PYTEST=1` resolves to the operator
database. There is no escape hatch; `conftest.py` redirects to a temp path.

## 4. Canonical economic state — measured 2026-08-20

    paper portfolio cash        100000.0
    paper positions             0
    paper trades                0
    paper settlements           0
    paper settlement legs       0
    execution commitments       0
    dex_balances                0      (intentional)
    dex_funding_events          0      (intentional)
    dex_positions / dex_trades  0
    legacy positions            0
    legacy trade_outcomes       0
    manual_trades and its legs / cost events / corrections   0  (see §5b)

Re-measured after a full manual-trade lifecycle was driven through the LIVE
API against this database: every virtual count above was unchanged and cash
was still exactly 100000.0. That is the §5b isolation boundary, verified on
the operator's own store rather than only in a test fixture.

FORWARD REPAIR ONLY. Never restore an old database to make a count match an
expectation, and never mutate the canonical economy to make a report or a
test look clean. If reality differs from what a document predicts, the
document is corrected — not the economy.

> A previous revision of this file asserted 21,194 legacy trade_outcomes,
> 667 legacy open positions and cash $63,550.84. All three were stale; the
> measured values are above. This is why the header rule exists.

## 5. The canonical chain — do not create parallel types

    ObservedEvidence -> TradeDecision -> RiskDecision -> OrderPlan
      -> ExecutionVenue -> VirtualCexAdapter -> ExecutionResult
      -> Settlement -> RealizedOutcome -> Learning

- **TradeDecision / RiskDecision / OrderPlan** are the only decision
  authorities. Do not introduce a parallel decision type or a shortcut that
  skips one.
- **ExecutionResult** is the sole authority on what execution did. A caller's
  belief about a fill is not an ExecutionResult.
- **RealizedOutcome** is the sole learning authority. Calibration and
  expectancy read realized outcomes, never intentions. Decisions recorded in
  `EVIDENCE_ONLY` carry `EXECUTION_SUPPRESSED` and are barred from fill
  calibration and portfolio P&L — nothing failed, execution was not permitted.

Four identities stay separate and are never collapsed:
**asset_class · product · venue · instrument**.
**PRODUCT IS NEVER INFERRED FROM LEVERAGE.** A 1x perpetual is `CRYPTO_PERP`.

Execution authority by product:

| product | authority |
|---|---|
| CRYPTO_SPOT | Kraken spot book (`wss://ws.kraken.com/v2`) |
| CRYPTO_PERP (US) | Bitnomial public perpetual book |
| EQUITY | Alpaca read-only quote |
| FUTURES / FOREX | none — **fail closed** |

Cross-venue evidence may inform analysis. It may never silently become
target-product execution authority.

## 5b. Manual operator execution — MANUAL_OPERATOR

A trade the OPERATOR placed by hand at a venue this program cannot reach.
`lib/venue_capabilities.py` already called that `UI_ONLY`: a missing
execution API is a fact about the venue, not a defect here, and manual
execution is a FIRST-CLASS MODE rather than failed automation.

`lib/execution_mode.py` is the taxonomy. It answers the one question neither
existing vocabulary could — **did this program place the order, or did the
human?** — and it IMPORTS `VIRTUAL_CEX` / `VIRTUAL_DEX` / `SHADOW` from
`execution_venue` rather than retyping them, so a second vocabulary cannot
fork off the first.

| mode | submitted_by_jarvis | real money | may move virtual cash |
|---|---|---|---|
| VIRTUAL_CEX / VIRTUAL_DEX | yes | no | yes |
| SHADOW | no | no | no |
| MANUAL_OPERATOR | **no** | **yes** | **no** |
| LIVE_AUTONOMOUS | yes | yes | no — **refused, see below** |

**LIVE_AUTONOMOUS is a graduation state, not a feature flag.** It exists so
nothing needs renaming when it is earned; `assert_executable()` refuses it
unconditionally, at the call site, so re-adding it to a table or an enum
cannot quietly enable it.

    lib/execution_mode.py      the mode axis
    lib/manual_execution.py    dataclasses, validation, deterministic economics
    lib/manual_trade_store.py  persistence + lifecycle (imports NO book writer)
    lib/account_economics.py   account entitlements, capital ownership
    app/routers/manual.py      /api/manual/*
    manual_trades / _legs / _cost_events / _corrections

### What it refuses to do

- **Never claims JARVIS submitted.** `submitted_by_jarvis` is False on the
  mode, on every response and in every outcome's provenance. There is no
  submit path and no adapter — AST-asserted, not merely absent.
- **Never fabricates a thesis.** An independent trade carries `thesis_id`
  NULL and `recommendation` NULL, and that is a COMPLETE record. A
  DISAGREEMENT keeps the link: JARVIS said short, the operator went long,
  both are stored. A schema requiring agreement would discard the most
  informative case there is.
- **Never rewrites the recommendation to match the execution.**
  `RecommendationSnapshot` is frozen and is explicitly NOT correctable. The
  gap between recommended and actual entry is the whole measurement of
  execution quality; averaging it away leaves a system that cannot tell a
  bad thesis from a late entry.
- **Never turns UNKNOWN into zero.** Costs are `float | None`; an
  unevidenced fee makes net P&L None. WHICH costs a product can incur comes
  from `product_cost_profile`, so §6's forbidden inferences hold — 50x
  leverage still borrows nothing.
- **One thesis does not vote twice.** Scale-ins, partial exits, funding and
  rebates are facts WITHIN one trade. One trade → one `thesis_id` → at most
  one `RealizedOutcome` → one `OPERATOR` arm (a new member of
  `trade_thesis.ARMS`), so `sample_count` still counts DISTINCT THESES.
- **Never touches the virtual economy.** Own tables, no paper/dex writer
  imported. Measured against the REAL operator DB: a full lifecycle left
  every virtual table at 0 and cash at exactly 100000.0.

### Declared-absent costs — the one deliberate escape

The UNKNOWN rule would otherwise make every equity trade permanently
unknowable (`SEC_TAF_WHERE_APPLICABLE` is conditional), which pressures a
future maintainer into weakening the rule itself. So an operator may DECLARE
a category not charged — a promotional zero-fee window is a FACT. A
declaration is evidence, is recorded, and declaring a category that also
carries a charge REFUSES rather than picking a winner.

### Learning is blocked, not faked, when costs are incomplete

`RealizedOutcome` stores floats, so an unevidenced fee necessarily lands as
0.0 and the net reads BETTER than the trade was. The outcome is still
written — real history, worth inspecting — marked
`BLOCKED_INCOMPLETE_COSTS`, and it does not vote until the evidence arrives.
Supplying the missing cost later re-derives it and unblocks it.
**THE BOT MUST NEVER LEARN THAT IT MADE MONEY BECAUSE A COST WAS NEVER
ENTERED.**

### Account economics — a public schedule is not an effective fee

`lib/venues.py` stays PUBLIC and untouched. Account entitlements (fee tier,
volume discount, maker rebate, promotion, waiver, credit) live in
`account_economics` and are applied as a COMPUTED VIEW keeping both
`public_usd` and `effective_usd`. A schedule edited to match one account's
promotion stops describing the venue, and every other consumer inherits a
discount it does not have.

- **A PROMOTION MUST HAVE AN END.** One without `effective_until` refuses —
  an unbounded "temporary" waiver keeps discounting after it has expired.
- **An entitlement modifies only the categories it NAMES.** Zero commission
  does not make spread, funding, gas or a liquidation penalty free, and the
  untouched categories are reported.
- **PROMOTIONAL CREDIT IS NOT OWNED CAPITAL**, and capital of UNKNOWN kind
  fails closed to NOT owned. Unproven money counted as equity inflates the
  book and every return computed against it.

### Reconciliation — neither side wins

A venue-reported realized figure is preserved as its OWN fact beside the
component sum, in `venue_reconciliation`'s existing vocabulary
(`RECONCILED` / `UNEXPLAINED_VENUE_COST` / `MODEL_INCOMPLETE` /
`COMPONENT_ONLY`). A report is **never back-solved into a missing cost**, and
an unexplained delta stays UNEXPLAINED — it is the honest measurement of what
the cost model does not yet know about that venue.

## 5c. Manual outcomes reaching LEARNING — the gate and the admission list

`lib/manual_learning.py` projects a CLOSED manual trade into `trade_outcomes`,
through the SAME writer the virtual book uses
(`canonical_learning.insert_learning_row`), so both populations have one row
shape and stay comparable.

### The gate reads EVIDENCE, never a cost field

`RealizedOutcome` stores costs as FLOATS. A fee nobody evidenced is
indistinguishable IN THE ROW from a fee that was genuinely zero — and the
bigger the missing fee, the better the trade looks. The canonical poison
case, in the tests: **+$60 gross, outcome object reports net +$60 and WIN,
real costs $62, so it was a LOSS.**

So eligibility consults `trade.unknown_cost_categories()` — which costs this
PRODUCT incurs (§6's authority) against what the legs and cost events
actually evidence. Verdicts:

    ELIGIBLE_COMPLETE
    BLOCKED_OPEN_TRADE                        OPEN / PARTIALLY_CLOSED
    BLOCKED_INVALID_STATE                     DRAFT / CANCELLED / ABANDONED
    BLOCKED_INCOMPLETE_COSTS                  a real cost is unevidenced
    BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS   venue figure vs components
    BLOCKED_NO_ECONOMICS

**RE-DERIVED AT THE CONSUMER.** `manual_trades.learning_state` already carries
`BLOCKED_INCOMPLETE_COSTS` from the producer and this does NOT trust it: a
stored verdict describes a past evaluation, and a hand-edited or
half-migrated row would carry a stale clean one. Pinned by forging `PENDING`
directly in the database and watching the gate refuse anyway.

Reconciliation materiality is POLICY, stated and versioned
(`manual_reconciliation_policy_v1`): an unexplained gap above
`max($0.05, 10% of |net|)` blocks; below it the residual is PRESERVED on the
row, never absorbed. A venue-reported figure is never back-solved into a
missing cost.

### Admission is an ALLOWLIST — `lib/learning_population.py`

`trade_outcomes.outcome_source` had two values and every consumer wrote its
policy as a DENYLIST, `!= "replay"`. That is safe only while replay is the
only thing worth excluding. A third population would have been admitted at
FULL WEIGHT by six of them:

    calibration          weight 1.0
    expectancy           weight 1.0, and counted as raw_live
    edge_cost_matrix     weight 1.0, and counted into n_live
    strategy_lifecycle   replay=False — indistinguishable from live
    jobs/paper_trading   the BOOTSTRAP certification gate
    signal_accuracy      NO SOURCE FILTER AT ALL — and it feeds LLM prompts

None would have raised or failed a test. All six now consult the authority.

| profile | live | NULL | replay | manual_operator |
|---|---|---|---|---|
| `JARVIS_EXECUTION` | 1.0 | 1.0 | 0.5 | **excluded** |
| `FORWARD_OBSERVED_CERTIFICATION` | 1.0 | 1.0 | excluded | **excluded** |
| `OPERATOR_EXECUTION` | excluded | excluded | excluded | 1.0 |

**AN UNCHARACTERISED SOURCE GETS WEIGHT `None`, NOT 1.0.** That inversion is
the point: the denylist gave full weight to everything it had not been told
to distrust, which is the wrong direction for a number that sizes positions.
Existing weights are unchanged, and an AST test refuses a returning
`!= "replay"`.

`manual_operator` is excluded from JARVIS statistics not because it is
untrustworthy — it is the most real evidence here — but because it answers a
different question. Calibration asks how often JARVIS'S OWN selection wins;
an outcome shaped by a person's entry timing, venue and exit discipline
cannot answer it, and pooled the number describes neither.

**It has a real reader**: `manual_learning.operator_population()`, reporting
thesis-linked and independent trades APART. A learning row nothing reads is a
log, not learning.

### 5c-bis. The CONSUMER that eligible manual evidence actually moves

Storing a labelled row is not learning from it. `lib/recommendation_calibration.py`
is the consumer an eligible **thesis-linked** manual outcome changes, in the
SAME transaction as the learning row.

**Why no existing consumer could take it** — traced, not assumed. `calibration`,
`expectancy`, `signal_accuracy`, `edge_cost_matrix` and `strategy_lifecycle`
all answer a variant of *"did the trade make money"* from JARVIS's OWN fills;
`decision_quality` needs a `DecisionObservation` with scheduled forward
horizons, which a manual trade has neither of (and it has no production caller
and reads a different store). Feeding a manual trade to any of them attributes
a person's entry timing, venue choice and exit discipline to the executor
being measured.

**What a manual trade honestly proves.** It separates prediction from
execution BY CONSTRUCTION — JARVIS made the claim, someone else produced the
fills. So the learnable quantity is PREDICTION ERROR, every term measured:

    recommended entry  vs  price actually paid   -> entry_deviation_bps
    expected fee       vs  fee actually charged  -> fee_deviation / fee_ratio
    expected funding   vs  funding settled       -> funding_deviation
    expected R         vs  R actually realized   -> r_deviation
    recommended venue  vs  venue actually used   -> venue_followed
    recommended side   vs  side actually taken   -> deviation_class

None of it needs the market path. *"Would the recommendation have hit its
target?"* DOES need the path, this system does not have it for a manual
trade, and answering it from two operator fills would be fabrication — so it
is not answered.

Measured live: recommended 10.00 against a 10.35 fill = **+350bp**; expected
$0.40 against $6.00 charged = a cost model wrong by **15x**; expected 1.8R
against 0.96R realized = **−0.84**.

**THE OPPOSED CASE IS RECORDED, NEVER INVERTED.** JARVIS said long, the
operator went short and made money. Scoring that against the long is tempting
and unsound: the operator's window is not JARVIS's horizon, so the two claims
are about different intervals. `OPPOSED_DIRECTION` gets its own counter and is
never pooled into the followed statistics **in either direction** — tested
from both sides.

**Promotion normalization, at the smallest honest size.** A declared-absent
cost is a waiver, so the sample carries `ACCOUNT_PROMOTIONAL` and is excluded
from every venue-scoped cost figure while remaining in the account-scoped one.
A promotional zero may teach *"this account paid zero"*; it may never teach
*"this venue is free"*. Its DIRECTIONAL evidence still counts — the promotion
changed the fee, not whether the side worked.

**AGGREGATES ARE RECOMPUTED FROM ROWS, NEVER INCREMENTED.** That is what makes
correction safe: re-projection supersedes ONE row in place (keeping the prior
learned values in `previous_values_json`) and every derived figure follows,
with no counter to unwind. A trade that LOSES evidence **withdraws its
measurement while keeping its event row** — a fee becoming unknown does not
un-know that the trade happened.

One thesis contributes once (`uq_reccal_thesis`). A second manual trade on the
same thesis is refused as `REFUSED_THESIS_ALREADY_CONTRIBUTED` — by name, not
silently. An **unlinked** trade contributes nothing and says
`REFUSED_NO_RECOMMENDATION`: there is no prediction to score.

    GET /api/manual/learning/recommendation-calibration[?venue=&product=&account_label=]

### One trade, one row, forever

The learning row's id IS the manual trade id, so `uq_trade_outcomes_canonical`
makes one-trade-one-vote a database fact. Four legs and two funding events
still make ONE row; corrections are not observations.

**Correction after projection FAILS CLOSED** to `PENDING_REPROJECTION`: the
stale row is neither silently revised nor duplicated. Re-projecting revises
that single row IN PLACE.

> **That is safe HERE and would NOT be safe for the virtual book.** Manual
> rows feed no INCREMENTAL aggregate — pattern memory and regime performance
> are excluded by admission — so revising one double-counts nothing. A
> canonical outcome cannot be revised this way, because its first projection
> already incremented those counters.

### Percentages travel with their denominator

    MARGIN                  collateral wholly OWN_CAPITAL
    MARGIN_MIXED_CAPITAL    collateral partly promotional/borrowed
    NULL / NULL             no evidenced collateral -> NO percentage at all

$10k owned plus $10k of non-withdrawable credit is not $20k of equity, and
the label is what stops the two being read alike.

### Corrections append, never overwrite

Operators mistype; statements settle late. `manual_trade_corrections` keeps
the previous value, the new value, the author, the evidence and the time. A
corrected book that cannot show what it used to say cannot explain why the
original disagreed. A correction that would break the book is refused with
the original intact.

## 6. Product-aware financing — the forbidden inferences

`lib/product_cost_profile.py` is the authority. A product pays only the costs
that exist for that product, on affirmative evidence:

    leverage > 1                 does NOT imply borrowing
    notional - margin            is NOT a loan principal
    short                        does NOT imply the underlying is borrowed
    funding                      is NOT interest on borrowed notional
    "brokers normally charge X"  is NOT evidence

Leverage lives in the CONTRACT — a perpetual short borrows no coin, a futures
short borrows no shares, initial margin is collateral and not a down payment.
Financing applies only where something is actually extended (margined spot, a
real equity short). Perpetual funding is a SIGNED TRANSFER, modelled
separately, never as borrow interest.

## 7. Invariants — do not weaken

- **Committed margin is not a loss.** At entry free cash falls by margin +
  fee; economic equity falls by the fee alone.
- **Per-leg costs.** Entry fee charged once at entry. Spread/slippage/impact
  live INSIDE the fill price — never subtract them again.
- **Catastrophic product gate ≠ trade expectancy ≠ risk.** The structural
  product test uses `fee / notional`, **never `fee / margin`**.
- **Whole contracts FLOOR** to the authorization, never ceil into more
  exposure.
- **A refusal is not a loss.** Venue/data/capability refusals never land
  against the thesis.

## 8. DEX wallet and funding authority

`lib/dex_wallet.py`. Balances live in `dex_balances` and are the ONLY economic
authority; a caller-supplied balance may only SHRINK what the ledger permits
and can never initialise or replace it.

**PROVENANCE IS NOT AUTHORIZATION.** `fund_wallet()` takes a sealed
`FundingGrant` from an issuer — naming an authority string funds nothing:

    CONFIGURED_VIRTUAL_ENDOWMENT  issue_endowment_grant(); mint and quantity
                                  must MATCH JARVIS_DEX_VIRTUAL_ENDOWMENT
                                  exactly. Unconfigured = empty wallet, never
                                  a default one.
    OPERATOR_GRANT                issue_operator_grant(); requires the
                                  JARVIS_DEX_OPERATOR_GRANT_APPROVAL secret,
                                  which is UNSET. No operator-grant workflow
                                  exists, so the authority is CLOSED rather
                                  than open with a friendly name.
    TEST_FIXTURE                  issue_test_fixture_grant(); refuses unless
                                  JARVIS_UNDER_PYTEST=1. Composed with the
                                  operator-DB refusal in §3, fixture money
                                  provably cannot reach canonical state.

Every credit writes balance + `DexFundingEvent` in one transaction, carrying
authority, actor, amount, asset, reason, policy version, event id, timestamp.

## 8b. DEX ledger authority — which book owns what

TWO STORES, ONE ECONOMIC EVENT.

    dex_wallet    DexBalance / DexFundingEvent
                  CANONICAL ASSET AUTHORITY for SOL and SPL balances.
                  Funded only through a sealed FundingGrant (§8).
    dex_paper     DexPortfolio / DexPosition / DexTrade
                  CANONICAL P&L / ACCOUNTING VIEW in USD, and the only
                  path that opens and closes positions today.

They were accidentally split-brained: the wallet was CONSULTED as an
authority by the autotrade gate, the venue adapter and the exit — "can you
afford this?" — and then never debited by any production path. The same 5
SOL could authorise an unlimited number of transactions, because a gate that
never charges is not a ledger.

**THE ASSET THAT PAYS A COST LOSES THAT ASSET.** Gas is paid in SOL, so
`dex_wallet.charge_network_fee()` debits the wallet once per leg, joining the
caller's transaction — the gas debit and the position it pays for are ONE
economic event. A fee larger than the balance raises `FeeAccountingInvariant`
rather than clamping; clamping would leave the book richer than reality by
the shortfall.

**USD ATTRIBUTION IS NOT A SECOND FEE.** Cash falls by the notional alone and
rises by the proceeds alone. `net_pnl` still subtracts both network legs,
because that is the USD VALUE of the SOL consumed — one expense in the
reporting unit, not a second expense.

    fee_settlement = SOL_WALLET          a persisted wallet paid, in SOL
    fee_settlement = USD_BOOK_NO_WALLET  no wallet exists, so the USD book
                                         paid — still exactly once

Refusals stay free: the entry checks the fee payer BEFORE committing and
returns `entry_insufficient_gas`; the exit returns
`EXIT_PENDING_INSUFFICIENT_GAS`. Rejected-before-chain consumes no SOL;
failed-after-chain may; success consumes it exactly once.

`settle_swap_success` / `settle_swap_failure` remain the full asset-exchange
primitives and still have **no production caller** — the USD book performs
the exchange in its own units and settles only the gas leg in the wallet.

## 8c. Valuation authority — display is not truth

`exit_quote` is the ONE exit pricer, used by settlement AND by valuation, and
it labels which happened:

    AUTHORIZED_BID            settlement: a measured, authorized network fee
    STATIC_VALUATION_DEFAULT  valuation: the static constant

**STATIC_VALUATION_DEFAULT is DISPLAY ONLY, and proven so.** Gas is paid from
the wallet and is not deducted from the pool's output, so moving the static
constant by four orders of magnitude leaves `equity_executable_usd` and
`open_value_executable_usd` bit-identical while only the per-row display
figure moves. `summary()` reaches **no provider at all** — a UI refresh must
not become a provider storm.

**POOL EXECUTABILITY AND GAS EXECUTABILITY ARE DIFFERENT QUESTIONS.**

    open_value_executable_usd             what the AMM could return
    gas / gas_blocked                     can the fee payer transact at all
    executable_after_all_constraints_usd  what is actually reachable today
    gas_blocked_pool_value_usd            reported, NOT zeroed

The summary's gas view is the STATIC operability floor, labelled
`STATIC_POLICY_ONLY`; the live fee market is measured at settlement only.
Blocked is not zero, and UNKNOWN is not zero.

## 9. Solana dynamic fee authority — WIRED into canonical execution

`lib/solana_fees.py` MEASURES. `lib/solana_fee_policy.py` decides what the
operator will pay. `lib/dex_network_cost.py` is the ONE place canonical DEX
execution composes the two, and it has real callers on BOTH sides of a trade:

    dex_autotrade.evaluate_candidate   the expectancy gate        (entry)
    dex_paper.open_dex_position        the booked entry, inheriting the
                                       gate's authorized bid      (entry)
    execution_venue.VirtualDexAdapter  before simulated submission (entry)
    dex_paper.close_dex_position       the settled exit           (EXIT)
    dex_wallet.gas_state               reserve = the AUTHORIZED BID

**Entry and exit are symmetric in authority and asymmetric in policy.** An
exit that measures nothing would teach "expensive to get in, cheap to get
out" — a market that exists nowhere, flattering exactly the positions a real
desk finds hardest to close.

    exit action          default priority     ceiling
    NORMAL_EXIT          HIGH                 0.002  SOL
    URGENT_EXIT          VERY_HIGH            0.0035 SOL
    SEVERE_RISK_EXIT     MAX_ACCEPTANCE       0.0035 SOL

`exit_action` is DECLARED by the caller (`URGENT_RISK_EXIT` is an accepted
alias); an unrecognised one is refused rather than widening a ceiling.
Measured live 2026-08-20 on the real SOL/USDC pool: a normal exit at HIGH
cost 77,000 lamports, an urgent exit at VeryHigh cost 2,862,143 — above the
normal ceiling, inside the emergency one. The asymmetry is load-bearing
today, not theoretical.

A NORMAL_EXIT fails closed on an unknown fee. A funded wallet that cannot
pay the authorized bid holds the position `EXIT_PENDING_INSUFFICIENT_GAS`:
selling the last SOL needed to execute the sale is not an executable exit.
A refused exit charges nothing and leaves the position open.

Sequence: identify the ACTION -> select an allowed PRIORITY LEVEL -> gather
real writable-account context -> MEASURE -> AUTHORIZE -> check the persisted
SOL balance -> only then submit. A NORMAL_ENTRY refuses on an UNKNOWN
estimate, on a measured fee above policy, on a fee that destroys expected
edge, on the notional cap, and on insufficient persisted SOL.

**Measurement happens BEFORE any write transaction opens**, and the
invariant is proven behaviourally: the test's injected estimator performs an
independent write from a second connection mid-measurement, which can only
succeed if no write lock is held.

**Health telemetry can never block its caller.** `provider_health.record`
opens a SECOND connection, so a caller holding the write lock made it wait
out the engine's 30s busy timeout — a silent stall, not a visible deadlock.
Fixing the call site was not enough (it recurred from a caller that owned
the transaction), so the health write now takes a **250ms busy timeout and
gives up**. Losing a health row is a small honest cost; stalling a fee
estimate to record one is not.

**Hermetic by construction**: the estimator refuses under pytest unless
`JARVIS_REAL_PROVIDER_TESTS=1`. Tests inject `fetch=` / `fee_fetch=`.

### Three facts, never collapsed

    MEASURED    NetworkFeeEstimate — what the fee market indicated.
                No policy has touched it; there is no `capped` field.
    AUTHORIZED  FeeAuthorization — the BID. May sit deliberately below the
                measurement, and says so via
                `bid_below_measured_requirement`.
    ACTUAL      what the chain took. The ONLY one ever charged, exactly
                once. None means "not established", never zero.

`ExecutionResult` carries all three plus `fee_provenance`. Learning never
recomputes an execution cost — `RealizedOutcome` stays the authority.

**The network fee is CHARGED, not merely recorded.** It used to be measured,
stored on the position and reported in `total_costs_usd` while cash fell by
the notional alone and rose by the proceeds alone — so `net_pnl` omitted both
network legs and a round trip could report a profit it had not made. Each leg
is charged exactly once at the moment it is incurred, and the entry and exit
legs stay separately answerable: merged, they cannot say which half was
expensive.

### Priority level and action policy are ORTHOGONAL

`ECONOMY|NORMAL|HIGH|VERY_HIGH|MAX_ACCEPTANCE` answer *how hard to bid*.
`NORMAL_ENTRY|NORMAL_EXIT|URGENT_EXIT|SEVERE_RISK_EXIT` answer *which
economics apply*. A HIGH-priority ENTRY gets NORMAL_ENTRY caps. The only
coupling runs action -> permitted priority levels; a table mapping
`HIGH -> NORMAL_EXIT` used to run it the wrong way and is gone.

### Authority hierarchy and context

    priority fee   1. Helius getPriorityFeeEstimate   (primary)
                   2. getRecentPrioritizationFees     (fallback)
                   3. nothing -> UNKNOWN. Never a constant, never zero.
    base fee       protocol per-signature constant, 5,000 lamports
    compute units  400,000 = DEFAULT_BUDGET_ASSUMPTION (see UNKNOWNs)

Context quality is part of the answer, best first:
`TRANSACTION_SPECIFIC` -> `LOCAL_ACCOUNT_SET` -> `GLOBAL_ESTIMATE`. The
writable accounts of a swap (pool, mint, wrapped SOL) reach BOTH the Helius
primary and the RPC fallback, which are labelled `LOCAL_ACCOUNT_FALLBACK` vs
`GLOBAL_FALLBACK`.

**A global zero is not proof about a specific transaction.** Measured
2026-08-20 on the same call at the same moment: the account-aware form
returned **12.0 micro-lamports/CU**, the accountless form **0.0**. The
accountless fallback would have bid ZERO priority for a transaction facing a
real local market.

**`priorityLevel` is CASE-SENSITIVE and Capitalised**: `Low | Medium | High |
VeryHigh | UnsafeMax`. Lowercase `"high"` returns **-32602 Invalid params**.
That bug silently disabled the primary for a whole phase because the fallback
answered every call — the reason `fee_estimator_health()` exists.

**Units are part of the type.** Both estimators return **micro-lamports per
compute unit**:

    priority_fee_lamports      = ceil(price_ulam_per_cu * CU / 1e6)
    total_network_fee_lamports = base_fee_lamports + priority_fee_lamports
    SOL                        = lamports / 1e9

Computed in `Decimal` — the UnsafeMax product is ~6.4e16, past float64's
consecutive-integer limit. Helius returns a **float**; `SetComputeUnitPrice`
takes a u64, so `executable_compute_unit_price_micro_lamports()` quantizes
ONCE (ceiling) before the fee is derived, and that same integer is what gets
persisted. Deriving from one value and storing another is a real defect that
was fixed, not a hypothetical.

**UnsafeMax is structurally NON-EXECUTABLE** — refused at the call site by
`assert_executable_level()`, not merely left unselected, so re-adding it to
the level table cannot quietly enable it. Measured: ~64.14 SOL (Phase 6) and
~56.59 SOL (2026-08-20) on a 400k-CU transaction. It is ~64 SOL, **not
~64,000 SOL**; the old figure divided by 1e6 twice instead of 1e6 then 1e9.

### Operator fee policy (`solana_fee_policy_v2`) — willingness to pay

Absolute **total-network-fee** ceilings. These are policy, not gas prices,
not expected costs, not Solana constants, not a promise of inclusion:

    NORMAL ENTRY                 0.002  SOL
    NORMAL EXIT                  0.002  SOL
    URGENT RISK REDUCTION        0.0035 SOL
    SEVERE RISK REDUCTION        0.0035 SOL

The priority ceiling is DERIVED (total minus base fee) so the two cannot
drift. The emergency fallback defaults to and is CLAMPED at the severe
ceiling. Percentage caps on expected edge and notional apply independently. A
live estimate above policy **REFUSES** (`FEE_EXCEEDS_AUTHORISED_POLICY`); the
ceiling is never widened to meet the market. All values are env-overridable
per action and carry a policy version.

## 10. Provider health and fallback observability

`lib/provider_health.py` is the single health surface — (provider, capability)
keyed, failure classes distinguished (`RATE_LIMITED` / `AUTH_FAILED` /
`PAYMENT_REQUIRED` / `STALE` / `DEGRADED` / `UNAVAILABLE` / `DISABLED` /
`NOT_CONFIGURED` / `HEALTHY`), errors sanitised before storage. Do not build a
second monitoring architecture.

`solana_fees.fee_estimator_health()` reads that table and answers the one
question per-provider rows cannot: **is the fallback covering for a primary
that never works?** Surfaced on `GET /api/providers/health` under
`solana_fee_estimator`. Malformed requests escalate on the FIRST occurrence
(retrying cannot fix them); a single transient blip does not escalate.

**Telemetry may be best-effort; its loss may not be invisible.** The health
write gives up after 250ms rather than blocking economic execution behind
SQLite's write lock, and every abandoned write is counted, timestamped and
attributed per provider — surfaced in the same payload as
`dropped_health_writes`. A silently vanishing health row is precisely how a
dead primary comes to look healthy.

## 10b. Provider audit — measured 2026-08-20, read-only

Every result below came from a live READ. No purchase, upgrade, integration
or refactor; no webhook created, modified or deleted.

**33 keys in `.env`, 13 providers with credentials.** No secret appears here
or in any test, log or payload.

| provider | credential | verified capability | tier | disposition |
|---|---|---|---|---|
| **Helius** | present | RPC health/version/balance/signatures, **DAS** getAsset + getAssetsByOwner, **getPriorityFeeEstimate**, **Enhanced v0** transactions, **Wallet v1 transfers**, webhooks LIST | paid, all probed products entitled | KEEP_PRIMARY |
| **Alpaca** | present | account, **quotes feed=sip 200**, **options feed=opra 200**, iex, crypto | **PAID CONFIRMED — SIP and OPRA both entitled** | KEEP_PRIMARY |
| **Kraken** | present | private read-only authenticated, **7/7 scopes granted, 0 missing**; public + futures keyless | direct access confirmed; fee tier taker 0.8 / maker 0.4 at $0 volume | KEEP_PRIMARY |
| **Massive** | present | `get_previous_close_agg` via the `massive` SDK (pinned 2.8.0) | working | KEEP_SECONDARY |
| **TwelveData** | present | api_usage + quote | **`plan_category: grow`** | REPAIR_CONFIGURATION (below) |
| **CoinGecko** | present | demo `/ping` 200 | **DEMO, not Pro** — pro host replies "use api.coingecko.com" | KEEP_SPECIALIZED |
| **AllRates** | present | `/rate` 200 via Bearer | free tier, 300-request LIFETIME cap | KEEP_SECONDARY |
| **FRED / EIA / OpenFIGI** | present | series / v2 / v3 mapping, all 200 | free | KEEP_SPECIALIZED |
| **Tavily / Stocklake** | present | MCP `initialize` 200 — **5** and **17** tools | working | KEEP_SPECIALIZED |
| **Exa / Firecrawl** | **absent — and not needed** | MCP `initialize` 200 **KEYLESS**, **2** and **3** tools live | free tier | KEEP_SPECIALIZED |
| **LunarCrush** | present | **HTTP 402** | credential VALID, **subscription inactive** | DEFER — do not repurchase without review |
| **Massive MCP** | n/a | **401 on every auth style, 0 tools** | `mcp.massive.com` wants a JWT; `MASSIVE_API_KEY` is a REST key | REPAIR_CONFIGURATION |

> **A missing key is not proof a provider is unavailable.** Exa and
> Firecrawl were first classified NOT_CONFIGURED purely because
> `EXA_API_KEY` / `FIRECRAWL_API_KEY` are unset — then answered keyless with
> working tools. Five of the six MCP servers are live; only Massive's is
> dead. Probe before classifying, in both directions.

### Misconfigurations found (none corrected in place — evidence only)

- **SIX `HELIUS_*` keys in `.env` have ZERO references anywhere** — not in
  code, tests, docs or `.env.example`: `HELIUS_ACTIVE_WALLET_LIMIT`,
  `HELIUS_SMART_MONEY_LIMIT`, `HELIUS_MIN_WALLET_TRADES`,
  `HELIUS_MIN_SMART_MONEY_SCORE`, `HELIUS_MIN_COPY_SCORE`,
  `HELIUS_DISCOVERY_LOOKBACK_DAYS`. The operator authored six tuning knobs
  that configure nothing. Conversely five knobs the code DOES read
  (`HELIUS_PAGE_LIMIT`, `_MAX_PAGES_PER_POLL`, `_BACKFILL_LIMIT`,
  `_MAX_RETRIES`, `_MIN_CALL_SPACING`) are absent from `.env` and run on
  defaults.
- **`TWELVEDATA_RPM=377` contradicts the repo's own mapping.** `lib/twelvedata`
  documents `8 = Basic, 55 = Grow, 610 = Pro`; the measured plan is `grow`.
  377 is exactly the `plan_limit` field, so it looks copied from there. The
  code's own warning applies: an RPM the plan disallows earns 429s. **Not
  changed** — which figure is right is a question for the provider.
  UNKNOWN_PENDING_PROVIDER_EVIDENCE.
- **`mcp_client` maps a REST key onto an MCP endpoint for Massive.** The REST
  path works; the MCP entry returns 401 every call.

### `WALLET_QUEUE_URL` / `WALLET_QUEUE_SECRET` — the answer is: they do not exist

They appear in **one planning document**
(`JARVIS_MASTER_UI_UX_HELIUS_WALLET_ALPHA_PROMPT.md` §92) and nowhere else —
not in `.env`, not in `.env.example`, and read by no code. Nor is
`HELIUS_WEBHOOK_AUTH_SECRET`. There is no webhook receiver, no queue, no
gateway, and no worker.

- **They are NOT Helius settings.** That document already says so: Helius
  authenticates with `HELIUS_API_KEY`; the queue pair was proposed as
  *internal JARVIS service-to-service* configuration.
- **Nothing is required, and nothing breaks when they are absent.**
- **Helius never receives either value.**
- **JARVIS monitors wallets today WITHOUT them**, by POLLING:

      HELIUS_WATCH_WALLETS -> wallet_activity.collect_once()
        -> GET api.helius.xyz /v1/wallet/{addr}/transfers  (page<=100)
        -> parse_transfers (amount, never amountRaw/decimals)
        -> wallet_observations, idempotent by dedup_key
        -> wallet_registry / wallet_scoring -> On-Chain Desk

  `lib/wallet_activity` states the reason in its first line: the webhook
  design was **dropped rather than hardened**, because this machine holds
  every venue credential and accepts no inbound connection. Polling reaches
  the same data with no internet-facing host to isolate or patch. Latency is
  the trade and does not bind — wallet flow is slow context, never entry
  timing.
- **A queue is unnecessary at this scale**: 5 watched wallets.

### 10c. Helius wallet polling — RUNNING, and separate from the scheduler

`lib/wallet_poller.py` is one timer calling one existing function. The
pipeline was complete and verified; nothing called it, because the legacy
scheduler owned that job and **stays disabled** — it also executes signals,
opens paper positions and manages the book. Wallet intelligence and live
trading were a single switch; they are not any more.

    JARVIS_HELIUS_WALLET_POLLING_ENABLED     true   (operator .env)
    JARVIS_HELIUS_WALLET_POLL_INTERVAL_SECONDS  900

- **OFF unless explicitly enabled, and a typo cannot fake it.** `bool("false")`
  is True, so the flag is read against an ALLOWLIST — `1/true/yes/on/enabled`.
  "false", "no", "0", "off", "disabled", "maybe" all leave it stopped.
  Interval clamped to `[60, 86400]`; a malformed value falls back.
- **900s is the cadence this job already had**, so it is what the existing
  pagination, pacing and backfill budgets were sized against. Measured: a
  pass over 5 wallets costs **5 provider calls** and takes **~20s** — two
  orders of magnitude of headroom, so no pass can lap another.
- **Overlap is refused, not queued.** One thread runs poll-then-sleep, and a
  non-blocking lock turns any concurrent entry into a counted refusal.
- **A FAILED pass is not an EMPTY pass.** On failure the counts stay `null`
  rather than dropping to zero — "we could not reach Helius" and "we looked
  and the chain was quiet" are different facts. A quiet chain still reports
  `0`, because that IS a measurement.
- **No address, key or credential reaches the status payload.** The collector
  prefixes its own errors with `{address[:8]}…`, so the prefix is stripped
  and anything else address-shaped is scrubbed to `<wallet>`.
- **Independent of `JARVIS_DISABLE_SCHEDULER` in both directions** — the
  poller never reads it, and `start()` sits OUTSIDE the scheduler branch in
  `lifespan`, asserted on the AST.

`GET /api/onchain/wallet-polling` reports enabled / running /
last_started_at / last_completed_at / last_result / next_run_at / observed /
inserted / deduplicated / provider_calls / last_error.

**Verified live, twice, then again across a restart:**

    pass 1   287 observed   287 inserted     0 deduplicated   5 calls
    pass 2   287 observed     0 inserted   287 deduplicated   5 calls
    restart  280 observed     0 inserted   280 deduplicated   5 calls

Stored `helius:` events unchanged at **20,722** across the restart —
restart safety comes from the existing `dedup_key`, not from any state the
poller holds. The 287 genuine observations collected during verification
were KEPT.

### 10d. Wallet SHADOW intelligence — visible at `#onchain`

`lib/wallet_event_classifier.py` + `lib/wallet_shadow_intel.py`. Stored
transfers become classified economic events, deterministic theses or named
refusals, forward checkpoints, and a source-isolated performance view — all
rendered on the EXISTING On-Chain Desk.

    http://127.0.0.1:3000/#onchain      Nav rail -> On-Chain Desk

Six panels: Helius Wallet Polling · Wallet Intelligence — Classification ·
Shadow Theses · Refusals · Forward Outcomes · Recent Classified Activity.
Routes: `/api/onchain/shadow/{summary,events,theses,refusals}` and
`POST /api/onchain/shadow/process`.

**A TRANSFER IS NOT A TRADE.** The feed carries no program or instruction, so
only a PAIRED SWAP can be established: one signature moving a quote asset out
and a token in. Everything else stays `UNKNOWN_TRANSFER`.

**MULTIPLE LEGS ARE NOT MULTIPLE VOTES.** Measured on the live store:

    20,785 transfer legs -> 3,890 signatures -> 1,266 market observations
    16.4 legs and 3.07 signatures per observation

Wallets acting on the same token within `CLUSTER_WINDOW_SECONDS` (900) are ONE
observation; all contributing wallets are kept as evidence. `uq_wse_cluster`
makes it a database fact.

**Live counts (2026-08-20):** 411 `CLASSIFIED_TRADING_EVENT`, 402
`PARTIAL_EVIDENCE`, 321 `UNKNOWN`, 132 `CLASSIFIED_NON_TRADING_EVENT`.
**0 eligible theses; all 1,266 refused** — 411 `UNKNOWN_WALLET_QUALITY`, 402
`PARTIAL_TRANSACTION_EVIDENCE`, 321 `UNKNOWN_EVENT_TYPE`, 132
`NON_TRADING_TRANSFER`. That is the gate working: **1 of 1,086 registry
wallets carries a usable score**, and an unproven wallet is not a neutral one.
Nothing was weakened to fill the theses table, and the page says so.

> **TWO DEFECTS THE DATA AND THE TESTS FOUND — both produced confident wrong
> answers.**
>
> **Native SOL is a 43-character pseudo-mint ending `…111`, one character
> from WSOL's `…112`** — `NATIVE_SOL_PSEUDO_MINT`. It is **14,610 of 20,778
> legs (70%)** and reports `symbol == "SOL"` in 100% of them, which is how it
> was identified: by counting, not by recognising a prefix. Omitting it made
> SOL the SUBJECT of ordinary SOL-for-token swaps and found 13 trades where
> there are 1,883.
>
> **`all(... for l in legs if l.counterparty)` is vacuously TRUE** when no leg
> has a counterparty, so every unknown-counterparty group was called a
> `SELF_TRANSFER`. At least one counterparty must now be present.
>
> Also: taking `legs[0]` as the subject skewed the result **1,879 buys to 10
> sells** — alphabetical order, not a market. The subject is the largest
> non-quote leg. And 904 signatures move a quote asset OUT and a token OUT
> with nothing back: no consideration observed, so not a sale.

**Prices come from the existing snapshot store, near the EVENT** — never
today's price, never the wallet's later exit. Missing stays missing, stale is
refused by name, `UNRESOLVED` is never a loss, every return is shown before
AND after an assumed 3% round trip, and no expectancy is stated below 20
resolved samples.

**Isolation by construction**: own tables (`wallet_shadow_events`,
`wallet_shadow_outcomes`), `source = HELIUS_WALLET_INTELLIGENCE`,
`execution_mode = SHADOW`, and **nothing written to `trade_outcomes`** — so no
consumer has to remember to exclude it. Idempotent on `cluster_id`.

**Browser-verified** against the live operator database: real counts, **zero
full addresses or mints rendered**, missing shown as `UNKNOWN`/`—` never `0`,
no console errors, every request 200, 3 fetches in a quiet 25s window.

### Observability gaps (reported, not fixed)

- **`provider_health` tracks 2 of 13 providers** — Helius' fee estimator and
  LunarCrush. `/api/providers/health` is truthful about what it tracks and
  that is almost nothing. There is also a SECOND health system,
  `intelligence_source_health`, for news sources.
- **Failure handling is uneven.** Alpaca distinguishes 401/403/404, Helius
  401/404/429, LunarCrush 402; TwelveData, Massive and AllRates handle 429
  only; `crypto_market_data` and `fred_client` distinguish no status at all —
  they swallow every exception and return `None`/`{}`, which fails closed and
  never fabricates a zero, but cannot tell "unpaid" from "unreachable".
- **AllRates fails closed hard and remembers**: a 429 disables it
  permanently, keyed to a hash of the credential so a new key clears it —
  the free tier's 300-request cap never resets.

## 11. Tests and CI

    full suite   4,732 passed - 16 skipped - 0 failed, exit code 0 (acca096)
    Ubuntu CI    all six jobs green
                 runtime contract - frontend typecheck+build - secret scan
                 - pytest - migration/bootstrap - dependency audit

Never carry a red typecheck or a failing test as "pre-existing".

## 12. Remaining UNKNOWNs — do not paper over these

- **`simulateTransaction` is not yet authoritative** — there is no canonical
  transaction builder/message to simulate.
- **`getFeeForMessage` is not yet authoritative**, for the same reason.
- **400,000 CU remains `DEFAULT_BUDGET_ASSUMPTION`** until measured. It must
  not harden into a measurement.
- **The GLOBAL fallback still reports 0.0** while the account-aware form
  reports a non-zero local market. Canonical execution always supplies
  accounts, so this bites only a caller that supplies none.
- **The USD book still holds token positions in USD, not as SPL balances.**
  The wallet is the asset authority for SOL and settles every gas leg, but a
  DEX position is still a `DexPosition` row valued in USD rather than a token
  balance in `dex_balances`. The two agree on gas and on P&L; they are not
  yet one representation of the same holding.
- **USD attribution of a SOL fee uses the caller's `sol_price_usd`.** Whatever
  price the quote used values the fee. There is no independent SOL valuation
  authority with its own freshness and source.
- **Fractional priority-fee estimates are unobserved live** — the quantizer is
  contract-driven and defensive, not empirically calibrated.
- **BTCC accounting / funding / cross-margin liquidation details remain
  UNKNOWN** wherever not backed by authoritative realized evidence. Do not
  infer them from other venues.
- **`RealizedOutcome` has no dedicated liquidation-fee field.** A manual
  `LIQUIDATION_FEE` or `OTHER_VERIFIED_COST` is folded into `commission_usd`
  and the exact dollars are recorded in
  `provenance["cost_category_map"]`, so the fold is auditable and
  reversible. Dropping them would understate the trade; adding a field would
  ripple into the persisted schema and the learning projection's validation.
  Worth revisiting, deliberately.
- **No option product exists.** `lib/instruments.py` carries no option
  contract identity, so `manual_execution` REFUSES `OPTION` rather than
  recording it as a near-miss product with the wrong economics. Manual
  option trades cannot be recorded until that identity exists.
- **Manual trades ARCHIVE rather than copy on a canonical epoch cutover.**
  Classified `ARCHIVE_ONLY_ECONOMIC`. Unlike everything else in that class
  the case is genuinely arguable — the retired simulator neither produced
  nor corrupted them — but copying money-bearing rows into a fresh book is
  the one mistake a cutover cannot undo, and their thesis links point at
  signals that do NOT cross. **Promoting them to a COPY class is an operator
  decision and has not been made.**
- **No manual position MONITORING exists yet.** The data model supports it
  (open quantity, stop/target distance, funding, liquidation inputs) and no
  code computes it. `estimate_cross_liquidation` still returns UNKNOWN for
  every venue, which is correct until a venue's maintenance tiers are
  evidenced.
- **Manual outcomes feed `recommendation_calibration` and nothing else**
  (§5c-bis). They remain excluded from every JARVIS-execution statistic, and
  that exclusion is correct rather than pending: those measure how JARVIS
  executes, and JARVIS did not execute these.
- **`recommendation_calibration` is measured but not yet ACTED ON.** Nothing
  reads it to adjust a fee estimate, a size or a gate. Wiring a measured
  cost error back into the live cost model changes execution economics and
  is a separately reviewed step — deliberately not taken here.
- **UNLINKED manual trades reach no calibration consumer at all.** An
  explicit consumer-level REFUSAL, not an omission: they carry no
  prediction, so there is nothing to score without fabricating a thesis.
  They remain fully visible in `operator_population()`. If operator-skill or
  execution-economics analytics are wanted, that is a new consumer with its
  own review.
- **Directional evidence comes only from FOLLOWED trades.** Opposed trades
  are counted and never scored. Turning disagreement into evidence needs a
  horizon-matched comparison this system cannot currently make.
- **`realized_outcome.finalize` classifies on a bare sign test**, so a
  round trip whose costs cancel its gross to within float noise
  (~1e-14) is recorded as a LOSS rather than BREAKEVEN. Observed while
  building the manual poison case. It affects the VIRTUAL book identically
  and is not manual-specific; changing the WIN/LOSS threshold is a
  system-wide decision and was deliberately not made here.
- **`_refresh_signal_accuracy_conn` still pools replay with live**, and has
  no epoch filter. Both predate this work; only the manual exclusion was
  added, because widening that change needs its own evidence.
- **No supersession exists in canonical learning.** A corrected MANUAL
  outcome revises its single row in place, which is safe only because
  manual rows feed no incremental aggregate (§5c). A corrected VIRTUAL
  outcome after APPLIED still reports `LEARNING_STATE_CORRUPT` and needs
  operator repair — unchanged, and still the honest fail-closed answer.
- **Host WHEA / `HOST_HARDWARE_UNSTABLE` remains unresolved** unless new
  evidence proves otherwise. It is a host-level concern and is not
  represented anywhere in this repository.
- ~~`HELIUS_WATCH_WALLETS` is EMPTY~~ **STALE — corrected 2026-08-20.** It
  holds **5** valid 44-character addresses, and the registry has run: 5 seed,
  944 candidates, 142 excluded entities, 1,086 rows. The old claim below is
  kept struck through because it is exactly the sort of line a later session
  would otherwise trust.
  (The struck claim continued: "so Wallet Alpha renders NOT CONFIGURED —
  correct, not broken." It is not correct any more.)
- Execution phases needing a funded Solana keypair are BLOCKED: there is no
  Solana signer in the repo and key material is not handled here.

## 13. Next phase — NOT STARTED

Phases 6.2 and 6.3 are COMPLETE: DEX entry and exit both measure. Candidates
next, in no fixed order and none of them started:

- **A canonical unsigned transaction builder** — what would turn
  `simulateTransaction` and `getFeeForMessage` from UNKNOWN into measured,
  and 400k CU from an assumption into a number. Needs no signer and no key
  material; building one is not the same as being able to submit.
- **Reconcile the two DEX books**, so the USD position book and the SOL gas
  ledger stop being separate economies.
- **Provider entitlement audit** — what is actually being CONSUMED from
  already-paid Alpaca/Helius/Kraken access, before any new provider is
  considered. Inventory taken 2026-08-20 (repo/config/runtime only, nothing
  probed): keys configured for Alpaca, Helius, Kraken, CoinGecko,
  LunarCrush, TwelveData, Massive, Stocklake, AllRates, Tavily, OpenFIGI,
  FRED, EIA. **Measured live: LunarCrush returns HTTP 402 — the credential
  is valid and the subscription is inactive**, so it is configured and
  unusable. CoinGecko and LunarCrush are already-held keys, NOT new
  purchases. WebSocket consumers are bitnomial, kraken_stream,
  orderbook_stream, td_forex_stream. What is actually CONSUMED from each
  paid tier — Alpaca SIP, OPRA, Helius paid features, Kraken direct — is
  NOT established and is the audit's job.
- **The Manual Trade Desk BACKEND and its LEARNING BRIDGE are DONE**
  (§5b, §5c). What is not built: monitoring of open manual positions, the
  UI, and any admission of manual evidence into a JARVIS statistic — the
  last of which needs promotion normalisation first (§12).

### Manual Trade Desk — UI, when it is built

**ADDITIVE. The existing design language is kept, not rebuilt.** The front
end is Svelte 5 runes, `frontend/src/lib/`. A desk fits the existing shape
with no new patterns:

    sections/ManualDesk.svelte    a new section beside VirtualCex/VirtualDex
    stores/section.svelte.ts      add `manualdesk` to SectionId + SECTIONS
    components/NavRail.svelte     one icon; it belongs in the existing
                                  "Virtual Trading" group renamed to cover
                                  real manual execution, or its own group
    lib/api.ts                    hand-mirrored types for /api/manual/*

Reusable as-is: `Panel`, `KpiTile`, `StateBadge`, `StateNote`, `Pill`,
`ColumnChooser`, `VirtualList`, `EdgeCostMatrix`. Panel-level popout and the
command palette work for free. `GET /api/manual/vocabulary` exists so the
entry form never invents a product, unit, state or evidence rank.

Two things the UI must not do: show a manual position inside the virtual
book's exposure or equity curve, and render an UNKNOWN cost as 0.00 —
`StateNote` already carries the "not measured" idiom used elsewhere.

UI work is ADDITIVE when it comes: the existing Command Center design
language is kept, not rebuilt. The scheduler stays OFF, and autonomy is
earned through forward evidence rather than granted because code exists.

## 14. Working rules

- Verify repo path, HEAD, working tree and the running process before
  changing anything. Measured state beats any prompt.
- One concern per commit; do not mix documentation with code.
- Read the actual implementation before trusting a description of it —
  including this document.

## 15. The wallet-intelligence cycle — measured 2026-08-21

**THREE COMPONENTS WERE COMPLETE AND NOTHING CALLED THEM.** `wallet_swaps`
decoded full transactions, `wallet_scoring` measured wallets and
`wallet_alpha` measured post-entry moves — each finished, each tested, each
reachable only from the disabled legacy scheduler. `lib/wallet_intel_cycle`
is the caller.

**IT IS NOT A SCHEDULER.** It owns no timer, no thread and no queue. It runs
at the END of each wallet poll, inside `wallet_poller.poll_once`, so the
next cycle is the next poll. Seven stages, in order:

    ENRICH_SWAP_EVIDENCE      bounded getTransaction -> wallet_swaps
    RESOLVE_WALLET_ALPHA      promote proven entries, fill due horizons
    RESCORE_AFFECTED_WALLETS  only wallets whose evidence changed
    COLLECT_PRICE_SNAPSHOTS   quote assets first, then exact mints
    PROCESS_SHADOW_EVENTS     classify/reclassify, gate, persist
    RESOLVE_OUTCOMES          checkpoints whose price is due AND exists
    REFRESH_SUMMARIES         source-isolated performance

A stage that fails is recorded and **the rest still run** — `CYCLE_PARTIAL`.
An unreachable price provider must not stop outcome resolution that needs no
provider. Measured: one live pass is ~75-80s.

### The blocker the phase prompt did not name — and the one that mattered

A watched wallet showed **225 transfer legs, 50 of them token-against-SOL,
and ZERO scoreable round trips.** Not the wallet's behaviour:

    lib/quote_valuation      MAX_BAR_DISTANCE_HOURS = 6
    SOL/USD 1H last bar      2026-08-19T12:00Z
    measured staleness       39.8 HOURS
    SOL over that gap        $77.44 -> $89.40  (+15%)

The hourly SOL series is filled by `lib/ohlcv.fetch_multi_timeframe`, which
was a scheduler job. With the scheduler off it froze, so **every SOL-quoted
round trip was unpriceable and therefore every wallet was unscoreable.** The
guard was right to refuse; the input was missing. The cycle now refreshes it
through the same canonical fetcher. **The 6-hour tolerance was NOT relaxed**
— a stale quote still refuses to value a trade.

Same root cause froze `token_activity_snapshots` at 2026-08-19T13:18Z.

### Bounded, everywhere

    enrichment      40 signatures + 60 provider calls per cycle
                    3 attempts, backoff 300s/1800s/7200s, 0.12s spacing
                    7-day age limit; older signatures are left alone
    scoring         12 wallets per cycle, selected by CHANGED EVIDENCE
    prices          120 mints per cycle, 30 per call, 4 calls
    processing      3-day leg window, not a 20,785-row replay

Enrichment states: `PENDING` `ENRICHED` `PARTIAL` `RETRYABLE_FAILURE`
`PERMANENTLY_UNRESOLVED` `REFUSED_NON_TRADING`. **ENRICHED and
REFUSED_NON_TRADING are both ANSWERS** — "this was a buy" and "this was a
failed transaction" are equally final, and re-reading either spends a call
to learn nothing. Idempotent on `uq_wse_signature_wallet`.

### Reclassification supersedes — it does not vote twice

`cluster_key` hashes the event TYPE. So the moment full-transaction evidence
turns an `UNKNOWN_TRANSFER` into a `TOKEN_BUY`, the cluster identity
**changes** and the old row would sit beside its own replacement as a second
independent observation — a double vote produced by the one pass whose
entire purpose is to correct a classification.

Prior rows are `revision_state = SUPERSEDED`, keep `prior_event_type` /
`prior_classification`, have their unresolved checkpoints EXPIRED, and are
excluded from every read by one predicate (`CURRENT_ONLY`, `_current()` in
the router). Measured on the live store: **7 signatures the transfers feed
could not explain collapsed from 5 clusters to 2.**

Also: a persisted `reference_price_usd` is PRESERVED on reprocess. Snapshots
are pruned by age, so re-deriving a point-in-time fact from what happens to
remain in the store would demote a thesis admitted on evidence that did
exist — the same mistake as using today's price for an old event.

### Prices use POOLS, not the cheaper token endpoint

`include=top_pools` returns the same object `token_surge.snapshot_from_pool`
already flattens, 30 mints per call. The token endpoint would be a third of
the payload and would write rows with NULL transaction buckets — and
`token_surge.baseline_from` coerces a missing `buys_m5` to **zero** when it
takes the median, so every price-only row would drag a token's baseline down
and manufacture a surge in the pass that feeds wallet discovery. A top pool
returned for a mint on the QUOTE side is dropped rather than stored under
the requested mint's identity.

### The bootstrap is not circular

    A. OBSERVED WALLET PERFORMANCE   the wallet's own entries and what the
                                     token did next. `reconstruct_trades`
                                     and `wallet_alpha`. Needs NO thesis.
    B. JARVIS SHADOW PERFORMANCE     how a thesis derived from that wallet
                                     did. `wallet_shadow_outcomes`.

`SCORE_BOOTSTRAP_POPULATION = OBSERVED_WALLET_ECONOMIC_EVENTS`. B is never
fed back into A — pinned by a test that greps for ACCESS, not vocabulary.
`score_registry_wallets` and `score_wallets` both delegate to one
`_score_one`, so there is one wallet score and not two.

### Measured before -> after, one real cycle on the operator database

    market observations (current)     1,266 -> 1,283   (29 superseded)
    BALANCE_DELTA_EVIDENCE events         0 -> 16
    enrichment rows                       0 -> 68      (all NOT_A_TRADE)
    wallets with a usable score           1 -> 2
    registry NEVER_ANALYSED           1,060 -> 1,022
    registry NO_VERIFIED_TRADES          16 -> 55
    event mints priced                   45 -> 166
    event mints FRESH (<3600s)            0 -> 154
    snapshot rows                    10,779 -> 11,097
    SOL/USD 1H staleness              39.8h -> 0.35h
    eligible theses                       0 -> 0
    resolved outcomes                     0 -> 0

New event types only full-transaction evidence can establish:
`NON_ECONOMIC_TRANSACTION` 10, `LIQUIDITY_ADD` 6, `EXCHANGE_WITHDRAWAL` 1,
`FAILED_TRANSACTION` 0 so far.

**All 68 enriched signatures came back NOT a trade** — overwhelmingly "value
arrived and nothing was paid". A token inflow with no payment is not a buy,
and the transfers feed could not tell the difference.

### Why ZERO theses is still the honest answer

Two binding constraints, both measured, neither weakened:

1. **1,261 of the original 1,266 observations carry NO wallet attribution.**
   `watched_wallet` is persisted only from `975b874` forward; every earlier
   row is `LEGACY_PARTIAL` with an empty `wallets_json`. `evaluate()` reads
   contributing wallets from those rows, so even a perfectly scored wallet
   cannot rescue them. Only NEW observations can ever qualify.
2. **No watched wallet has closed a single round trip.** Measured across
   fully-drained Helius history: one seed wallet has 26 OPEN positions and 0
   closed; the others have 1-21 legs total. `MIN_TRADES_FOR_SCORE = 8`
   CLOSED round trips is unreachable for an accumulator. Deeper paging does
   not help — `fully_drained` is already true.

The 2 scored wallets are discovered candidates, not watched ones.

## 16. Wallet intelligence — remaining UNKNOWNs

- **`wallet_trades` is still EMPTY (0 rows), so no observation has been
  promoted to `VERIFIED_BUY_ENTRY` and no post-entry alpha exists.** The
  chain is wired end to end — enrichment lands a ledger row from the same
  fetch, `promote_holder_to_verified_entry` reads it, `wallet_alpha`
  resolves horizons — but every signature enriched so far was NOT a trade,
  so nothing has reached the ledger yet. **0 of 1,397 observations are
  alpha-eligible** (900 `POOL_TX_SIGNER`, 497 `HOLDER_SNAPSHOT`). This is
  the next thing to watch, not a thing to fix by loosening.
- **The 5 watched wallets may simply not be worth watching.** 3 of 5 have
  under 25 lifetime transfer legs. Whether the seed list should change is an
  OPERATOR decision and has not been made.
- **`SNAPSHOT_RETENTION_HOURS = 48` in `token_surge`.** `prune_snapshots`
  runs inside `scan_and_score`, which is a scheduler job and therefore not
  running — but if it ever runs it deletes price evidence older than 48h.
  The persisted `reference_price_usd` survives it (above); a 7d checkpoint's
  own due-time price would be collected fresh at due time, so the horizon is
  reachable. **Not proven end to end, because no thesis exists yet.**
- **`unpriced_trades` was never written.** `reconstruct_trades` returns
  `unpriced_legs`; `_score_one` reads `rec.get("unpriced")`, which is always
  None. Pre-existing, carried forward deliberately rather than fixed inside
  a phase about something else. The column is null for every wallet.
- **Helius `/v1/transfers` returns intermittent 502s.** 7 registry rows sit
  at `analysis_status=FAILED` from exactly that. It is transient — the same
  wallet succeeds on a later pass — and the cycle retries by round-robin on
  `last_analysis_at`, so it self-heals. It is NOT a measurement of zero and
  the existing counts are left intact.
- **`POSITION_INCREASE` / `POSITION_REDUCTION` / `FULL_EXIT` are declared
  and never produced.** They need holdings state across time, which nothing
  computes. Left unproduced rather than approximated.
- **The 3% round trip is still an ASSUMPTION**, labelled `ASSUMPTION`, and
  gross is shown beside net.
- **No bitmap screenshot is obtainable** — the Browser pane does not
  composite in this environment. Verified against the live rendered DOM
  instead: text, geometry (4 panels at 557px, none clipped), network (all
  200), console (no errors), and redaction (**zero** base58 strings of 32+
  characters anywhere in `document.body.innerText`).

