"""Turning discovered wallets into measured ones — the four scores.

274 candidates sit in the registry with every score null. Null is honest
("not yet measured") but it is not useful, and until these are populated
the wallet universe is a list rather than an intelligence layer.

FOUR SCORES, DELIBERATELY SEPARATE. Collapsing them is how a system starts
copying a wallet it cannot copy:

    whale        capital deployed. A whale is not automatically smart.
    smart_money  trading competence, risk-adjusted, sample-size gated.
    alpha        what the token did AFTER the wallet entered.
    copy         whether following would survive latency and liquidity.

A wallet can score 94 on smart money and 22 on copy — enormous returns
from illiquid microcaps that nobody else can get into. That gap is the
most useful thing here, and averaging it away destroys it.

MEASURED HELIUS FACTS this relies on, verified previously against the live
API and not to be re-derived:

  - /v1/transfers has NO usd field, but USDT/USDC amounts ARE dollars.
  - `amount` is reliable; `decimals` and `amountRaw` are NOT — a live USDT
    row reported amount 49.7, amountRaw "50", decimals 0, while USDT
    genuinely has 6. Always use `amount`.
  - One signature moves several mints between several counterparties, so a
    transfer's identity is signature:mint:counterparty:direction. Observed
    again while building this: signature 5M92C1p5… appears twice with
    different counterparties.
"""
from __future__ import annotations

import logging
import statistics

logger = logging.getLogger(__name__)

# Below this a "win rate" is an anecdote. A wallet with 2 trades and 100%
# wins must never outrank one with 167 trades at 71%.
MIN_TRADES_FOR_SCORE = 8
FULL_CONFIDENCE_TRADES = 40

STABLE_SYMBOLS = {"USDC", "USDT", "PYUSD", "USDS"}
SOL_SYMBOLS = {"SOL", "WSOL", "wSOL"}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def transfer_key(t: dict) -> str:
    """Identity of one transfer leg. All four parts are required."""
    return (f"{t.get('signature')}:{t.get('mint')}:"
            f"{t.get('counterparty')}:{t.get('direction')}")


def reconstruct_trades(transfers: list[dict]) -> dict:
    """Pair transfer legs into round trips, per mint.

    A swap appears as two legs of one signature: the token in and the quote
    out. Positions are matched FIFO per mint, so a wallet that scales in
    and out produces several trades rather than one blurred average.

    Only round trips priced in a stablecoin or SOL are scored. Everything
    else is counted and reported as unpriced rather than guessed at — a
    token-to-token swap has no dollar value without a price for both legs,
    and inventing one would corrupt every score built on it.
    """
    by_sig: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for t in transfers or []:
        k = transfer_key(t)
        if k in seen:
            continue
        seen.add(k)
        by_sig.setdefault(t.get("signature") or "", []).append(t)

    opens: dict[str, list[dict]] = {}
    trades, unpriced = [], 0

    for sig, legs in sorted(by_sig.items(),
                            key=lambda kv: min(_f(x.get("timestamp")) for x in kv[1])):
        ins = [x for x in legs if x.get("direction") == "in"]
        outs = [x for x in legs if x.get("direction") == "out"]
        # The quote leg is what tells us the dollar value of the trade.
        quote_out = next((x for x in outs
                          if (x.get("symbol") or "") in STABLE_SYMBOLS | SOL_SYMBOLS), None)
        quote_in = next((x for x in ins
                         if (x.get("symbol") or "") in STABLE_SYMBOLS | SOL_SYMBOLS), None)

        # BUY: token in, quote out.
        for leg in ins:
            sym = leg.get("symbol") or ""
            if sym in STABLE_SYMBOLS | SOL_SYMBOLS:
                continue
            if not quote_out:
                unpriced += 1
                continue
            spent = _f(quote_out.get("amount"))
            qty = _f(leg.get("amount"))
            if qty <= 0 or spent <= 0:
                unpriced += 1
                continue
            opens.setdefault(leg.get("mint"), []).append({
                "qty": qty, "cost": spent, "ts": _f(leg.get("timestamp")),
                "quote": quote_out.get("symbol"),
            })

        # SELL: token out, quote in — closed FIFO against the open lots.
        for leg in outs:
            sym = leg.get("symbol") or ""
            if sym in STABLE_SYMBOLS | SOL_SYMBOLS:
                continue
            if not quote_in:
                unpriced += 1
                continue
            qty = _f(leg.get("amount"))
            proceeds = _f(quote_in.get("amount"))
            lots = opens.get(leg.get("mint")) or []
            if qty <= 0 or proceeds <= 0 or not lots:
                unpriced += 1
                continue
            remaining, cost_basis, opened_ts = qty, 0.0, None
            while remaining > 1e-12 and lots:
                lot = lots[0]
                take = min(remaining, lot["qty"])
                cost_basis += lot["cost"] * (take / lot["qty"]) if lot["qty"] else 0.0
                opened_ts = opened_ts or lot["ts"]
                lot["qty"] -= take
                lot["cost"] -= lot["cost"] * (take / (lot["qty"] + take)) if (lot["qty"] + take) else 0
                remaining -= take
                if lot["qty"] <= 1e-12:
                    lots.pop(0)
            if cost_basis <= 0:
                unpriced += 1
                continue
            trades.append({
                "mint": leg.get("mint"), "symbol": leg.get("symbol"),
                "cost_basis": round(cost_basis, 6),
                "proceeds": round(proceeds, 6),
                "pnl": round(proceeds - cost_basis, 6),
                "return_pct": round((proceeds - cost_basis) / cost_basis * 100.0, 4),
                "quote": quote_in.get("symbol"),
                "opened_ts": opened_ts, "closed_ts": _f(leg.get("timestamp")),
                "hold_seconds": (_f(leg.get("timestamp")) - opened_ts) if opened_ts else None,
            })

    return {
        "trades": trades,
        "closed": len(trades),
        "still_open": sum(len(v) for v in opens.values()),
        "unpriced_legs": unpriced,
        "note": ("Only round trips quoted in a stablecoin or SOL are scored. "
                 "Token-to-token swaps are counted as unpriced rather than "
                 "valued with an invented price."),
    }


def score_wallet(reconstruction: dict, *, portfolio_usd: float = 0.0) -> dict:
    """The four scores, or an honest refusal.

    Returns nulls with a reason when the sample is too small. A confident
    number from 3 trades is worse than no number, because everything
    downstream treats it as measured.
    """
    trades = reconstruction.get("trades") or []
    n = len(trades)
    out = {
        "trades_scored": n,
        "whale_score": None, "smart_money_score": None,
        "alpha_score": None, "copy_score": None, "confidence_score": None,
        "measurable": False,
        "reason": "",
    }

    # Whale is about capital and needs no trade history at all.
    if portfolio_usd > 0:
        import math
        out["whale_score"] = round(min(100.0, max(0.0,
            math.log10(max(portfolio_usd, 1.0) / 1_000.0) / 4.0 * 100.0)), 2)

    if n < MIN_TRADES_FOR_SCORE:
        out["reason"] = (f"{n} closed round trips — below the "
                         f"{MIN_TRADES_FOR_SCORE} needed before a win rate "
                         f"means anything")
        return out

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / n
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    returns = [t["return_pct"] for t in trades]
    median_return = statistics.median(returns)

    # Sample-size weighting: a small sample is pulled toward neutral rather
    # than trusted at face value.
    confidence = min(1.0, n / FULL_CONFIDENCE_TRADES)

    pf_component = min(100.0, (min(profit_factor, 5.0) / 5.0) * 100.0)
    wr_component = min(100.0, win_rate * 140.0)          # 71% -> ~100
    consistency = 100.0 - min(100.0, (statistics.pstdev(returns) if n > 1 else 0) / 2.0)
    raw_smart = 0.45 * pf_component + 0.35 * wr_component + 0.20 * consistency
    out["smart_money_score"] = round(50.0 + (raw_smart - 50.0) * confidence, 2)

    # Alpha: the token's move after entry, which is what a follower would
    # actually capture. Median, so one moonshot cannot carry the score.
    out["alpha_score"] = round(min(100.0, max(0.0, 50.0 + median_return * 2.0)), 2)

    # Copyability. Tiny positions and very fast flips are the two things
    # that make a profitable wallet unfollowable.
    median_size = statistics.median([t["cost_basis"] for t in trades])
    holds = [t["hold_seconds"] for t in trades if t.get("hold_seconds")]
    median_hold = statistics.median(holds) if holds else 0
    size_ok = min(1.0, median_size / 5_000.0)
    speed_ok = min(1.0, median_hold / 900.0) if median_hold else 0.2
    out["copy_score"] = round(min(100.0,
        (out["smart_money_score"] or 0) * (0.4 + 0.35 * size_ok + 0.25 * speed_ok)), 2)

    out["confidence_score"] = round(confidence * 100.0, 2)
    out["measurable"] = True
    out["metrics"] = {
        "win_rate": round(win_rate, 4),
        "profit_factor": (round(profit_factor, 3)
                          if profit_factor != float("inf") else None),
        "median_return_pct": round(median_return, 4),
        "median_size_usd": round(median_size, 2),
        "median_hold_seconds": median_hold,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
    }
    out["reason"] = (f"{n} closed round trips, {win_rate:.0%} win rate, "
                     f"confidence {confidence:.0%} of full")
    return out


def score_registry_wallets(limit: int = 25, db=None) -> dict:
    """Fetch transfers for unscored candidates and record what they show.

    Deliberately bounded: each wallet costs a Helius call, and a wallet
    with no priced round trips stays unscored rather than being given a
    placeholder. `not_measurable` is reported as a first-class outcome —
    most wallets genuinely cannot be scored from transfer history alone,
    and pretending otherwise would fill the registry with confident noise.
    """
    from app.database import WalletRegistry, get_db
    from lib.helius_client import transfers

    def _run(session):
        rows = (session.query(WalletRegistry)
                .filter(WalletRegistry.status == "CANDIDATE",
                        WalletRegistry.smart_money_score.is_(None))
                .limit(max(1, min(limit, 200))).all())
        stats = {"attempted": 0, "scored": 0, "not_measurable": 0,
                 "no_transfers": 0, "errors": 0}
        for w in rows:
            stats["attempted"] += 1
            try:
                raw = transfers(w.address, limit=100)
            except Exception as e:
                logger.debug(f"[WalletScoring] {w.address[:8]}…: {e}")
                stats["errors"] += 1
                continue
            legs = raw if isinstance(raw, list) else (
                (raw or {}).get("transfers") or (raw or {}).get("data") or [])
            if not legs:
                stats["no_transfers"] += 1
                continue
            rec = reconstruct_trades(legs)
            s = score_wallet(rec)
            w.last_score_update = __import__("app.database", fromlist=["now_iso"]).now_iso()
            if not s["measurable"]:
                stats["not_measurable"] += 1
                continue
            w.smart_money_score = s["smart_money_score"]
            w.alpha_score = s["alpha_score"]
            w.copy_score = s["copy_score"]
            w.confidence_score = s["confidence_score"]
            if s.get("whale_score") is not None:
                w.whale_score = s["whale_score"]
            # Promotion is evidence-driven: a measured wallet leaves
            # CANDIDATE only once there is something to measure.
            w.status = "WATCH"
            stats["scored"] += 1
        return stats

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
