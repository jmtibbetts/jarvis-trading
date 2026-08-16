"""On-chain desk routes — wallet intelligence, token surge, DEX book, lending.

Six modules had been built and tested with no way to reach them from the
browser: dex_paper, dex_swap_math, wallet_discovery, token_surge,
capital_stake and capital_lending. Intelligence that cannot be seen changes
no decision, which is the same complaint §4 makes about a silent failure —
the work exists and the operator cannot tell.

Every route here returns PROVENANCE alongside its numbers. On-chain values
carry very different weights: an obligation decoded by the canonical Kamino
layout is verified, a liquidation distance derived from it is calculated,
and a cascade estimate is modelled. Collapsing those into one confident
number is how a model starts being read as a measurement.
"""
from fastapi import APIRouter, Body

from app.routers.common import *  # noqa: F401,F403

router = APIRouter()


# ── Wallet registry ──────────────────────────────────────────────────────
@router.get("/onchain/wallets")
def onchain_wallets(status: str = None, limit: int = 100):
    """The wallet universe, with its lifecycle state.

    Seeds are NOT presented as smart money — they are unproven candidates
    that happen to have been supplied by hand, and the registry keeps that
    distinction because the whole point is to MEASURE which wallets earn
    the label rather than assume it.
    """
    from app.database import WalletRegistry, get_db
    from lib.wallet_registry import counts

    with get_db() as db:
        q = db.query(WalletRegistry)
        if status:
            q = q.filter(WalletRegistry.status == status.upper())
        q = q.order_by(WalletRegistry.smart_money_score.desc().nullslast(),
                       WalletRegistry.first_discovered_at.desc())
        rows = q.limit(max(1, min(limit, 500))).all()
        out = [{
            "address": w.address,
            "status": w.status,
            "source": w.source,
            "pinned": bool(w.pinned),
            "entity_type": w.entity_type,
            "entity_name": w.entity_name,
            "is_trader": w.is_trader,
            "discovery_reason": w.discovery_reason,
            "first_discovered_at": w.first_discovered_at,
            # None means NOT YET MEASURED, never zero. A wallet with no
            # score has not been analysed; showing 0 would rank it as bad.
            "whale_score": w.whale_score,
            "smart_money_score": w.smart_money_score,
            "alpha_score": w.alpha_score,
            "copy_score": w.copy_score,
            "confidence_score": w.confidence_score,
        } for w in rows]
    return {"wallets": out, "counts": counts(),
            "note": ("Scores are null until a wallet has been analysed. Null "
                     "is 'not yet measured', not zero — an unscored wallet "
                     "is unknown, not bad.")}


@router.get("/onchain/discovery/status")
def discovery_status():
    """Is autonomous discovery actually running, and what has it found?"""
    from app.database import WalletRegistry, get_db
    from lib.wallet_registry import counts, discovery_enabled

    with get_db() as db:
        by_source = {}
        for w in db.query(WalletRegistry).all():
            by_source[w.source or "unknown"] = by_source.get(w.source or "unknown", 0) + 1
    try:
        from app.scheduler import job_status
        job = job_status.get("wallet_discovery") or {}
    except Exception:
        job = {}
    return {"enabled": discovery_enabled(), "counts": counts(),
            "by_source": by_source,
            "last_run": job.get("last"), "job_status": job.get("status"),
            "job_error": job.get("error")}


@router.post("/onchain/discovery/run")
def discovery_run(max_tokens: int = 5):
    """Run one discovery pass now. Bounded — each token costs 2 RPC calls."""
    from lib.wallet_discovery import discover_from_tokens
    return discover_from_tokens(max_tokens=max(1, min(max_tokens, 20)))


# ── Token surge ──────────────────────────────────────────────────────────
@router.get("/onchain/surge")
def token_surge(limit: int = 20):
    """Tokens becoming unusually active FOR THEMSELVES.

    Ranked by acceleration against each token's own baseline, not by size —
    a pair doing $2M every day is not news, and one that went from $5k to
    $500k in an hour is.
    """
    from lib.token_surge import enabled, scan_and_score

    # ONE pipeline. This route used to call `score_snapshot(snap, [])` — an
    # empty history literal — so every token was permanently scored by the
    # new-token model, and the rigorous baseline machinery it was calling
    # never had anything to be rigorous about. scan_and_score persists each
    # observation and scores against the token's own stored history, and
    # wallet discovery reads the same result.
    result = scan_and_score(limit=max(1, min(limit, 100)))
    return {"enabled": enabled(), **result}


# ── Virtual DEX book ─────────────────────────────────────────────────────
@router.get("/onchain/dex/book")
def dex_book():
    """The simulated on-chain book — separate from paper and Auto Sim."""
    from app.database import DexPosition, get_db
    from lib.dex_paper import summary

    with get_db() as db:
        rows = db.query(DexPosition).filter(DexPosition.status == "Open").all()
        positions = [{
            "id": p.id, "mint": p.mint, "symbol": p.symbol, "dex": p.dex,
            "qty_tokens": p.qty_tokens,
            "entry_price_usd": p.entry_price_usd,
            "quoted_price_usd": p.quoted_price_usd,
            "current_price_usd": p.current_price_usd,
            "notional_usd": p.notional_usd,
            # Kept separate on purpose: the pool's price, your own size, and
            # the chain each call for a different remedy.
            "entry_impact_pct": p.entry_impact_pct,
            "entry_pool_fee_usd": p.entry_pool_fee_usd,
            "entry_network_fee_usd": p.entry_network_fee_usd,
            "pool_reserve_usd_at_entry": p.pool_reserve_usd_at_entry,
            "opened_at": p.opened_at, "notes": p.notes,
        } for p in rows]
    return {**summary(), "positions": positions}


@router.get("/onchain/dex/quote")
def dex_quote(amount_usd: float, reserve_usd: float, dex: str = None,
              sol_price_usd: float = 0.0, concentrated: bool = False):
    """Price a swap before taking it, with every cost itemised."""
    from lib.dex_swap_math import (max_size_for_impact, quote_swap,
                                   round_trip_cost)
    q = quote_swap(amount_usd, reserve_usd, dex=dex,
                   sol_price_usd=sol_price_usd, concentrated=concentrated)
    if not q.get("ok"):
        return q
    rt = round_trip_cost(amount_usd, reserve_usd, dex=dex,
                         sol_price_usd=sol_price_usd, concentrated=concentrated)
    return {**q,
            "round_trip_cost_pct": rt.get("round_trip_cost_pct"),
            "breakeven_move_pct": rt.get("breakeven_move_pct"),
            "max_size_1pct_impact": max_size_for_impact(reserve_usd, 1.0, dex=dex),
            "max_size_2pct_impact": max_size_for_impact(reserve_usd, 2.0, dex=dex)}


@router.get("/onchain/dex/trades")
def dex_trades(limit: int = 50):
    """Closed swaps, newest first — the book's actual record.

    Costs are itemised rather than netted into one P&L figure, because on
    a DEX they fail differently: price impact is a function of YOUR size
    against pool depth, the pool fee is the venue's cut, and the network
    fee is the chain's. Collapsing them hides which one made a trade
    unprofitable.
    """
    from app.database import DexTrade, get_db

    with get_db() as db:
        rows = (db.query(DexTrade)
                .order_by(DexTrade.closed_at.desc())
                .limit(max(1, min(limit, 500))).all())
        return {"trades": [{
            "id": t.id, "mint": t.mint, "symbol": t.symbol, "dex": t.dex,
            "qty_tokens": t.qty_tokens,
            "entry_price_usd": t.entry_price_usd,
            "exit_price_usd": t.exit_price_usd,
            "notional_usd": t.notional_usd,
            "gross_pnl_usd": t.gross_pnl_usd,
            "net_pnl_usd": t.net_pnl_usd,
            "pnl_pct": t.pnl_pct,
            "entry_impact_pct": t.entry_impact_pct,
            "exit_impact_pct": t.exit_impact_pct,
            "total_fees_usd": t.total_fees_usd,
            "exit_reason": t.exit_reason,
            "opened_at": t.opened_at, "closed_at": t.closed_at,
        } for t in rows], "count": len(rows)}


@router.post("/onchain/dex/open")
def dex_open(payload: dict = Body(...)):
    """Simulate a buy against real pool depth.

    Every refusal names itself — "impact 4.2% exceeds the 3.0% ceiling" is
    actionable, an empty response is not.
    """
    from lib.dex_paper import open_dex_position

    required = ("mint", "reserve_usd", "price_usd")
    missing = [k for k in required if payload.get(k) in (None, "")]
    if missing:
        return {"error": f"missing required field(s): {', '.join(missing)}"}

    return open_dex_position(
        mint=str(payload["mint"]),
        symbol=payload.get("symbol"),
        pool_address=payload.get("pool_address"),
        dex=payload.get("dex"),
        reserve_usd=float(payload["reserve_usd"]),
        price_usd=float(payload["price_usd"]),
        size_usd=(float(payload["size_usd"])
                  if payload.get("size_usd") not in (None, "") else None),
        stop_price_usd=(float(payload["stop_price_usd"])
                        if payload.get("stop_price_usd") not in (None, "") else None),
        target_price_usd=(float(payload["target_price_usd"])
                          if payload.get("target_price_usd") not in (None, "") else None),
        sol_price_usd=float(payload.get("sol_price_usd") or 0.0),
        concentrated=bool(payload.get("concentrated")),
    )


@router.post("/onchain/dex/close")
def dex_close(payload: dict = Body(...)):
    """Simulate the sell, priced against pool depth on the way OUT too.

    Getting in cheaply and being unable to get out is the characteristic
    on-chain failure, so exit impact is quoted rather than assumed.
    """
    from lib.dex_paper import close_dex_position

    if not payload.get("position_id"):
        return {"error": "position_id is required"}
    if payload.get("price_usd") in (None, ""):
        return {"error": "price_usd is required — the exit needs a price"}

    return close_dex_position(
        str(payload["position_id"]),
        float(payload["price_usd"]),
        reserve_usd=(float(payload["reserve_usd"])
                     if payload.get("reserve_usd") not in (None, "") else None),
        reason=str(payload.get("reason") or "manual"),
        sol_price_usd=float(payload.get("sol_price_usd") or 0.0),
        concentrated=bool(payload.get("concentrated")),
    )


@router.get("/onchain/dex/sizing")
def dex_sizing(reserve_usd: float, cash_usd: float | None = None):
    """The size this pool can absorb, and why it is bounded there.

    On-chain the binding constraint is POOL DEPTH, not account equity — a
    book that sizes off cash alone will happily propose a trade the pool
    cannot fill without moving the price against itself.
    """
    from lib.dex_paper import (max_impact_pct, min_pool_reserve_usd,
                               size_for_pool, summary)

    cash = cash_usd if cash_usd is not None else summary().get("cash_usd", 0.0)
    out = size_for_pool(float(reserve_usd), float(cash))
    return {**out,
            "reserve_usd": reserve_usd, "cash_usd": cash,
            "limits": {"max_impact_pct": max_impact_pct(),
                       "min_pool_reserve_usd": min_pool_reserve_usd()},
            "provenance": "CALCULATED from pool reserve and constant-product math"}


# ── Capital: staking and lending ─────────────────────────────────────────
@router.get("/onchain/stake/{wallet}")
def wallet_stake(wallet: str, sol_price_usd: float = 0.0):
    """Native staking, and whether capital is becoming liquid."""
    from lib.capital_stake import capital_liquidity_signal, stake_accounts_for
    stake = stake_accounts_for(wallet)
    return {**stake, "signal": capital_liquidity_signal(stake, sol_price_usd),
            "provenance": "VERIFIED on-chain (RPC-parsed stake accounts)"}


@router.get("/onchain/lending/{wallet}")
def wallet_lending(wallet: str):
    """Kamino positions for one wallet, decoded canonically."""
    from lib.capital_lending import (DECODER_SOURCE, DECODER_VERSION,
                                     obligations_for)
    positions = obligations_for(wallet)
    # Assets are now NAMED: reserve decoding resolves each deposit and
    # borrow to its mint, decimals, oracle price and liquidation threshold.
    for p in positions:
        raw = p.pop("_raw", None)
        if raw:
            from lib.capital_reserves import name_positions
            p["assets"] = name_positions(raw)
    return {"wallet": wallet, "positions": positions,
            "count": len(positions),
            "decoder": {"source": DECODER_SOURCE, "version": DECODER_VERSION},
            "provenance": {
                "position_values": "VERIFIED — canonical Kamino layout",
                "health_factor": "CALCULATED from Kamino's own rule",
                "asset_identity": "VERIFIED — canonical Kamino reserve layout",
                "prices": ("VERIFIED — Kamino's reserve oracle, not exchange "
                           "spot: liquidation is decided by the protocol's "
                           "own price"),
                "collateral_amounts": ("deposits are cTOKENS; the underlying "
                                       "figure is derived from Kamino's own "
                                       "position value, not amount x price"),
                "liquidation_price": ("UNAVAILABLE for multi-collateral "
                                      "positions — no single honest price "
                                      "exists when several assets move health"),
            }}


@router.get("/onchain/lending/risk/scan")
def lending_risk_scan(limit_scanned: int = 5000, min_debt_usd: float = 10_000.0):
    """Kamino positions closest to forced selling.

    Scans the protocol rather than a watchlist: a cascade is driven by
    aggregate leverage, so counting only known wallets would measure the
    watchlist instead of the market.
    """
    from lib.capital_lending import scan_positions_at_risk
    res = scan_positions_at_risk(limit_scanned=max(100, min(limit_scanned, 50_000)),
                                 min_debt_usd=min_debt_usd)

    # Name the assets, then model the shock ladder over them. Both steps
    # need reserve data, so a position whose reserve will not decode stays
    # UNRESOLVED and is counted rather than quietly dropped.
    from lib.capital_reserves import load_reserves, name_positions, position_reserves
    from lib.liquidation_stress import aggregate_by_asset, stress_ladder

    positions = res.get("positions", [])[:50]
    wanted = []
    for p in positions:
        raw = p.get("_raw")
        if raw:
            slots = position_reserves(raw)
            wanted += [d["reserve"] for d in slots["deposits"]]
            wanted += [b["reserve"] for b in slots["borrows"]]
    reserves = load_reserves(wanted) if wanted else {}
    for p in positions:
        raw = p.pop("_raw", None)
        if raw:
            p["assets"] = name_positions(raw, reserves)

    res["positions"] = positions
    res["by_asset"] = aggregate_by_asset(positions)
    res["stress"] = {
        fam: stress_ladder(positions, family=fam)
        for fam in ("SOL_FAMILY", "STABLE")
    }
    res["provenance"] = {
        "values": "VERIFIED — canonical Kamino decode",
        "asset_identity": "VERIFIED — canonical Kamino reserve layout",
        "prices": "VERIFIED — Kamino reserve oracle, not exchange spot",
        "risk_state": "CALCULATED from Kamino's own health rule",
        "stress_ladder": ("MODELLED — hypothetical prices over verified "
                          "positions. Not a forecast."),
    }
    return res


# ── Protocol registry ────────────────────────────────────────────────────
@router.get("/onchain/protocols")
def protocols():
    """Which programs JARVIS can recognise, and which it cannot.

    Exposed because an unrecognised program is a KNOWN GAP rather than an
    absence of activity, and the operator should be able to see the edge of
    what the parser understands.
    """
    from lib.solana_protocols import LST_MINTS, PROGRAMS, STAKE_POOLS
    return {
        "programs": [{"program_id": pid, "category": cat, "name": name,
                      "verified_on_chain": ok}
                     for pid, (cat, name, ok) in sorted(
                         PROGRAMS.items(), key=lambda kv: kv[1][0])],
        "stake_pools": [{"account": a, "provider": p}
                        for a, p in STAKE_POOLS.items()],
        "lst_mints": [{"mint": m, "symbol": s, "provider": p,
                       "underlying": "SOL"}
                      for m, (s, p) in LST_MINTS.items()],
        "note": ("Every program ID was checked with getAccountInfo before "
                 "being registered. An unlisted program is UNKNOWN, which is "
                 "not the same as 'not a protocol'."),
    }
