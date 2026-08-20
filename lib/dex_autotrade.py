"""Autonomous virtual DEX trading — surge to thesis to route to position.

The DEX surface was manual: an operator typed a mint, a pool reserve and a
price, and got a simulated swap. Useful for development and useless for
training, because a training set of hand-picked trades measures the
operator, not the system.

This is the automated path, and its shape is the point:

    TOKEN SURGE + WALLET INTELLIGENCE + DEX DISCOVERY
                        ↓
                  TRADE THESIS
                        ↓
                 DEX ELIGIBILITY
                        ↓
                   ROUTE QUOTE
                        ↓
        NET EXPECTANCY AFTER DEX COSTS   <- the gate
                        ↓
              RISK + IMPACT SIZING
                        ↓
                VIRTUAL DEX ORDER

WALLET INTELLIGENCE IS A SENSOR, NOT AN OVERRIDE. Verified smart-money
buying is evidence that raises conviction. It is never permission to skip
liquidity, impact, sizing or the cost gate. A wallet can be genuinely
excellent and the trade still economically untradeable for JARVIS —
the wallet got in earlier, cheaper, and possibly at a size the pool could
still absorb.

WHAT JARVIS COULD HAVE PAID IS NOT WHAT THE WALLET PAID. The wallet's
execution price answers "what did this observed wallet get?". JARVIS's
answers "what could JARVIS have got AFTER detecting it?" — a different
number, separated by detection latency, and the gap between them is the
only honest measure of copyability.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

AUTOTRADE_VERSION = "dex_autotrade_v1"

# Refusal reasons. Each is a distinct lesson, so none of them collapse
# into a generic "skipped".
NO_ROUTE = "NO_ROUTE"
IMPACT_TOO_HIGH = "IMPACT_TOO_HIGH"
POOL_TOO_THIN = "POOL_TOO_THIN"
NEGATIVE_NET_EXPECTANCY = "NEGATIVE_NET_EXPECTANCY"
INSUFFICIENT_GAS = "INSUFFICIENT_GAS"
ALREADY_HOLDING = "ALREADY_HOLDING"
DEPTH_UNKNOWN = "DEPTH_UNKNOWN"
DISABLED = "DISABLED"

# A thesis must clear this AFTER every DEX cost, not before.
MIN_NET_R = 0.05


def enabled() -> bool:
    import os
    return (os.getenv("DEX_AUTOTRADE_ENABLED", "").lower()
            in ("1", "true", "yes"))


def evaluate_candidate(candidate: dict, *, gas_balance_sol: float = 0.0,
                       sol_price_usd: float = 0.0,
                       cash_usd: float = 0.0) -> dict:
    """Decide whether one surging token is worth a virtual DEX position.

    Every refusal names itself. "Skipped" as a single bucket would hide
    whether the desk is finding nothing, being refused by liquidity, or
    being refused by economics — and those call for completely different
    responses.
    """
    from lib.dex_paper import size_for_pool
    from lib.dex_swap_math import (depth_adjusted_size, quote_swap,
                                   spendable_native)

    mint = candidate.get("mint")
    symbol = candidate.get("symbol") or (mint or "")[:8]
    reserve = float(candidate.get("reserve_usd") or 0)
    price = float(candidate.get("price_usd") or 0)
    depth_conf = candidate.get("depth_confidence")
    gross_r = candidate.get("gross_expected_r")

    out = {"mint": mint, "symbol": symbol, "eligible": False,
           "version": AUTOTRADE_VERSION}

    if not enabled():
        return {**out, "reason": DISABLED,
                "detail": "DEX_AUTOTRADE_ENABLED is not set"}
    if price <= 0 or not mint:
        return {**out, "reason": NO_ROUTE, "detail": "no priceable token"}

    # GAS FIRST. A wallet that cannot pay for a transaction cannot make
    # one, and discovering that after sizing wastes the whole evaluation.
    #
    # THE PERSISTED WALLET IS THE AUTHORITY (P0-3). The gas_balance_sol
    # argument survives as a legacy shim, but it can only ever SHRINK what
    # the ledger says — a caller passing a bigger number than the wallet
    # holds is describing a wallet that does not exist, and believing it
    # was exactly how an impossible trade became executable.
    from lib import dex_wallet as DW
    if DW.initialized():
        wallet_sol = DW.balance(DW.SOL_MINT)["available"]
        effective_sol = (min(float(gas_balance_sol), wallet_sol)
                         if gas_balance_sol else wallet_sol)
        gas = spendable_native(effective_sol)
        gas["authority"] = "PERSISTED_WALLET"
        gas["wallet_available_sol"] = wallet_sol
        if gas_balance_sol and float(gas_balance_sol) > wallet_sol:
            gas["caller_exceeded_wallet"] = float(gas_balance_sol)
    else:
        gas = spendable_native(gas_balance_sol)
        gas["authority"] = "LEGACY_CALLER_SUPPLIED"
    if not gas["can_transact"]:
        return {**out, "reason": INSUFFICIENT_GAS, "detail": gas["reason"],
                "gas": gas}

    sizing = size_for_pool(reserve, cash_usd, dex=candidate.get("dex"),
                           depth_confidence=depth_conf)
    if not sizing.get("ok"):
        return {**out,
                "reason": (POOL_TOO_THIN if sizing.get("bound_by") == "pool_too_thin"
                           else NO_ROUTE),
                "detail": sizing.get("reason"), "sizing": sizing}

    q = quote_swap(sizing["size_usd"], reserve, dex=candidate.get("dex"),
                   sol_price_usd=sol_price_usd,
                   concentrated=bool(candidate.get("concentrated")))
    if not q.get("ok"):
        return {**out, "reason": NO_ROUTE, "detail": q.get("reason")}

    # UNCERTAIN DEPTH WEIGHTS THE PREDICTED IMPACT UP. Being wrong about
    # depth costs far more than trading smaller than necessary.
    adj = depth_adjusted_size(sizing["size_usd"], depth_conf)
    effective_impact = q["price_impact_pct"] * adj["impact_multiplier"]
    from lib.dex_paper import max_impact_pct
    if effective_impact > max_impact_pct():
        return {**out, "reason": IMPACT_TOO_HIGH,
                "detail": (f"impact {q['price_impact_pct']:.2f}% weighted to "
                           f"{effective_impact:.2f}% by {adj['depth_confidence']} "
                           f"depth, above the {max_impact_pct()}% cap"),
                "quote": q}

    # THE GATE. Net of every DEX cost, not gross.
    cost_usd = q["total_cost_usd"]
    risk_usd = candidate.get("risk_usd") or sizing["size_usd"]
    cost_r = (cost_usd / risk_usd) if risk_usd else None
    net_r = (float(gross_r) - cost_r) if (gross_r is not None and cost_r is not None) else None

    if net_r is not None and net_r < MIN_NET_R:
        return {**out, "reason": NEGATIVE_NET_EXPECTANCY,
                "detail": (f"{gross_r:+.3f}R gross less {cost_r:.3f}R of DEX "
                           f"cost leaves {net_r:+.3f}R — below the "
                           f"{MIN_NET_R}R bar. This is a VENUE result: the "
                           f"same thesis may clear on a CEX."),
                "gross_r": gross_r, "cost_r": cost_r, "net_r": net_r}

    return {
        **out, "eligible": True,
        "size_usd": sizing["size_usd"],
        "price_usd": price,
        "reserve_usd": reserve,
        "depth_confidence": adj["depth_confidence"],
        "impact_pct": q["price_impact_pct"],
        "effective_impact_pct": effective_impact,
        "cost_usd": cost_usd, "cost_r": cost_r,
        "gross_r": gross_r, "net_r": net_r,
        "gas": gas, "quote": q, "sizing": sizing,
        "detail": (f"${sizing['size_usd']:,.0f} at "
                   f"{q['price_impact_pct']:.2f}% impact, "
                   f"depth {adj['depth_confidence']}"),
    }


def run_once(*, max_positions: int = 3, cash_usd: float | None = None,
             gas_balance_sol: float | None = None,
             sol_price_usd: float = 0.0,
             db=None) -> dict:
    """One autonomous pass: surging tokens -> evaluated -> opened.

    Reads the SAME surge result the UI reads — there is one surge engine,
    and a second definition here would be the split-brain this whole
    programme has been removing.
    """
    from app.database import get_db
    from lib.dex_paper import get_portfolio, open_dex_position
    from lib.token_surge import scan_and_score

    stats = {"scanned": 0, "eligible": 0, "opened": 0,
             "refused": {}, "version": AUTOTRADE_VERSION}
    if not enabled():
        return {**stats, "skipped": "DEX_AUTOTRADE_ENABLED is not set"}

    # The autonomous path OWNS its wallet: it is seeded (idempotently)
    # before any evaluation, so persisted state is always the authority
    # here and the legacy shim path below never applies to it. The old
    # default of gas_balance_sol=1.0 was the exact fictional input P0-3
    # removes -- autonomous execution now defaults to the ledger.
    from lib import dex_wallet as DW
    DW.ensure_wallet()

    def _run(session):
        pf = get_portfolio(session)
        cash = cash_usd if cash_usd is not None else float(pf.cash_usd or 0)

        # persist=False: the scheduled sampler owns surge history, and an
        # autotrade pass must not become a second writer of the baseline.
        surge = scan_and_score(limit=40, persist=False)
        for tok in (surge.get("tokens") or [])[:40]:
            if stats["opened"] >= max_positions:
                break
            stats["scanned"] += 1
            ev = evaluate_candidate(
                {**tok, "risk_usd": None},
                gas_balance_sol=(gas_balance_sol or 0.0),
                sol_price_usd=sol_price_usd, cash_usd=cash)
            if not ev.get("eligible"):
                r = ev.get("reason") or "UNKNOWN"
                stats["refused"][r] = stats["refused"].get(r, 0) + 1
                continue
            stats["eligible"] += 1
            opened = open_dex_position(
                mint=ev["mint"], symbol=ev["symbol"],
                pool_address=tok.get("pool_address"), dex=tok.get("dex"),
                reserve_usd=ev["reserve_usd"], price_usd=ev["price_usd"],
                size_usd=ev["size_usd"], sol_price_usd=sol_price_usd,
                db=session)
            if opened.get("error"):
                stats["refused"]["OPEN_REFUSED"] = \
                    stats["refused"].get("OPEN_REFUSED", 0) + 1
                continue
            stats["opened"] += 1
            cash -= ev["size_usd"]
        return stats

    if db is not None:
        return _run(db)
    with get_db() as session:
        return _run(session)


def copyability_gap(*, wallet_price: float, wallet_at: float,
                    jarvis_detected_at: float, jarvis_quote: float,
                    jarvis_fill: float | None = None) -> dict:
    """How much of a wallet's entry JARVIS could actually have captured.

    THE DISTINCTION THAT MAKES COPYABILITY MEAN ANYTHING. The wallet's
    price answers "what did this wallet pay?"; JARVIS's answers "what could
    JARVIS have paid AFTER detecting it?". Reporting the wallet's return as
    if JARVIS could have had it is the flattering error this prevents — the
    wallet was earlier, cheaper, and possibly took the size that made the
    pool worse.
    """
    latency = max(0.0, float(jarvis_detected_at) - float(wallet_at))
    fill = float(jarvis_fill if jarvis_fill is not None else jarvis_quote)
    decay = ((fill - float(wallet_price)) / float(wallet_price) * 100.0
             if wallet_price else None)
    return {
        "wallet_execution_price": float(wallet_price),
        "wallet_execution_at": float(wallet_at),
        "jarvis_detection_at": float(jarvis_detected_at),
        "detection_latency_s": latency,
        "hypothetical_jarvis_quote": float(jarvis_quote),
        "hypothetical_jarvis_fill": fill,
        # Positive means JARVIS would have paid MORE than the wallet did.
        "entry_decay_pct": decay,
        "captured_fraction": (float(wallet_price) / fill) if fill else None,
        "note": ("wallet execution and JARVIS execution are separate "
                 "measurements; the gap between them IS copyability"),
    }
