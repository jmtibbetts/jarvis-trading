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
                      db=None) -> dict:
    """Simulate a buy. Every refusal names its own reason."""
    from app.database import DexPosition, get_db
    from lib.dex_swap_math import quote_swap

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
        q = quote_swap(amount, reserve_usd, dex=dex,
                       sol_price_usd=sol_price_usd, concentrated=concentrated)
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
                "quote": q, "sizing": sizing}

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


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
        gross_out = float(pos.qty_tokens or 0) * float(price_usd or 0)
        res = float(reserve_usd if reserve_usd is not None
                    else (pos.pool_reserve_usd_at_entry or 0))

        q = quote_swap(gross_out, res, dex=pos.dex,
                       sol_price_usd=sol_price_usd, concentrated=concentrated)
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


def summary(db=None) -> dict:
    """Book state. Equity counts only trades after `reset_at`."""
    from app.database import DexPosition, get_db

    def _run(session):
        pf = get_portfolio(session)
        open_rows = session.query(DexPosition).filter(
            DexPosition.status == "Open").all()
        open_value = sum(float(p.qty_tokens or 0) * float(p.current_price_usd or 0)
                         for p in open_rows)
        cash = float(pf.cash_usd or 0)
        return {
            "starting_usd": float(pf.starting_usd or 0),
            "cash_usd": round(cash, 2),
            "open_positions": len(open_rows),
            "open_value_usd": round(open_value, 2),
            "equity_usd": round(cash + open_value, 2),
            "realized_pnl_usd": round(float(pf.realized_pnl_usd or 0), 2),
            "total_trades": int(pf.total_trades or 0),
            "wins": int(pf.wins or 0), "losses": int(pf.losses or 0),
            "reset_at": pf.reset_at,
            "limits": {"max_impact_pct": max_impact_pct(),
                       "min_pool_reserve_usd": min_pool_reserve_usd(),
                       "leverage": "none — a pool does not lend",
                       "shorting": "none — spot AMM only"},
        }

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
