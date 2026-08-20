"""A virtual on-chain trading book — swaps priced the way an AMM prices them.

Separate from `paper_engine` on purpose. That engine models a broker: a
quoted price, a percentage commission, leverage, a short side. None of
those exist at a constant-product pool, and running DEX trades through it
would produce a book whose every number is plausible and wrong.

What this book refuses to do, and why:

  NO LEVERAGE      A pool will not lend to you. Offering 5x on a memecoin
                   describes a venue that does not exist.
  NO SHORTS        Spot AMM only. You cannot sell what you cannot borrow.
  SIZE BOUNDED BY POOL DEPTH, NOT EQUITY
                   The binding constraint on-chain is what the pool can
                   absorb. Measured on live data: $25,000 into a $50,000
                   pool is 49.9% price impact — half the stake gone on
                   entry, before the trade is even wrong. A book that sizes
                   from equity alone will happily do that.
  COSTS ITEMISED   Pool fee, price impact and network fee are recorded
                   separately. One is the venue's price, one is your own
                   size, one is the chain. They have different remedies and
                   collapsing them into "slippage" hides which applies.

Entry impact is stored on the position at open. Recomputing it later from
a mid price that has since moved would quietly rewrite what the trade
actually cost.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_STARTING_USD = 10_000.0

# Impact ceiling for a single entry. Deliberately tight: past a couple of
# percent you are not taking a price, you are setting one.
DEFAULT_MAX_IMPACT_PCT = 2.0
# A pool thinner than this cannot support a position worth taking.
MIN_POOL_RESERVE_USD = 25_000.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def max_impact_pct() -> float:
    return _cfg_float("DEX_MAX_IMPACT_PCT", DEFAULT_MAX_IMPACT_PCT)


def min_pool_reserve_usd() -> float:
    return _cfg_float("DEX_MIN_POOL_RESERVE_USD", MIN_POOL_RESERVE_USD)


def get_portfolio(db, user_id: str | None = None):
    from app.database import DEFAULT_USER_ID, DexPortfolio
    uid = user_id or DEFAULT_USER_ID
    pf = db.query(DexPortfolio).filter(DexPortfolio.user_id == uid).first()
    if pf is None:
        pf = DexPortfolio(user_id=uid, starting_usd=DEFAULT_STARTING_USD,
                          cash_usd=DEFAULT_STARTING_USD, reset_at=_now())
        db.add(pf)
        db.flush()
    return pf


def size_for_pool(reserve_usd: float, cash_usd: float, *,
                  risk_usd: float | None = None, dex: str | None = None,
                  depth_confidence: str | None = None) -> dict:
    """How much this pool can actually absorb — the DEX sizing primitive.

    Takes the SMALLEST of three ceilings and says which one bound the
    trade. "Position too small" and "pool too thin" are different problems
    and the operator should not have to guess which one happened.
    """
    from lib.dex_swap_math import max_size_for_impact, pool_fee_bps

    reserve_usd = float(reserve_usd or 0)
    cash_usd = float(cash_usd or 0)
    floor = min_pool_reserve_usd()
    if reserve_usd < floor:
        return {"ok": False, "size_usd": 0.0, "bound_by": "pool_too_thin",
                "reason": (f"pool holds ${reserve_usd:,.0f}, below the "
                           f"${floor:,.0f} floor — impact would dominate any "
                           f"edge")}

    cap = max_impact_pct()
    depth_cap = max_size_for_impact(reserve_usd, cap, dex=dex)
    # DEPTH CERTAINTY IS NOT A LABEL. A pool whose local depth has only
    # been MODELLED must not be sized like one whose reserves were read
    # off the chain — otherwise the provenance is honest and the
    # behaviour is not. Uncertainty is asymmetric here: being wrong about
    # depth costs far more than trading smaller than necessary.
    from lib.dex_swap_math import depth_adjusted_size
    adj = depth_adjusted_size(depth_cap, depth_confidence)
    depth_cap = adj["size_usd"]
    wanted = risk_usd if risk_usd is not None else cash_usd
    candidates = {"impact_cap": depth_cap, "cash": cash_usd,
                  "requested": float(wanted)}
    bound_by = min(candidates, key=candidates.get)
    size = candidates[bound_by]

    if size <= 0:
        return {"ok": False, "size_usd": 0.0, "bound_by": bound_by,
                "reason": f"no size available ({bound_by})"}
    return {
        "ok": True, "size_usd": round(size, 6), "bound_by": bound_by,
        "impact_cap_pct": cap, "depth_cap_usd": round(depth_cap, 6),
        "depth_confidence": adj["depth_confidence"],
        "depth_size_factor": adj["size_factor"],
        "impact_uncertainty_multiplier": adj["impact_multiplier"],
        "fee_bps": pool_fee_bps(dex),
        "reason": (f"${size:,.2f} — limited by {bound_by} "
                   f"(pool depth allows ${depth_cap:,.2f} at {cap}% impact)"),
    }


def open_dex_position(*, mint: str, symbol: str | None, pool_address: str | None,
                      dex: str | None, reserve_usd: float, price_usd: float,
                      size_usd: float | None = None,
                      stop_price_usd: float | None = None,
                      target_price_usd: float | None = None,
                      signal_id: str | None = None,
                      sol_price_usd: float = 0.0,
                      concentrated: bool = False,
                      priority_lamports: int | None = None,
                      fee_fetch=None,
                      db=None) -> dict:
    """Simulate a buy. Every refusal names its own reason.

    `priority_lamports` IS THE AUTHORIZED BID, handed down by the caller
    that measured it (lib.dex_autotrade, through lib.dex_network_cost).
    When it is absent this function MEASURES for itself rather than
    reaching for the static default: an entry priced off a 100,000-lamport
    constant is priced off nothing, and it would silently disagree with the
    gate that approved it.
    """
    from app.database import DexPosition, get_db
    from lib import dex_network_cost as NC
    from lib.dex_swap_math import quote_swap

    # MEASURED BEFORE THE TRANSACTION OPENS, NEVER INSIDE IT.
    #
    # This is a standing invariant of the DEX ledger: no provider, LLM or
    # network call may happen while a write transaction is open. Pricing
    # inside `_run` broke it twice over — an RPC round trip held the SQLite
    # write lock, and the provider-health write it triggers opens a SECOND
    # connection that then waits on the first for the full 30s busy timeout.
    # The result was not a deadlock that announced itself; it was a suite
    # that got mysteriously slow.
    priced = None
    network_cost = None
    bid = priority_lamports
    if bid is None:
        priced = NC.price_transaction(
            action="NORMAL_ENTRY", priority_level="NORMAL",
            mint=mint, pool_address=pool_address,
            sol_price_usd=sol_price_usd, notional_usd=size_usd,
            fetch=fee_fetch)
        if not priced["ok"]:
            return {"error": f"network fee refused: {priced['detail']}",
                    "network_cost": NC.fee_provenance(priced)}
        bid = priced["priority_lamports_for_quote"]
        network_cost = NC.fee_provenance(priced)

    def _run(session):
        pf = get_portfolio(session)
        if float(price_usd or 0) <= 0:
            return {"error": "no price for this token"}

        existing = session.query(DexPosition).filter(
            DexPosition.mint == mint, DexPosition.status == "Open").first()
        if existing is not None:
            return {"error": f"already holding {symbol or mint[:8]}"}

        sizing = size_for_pool(reserve_usd, float(pf.cash_usd or 0),
                               risk_usd=size_usd, dex=dex)
        if not sizing.get("ok"):
            return {"error": f"dex sizing refused: {sizing['reason']}",
                    "sizing": sizing}

        amount = sizing["size_usd"]
        # The authorized bid was established above, outside this
        # transaction. DYNAMIC, never the static constant: an entry priced
        # off a 100,000-lamport default is priced off nothing.
        q = quote_swap(amount, reserve_usd, dex=dex,
                       sol_price_usd=sol_price_usd,
                       priority_lamports=bid, concentrated=concentrated)
        if not q.get("ok"):
            return {"error": f"swap quote failed: {q.get('reason')}"}
        if q["price_impact_pct"] > max_impact_pct():
            # Defence in depth: sizing should already prevent this.
            return {"error": (f"impact {q['price_impact_pct']:.2f}% exceeds "
                              f"the {max_impact_pct()}% cap"), "quote": q}

        # Tokens are bought with what SURVIVES the costs, at the average
        # price achieved — not at the quoted spot, which nobody got.
        received = q["received_usd"]
        qty = received / float(price_usd)
        avg_entry = amount / qty if qty else float(price_usd)

        pos = DexPosition(
            mint=mint, symbol=symbol, pool_address=pool_address, dex=dex,
            status="Open", qty_tokens=qty, entry_price_usd=avg_entry,
            quoted_price_usd=float(price_usd), notional_usd=amount,
            entry_pool_fee_usd=q["pool_fee_usd"],
            entry_impact_usd=q["price_impact_usd"],
            entry_impact_pct=q["price_impact_pct"],
            entry_network_fee_usd=q["network_fee_usd"],
            pool_reserve_usd_at_entry=float(reserve_usd),
            stop_price_usd=stop_price_usd, target_price_usd=target_price_usd,
            current_price_usd=float(price_usd), signal_id=signal_id,
            opened_at=_now(), updated_at=_now(),
            notes=sizing["reason"],
        )
        session.add(pos)
        pf.cash_usd = float(pf.cash_usd or 0) - amount
        pf.updated_at = _now()
        session.flush()

        logger.info(f"[DexPaper] BUY {symbol or mint[:8]} ${amount:,.2f} "
                    f"impact={q['price_impact_pct']:.2f}% "
                    f"cost={q['total_cost_pct']:.2f}% bound_by={sizing['bound_by']}")
        return {"ok": True, "position_id": pos.id, "qty_tokens": qty,
                "avg_entry_price_usd": avg_entry, "notional_usd": amount,
                "quote": q, "sizing": sizing,
                "priority_lamports": bid, "network_cost": network_cost}

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


# ── Executable exit economics ────────────────────────────────────────────
# A DEX POSITION IS WORTH WHAT THE BOOK CAN ACTUALLY GET OUT.
#
# `qty_tokens * current_price_usd` is a MARK. It is what a mid-price
# multiplication says, and on-chain that is frequently not recoverable: the
# same position can mark at $10,000 and quote an executable exit of $7,800,
# or of nothing at all when the route has gone. Crediting the mark to equity
# is the simulator paying the book money it could not have withdrawn — which
# is the precise failure the golden rule forbids.
#
# This is the ONE exit pricer. close_dex_position() and summary() both call
# it, so what the book is worth and what closing it would actually yield can
# never be computed two different ways.

EXIT_OK = "PRICED"
EXIT_UNPRICEABLE = "UNPRICEABLE"
EXIT_NO_MARK = "NO_MARK_PRICE"


def exit_quote(pos, *, price_usd: float | None = None,
               reserve_usd: float | None = None,
               sol_price_usd: float = 0.0,
               concentrated: bool = False) -> dict:
    """What this position would actually realise if it were closed now.

    Never falls back to the mark. An exit that cannot be routed returns
    `executable_value_usd = None` with a reason — substituting the mark
    would record a perfect escape from the exact situation that loses real
    money, and a model that rewards illiquidity teaches the desk to seek it.
    """
    from lib.dex_swap_math import quote_swap

    qty = float(pos.qty_tokens or 0)
    mark_price = float(price_usd if price_usd is not None
                       else (pos.current_price_usd or 0) or 0)
    mark_value = qty * mark_price

    base = {
        "position_id": pos.id,
        "mark_price_usd": mark_price,
        "mark_value_usd": round(mark_value, 6),
        "executable_value_usd": None,
        "exit_impact_pct": None,
        "pool_fee_usd": None,
        "network_fee_usd": None,
        "depth_confidence": None,
        "route": pos.dex,
        "quoted_at": _now(),
    }

    if mark_value <= 0:
        # No mark means no basis for a quote — and no basis to claim a
        # value either. Both stay unknown rather than becoming zero.
        return {**base, "status": EXIT_NO_MARK,
                "reason": "no current price on file for this position"}

    res = float(reserve_usd if reserve_usd is not None
                else (pos.pool_reserve_usd_at_entry or 0))
    q = quote_swap(mark_value, res, dex=pos.dex,
                   sol_price_usd=sol_price_usd, concentrated=concentrated)
    if not q.get("ok"):
        return {**base, "status": EXIT_UNPRICEABLE,
                "reason": q.get("reason") or "no route could price this exit"}

    executable = float(q["received_usd"])
    return {
        **base,
        "status": EXIT_OK,
        "executable_value_usd": round(executable, 6),
        "exit_impact_pct": q["price_impact_pct"],
        "pool_fee_usd": q["pool_fee_usd"],
        "network_fee_usd": q["network_fee_usd"],
        "network_fee_sol": q.get("network_fee_sol"),
        "depth_confidence": q.get("depth_confidence"),
        "depth_model": q.get("depth_model"),
        # What the mid-price multiplication overstates the position by.
        "exit_drag_usd": round(mark_value - executable, 6),
        "exit_drag_pct": (round(100.0 * (mark_value - executable) / mark_value, 4)
                          if mark_value else None),
        "reason": None,
    }


def close_dex_position(position_id: str, price_usd: float, *,
                       reserve_usd: float | None = None,
                       reason: str = "manual", sol_price_usd: float = 0.0,
                       concentrated: bool = False, db=None) -> dict:
    """Simulate the sell. The exit is priced against pool depth too —
    getting IN cheaply and being unable to get OUT is the characteristic
    on-chain failure, and a book that ignores exit impact never shows it."""
    from app.database import DexPosition, DexTrade, get_db
    from lib.dex_swap_math import quote_swap

    def _run(session):
        pos = session.query(DexPosition).filter(
            DexPosition.id == position_id).first()
        if pos is None or pos.status != "Open":
            return {"error": "position not found or already closed"}

        pf = get_portfolio(session)
        # THE SAME PRICER THE BOOK IS VALUED WITH. If closing used one
        # arithmetic and equity used another, the book would be worth one
        # number until you sold it and a different one afterwards — and
        # the discrepancy would look like slippage rather than a bug.
        eq = exit_quote(pos, price_usd=price_usd, reserve_usd=reserve_usd,
                        sol_price_usd=sol_price_usd, concentrated=concentrated)
        gross_out = float(eq["mark_value_usd"])
        q = {"ok": eq["status"] == EXIT_OK,
             "reason": eq.get("reason"),
             "received_usd": eq["executable_value_usd"],
             "price_impact_pct": eq["exit_impact_pct"],
             "pool_fee_usd": eq["pool_fee_usd"],
             "network_fee_usd": eq["network_fee_usd"]}
        if not q.get("ok"):
            # AN UNPRICEABLE EXIT IS A RISK EVENT, NOT A FREE ONE.
            #
            # This used to book `proceeds = gross_out` with zero impact,
            # zero pool fee and zero network fee — so the ONE scenario a
            # DEX trader actually fears, liquidity vanishing underneath an
            # open position, resolved as the best possible outcome: a
            # perfect fill at the mid with no costs at all.
            #
            # A model that rewards illiquidity teaches the desk to seek it.
            # The position stays OPEN and is marked EXIT_PENDING_NO_LIQUIDITY
            # so it appears in risk as what it is: capital that cannot
            # currently be recovered.
            pos.status = "Open"
            pos.exit_state = "EXIT_PENDING_NO_LIQUIDITY"
            pos.exit_blocked_reason = (q.get("reason")
                                       or "no route could price this exit")[:300]
            pos.exit_last_attempt_at = _now()
            session.flush()
            logger.warning(
                f"[DexPaper] {pos.symbol}: exit could not be priced "
                f"({pos.exit_blocked_reason}) — held EXIT_PENDING_NO_LIQUIDITY "
                f"rather than booked as a costless fill")
            return {
                "error": "exit_unpriceable",
                "state": "EXIT_PENDING_NO_LIQUIDITY",
                "position_id": pos.id,
                "reason": pos.exit_blocked_reason,
                "detail": ("The position is still open. An exit that cannot "
                           "be routed is a liquidity failure, and booking it "
                           "at the mid would record a perfect escape from "
                           "the exact situation that loses real money."),
                "mark_value_usd": round(gross_out, 6),
                "executable_value_usd": None,
            }
        else:
            proceeds = q["received_usd"]
            exit_impact = q["price_impact_pct"]
            pool_fee = q["pool_fee_usd"]
            net_fee = q["network_fee_usd"]

        notional = float(pos.notional_usd or 0)
        gross_pnl = gross_out - notional
        costs = (float(pos.entry_pool_fee_usd or 0)
                 + float(pos.entry_impact_usd or 0)
                 + float(pos.entry_network_fee_usd or 0)
                 + pool_fee + net_fee + (gross_out - proceeds - pool_fee - net_fee))
        net_pnl = proceeds - notional

        opened = pos.opened_at
        hold_min = None
        try:
            hold_min = (datetime.fromisoformat(_now())
                        - datetime.fromisoformat(opened)).total_seconds() / 60.0
        except (TypeError, ValueError):
            pass

        session.add(DexTrade(
            position_id=pos.id, mint=pos.mint, symbol=pos.symbol,
            pool_address=pos.pool_address, dex=pos.dex,
            qty_tokens=pos.qty_tokens, notional_usd=notional,
            entry_price_usd=pos.entry_price_usd, exit_price_usd=float(price_usd),
            gross_pnl_usd=round(gross_pnl, 6),
            total_costs_usd=round(costs, 6),
            net_pnl_usd=round(net_pnl, 6),
            net_pnl_pct=round(100.0 * net_pnl / notional, 4) if notional else 0.0,
            entry_impact_pct=pos.entry_impact_pct, exit_impact_pct=exit_impact,
            pool_fees_usd=round(float(pos.entry_pool_fee_usd or 0) + pool_fee, 6),
            network_fees_usd=round(float(pos.entry_network_fee_usd or 0) + net_fee, 6),
            reason=reason, opened_at=opened, closed_at=_now(),
            hold_minutes=hold_min,
        ))

        pos.status = "Closed"
        pos.current_price_usd = float(price_usd)
        pos.updated_at = _now()

        pf.cash_usd = float(pf.cash_usd or 0) + proceeds
        pf.realized_pnl_usd = float(pf.realized_pnl_usd or 0) + net_pnl
        pf.total_trades = int(pf.total_trades or 0) + 1
        if net_pnl >= 0:
            pf.wins = int(pf.wins or 0) + 1
        else:
            pf.losses = int(pf.losses or 0) + 1
        pf.updated_at = _now()

        logger.info(f"[DexPaper] SELL {pos.symbol or pos.mint[:8]} "
                    f"net=${net_pnl:,.2f} gross=${gross_pnl:,.2f} "
                    f"exit_impact={exit_impact:.2f}% ({reason})")
        return {"ok": True, "net_pnl_usd": round(net_pnl, 6),
                "gross_pnl_usd": round(gross_pnl, 6),
                "total_costs_usd": round(costs, 6),
                "exit_impact_pct": exit_impact, "proceeds_usd": round(proceeds, 6)}

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def summary(db=None, *, sol_price_usd: float = 0.0) -> dict:
    """Book state, on MARK and on EXECUTABLE economics, side by side.

    Equity counts only trades after `reset_at`.

    Two totals, because the difference is the point:

        equity_mark_usd        what a mid-price multiplication says
        equity_executable_usd  what the book could actually get out

    `equity_executable_usd` is the ECONOMIC, RISK and LEARNING authority.
    The mark is display and reference only.

    UNPRICEABLE POSITIONS ARE NOT SILENTLY MARKED. A position whose exit
    cannot be routed contributes NOTHING to executable equity — its mark
    is reported separately as `unpriceable_mark_value_usd`, so the operator
    sees "$X is currently unrecoverable" rather than one confident total
    that has quietly absorbed it. Manufacturing a precise-looking number
    out of a genuinely unknown one is the failure this replaces.
    """
    from app.database import DexPosition, get_db

    def _run(session):
        pf = get_portfolio(session)
        open_rows = session.query(DexPosition).filter(
            DexPosition.status == "Open").all()

        mark_total = 0.0
        executable_total = 0.0
        unpriceable_mark = 0.0
        unpriceable = 0
        rows: list[dict] = []

        for p in open_rows:
            q = exit_quote(p, sol_price_usd=sol_price_usd)
            mark_total += float(q["mark_value_usd"] or 0)
            if q["status"] == EXIT_OK:
                executable_total += float(q["executable_value_usd"] or 0)
            else:
                unpriceable += 1
                unpriceable_mark += float(q["mark_value_usd"] or 0)
            rows.append({
                "position_id": p.id, "symbol": p.symbol, "mint": p.mint,
                "qty_tokens": p.qty_tokens,
                "mark_value_usd": q["mark_value_usd"],
                "executable_exit_value_usd": q["executable_value_usd"],
                "exit_drag_usd": q.get("exit_drag_usd"),
                "exit_drag_pct": q.get("exit_drag_pct"),
                "current_exit_impact_pct": q["exit_impact_pct"],
                "current_exit_pool_fees_usd": q["pool_fee_usd"],
                "current_exit_network_fee_usd": q["network_fee_usd"],
                "current_route": q["route"],
                "current_depth_confidence": q["depth_confidence"],
                "exit_quote_at": q["quoted_at"],
                "exit_quote_status": q["status"],
                "exit_state": p.exit_state,
                "exit_blocked_reason": p.exit_blocked_reason,
                "notional_usd": p.notional_usd,
                "executable_net_unrealized_pnl_usd": (
                    round(float(q["executable_value_usd"]) - float(p.notional_usd or 0), 6)
                    if q["executable_value_usd"] is not None else None),
            })

        cash = float(pf.cash_usd or 0)
        return {
            "starting_usd": float(pf.starting_usd or 0),
            "cash_usd": round(cash, 2),
            "open_positions": len(open_rows),

            # Display / reference.
            "open_value_mark_usd": round(mark_total, 2),
            "equity_mark_usd": round(cash + mark_total, 2),

            # THE AUTHORITY. Cash plus what the open book could realise.
            "open_value_executable_usd": round(executable_total, 2),
            "equity_executable_usd": round(cash + executable_total, 2),
            "known_executable_equity_usd": round(cash + executable_total, 2),

            # Reported beside it rather than folded into it.
            "unpriceable_positions": unpriceable,
            "unpriceable_mark_value_usd": round(unpriceable_mark, 2),

            "exit_drag_usd": round(mark_total - executable_total - unpriceable_mark, 2),

            # `equity_usd` is retained as an alias of the EXECUTABLE total,
            # not the mark. Every existing caller that reads it was reading
            # a number that overstated the book, and the conservative
            # reading is the one that should win a name collision.
            "equity_usd": round(cash + executable_total, 2),

            "realized_pnl_usd": round(float(pf.realized_pnl_usd or 0), 2),
            "total_trades": int(pf.total_trades or 0),
            "wins": int(pf.wins or 0), "losses": int(pf.losses or 0),
            "reset_at": pf.reset_at,
            "positions_valuation": rows,
            "valuation_policy": (
                "equity_executable_usd is cash plus what the open book could "
                "actually be sold for now, priced against pool depth. "
                "Positions whose exit cannot be routed contribute ZERO to it "
                "and are reported separately as unpriceable_mark_value_usd — "
                "their mark is not recoverable capital and is never added."
            ),
            "limits": {"max_impact_pct": max_impact_pct(),
                       "min_pool_reserve_usd": min_pool_reserve_usd(),
                       "leverage": "none — a pool does not lend",
                       "shorting": "none — spot AMM only"},
        }

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
