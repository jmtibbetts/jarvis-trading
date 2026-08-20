# JARVIS TRADING — canonical handoff

**HISTORICAL PROMPTS AND OLD REPORTS ARE NOT CURRENT-STATE AUTHORITY.**

**MEASURED CURRENT REPO/RUNTIME STATE OVERRIDES STALE DOCUMENTATION —
including this file.** If a claim here disagrees with the machine, the
machine is right and this file is a bug. Verify before you rely.

This is a CURRENT-STATE document. It is not a diary, a changelog, a
transcript, or an archive of old prompts. Everything historical lives in
`git log`. Keep it short enough to seed a fresh session.

*Last measured: 2026-08-20, at code SHA `ac77450`.*

---

## 1. Runtime and repository

    project        JARVIS TRADING (not EIDOS — do not rename)
    runtime        WSL2 Ubuntu 24.04 ONLY
    active repo    /home/nullcode/jarvis-trading
    interpreter    .venv/bin/python  (never bare python3, never Windows Python)
    remote         origin -> github.com/jmtibbetts/jarvis-trading
    code SHA       ac7745079b2508ac400ba6ce981e1a06924e619e

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

### Verifying that the running server is not stale

`GET /api/system/version` reports `backend_commit` by shelling out to
`git rev-parse HEAD` **at request time, in the repo directory**. It therefore
reports the REPOSITORY's SHA, not the SHA of the code loaded in the process.
**It will report agreement even when the running server is stale**, and it
did exactly that on 2026-08-20. Do not use it as proof.

Prove the running build BEHAVIOURALLY instead — call an endpoint whose shape
changed in the commit you care about — or compare the process start time
(`ps -o lstart= -p <pid>`) against the commit time. Note the SPA fallback
answers any unmatched path with `index.html` and HTTP **200**, so a missing
route looks like a success until JSON parsing fails.

## 2. Posture — currently in force

    JARVIS_PLATFORM_MODE           VIRTUAL_ONLY
    JARVIS_RUNTIME_MODE            FULL_VIRTUAL   (explicit, not defaulted)
    JARVIS_DISABLE_SCHEDULER       1  — no background jobs, no signal execution
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

## 9. Solana dynamic fee authority

`lib/solana_fees.py` measures; `lib/solana_fee_policy.py` decides what the
operator will pay. They are deliberately separate modules.

    priority fee   1. Helius getPriorityFeeEstimate   (primary)
                   2. getRecentPrioritizationFees     (fallback)
                   3. nothing -> UNKNOWN. Never a constant, never zero.
    base fee       protocol per-signature constant, 5,000 lamports
    compute units  400,000 = DEFAULT_BUDGET_ASSUMPTION (see UNKNOWNs)

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

## 11. Tests and CI

    full suite   4,277 passed - 16 skipped - 0 failed   (at ac77450)
    Ubuntu CI    all six jobs green (run 32381149191)
                 runtime contract - frontend typecheck+build - secret scan
                 - pytest - migration/bootstrap - dependency audit

Never carry a red typecheck or a failing test as "pre-existing".

## 12. Remaining UNKNOWNs — do not paper over these

- **`simulateTransaction` is not yet authoritative** — there is no canonical
  transaction builder/message to simulate.
- **`getFeeForMessage` is not yet authoritative**, for the same reason.
- **400,000 CU remains `DEFAULT_BUDGET_ASSUMPTION`** until measured. It must
  not harden into a measurement.
- **The fee modules are tested but NOT wired into canonical production DEX
  execution.** Policy is enforced where it is called; it is not yet called by
  the execution path. That is Phase 6.2.
- **`getRecentPrioritizationFees` returned 0.0 across all 150 slots** on
  2026-08-20. A silent fallback today would bid ZERO priority. Fallback
  success is not fallback equivalence.
- **Fractional priority-fee estimates are unobserved live** — the quantizer is
  contract-driven and defensive, not empirically calibrated.
- **BTCC accounting / funding / cross-margin liquidation details remain
  UNKNOWN** wherever not backed by authoritative realized evidence. Do not
  infer them from other venues.
- **Host WHEA / `HOST_HARDWARE_UNSTABLE` remains unresolved** unless new
  evidence proves otherwise. It is a host-level concern and is not
  represented anywhere in this repository.
- `HELIUS_WATCH_WALLETS` is EMPTY, so Wallet Alpha renders NOT CONFIGURED —
  correct, not broken.
- Execution phases needing a funded Solana keypair are BLOCKED: there is no
  Solana signer in the repo and key material is not handled here.

## 13. Next phase — Phase 6.2, NOT STARTED

**Phase 6.2: canonical DEX fee integration.** Wire `lib/solana_fees` and
`lib/solana_fee_policy` into the canonical DEX execution path so that measured
fees and authorised ceilings actually govern execution, rather than being
correct-but-uncalled.

Do not begin UI work.

## 14. Working rules

- Verify repo path, HEAD, working tree and the running process before
  changing anything. Measured state beats any prompt.
- One concern per commit; do not mix documentation with code.
- Read the actual implementation before trusting a description of it —
  including this document.
