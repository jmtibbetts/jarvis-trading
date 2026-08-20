"""The virtual DEX wallet — persisted balances as the ONLY economic authority.

WHAT THIS REPLACES. Autonomous DEX execution used to accept its own solvency
as a function argument: `gas_balance_sol=1.0` was a caller default, and the
venue adapter believed whatever number arrived. A caller could make an
impossible trade executable by typing a bigger balance — which is not a
simulation of a wallet, it is a simulation of whatever the caller wishes
were true.

Balances now live in one place, `dex_balances`, and every check and every
settlement reads and mutates THAT. A caller-supplied balance is never an
authority: it may only SHRINK what the ledger permits (a conservative
per-call limit), and it can never initialise, replace or stand in for the
ledger. A wallet nobody funded is empty, and execution against an empty
wallet is refused rather than imagined into solvency.

VIRTUAL VALUE HAS PROVENANCE, AND PROVENANCE IS NOT AUTHORIZATION. Balances
appear only through fund_wallet(), which writes the credit and its
DexFundingEvent in one transaction, naming the authority that authorised it.
Reading a wallet never funds it.

But naming an authority was never the same as HAVING one. fund_wallet used
to fund on `authority in FUNDING_AUTHORITIES`, so any caller anywhere could
mint capital by typing the string TEST_FIXTURE. It now takes a sealed
FundingGrant that only an issuer can produce, and each issuer checks
something real — see the funding-authority section below.

GAS IS A BALANCE, NOT A DEDUCTION FROM OUTPUT. SOL pays for transactions.
Modelling gas as a synthetic haircut on the tokens received hides the state
the simulator most needs to reach honestly: tokens held, no gas, wallet
stuck. SOL is a real row here, debited by real events.

THE THREE OUTCOMES, WITH DIFFERENT ECONOMICS:

    REJECTED before submission   -> nothing moves. No asset exchange, no
                                    gas, no fee. A refusal costs nothing
                                    because nothing reached the chain.
    FAILED after submission      -> gas only. The chain charged the fee for
                                    a transaction that did not exchange
                                    assets. This state exists on purpose.
    SUCCESS                      -> input debited, output credited, network
                                    fee debited from SOL. Exactly once each.

ATOMICITY. Every settlement is one short SQLite transaction through the
instrumented session boundary. The available-balance check happens INSIDE
the same transaction as the debit, so two concurrent spenders serialize on
SQLite's write lock and the second sees the first's debit — a double-spend
would require both to read the same available quantity inside their own
write transactions, which WAL SQLite does not permit. No provider, LLM or
network call ever happens while the transaction is open.

THE STARTING ENDOWMENT IS A DECLARATION, NOT A RECOVERY. The canonical
epoch contains no DEX balances, no positions and no trades (verified:
the legacy portfolio table is empty, 0 rows in both trade tables). There is nothing to migrate
and nothing to guess. A fresh wallet is seeded with the declared virtual
endowment below — the same kind of decision as the paper book's 100k — and
the legacy pre-cutover dex economy is never consulted.
"""
from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Well-known mints, so balances are keyed the way real wallets key them.
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

_SYMBOLS = {SOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}

def _rows(db, user_id: str):
    from app.database import DexBalance
    return db.query(DexBalance).filter(DexBalance.user_id == user_id)


def _row(db, user_id: str, mint: str):
    from app.database import DexBalance
    return (db.query(DexBalance)
            .filter(DexBalance.user_id == user_id, DexBalance.mint == mint)
            .first())


# ── Funding authorities. Every credit names one. ────────────────────────
#
# PROVENANCE IS NOT AUTHORIZATION. Naming an authority records WHY value
# appeared; it does not establish that anyone was entitled to create it.
# The first version checked `authority in FUNDING_AUTHORITIES` and funded
# on that basis, which meant any caller anywhere — the autonomous DEX, a
# scheduler job, an API handler — could mint virtual capital by typing the
# five-letter string TEST_FIXTURE. The enum was the whole gate.
#
# So authority is now a CAPABILITY, not a label. fund_wallet() accepts only
# a sealed FundingGrant, and a grant can only come from an issuer that
# checked something real:
#
#   CONFIGURED_VIRTUAL_ENDOWMENT  issue_endowment_grant() — the mint and
#                                 quantity must MATCH the configured
#                                 training-account policy exactly, so this
#                                 authority cannot mint an arbitrary
#                                 amount even from inside this module.
#   OPERATOR_GRANT                issue_operator_grant() — requires an
#                                 approval secret the operator configured
#                                 out of band. No secret configured means
#                                 no operator-grant workflow exists, and
#                                 the authority is simply UNAVAILABLE
#                                 rather than pretended into existence.
#   TEST_FIXTURE                  issue_test_fixture_grant() — refuses
#                                 outside a pytest process. Under pytest,
#                                 app.database already refuses to open the
#                                 operator database at all, so a fixture
#                                 grant can only ever credit a throwaway
#                                 one. The two guards compose into: TEST
#                                 FIXTURES CANNOT TOUCH CANONICAL MONEY.
CONFIGURED_VIRTUAL_ENDOWMENT = "CONFIGURED_VIRTUAL_ENDOWMENT"
OPERATOR_GRANT = "OPERATOR_GRANT"
TEST_FIXTURE = "TEST_FIXTURE"

FUNDING_AUTHORITIES = (CONFIGURED_VIRTUAL_ENDOWMENT, OPERATOR_GRANT,
                       TEST_FIXTURE)

# The operator's out-of-band approval for a manual grant. Absent by design:
# there is no operator-grant workflow yet, and an authority with no workflow
# should be closed, not open with a friendly name.
OPERATOR_GRANT_APPROVAL_ENV = "JARVIS_DEX_OPERATOR_GRANT_APPROVAL"

# Set by conftest.py before any application import. app.database raises
# outright if this is set and the path resolves to the operator database.
UNDER_PYTEST_ENV = "JARVIS_UNDER_PYTEST"


class FundingAuthorizationError(RuntimeError):
    """Virtual value was requested by a caller that could not authorise it."""


# Unforgeable only in the sense that matters: a caller cannot construct a
# grant by accident, by deserialising one, or by naming an authority. It is
# module-private, and a structural test asserts no canonical runtime module
# reaches for it.
_GRANT_SEAL = object()


@dataclass(frozen=True)
class FundingGrant:
    """An authorised creation of virtual value. Only an issuer makes one."""
    authority: str
    mint: str
    quantity: float
    reason: str
    actor: str
    issued_at: str
    symbol: str | None = None
    policy_version: str | None = None
    provenance: dict = field(default_factory=dict)
    seal: object = None

    def __post_init__(self):
        if self.seal is not _GRANT_SEAL:
            raise FundingAuthorizationError(
                "a FundingGrant may only be created by an issuer in "
                "lib.dex_wallet; constructing one directly is exactly the "
                "'the enum is the authorization' defect this replaced")
        if self.authority not in FUNDING_AUTHORITIES:
            raise FundingAuthorizationError(
                f"unknown funding authority {self.authority!r}")
        qty = float(self.quantity)
        if qty <= 0 or qty != qty:
            raise FundingAuthorizationError(
                f"funding quantity {self.quantity!r} is not positive")


def _seal(authority: str, *, mint: str, quantity: float, reason: str,
          actor: str, symbol: str | None = None,
          policy_version: str | None = None,
          provenance: dict | None = None) -> FundingGrant:
    return FundingGrant(
        authority=authority, mint=mint, quantity=float(quantity),
        reason=reason, actor=actor, issued_at=_now(), symbol=symbol,
        policy_version=policy_version,
        provenance={**(provenance or {}), "issuer_actor": actor},
        seal=_GRANT_SEAL)


def operator_grant_workflow_available() -> bool:
    """Is there an operator-grant workflow at all? Currently: no."""
    return bool((os.getenv(OPERATOR_GRANT_APPROVAL_ENV) or "").strip())


def issue_operator_grant(*, mint: str, quantity: float, reason: str,
                         actor: str, approval: str,
                         symbol: str | None = None,
                         provenance: dict | None = None) -> FundingGrant:
    """A grant the operator explicitly authorised, or nothing at all.

    The approval is compared against a secret only the operator can set,
    with a constant-time comparison. Until one is configured this raises
    for every caller, which is the honest state of a workflow that does not
    exist yet — better than an enum value that reads like permission.
    """
    expected = (os.getenv(OPERATOR_GRANT_APPROVAL_ENV) or "").strip()
    if not expected:
        raise FundingAuthorizationError(
            f"OPERATOR_GRANT is unavailable: no operator-grant workflow is "
            f"configured ({OPERATOR_GRANT_APPROVAL_ENV} is unset). The "
            f"authority exists as a provenance label for a workflow that "
            f"has not been built; it is not a way to create value.")
    if not hmac.compare_digest(str(approval or ""), expected):
        raise FundingAuthorizationError(
            "OPERATOR_GRANT refused: the supplied approval does not match "
            "the configured operator approval")
    if not str(actor or "").strip():
        raise FundingAuthorizationError(
            "OPERATOR_GRANT refused: an operator grant must name the actor "
            "that authorised it")
    return _seal(OPERATOR_GRANT, mint=mint, quantity=quantity, reason=reason,
                 actor=str(actor).strip(), symbol=symbol,
                 provenance={**(provenance or {}),
                             "approval_source": OPERATOR_GRANT_APPROVAL_ENV})


def issue_test_fixture_grant(*, mint: str, quantity: float,
                             reason: str = "test fixture",
                             symbol: str | None = None) -> FundingGrant:
    """Fixture money, and ONLY inside a test process.

    Composes with app.database's unconditional refusal to open the operator
    database under pytest: outside pytest this raises, and inside pytest the
    only database reachable is a throwaway. There is no configuration that
    satisfies both, which is the point.
    """
    if os.getenv(UNDER_PYTEST_ENV) != "1":
        raise FundingAuthorizationError(
            f"TEST_FIXTURE is not a funding authority outside a test "
            f"process ({UNDER_PYTEST_ENV} is not set). Canonical runtime — "
            f"the autonomous DEX, the scheduler, API execution, the "
            f"production CLI — cannot create virtual value this way.")
    return _seal(TEST_FIXTURE, mint=mint, quantity=quantity, reason=reason,
                 actor="pytest", symbol=symbol,
                 provenance={"under_pytest": True})


ENDOWMENT_ENV = "JARVIS_DEX_VIRTUAL_ENDOWMENT"
ENDOWMENT_POLICY_VERSION = "dex_endowment_v1"

# Symbol -> mint, so an operator can write the policy in symbols.
_MINTS_BY_SYMBOL = {"SOL": SOL_MINT, "USDC": USDC_MINT, "USDT": USDT_MINT}

# Swap outcomes.
REJECTED_BEFORE_SUBMIT = "REJECTED_BEFORE_SUBMIT"
FAILED_ON_CHAIN = "FAILED_ON_CHAIN"
FAILED_BEFORE_CHAIN = "FAILED_BEFORE_CHAIN"
SETTLED = "SETTLED"

# An actual fee larger than everything the wallet holds is not a balance to
# clamp -- it is a contradiction between the model and the chain.
FEE_EXCEEDS_AVAILABLE = "ACTUAL_FEE_EXCEEDS_AVAILABLE_BALANCE"


class UnfundedWallet(Exception):
    """Execution was attempted against a wallet nobody funded."""


class FeeAccountingInvariant(Exception):
    """An actual network fee could not be charged in full.

    Raised rather than clamped. `max(0, balance - fee)` would charge less
    than the chain took and leave the book richer than reality by the
    shortfall -- the simulator making money because it failed to charge the
    full economic cost. The shortfall is surfaced, never absorbed.
    """

    def __init__(self, detail: dict):
        super().__init__(detail.get("reason") or FEE_EXCEEDS_AVAILABLE)
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configured_endowment() -> dict:
    """The endowment policy, parsed from configuration. Empty by default.

    NO HIDDEN DEFAULT. If nothing is configured the answer is an empty
    wallet, and autonomous execution refuses for insufficient balance --
    which is the correct behaviour for an account nobody funded. The old
    10,000 USDC + 1 SOL was an inherited caller assumption, not a decision,
    and it is gone.

    Format: "USDC:10000,SOL:1" -- symbols or raw mints, both accepted.
    """
    raw = (os.getenv(ENDOWMENT_ENV) or "").strip()
    if not raw:
        return {}
    out: dict = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, qty = part.partition(":")
        name = name.strip()
        mint = _MINTS_BY_SYMBOL.get(name.upper(), name)
        try:
            amount = float(qty)
        except ValueError:
            logger.warning("[DexWallet] endowment entry %r has a "
                           "non-numeric quantity; ignored", part)
            continue
        if amount > 0:
            out[mint] = amount
    return out


def issue_endowment_grant(*, mint: str, quantity: float,
                          symbol: str | None = None) -> FundingGrant:
    """A grant for the CONFIGURED training endowment, and nothing else.

    The mint and quantity are checked against the configured policy rather
    than trusted from the caller, so this authority cannot be used to mint
    an arbitrary amount even from inside this module. An endowment that
    does not match what was configured is not an endowment.
    """
    policy = configured_endowment()
    if not policy:
        raise FundingAuthorizationError(
            f"CONFIGURED_VIRTUAL_ENDOWMENT is unavailable: nothing is "
            f"configured in {ENDOWMENT_ENV}. An unconfigured endowment is "
            f"an empty wallet, not a default one.")
    if mint not in policy:
        raise FundingAuthorizationError(
            f"{mint!r} is not in the configured endowment policy")
    if float(policy[mint]) != float(quantity):
        raise FundingAuthorizationError(
            f"endowment for {mint!r} is {policy[mint]!r} in {ENDOWMENT_ENV}, "
            f"not {quantity!r}; the configured policy is the authority")
    return _seal(CONFIGURED_VIRTUAL_ENDOWMENT, mint=mint, quantity=quantity,
                 reason=f"configured training endowment via {ENDOWMENT_ENV}",
                 actor=ENDOWMENT_ENV, symbol=symbol,
                 policy_version=ENDOWMENT_POLICY_VERSION,
                 provenance={"env": ENDOWMENT_ENV})


def fund_wallet(grant: FundingGrant, *, user_id: str | None = None) -> dict:
    """Create virtual economic value from an AUTHORISED grant. The only way.

    Takes a sealed FundingGrant rather than an authority string, so that
    "which authority is this?" and "was this authorised?" stop being the
    same question. A caller who has a grant has already passed the issuer's
    check; a caller who only has the word TEST_FIXTURE has nothing.

    Balance and funding event are written in one transaction: a credit
    without its reason, or a reason without its credit, would each be a lie
    of a different kind.
    """
    import json

    from app.database import (DEFAULT_USER_ID, DexBalance, DexFundingEvent,
                              get_db)
    if not isinstance(grant, FundingGrant) or grant.seal is not _GRANT_SEAL:
        raise FundingAuthorizationError(
            "fund_wallet requires a sealed FundingGrant from an issuer "
            "(issue_endowment_grant / issue_operator_grant / "
            "issue_test_fixture_grant). Naming an authority is provenance, "
            "not permission.")
    mint = grant.mint
    qty = float(grant.quantity)
    symbol = grant.symbol or _SYMBOLS.get(mint, mint[:6])
    uid = user_id or DEFAULT_USER_ID

    # The event carries the actor and the grant's issue time alongside the
    # amount, asset, authority, reason and (row) id the table already held.
    provenance = {**dict(grant.provenance or {}),
                  "actor": grant.actor,
                  "granted_at": grant.issued_at}

    with get_db() as db:
        row = _row(db, uid, mint)
        if row is None:
            row = DexBalance(user_id=uid, mint=mint, symbol=symbol,
                             total_quantity=0.0, reserved_quantity=0.0)
            db.add(row)
            db.flush()
        row.total_quantity = float(row.total_quantity or 0.0) + qty
        row.updated_at = _now()
        event = DexFundingEvent(
            user_id=uid, mint=mint, symbol=symbol,
            quantity=qty, authority=grant.authority, reason=grant.reason,
            policy_version=grant.policy_version,
            provenance_json=json.dumps(provenance, default=str))
        db.add(event)
        db.flush()
        total = float(row.total_quantity)
        event_id = event.id
    logger.warning("[DexWallet] FUNDED %.9g %s under %s by %s — %s",
                   qty, symbol, grant.authority, grant.actor, grant.reason)
    return {"mint": mint, "credited": qty, "total_after": total,
            "authority": grant.authority, "actor": grant.actor,
            "event_id": event_id}


def apply_configured_endowment(*, user_id: str | None = None) -> dict:
    """Fund a wallet from the CONFIGURED policy, once, if one exists.

    Idempotent by funding event: a wallet that already has an endowment
    event is not funded again. Returns what it did and why, including the
    case where it deliberately did nothing.
    """
    from app.database import DEFAULT_USER_ID, DexFundingEvent, get_db
    uid = user_id or DEFAULT_USER_ID
    policy = configured_endowment()
    if not policy:
        return {"funded": False,
                "reason": f"no {ENDOWMENT_ENV} configured — the wallet "
                          f"stays empty and execution will refuse for "
                          f"insufficient balance",
                "authority": None}

    with get_db() as db:
        already = (db.query(DexFundingEvent)
                   .filter(DexFundingEvent.user_id == uid,
                           DexFundingEvent.authority
                           == CONFIGURED_VIRTUAL_ENDOWMENT)
                   .first())
    if already is not None:
        return {"funded": False, "reason": "endowment already applied",
                "authority": CONFIGURED_VIRTUAL_ENDOWMENT}

    credited = {}
    for mint, qty in policy.items():
        fund_wallet(issue_endowment_grant(mint=mint, quantity=qty),
                    user_id=uid)
        credited[_SYMBOLS.get(mint, mint[:6])] = qty
    return {"funded": True, "credited": credited,
            "authority": CONFIGURED_VIRTUAL_ENDOWMENT,
            "policy_version": ENDOWMENT_POLICY_VERSION}


def funding_history(*, user_id: str | None = None) -> list[dict]:
    """Every credit and its provenance: who, what, how much, why, when."""
    import json

    from app.database import DEFAULT_USER_ID, DexFundingEvent, get_db
    uid = user_id or DEFAULT_USER_ID

    def _actor(raw) -> str | None:
        try:
            return (json.loads(raw or "{}") or {}).get("actor")
        except (TypeError, ValueError):
            return None

    with get_db() as db:
        rows = (db.query(DexFundingEvent)
                .filter(DexFundingEvent.user_id == uid)
                .order_by(DexFundingEvent.created_at).all())
        return [{"event_id": r.id, "mint": r.mint, "symbol": r.symbol,
                 "quantity": float(r.quantity), "authority": r.authority,
                 "actor": _actor(r.provenance_json),
                 "policy_version": r.policy_version,
                 "reason": r.reason, "at": r.created_at} for r in rows]


def initialized(db=None, *, user_id: str | None = None) -> bool:
    from app.database import DEFAULT_USER_ID, get_db
    uid = user_id or DEFAULT_USER_ID
    if db is not None:
        return _rows(db, uid).count() > 0
    with get_db() as session:
        return _rows(session, uid).count() > 0


def balance(mint: str, db=None, *, user_id: str | None = None) -> dict:
    """One asset's state. Available is DERIVED, so it cannot drift."""
    from app.database import DEFAULT_USER_ID, get_db
    uid = user_id or DEFAULT_USER_ID

    def _run(session):
        row = _row(session, uid, mint)
        if row is None:
            return {"mint": mint, "symbol": _SYMBOLS.get(mint, mint[:6]),
                    "total": 0.0, "reserved": 0.0, "available": 0.0,
                    "exists": False}
        total = float(row.total_quantity or 0.0)
        reserved = float(row.reserved_quantity or 0.0)
        return {"mint": mint, "symbol": row.symbol, "total": total,
                "reserved": reserved,
                "available": max(0.0, total - reserved), "exists": True}

    if db is not None:
        return _run(db)
    with get_db() as session:
        return _run(session)


def balances(db=None, *, user_id: str | None = None) -> list[dict]:
    from app.database import DEFAULT_USER_ID, get_db
    uid = user_id or DEFAULT_USER_ID

    def _run(session):
        out = []
        for row in _rows(session, uid).all():
            total = float(row.total_quantity or 0.0)
            reserved = float(row.reserved_quantity or 0.0)
            out.append({"mint": row.mint, "symbol": row.symbol,
                        "total": total, "reserved": reserved,
                        "available": max(0.0, total - reserved),
                        "updated_at": row.updated_at})
        return out

    if db is not None:
        return _run(db)
    with get_db() as session:
        return _run(session)


def gas_state(*, priority_lamports: int = 0, fee_estimate=None,
              fee_authorization=None, db=None,
              user_id: str | None = None) -> dict:
    """Gas capability from the PERSISTED SOL balance, never an argument.

    TWO RESERVES, KEPT APART (they answer different questions):

      immediate_transaction_reserve   what THIS transaction is estimated to
                                      cost — from a live FeeEstimate when
                                      one is supplied. Dynamic by
                                      construction: it is whatever the fee
                                      market says right now, not a constant.
      future_operability_reserve      what the wallet keeps so it can still
                                      transact AFTER this trade — the
                                      spendable_native policy (a multiple of
                                      the protocol base fee). This is an
                                      operability floor, not a fee estimate.

    Without a FeeEstimate the old static path still answers (legacy
    callers), but it is labelled STATIC_POLICY_ONLY so nobody mistakes an
    operability floor for a measured transaction cost.
    """
    from lib.dex_swap_math import spendable_native
    sol = balance(SOL_MINT, db, user_id=user_id)

    # THE RESERVE IS WHAT WE WOULD ACTUALLY PAY. A FeeAuthorization is the
    # strongest answer available: it is the AUTHORIZED BID, which may sit
    # deliberately below the measurement when policy capped it. Reserving
    # the raw measurement would hold back SOL for a fee we are not
    # permitted to pay; reserving a static floor would hold back a number
    # unrelated to the transaction.
    if fee_authorization is not None and getattr(fee_authorization,
                                                 "allowed", False):
        from lib.solana_fees import lamports_to_sol
        immediate_sol = lamports_to_sol(
            int(fee_authorization.authorized_bid_lamports))
        floor = spendable_native(sol["available"])
        operability_sol = floor["execution_reserve_sol"]
        required = immediate_sol + operability_sol
        available = sol["available"]
        gas = {
            "balance_sol": available,
            "immediate_transaction_reserve_sol": immediate_sol,
            "future_operability_reserve_sol": operability_sol,
            "execution_reserve_sol": required,
            "max_spendable_sol": max(0.0, available - required),
            "can_transact": available >= required,
            "reason": (None if available >= required else
                       f"balance {available:.9f} SOL is below the "
                       f"{immediate_sol:.9f} authorized network fee plus the "
                       f"{operability_sol:.9f} operability reserve"),
            "reserve_basis": "AUTHORIZED_FEE_BID",
            "authorized_bid_lamports": int(
                fee_authorization.authorized_bid_lamports),
            "measured_total_network_fee_lamports": (
                fee_authorization.measured_total_network_fee_lamports),
            "bid_below_measured_requirement": bool(
                fee_authorization.bid_below_measured_requirement),
            "action_policy": fee_authorization.action_policy,
            "priority_level": fee_authorization.priority_level,
            "fee_estimate_quality": fee_authorization.quality,
            "fee_estimate_context": fee_authorization.context_quality,
        }
        gas["source"] = "PERSISTED_WALLET"
        gas["wallet_sol_total"] = sol["total"]
        gas["wallet_sol_reserved"] = sol["reserved"]
        return gas

    if fee_estimate is not None and getattr(fee_estimate, "ok", False):
        immediate_sol = float(fee_estimate.total_lamports) / 1e9
        # Operability floor from the SAME policy the static path uses,
        # computed on a zero-priority transaction: base fee headroom only.
        floor = spendable_native(sol["available"])
        operability_sol = floor["execution_reserve_sol"]
        required = immediate_sol + operability_sol
        available = sol["available"]
        gas = {
            "balance_sol": available,
            "immediate_transaction_reserve_sol": immediate_sol,
            "future_operability_reserve_sol": operability_sol,
            "execution_reserve_sol": required,
            "max_spendable_sol": max(0.0, available - required),
            "can_transact": available >= required,
            "reason": (None if available >= required else
                       f"balance {available:.9f} SOL is below the "
                       f"{immediate_sol:.9f} estimated fee plus the "
                       f"{operability_sol:.9f} operability reserve"),
            "reserve_basis": "DYNAMIC_FEE_ESTIMATE",
            "fee_estimate_quality": fee_estimate.quality,
            "fee_estimate_policy": fee_estimate.priority_policy,
            "fee_estimate_source": fee_estimate.priority_estimate_source,
        }
    else:
        gas = spendable_native(sol["available"],
                               priority_lamports=priority_lamports)
        gas["reserve_basis"] = "STATIC_POLICY_ONLY"
        if fee_estimate is not None:
            gas["fee_estimate_quality"] = getattr(fee_estimate, "quality",
                                                  "UNKNOWN")
            gas["fee_estimate_rejected_reason"] = getattr(
                fee_estimate, "reason", None)
    gas["source"] = "PERSISTED_WALLET"
    gas["wallet_sol_total"] = sol["total"]
    gas["wallet_sol_reserved"] = sol["reserved"]
    return gas


def max_spendable_sol(*, priority_lamports: int = 0, fee_estimate=None,
                      db=None, user_id: str | None = None) -> dict:
    """MAX for SOL itself: everything except the required reserves.

    With a live FeeEstimate the reserve is THIS transaction's estimated
    cost plus the operability floor — dynamic, because the fee market is.
    Without one, the static operability policy answers, labelled as such.
    If the required amount cannot be established, the answer is a refusal,
    not a guess.
    """
    gas = gas_state(priority_lamports=priority_lamports,
                    fee_estimate=fee_estimate, db=db, user_id=user_id)
    if not gas.get("can_transact"):
        return {"ok": False, "max_sol": 0.0,
                "reason": gas.get("reason") or "cannot fund a transaction",
                "gas": gas}
    return {"ok": True, "max_sol": gas["max_spendable_sol"],
            "reserve_sol": gas["execution_reserve_sol"], "gas": gas}


class SwapRejected(Exception):
    """Refused BEFORE submission: nothing moved, nothing was charged."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def check_swap(*, input_mint: str, input_qty: float,
               priority_lamports: int = 0, fee_estimate=None, db=None,
               user_id: str | None = None) -> dict:
    """Pre-submission validation against the ledger. Read-only.

    Raises SwapRejected — a rejection consumes NOTHING, and raising makes
    it impossible for a caller to ignore the refusal and settle anyway.
    """
    if not input_mint:
        raise SwapRejected("NO_INPUT_ASSET", "the swap names no input mint")
    qty = float(input_qty or 0.0)
    if qty <= 0.0 or qty != qty or qty in (float("inf"), float("-inf")):
        raise SwapRejected("INVALID_QUANTITY", f"input quantity {input_qty!r}")

    gas = gas_state(priority_lamports=priority_lamports,
                    fee_estimate=fee_estimate, db=db, user_id=user_id)
    if not gas.get("can_transact"):
        raise SwapRejected("INSUFFICIENT_GAS", gas.get("reason"))

    asset = balance(input_mint, db, user_id=user_id)
    if input_mint == SOL_MINT:
        # Spending SOL and paying gas come from the same balance: the
        # spendable ceiling already excludes the reserve.
        if qty > gas["max_spendable_sol"] + 1e-12:
            raise SwapRejected(
                "INSUFFICIENT_BALANCE",
                f"swap wants {qty:.9f} SOL but only "
                f"{gas['max_spendable_sol']:.9f} is spendable after the "
                f"{gas['execution_reserve_sol']:.9f} gas reserve")
    elif qty > asset["available"] + 1e-12:
        raise SwapRejected(
            "INSUFFICIENT_BALANCE",
            f"swap wants {qty} {asset['symbol']} but only "
            f"{asset['available']} is available")
    return {"ok": True, "gas": gas, "input": asset}


def settle_swap_success(*, input_mint: str, input_qty: float,
                        output_mint: str, output_qty: float,
                        network_fee_sol: float,
                        output_symbol: str | None = None,
                        provenance: dict | None = None,
                        user_id: str | None = None) -> dict:
    """A swap that executed: debit input, credit output, debit gas. Once.

    GUARDED UPDATES, NOT READ-THEN-WRITE. The first version read the
    balance, checked it in Python, then wrote — and a two-thread test
    double-spent immediately. The reason is pysqlite's transaction
    handling: SELECTs run in autocommit and the write transaction only
    begins at the first DML, so both threads read the same stale balance
    before either took the write lock. Classic lost update.

    Every debit is therefore a single UPDATE whose WHERE clause re-checks
    availability. The check and the mutation execute as one statement under
    the write lock; a concurrent spend that landed first makes the WHERE
    match nothing, rowcount comes back 0, and the whole transaction rolls
    back. The database enforces the invariant, not Python's memory of it.
    """
    from sqlalchemy import text as _text

    from app.database import DEFAULT_USER_ID, DexBalance, get_db
    uid = user_id or DEFAULT_USER_ID
    qty_in = float(input_qty)
    qty_out = float(output_qty)
    fee_sol = float(network_fee_sol or 0.0)
    if qty_in <= 0 or qty_out < 0 or fee_sol < 0:
        raise SwapRejected("INVALID_SETTLEMENT",
                           f"in={input_qty} out={output_qty} fee={network_fee_sol}")

    with get_db() as db:
        if input_mint == SOL_MINT:
            # Spend and gas leave one row in one guarded statement.
            need = qty_in + fee_sol
            hit = db.execute(_text(
                "UPDATE dex_balances SET total_quantity = total_quantity - :need, "
                "updated_at = :now WHERE user_id = :uid AND mint = :mint AND "
                "(total_quantity - reserved_quantity) + 1e-12 >= :need"),
                {"need": need, "now": _now(), "uid": uid,
                 "mint": SOL_MINT}).rowcount
            if hit != 1:
                raise SwapRejected(
                    "INSUFFICIENT_GAS_AT_SETTLEMENT",
                    f"needs {need:.9f} SOL — a concurrent spend may have "
                    f"landed first")
        else:
            hit = db.execute(_text(
                "UPDATE dex_balances SET total_quantity = total_quantity - :qty, "
                "updated_at = :now WHERE user_id = :uid AND mint = :mint AND "
                "(total_quantity - reserved_quantity) + 1e-12 >= :qty"),
                {"qty": qty_in, "now": _now(), "uid": uid,
                 "mint": input_mint}).rowcount
            if hit != 1:
                raise SwapRejected(
                    "INSUFFICIENT_BALANCE_AT_SETTLEMENT",
                    f"needs {qty_in} of {input_mint[:8]} — a concurrent "
                    f"spend may have landed first")
            hit = db.execute(_text(
                "UPDATE dex_balances SET total_quantity = total_quantity - :fee, "
                "updated_at = :now WHERE user_id = :uid AND mint = :mint AND "
                "(total_quantity - reserved_quantity) + 1e-12 >= :fee"),
                {"fee": fee_sol, "now": _now(), "uid": uid,
                 "mint": SOL_MINT}).rowcount
            if hit != 1:
                # Raising rolls back the input debit above — one
                # transaction, all or nothing.
                raise SwapRejected(
                    "INSUFFICIENT_GAS_AT_SETTLEMENT",
                    f"needs {fee_sol:.9f} SOL for gas — a concurrent spend "
                    f"may have landed first")

        # Credit output (upsert). Same transaction, so a failure here rolls
        # the debits back too.
        out = _row(db, uid, output_mint)
        if out is None:
            out = DexBalance(user_id=uid, mint=output_mint,
                             symbol=output_symbol
                             or _SYMBOLS.get(output_mint, output_mint[:6]),
                             total_quantity=0.0, reserved_quantity=0.0)
            db.add(out)
            db.flush()
        db.execute(_text(
            "UPDATE dex_balances SET total_quantity = total_quantity + :qty, "
            "updated_at = :now WHERE user_id = :uid AND mint = :mint"),
            {"qty": qty_out, "now": _now(), "uid": uid, "mint": output_mint})

        sol_after = db.execute(_text(
            "SELECT total_quantity FROM dex_balances WHERE user_id = :uid "
            "AND mint = :mint"), {"uid": uid, "mint": SOL_MINT}).scalar()
        result = {
            "status": SETTLED,
            "debited": {"mint": input_mint, "qty": qty_in},
            "credited": {"mint": output_mint, "qty": qty_out},
            "network_fee_sol": fee_sol,
            "sol_total_after": float(sol_after or 0.0),
            "provenance": provenance or {},
        }
    logger.info("[DexWallet] SETTLED swap %s %.9g -> %s %.9g, gas %.9f SOL",
                _SYMBOLS.get(input_mint, input_mint[:6]), qty_in,
                _SYMBOLS.get(output_mint, output_mint[:6]), qty_out, fee_sol)
    return result


def charge_network_fee(*, network_fee_sol: float, leg: str,
                       context: dict | None = None,
                       db=None,
                       user_id: str | None = None) -> dict:
    """Consume the SOL that pays for one transaction leg. Exactly once.

    THE ASSET THAT PAYS A COST MUST ACTUALLY LOSE THAT ASSET. The wallet
    was being consulted as an authority — "can you afford this?" — and then
    never debited, so the same 5 SOL could authorise an unlimited number of
    transactions forever. A gate that never charges is not a ledger; it is
    a permission slip that renews itself.

    This is the ONE narrow primitive for a fee-only debit. `settle_swap_*`
    remains the primitive for a full asset exchange; this exists because
    the USD position book performs the exchange in its own units and needs
    only the gas leg settled here.

    GUARDED UPDATE, not read-then-write, for the same reason as
    settle_swap_success: two concurrent spenders must serialize on the row
    rather than both reading the same available quantity.

    `db` JOINS THE CALLER'S TRANSACTION, and callers that have one should
    pass it. The gas debit and the position it pays for are ONE economic
    event: settling them in two transactions would allow a crash between
    them to leave gas charged for a position that does not exist, or a
    position that never paid. It also avoids a second connection
    contending with the caller's own write lock.

    A fee larger than the wallet holds is a CONTRADICTION, not a balance to
    clamp. `max(0, balance - fee)` would charge less than the chain took
    and leave the book richer than reality by the shortfall.
    """
    from sqlalchemy import text as _text

    from app.database import DEFAULT_USER_ID, get_db
    uid = user_id or DEFAULT_USER_ID
    fee_sol = float(network_fee_sol or 0.0)
    if fee_sol < 0 or fee_sol != fee_sol:
        raise SwapRejected("INVALID_NETWORK_FEE", f"fee={network_fee_sol}")
    if fee_sol == 0.0:
        return {"charged_sol": 0.0, "leg": leg, "sol_total_after": None,
                "note": "a zero fee is not a debit"}

    def _charge(session):
        hit = session.execute(_text(
            "UPDATE dex_balances SET total_quantity = total_quantity - :fee, "
            "updated_at = :now WHERE user_id = :uid AND mint = :mint AND "
            "(total_quantity - reserved_quantity) + 1e-12 >= :fee"),
            {"fee": fee_sol, "now": _now(), "uid": uid,
             "mint": SOL_MINT}).rowcount
        if hit != 1:
            available = balance(SOL_MINT, session, user_id=uid)["available"]
            raise FeeAccountingInvariant({
                "reason": FEE_EXCEEDS_AVAILABLE,
                "leg": leg,
                "actual_network_fee_sol": fee_sol,
                "available_sol": available,
                "shortfall_sol": max(0.0, fee_sol - available),
                "context": context or {},
                "detail": ("the chain charged more gas than the wallet "
                           "holds; clamping would leave the book richer "
                           "than reality by the shortfall"),
            })
        sol_after = session.execute(_text(
            "SELECT total_quantity FROM dex_balances WHERE user_id = :uid "
            "AND mint = :mint"), {"uid": uid, "mint": SOL_MINT}).scalar()
        return float(sol_after or 0.0)

    if db is not None:
        sol_after = _charge(db)
    else:
        with get_db() as session:
            sol_after = _charge(session)
    logger.info("[DexWallet] GAS %.9f SOL charged for %s leg", fee_sol, leg)
    return {"charged_sol": fee_sol, "leg": leg,
            "sol_total_after": sol_after,
            "context": context or {}}


def settle_swap_failure(*, network_fee_sol: float, reached_chain: bool,
                        estimated_fee_sol: float | None = None,
                        reason: str | None = None,
                        estimator: str | None = None,
                        priority_policy: str | None = None,
                        user_id: str | None = None) -> dict:
    """A swap that did NOT exchange assets.

    reached_chain=False -> nothing is charged. Gas for a transaction that
                           never existed would be an invented cost.
    reached_chain=True  -> the chain took its fee. Debit it IN FULL.

    NO CLAMP. This previously did `max(0, balance - fee)`, which quietly
    charged only what was there: a 0.008 fee against a 0.005 balance
    debited 0.005 and reported success, leaving the book 0.003 richer than
    reality. That is the simulator making money by failing to charge the
    full economic cost.

    A fee larger than everything the wallet holds is not a balance to
    round off -- it is a contradiction between the model and the chain,
    because a real transaction could not have been submitted without a
    solvent fee payer. It is surfaced as an invariant failure carrying
    every number needed to reconcile it.
    """
    from sqlalchemy import text as _text

    from app.database import DEFAULT_USER_ID, get_db
    uid = user_id or DEFAULT_USER_ID
    fee_sol = float(network_fee_sol or 0.0)
    if not reached_chain:
        return {"status": FAILED_BEFORE_CHAIN, "network_fee_sol": 0.0,
                "estimated_fee_sol": estimated_fee_sol, "reason": reason}
    if fee_sol < 0:
        raise SwapRejected("INVALID_SETTLEMENT", f"fee={network_fee_sol}")

    with get_db() as db:
        # Guarded debit: charges the FULL fee or nothing at all.
        hit = db.execute(_text(
            "UPDATE dex_balances SET total_quantity = total_quantity - :fee, "
            "updated_at = :now WHERE user_id = :uid AND mint = :mint AND "
            "total_quantity + 1e-12 >= :fee"),
            {"fee": fee_sol, "now": _now(), "uid": uid,
             "mint": SOL_MINT}).rowcount
        if hit != 1:
            available = float(db.execute(_text(
                "SELECT total_quantity FROM dex_balances WHERE user_id = :uid "
                "AND mint = :mint"), {"uid": uid, "mint": SOL_MINT}).scalar()
                or 0.0)
            detail = {
                "reason": FEE_EXCEEDS_AVAILABLE,
                "actual_fee_sol": fee_sol,
                "estimated_fee_sol": estimated_fee_sol,
                "available_sol": available,
                "shortfall_sol": round(fee_sol - available, 12),
                "estimator": estimator,
                "priority_policy": priority_policy,
                "transaction_state": FAILED_ON_CHAIN,
                "note": ("a real transaction could not have been submitted "
                         "with an insolvent fee payer; the model and the "
                         "chain disagree and the difference is NOT absorbed"),
            }
            logger.error("[DexWallet] %s — fee %.9f SOL exceeds available "
                         "%.9f SOL (shortfall %.9f)", FEE_EXCEEDS_AVAILABLE,
                         fee_sol, available, detail["shortfall_sol"])
            raise FeeAccountingInvariant(detail)

        after = float(db.execute(_text(
            "SELECT total_quantity FROM dex_balances WHERE user_id = :uid "
            "AND mint = :mint"), {"uid": uid, "mint": SOL_MINT}).scalar()
            or 0.0)

    out = {"status": FAILED_ON_CHAIN, "network_fee_sol": fee_sol,
           "estimated_fee_sol": estimated_fee_sol,
           "sol_total_after": after, "reason": reason}
    if estimated_fee_sol is not None:
        # ESTIMATE AND ACTUAL BOTH SURVIVE. Replacing one with the other
        # destroys the only evidence of whether a policy is priced right.
        out["fee_estimate_miss_sol"] = round(fee_sol - float(estimated_fee_sol), 12)
    logger.warning("[DexWallet] FAILED on-chain swap consumed %.9f SOL gas "
                   "(%s); %.9f SOL remains", fee_sol,
                   reason or "no reason recorded", after)
    return out
