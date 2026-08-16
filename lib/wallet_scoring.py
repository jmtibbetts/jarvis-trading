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

# Quote handling lives in lib/quote_valuation — one authority for "what was
# this leg worth in dollars, at the moment it traded". These names are kept
# for the modules that still import them, but nothing here decides value
# from a symbol set any more.
from lib.quote_valuation import (ESTIMATED, MEASURED, is_valuable_quote,  # noqa: E402
                                 value_in_usd)

STABLE_SYMBOLS = {"USDC", "USDT", "PYUSD", "USDS"}
SOL_SYMBOLS = {"SOL", "WSOL", "wSOL"}

# Bumped whenever the arithmetic behind a score changes materially. Scores
# from different versions are NOT comparable and must not be pooled:
#   v1  quote amounts summed across USDC and SOL as though both were USD
#   v2  every leg normalized to USD at its own timestamp; alpha_score
#       vacated because it held realized return, not post-entry alpha
SCORE_VERSION = "v2_usd_normalized"


def migrate_legacy_alpha(db=None) -> dict:
    """Move pre-v2 alpha_score values to legacy_alpha_score, once.

    Those numbers are the wallet's own realized round-trip return computed
    from mixed quote units. Leaving them in `alpha_score` under the
    corrected definition would mix two incompatible semantics in one time
    series — the thing the audit forbids explicitly.
    """
    from app.database import WalletRegistry, get_db

    def _run(session):
        rows = (session.query(WalletRegistry)
                .filter(WalletRegistry.alpha_score.isnot(None))
                .filter(WalletRegistry.wallet_score_version.is_(None)).all())
        for w in rows:
            if w.legacy_alpha_score is None:
                w.legacy_alpha_score = w.alpha_score
            w.alpha_score = None
            w.wallet_score_version = "v1_legacy_migrated"
        return {"legacy_alpha_migrated": len(rows)}

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


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
    unpriced_reasons: list[str] = []

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
            if is_valuable_quote(sym):
                continue
            if not quote_out:
                unpriced += 1
                continue
            spent = _f(quote_out.get("amount"))
            qty = _f(leg.get("amount"))
            if qty <= 0 or spent <= 0:
                unpriced += 1
                continue
            # NORMALIZE AT THE LEG. The quote amount is a quantity of some
            # asset, not a dollar figure, and it is converted here — at the
            # trade's own timestamp — so nothing downstream can add SOL to
            # dollars. See lib/quote_valuation.
            ts = _f(leg.get("timestamp"))
            v = value_in_usd(spent, quote_out.get("symbol"), ts)
            if v["usd_value"] is None:
                unpriced += 1
                unpriced_reasons.append(v["reason"] or "unpriced buy quote")
                continue
            opens.setdefault(leg.get("mint"), []).append({
                "qty": qty,
                "cost_usd": v["usd_value"],
                "ts": ts,
                "quote_symbol": quote_out.get("symbol"),
                "quote_amount": spent,
                "quote_price_usd": v["quote_price_usd"],
                "price_source": v["price_source"],
                "price_quality": v["price_quality"],
            })

        # SELL: token out, quote in — closed FIFO against the open lots.
        for leg in outs:
            sym = leg.get("symbol") or ""
            if is_valuable_quote(sym):
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
            closed_ts = _f(leg.get("timestamp"))
            pv = value_in_usd(proceeds, quote_in.get("symbol"), closed_ts)
            if pv["usd_value"] is None:
                unpriced += 1
                unpriced_reasons.append(pv["reason"] or "unpriced sell quote")
                continue

            remaining, cost_basis_usd, opened_ts = qty, 0.0, None
            entry_quotes, entry_quality = set(), set()
            while remaining > 1e-12 and lots:
                lot = lots[0]
                take = min(remaining, lot["qty"])
                cost_basis_usd += lot["cost_usd"] * (take / lot["qty"]) if lot["qty"] else 0.0
                opened_ts = opened_ts or lot["ts"]
                entry_quotes.add(lot["quote_symbol"])
                entry_quality.add(lot["price_quality"])
                lot["qty"] -= take
                lot["cost_usd"] -= lot["cost_usd"] * (take / (lot["qty"] + take)) if (lot["qty"] + take) else 0
                remaining -= take
                if lot["qty"] <= 1e-12:
                    lots.pop(0)
            if cost_basis_usd <= 0:
                unpriced += 1
                continue

            pnl_usd = pv["usd_value"] - cost_basis_usd
            # The weakest link decides the trade's provenance: a MEASURED
            # exit against an ESTIMATED entry is an ESTIMATED round trip.
            quality = (ESTIMATED if ESTIMATED in (entry_quality | {pv["price_quality"]})
                       else MEASURED)
            trades.append({
                "mint": leg.get("mint"), "symbol": leg.get("symbol"),
                # ── USD, the only unit anything may aggregate ──
                "cost_basis_usd": round(cost_basis_usd, 6),
                "proceeds_usd": round(pv["usd_value"], 6),
                "pnl_usd": round(pnl_usd, 6),
                "notional_usd": round(cost_basis_usd, 6),
                "return_pct": round(pnl_usd / cost_basis_usd * 100.0, 4),
                # ── the quote identity that produced them ──
                "quote_symbol": pv["quote_symbol"],
                "quote_amount": pv["quote_amount"],
                "quote_price_usd": pv["quote_price_usd"],
                "entry_quote_symbols": sorted(q for q in entry_quotes if q),
                "price_source": pv["price_source"],
                "price_quality": quality,
                "opened_ts": opened_ts, "closed_ts": closed_ts,
                "hold_seconds": (closed_ts - opened_ts) if opened_ts else None,
            })

    return {
        "trades": trades,
        "closed": len(trades),
        "still_open": sum(len(v) for v in opens.values()),
        "unpriced_legs": unpriced,
        "unpriced_reasons": sorted(set(unpriced_reasons))[:5],
        "note": ("Every trade is normalized to USD at its own timestamp before "
                 "any aggregation. Stablecoin quotes use the assumed peg; SOL "
                 "quotes use the hourly close at the trade. A quote this desk "
                 "cannot value leaves the trade UNPRICED and excluded, never "
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
        # Present on EVERY return path, including the unmeasurable one, so
        # a caller persisting these never has to guess whether the key
        # exists — an absent count and a zero count are different claims.
        "winning_trades": 0, "losing_trades": 0,
        "whale_score": None, "smart_money_score": None,
        "alpha_score": None, "copy_score": None, "confidence_score": None,
        "metrics": {},
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

    # EVERY sum below is in USD, because reconstruct_trades normalized each
    # leg at its own timestamp. This aggregation used to add `t["pnl"]`
    # across trades whose quote was USDC for one and SOL for the next, and
    # call the total dollars.
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / n
    gross_win = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
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

    # WHAT THIS ACTUALLY IS: the wallet's own median REALIZED round-trip
    # return. It is NOT post-entry market alpha, and the comment that used
    # to sit here claimed it was — "the token's move after entry, which is
    # what a follower would actually capture" — directly above arithmetic
    # over closed trades. A reviewer reading that would conclude the
    # post-entry-alpha feature already existed, which is how the missing
    # capability stayed hidden.
    #
    # Named `legacy_alpha_score` until W5 replaces it with measured
    # post-entry horizons. The two definitions must never share a field.
    out["legacy_alpha_score"] = round(
        min(100.0, max(0.0, 50.0 + median_return * 2.0)), 2)
    out["alpha_score"] = None
    out["alpha_basis"] = ("NOT MEASURED — post-entry market alpha requires "
                          "forward price observations after each entry, which "
                          "are not yet collected. legacy_alpha_score is the "
                          "wallet's own realized return, a different metric.")

    # Copyability. Tiny positions and very fast flips are the two things
    # that make a profitable wallet unfollowable.
    median_size = statistics.median([t["cost_basis_usd"] for t in trades])
    holds = [t["hold_seconds"] for t in trades if t.get("hold_seconds")]
    median_hold = statistics.median(holds) if holds else 0
    size_ok = min(1.0, median_size / 5_000.0)
    speed_ok = min(1.0, median_hold / 900.0) if median_hold else 0.2
    out["copy_score"] = round(min(100.0,
        (out["smart_money_score"] or 0) * (0.4 + 0.35 * size_ok + 0.25 * speed_ok)), 2)

    out["confidence_score"] = round(confidence * 100.0, 2)
    out["measurable"] = True
    # Counts the lifecycle gates on, surfaced at the top level rather than
    # buried in `metrics` because they are evidence, not diagnostics.
    out["winning_trades"] = len(wins)
    out["losing_trades"] = len(losses)

    sizes = [t["cost_basis_usd"] for t in trades if t.get("cost_basis_usd")]
    out["metrics"] = {
        "average_size_usd": round(sum(sizes) / len(sizes), 2) if sizes else None,
        "largest_size_usd": round(max(sizes), 2) if sizes else None,
        "win_rate": round(win_rate, 4),
        "profit_factor": (round(profit_factor, 3)
                          if profit_factor != float("inf") else None),
        "median_return_pct": round(median_return, 4),
        "median_size_usd": round(median_size, 2),
        "median_hold_seconds": median_hold,
        "total_pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        # Provenance of the money itself: a book priced entirely off the
        # assumed peg is a weaker claim than one priced off measured bars.
        "priced_measured": sum(1 for t in trades
                               if t.get("price_quality") == MEASURED),
        "priced_estimated": sum(1 for t in trades
                                if t.get("price_quality") == ESTIMATED),
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
        # Two populations need scoring, and only the first was selected:
        #   1. never scored (CANDIDATE with a null score)
        #   2. scored by a SUPERSEDED engine — v1 summed mixed quote units,
        #      so its numbers are not comparable with v2's and would
        #      otherwise sit in the registry forever, since a promoted
        #      wallet is no longer a CANDIDATE and never re-qualifies.
        from sqlalchemy import or_
        rows = (session.query(WalletRegistry)
                .filter(~WalletRegistry.status.in_(("EXCLUDED_ENTITY", "ARCHIVED")))
                .filter(or_(
                    (WalletRegistry.status == "CANDIDATE")
                    & WalletRegistry.smart_money_score.is_(None),
                    WalletRegistry.wallet_score_version.is_(None),
                    WalletRegistry.wallet_score_version != SCORE_VERSION))
                .limit(max(1, min(limit, 200))).all())
        stats = {"attempted": 0, "scored": 0, "not_measurable": 0,
                 "no_transfers": 0, "errors": 0}
        for w in rows:
            stats["attempted"] += 1
            now = __import__("app.database", fromlist=["now_iso"]).now_iso()
            try:
                raw = transfers(w.address, limit=100)
            except Exception as e:
                # PROVIDER FAILURE — NOT a measurement of zero. Every count
                # already on this row is LEFT INTACT: overwriting a known
                # qualified_trades=12 with 0 because Helius timed out
                # destroys evidence and calls the result a measurement.
                # Only the run's own outcome is recorded, and
                # last_score_update is deliberately not touched so the
                # existing score keeps its real age.
                logger.debug(f"[WalletScoring] {w.address[:8]}…: {e}")
                w.analysis_status = "FAILED"
                w.analysis_error = f"{type(e).__name__}: {str(e)[:160]}"
                w.last_analysis_at = now
                stats["errors"] += 1
                continue

            legs = raw if isinstance(raw, list) else (
                (raw or {}).get("transfers") or (raw or {}).get("data") or [])
            if not legs:
                # A SUCCESSFUL read that found nothing. This IS a
                # measurement — of zero — and is a different fact from the
                # failure above.
                w.analysis_status = "NO_VERIFIED_TRADES"
                w.measurability_reason = "NO_TRANSFER_HISTORY"
                w.measurable = False
                w.sample_count = 0
                w.required_sample_count = MIN_TRADES_FOR_SCORE
                w.last_analysis_at = now
                w.analysis_error = None
                stats["no_transfers"] += 1
                continue
            rec = reconstruct_trades(legs)
            s = score_wallet(rec)
            w.last_score_update = __import__("app.database", fromlist=["now_iso"]).now_iso()

            # THE STATISTICS LIFECYCLE READS MUST BE THE STATISTICS SCORING
            # WRITES. Every column here already existed on WalletRegistry and
            # every value was already computed by score_wallet — but nothing
            # wrote them, so `wallet.qualified_trades or 0` in
            # wallet_lifecycle evaluated to 0 for every wallet ever scored and
            # the SMART_MONEY gate (>= 15 qualified trades) was unreachable by
            # construction. Schema, producer and consumer all correct; the
            # assignment between them simply absent.
            #
            # Written BEFORE the measurability gate on purpose. A wallet with
            # 3 round trips is not measurable, but "3 of 15" is the true and
            # useful answer to "why is this not smart money?" — skipping the
            # write left it indistinguishable from a wallet with none.
            m = s.get("metrics") or {}
            w.qualified_trades = s["trades_scored"]
            w.winning_trades = s["winning_trades"]
            w.losing_trades = s["losing_trades"]
            w.win_rate = m.get("win_rate")
            w.profit_factor = m.get("profit_factor")
            w.average_trade_size = m.get("average_size_usd")
            w.median_trade_size = m.get("median_size_usd")
            w.largest_trade = m.get("largest_size_usd")
            w.unpriced_trades = rec.get("unpriced")
            w.sample_count = s["trades_scored"]
            w.required_sample_count = MIN_TRADES_FOR_SCORE
            w.last_analysis_at = now
            w.analysis_error = None

            if not s["measurable"]:
                # INSUFFICIENT — distinct from zero and from failure. The
                # counts above are already persisted and true, so the
                # diagnostic reads "3 of 15" rather than "0 of 15", and the
                # scores stay NULL because the sample does not support them.
                w.measurable = False
                w.analysis_status = ("INSUFFICIENT" if s["trades_scored"]
                                     else "NO_VERIFIED_TRADES")
                w.measurability_reason = (
                    "INSUFFICIENT_QUALIFIED_TRADES" if s["trades_scored"]
                    else "NO_VERIFIED_TRADES")
                stats["not_measurable"] += 1
                continue

            w.measurable = True
            w.analysis_status = "MEASURED"
            w.measurability_reason = None
            w.smart_money_score = s["smart_money_score"]
            # alpha_score stays NULL until W5 measures post-entry horizons.
            # Writing the realized return here is what made the missing
            # capability invisible for so long.
            w.alpha_score = s["alpha_score"]
            w.legacy_alpha_score = s["legacy_alpha_score"]
            w.copy_score = s["copy_score"]
            w.confidence_score = s["confidence_score"]
            w.wallet_score_version = SCORE_VERSION
            if s.get("whale_score") is not None:
                w.whale_score = s["whale_score"]
            # SCORING DECIDES NOTHING. This used to write `w.status =
            # "WATCH"` inline, which made promotion a side effect of
            # measurement and was the ONLY lifecycle write in the codebase —
            # so SMART_MONEY and HIGH_CONVICTION were unreachable states.
            # lib/wallet_lifecycle owns transitions now, reading these
            # scores plus the observation evidence.
            stats["scored"] += 1
        return stats

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
