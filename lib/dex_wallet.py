"""The virtual DEX wallet — persisted balances as the ONLY economic authority.

WHAT THIS REPLACES. Autonomous DEX execution used to accept its own solvency
as a function argument: `gas_balance_sol=1.0` was a caller default, and the
venue adapter believed whatever number arrived. A caller could make an
impossible trade executable by typing a bigger balance — which is not a
simulation of a wallet, it is a simulation of whatever the caller wishes
were true.

Balances now live in one place, `dex_balances`, and every check and every
settlement reads and mutates THAT. Caller-supplied balance arguments are
accepted only as a legacy shim and can never raise what the ledger says —
they are recorded in provenance and ignored as authority the moment a
persisted wallet exists.

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

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Well-known mints, so balances are keyed the way real wallets key them.
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

_SYMBOLS = {SOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}

# THE DECLARED VIRTUAL ENDOWMENT. Applied only when a wallet has no rows at
# all in the current epoch. 10,000 USDC matches the paper dex book's
# declared start;
# 1.0 SOL matches the operating assumption the old caller-supplied default
# encoded — now stated once, here, instead of as a hidden keyword argument.
STARTING_ENDOWMENT = {
    USDC_MINT: 10_000.0,
    SOL_MINT: 1.0,
}

# Swap outcomes.
REJECTED_BEFORE_SUBMIT = "REJECTED_BEFORE_SUBMIT"
FAILED_ON_CHAIN = "FAILED_ON_CHAIN"
FAILED_BEFORE_CHAIN = "FAILED_BEFORE_CHAIN"
SETTLED = "SETTLED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(db, user_id: str):
    from app.database import DexBalance
    return db.query(DexBalance).filter(DexBalance.user_id == user_id)


def _row(db, user_id: str, mint: str):
    from app.database import DexBalance
    return (db.query(DexBalance)
            .filter(DexBalance.user_id == user_id, DexBalance.mint == mint)
            .first())


def ensure_wallet(db=None, *, user_id: str | None = None) -> dict:
    """Seed the declared endowment iff the wallet has no rows. Idempotent."""
    from app.database import DEFAULT_USER_ID, DexBalance, get_db
    uid = user_id or DEFAULT_USER_ID

    def _run(session):
        if _rows(session, uid).count() > 0:
            return {"created": False}
        for mint, qty in STARTING_ENDOWMENT.items():
            session.add(DexBalance(
                user_id=uid, mint=mint, symbol=_SYMBOLS.get(mint, mint[:6]),
                total_quantity=float(qty), reserved_quantity=0.0,
                updated_at=_now()))
        logger.info("[DexWallet] fresh wallet seeded with the declared "
                    "endowment: %s", {(_SYMBOLS.get(m, m[:6])): q
                                      for m, q in STARTING_ENDOWMENT.items()})
        return {"created": True}

    if db is not None:
        return _run(db)
    with get_db() as session:
        return _run(session)


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


def gas_state(*, priority_lamports: int = 0, fee_estimate=None, db=None,
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


def settle_swap_failure(*, network_fee_sol: float, reached_chain: bool,
                        reason: str | None = None,
                        user_id: str | None = None) -> dict:
    """A swap that did NOT exchange assets.

    reached_chain=True  -> the chain still charged gas. Debit it.
    reached_chain=False -> died before submission. Nothing is charged —
                           gas for a transaction that never existed would
                           be the simulator inventing a cost.
    """
    from app.database import DEFAULT_USER_ID, get_db
    uid = user_id or DEFAULT_USER_ID
    fee_sol = float(network_fee_sol or 0.0)
    if not reached_chain:
        return {"status": FAILED_BEFORE_CHAIN, "network_fee_sol": 0.0,
                "reason": reason}
    if fee_sol < 0:
        raise SwapRejected("INVALID_SETTLEMENT", f"fee={network_fee_sol}")

    from sqlalchemy import text as _text
    with get_db() as db:
        # Gas is owed even if it drives the balance to the floor: the chain
        # already took it. One guarded statement, clamped at zero —
        # negative SOL is not a thing.
        hit = db.execute(_text(
            "UPDATE dex_balances SET total_quantity = CASE WHEN "
            "total_quantity - :fee < 0 THEN 0 ELSE total_quantity - :fee END, "
            "updated_at = :now WHERE user_id = :uid AND mint = :mint"),
            {"fee": fee_sol, "now": _now(), "uid": uid,
             "mint": SOL_MINT}).rowcount
        if hit != 1:
            raise SwapRejected("NO_SOL_BALANCE_ROW",
                               "gas owed but the wallet has no SOL row")
        after = float(db.execute(_text(
            "SELECT total_quantity FROM dex_balances WHERE user_id = :uid "
            "AND mint = :mint"), {"uid": uid, "mint": SOL_MINT}).scalar()
            or 0.0)
    logger.warning("[DexWallet] FAILED on-chain swap consumed %.9f SOL gas "
                   "(%s); %.9f SOL remains", fee_sol,
                   reason or "no reason recorded", after)
    return {"status": FAILED_ON_CHAIN, "network_fee_sol": fee_sol,
            "sol_total_after": after, "reason": reason}
