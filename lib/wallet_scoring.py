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


def reconstruct_ledger_trades(rows) -> dict:
    """Build closed FIFO round trips from the canonical wallet ledger.

    ``wallet_trades`` is produced from full transaction balance deltas.  It
    is stronger evidence than pairing a shallow page of transfer records and
    it survives restarts and bounded deep-history backfills.  A ledger row
    with unknown USD value remains unpriced; it never becomes a zero-dollar
    fill.
    """
    from datetime import datetime

    def row_ts(row) -> float:
        value = getattr(row, "opened_at", None)
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0

    opens: dict[str, list[dict]] = {}
    trades: list[dict] = []
    unpriced = 0
    unpriced_reasons: set[str] = set()

    for row in sorted(rows or [], key=row_ts):
        mint = getattr(row, "mint", None)
        direction = str(getattr(row, "direction", "") or "").lower()
        qty = _f(getattr(row, "quantity", None))
        value_usd = getattr(row, "value_usd", None)
        ts = row_ts(row)
        if not mint or direction not in {"buy", "sell"} or qty <= 0:
            continue
        if value_usd is None or _f(value_usd) <= 0:
            unpriced += 1
            unpriced_reasons.add("ledger row has no authoritative USD value")
            continue
        value_usd = _f(value_usd)

        if direction == "buy":
            opens.setdefault(mint, []).append({
                "qty": qty,
                "cost_usd": value_usd,
                "ts": ts,
                "quote_symbol": getattr(row, "quote_mint", None),
                "price_quality": getattr(row, "price_quality", None),
            })
            continue

        lots = opens.get(mint) or []
        if not lots:
            # A sell whose acquisition predates the bounded history is not a
            # closed round trip.  Deep backfill may supply the missing buy.
            unpriced += 1
            unpriced_reasons.add("sell has no earlier priced buy in ledger")
            continue

        remaining = qty
        matched_qty = 0.0
        cost_basis_usd = 0.0
        opened_ts = None
        qualities: set[str] = set()
        while remaining > 1e-12 and lots:
            lot = lots[0]
            before_qty = lot["qty"]
            take = min(remaining, before_qty)
            fraction = take / before_qty if before_qty else 0.0
            cost_take = lot["cost_usd"] * fraction
            cost_basis_usd += cost_take
            matched_qty += take
            opened_ts = opened_ts or lot["ts"]
            if lot.get("price_quality"):
                qualities.add(lot["price_quality"])
            lot["qty"] -= take
            lot["cost_usd"] -= cost_take
            remaining -= take
            if lot["qty"] <= 1e-12:
                lots.pop(0)

        if matched_qty <= 0 or cost_basis_usd <= 0:
            unpriced += 1
            continue
        # If only part of the sale could be matched, only the corresponding
        # fraction of proceeds belongs to the proven round trip.
        proceeds_usd = value_usd * (matched_qty / qty)
        pnl_usd = proceeds_usd - cost_basis_usd
        exit_quality = getattr(row, "price_quality", None)
        if exit_quality:
            qualities.add(exit_quality)
        quality = ESTIMATED if ESTIMATED in qualities else MEASURED
        trades.append({
            "mint": mint,
            "symbol": getattr(row, "token_symbol", None),
            "cost_basis_usd": round(cost_basis_usd, 6),
            "proceeds_usd": round(proceeds_usd, 6),
            "pnl_usd": round(pnl_usd, 6),
            "notional_usd": round(cost_basis_usd, 6),
            "return_pct": round(pnl_usd / cost_basis_usd * 100.0, 4),
            "quote_symbol": getattr(row, "quote_mint", None),
            "quote_amount": getattr(row, "quote_amount", None),
            "quote_price_usd": getattr(row, "quote_price_usd", None),
            "entry_quote_symbols": [],
            "price_source": getattr(row, "price_source", None),
            "price_quality": quality,
            "opened_ts": opened_ts,
            "closed_ts": ts,
            "hold_seconds": (ts - opened_ts) if opened_ts else None,
        })

    return {
        "trades": trades,
        "closed": len(trades),
        "still_open": sum(len(v) for v in opens.values()),
        "unpriced_legs": unpriced,
        "unpriced_reasons": sorted(unpriced_reasons)[:5],
        "source": "WALLET_TRADE_BALANCE_DELTA_LEDGER",
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


def _score_one(w, *, session, transfers_fn, stats) -> str:
    """Measure ONE wallet from its own transfer history. Returns the outcome.

    Extracted verbatim from `score_registry_wallets` so that the targeted
    pass and the sweep share ONE implementation. A second scoring function
    would be a second wallet score, and the whole point of a score is that
    everyone means the same thing by it.
    """
    from app.database import now_iso

    stats["attempted"] += 1
    now = now_iso()
    # Prefer the durable balance-delta ledger populated by
    # wallet_swaps.sync_wallet_history.  The old path scored only the newest
    # 100 transfer legs, which cannot prove a round trip when the buy falls
    # on an older page.
    from app.database import WalletTrade
    ledger_rows = (session.query(WalletTrade)
                   .filter(WalletTrade.address == w.address,
                           WalletTrade.ledger_version ==
                           "swap_v1_balance_delta")
                   .order_by(WalletTrade.opened_at.asc()).all())
    raw = None
    try:
        if not ledger_rows:
            raw = transfers_fn(w.address, limit=100)
    except Exception as e:
        # PROVIDER FAILURE — NOT a measurement of zero. Every count already
        # on this row is LEFT INTACT: overwriting a known qualified_trades=12
        # with 0 because Helius timed out destroys evidence and calls the
        # result a measurement. Only the run's own outcome is recorded, and
        # last_score_update is deliberately not touched so the existing
        # score keeps its real age.
        logger.debug(f"[WalletScoring] {w.address[:8]}...: {e}")
        w.analysis_status = "FAILED"
        w.analysis_error = f"{type(e).__name__}: {str(e)[:160]}"
        w.last_analysis_at = now
        stats["errors"] += 1
        return "FAILED"

    legs = raw if isinstance(raw, list) else (
        (raw or {}).get("transfers") or (raw or {}).get("data") or [])
    if ledger_rows:
        legs = ledger_rows
    if not legs:
        # A SUCCESSFUL read that found nothing. This IS a measurement — of
        # zero — and is a different fact from the failure above.
        w.analysis_status = "NO_VERIFIED_TRADES"
        w.measurability_reason = "NO_TRANSFER_HISTORY"
        w.measurable = False
        w.sample_count = 0
        w.required_sample_count = MIN_TRADES_FOR_SCORE
        w.last_analysis_at = now
        w.analysis_error = None
        stats["no_transfers"] += 1
        return "NO_VERIFIED_TRADES"

    rec = (reconstruct_ledger_trades(legs) if ledger_rows
           else reconstruct_trades(legs))
    s = score_wallet(rec)
    w.last_score_update = now

    # THE STATISTICS LIFECYCLE READS MUST BE THE STATISTICS SCORING WRITES.
    # Written BEFORE the measurability gate on purpose: a wallet with 3 round
    # trips is not measurable, but "3 of 15" is the true and useful answer to
    # "why is this not smart money?" — skipping the write left it
    # indistinguishable from a wallet with none.
    m = s.get("metrics") or {}
    w.qualified_trades = s["trades_scored"]
    w.winning_trades = s["winning_trades"]
    w.losing_trades = s["losing_trades"]
    w.win_rate = m.get("win_rate")
    w.profit_factor = m.get("profit_factor")
    w.average_trade_size = m.get("average_size_usd")
    w.median_trade_size = m.get("median_size_usd")
    w.largest_trade = m.get("largest_size_usd")
    w.unpriced_trades = rec.get("unpriced_legs")
    w.sample_count = s["trades_scored"]
    w.required_sample_count = MIN_TRADES_FOR_SCORE
    w.last_analysis_at = now
    w.analysis_error = None

    if not s["measurable"]:
        # INSUFFICIENT — distinct from zero and from failure. The counts
        # above are already persisted and true, so the diagnostic reads
        # "3 of 15" rather than "0 of 15", and the scores stay NULL because
        # the sample does not support them.
        w.measurable = False
        w.analysis_status = ("INSUFFICIENT" if s["trades_scored"]
                             else "NO_VERIFIED_TRADES")
        w.measurability_reason = (
            "INSUFFICIENT_QUALIFIED_TRADES" if s["trades_scored"]
            else "NO_VERIFIED_TRADES")
        stats["not_measurable"] += 1
        return w.analysis_status

    w.measurable = True
    w.analysis_status = "MEASURED"
    w.measurability_reason = None
    w.smart_money_score = s["smart_money_score"]
    # alpha_score stays NULL until W5 measures post-entry horizons. Writing
    # the realized return here is what made the missing capability invisible
    # for so long.
    w.alpha_score = s["alpha_score"]
    w.legacy_alpha_score = s["legacy_alpha_score"]
    w.copy_score = s["copy_score"]
    w.confidence_score = s["confidence_score"]
    w.wallet_score_version = SCORE_VERSION
    if s.get("whale_score") is not None:
        w.whale_score = s["whale_score"]
    # SCORING DECIDES NOTHING. lib/wallet_lifecycle owns transitions,
    # reading these scores plus the observation evidence.
    stats["scored"] += 1
    return "MEASURED"


def _empty_stats() -> dict:
    return {"attempted": 0, "scored": 0, "not_measurable": 0,
            "no_transfers": 0, "errors": 0}


#: WHAT THE SCORE IS LEARNED FROM, and why that is not circular.
#
# There are TWO populations here and only one of them is used to score a
# wallet:
#
#   A. THE WALLET'S OWN economic events and what happened next. That is
#      what `reconstruct_trades` builds — round trips from the wallet's own
#      transfer history, priced from its own execution — and it needs no
#      JARVIS thesis to exist. This is what a wallet score measures.
#
#   B. JARVIS SHADOW THESES derived from that wallet. A separate
#      population, measured separately in `wallet_shadow_outcomes`, and
#      never fed back into the wallet score.
#
# The circular failure — a wallet needs a score to produce a thesis, a
# thesis needs an outcome to score the wallet — only appears if B is used
# to compute A. It is not. A wallet becomes measurable purely from its own
# history, which is why scoring can bootstrap from a standing start with
# zero theses in the table.
SCORE_BOOTSTRAP_POPULATION = "OBSERVED_WALLET_ECONOMIC_EVENTS"


def coverage(db=None) -> dict:
    """How much of the registry carries a usable score, and why not.

    An UNSCORED wallet is not a low-scoring one, and the difference is the
    whole reason a thesis can be refused for UNKNOWN_WALLET_QUALITY. The
    breakdown below is what makes that refusal legible instead of blank.
    """
    from sqlalchemy import text

    from app.database import engine

    out = {"registry_wallets": 0, "watched_wallets": 0, "scored": 0,
           "insufficient_evidence": 0, "with_resolved_samples": 0,
           "never_analysed": 0, "failed": 0,
           "coverage_pct": None, "score_version": SCORE_VERSION,
           "min_trades_for_score": MIN_TRADES_FOR_SCORE,
           "bootstrap_population": SCORE_BOOTSTRAP_POPULATION,
           "last_scoring_update": None, "by_analysis_status": {},
           "by_measurability_reason": {}, "state": "MEASURED"}
    try:
        with engine.connect() as conn:
            out["registry_wallets"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_registry")).scalar() or 0
            out["watched_wallets"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_registry WHERE pinned=1")
            ).scalar() or 0
            out["scored"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_registry WHERE measurable=1 "
                "AND (smart_money_score IS NOT NULL "
                "     OR alpha_score IS NOT NULL)")).scalar() or 0
            out["with_resolved_samples"] = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_registry "
                "WHERE COALESCE(sample_count,0) > 0")).scalar() or 0
            out["last_scoring_update"] = conn.execute(text(
                "SELECT MAX(last_score_update) FROM wallet_registry"
            )).scalar()
            for st, n in conn.execute(text(
                    "SELECT COALESCE(analysis_status,'NEVER_ANALYSED'), "
                    "COUNT(*) FROM wallet_registry GROUP BY 1 ORDER BY 2 DESC"
            )).fetchall():
                out["by_analysis_status"][st] = n
            for rs, n in conn.execute(text(
                    "SELECT measurability_reason, COUNT(*) FROM "
                    "wallet_registry WHERE measurability_reason IS NOT NULL "
                    "GROUP BY 1 ORDER BY 2 DESC")).fetchall():
                out["by_measurability_reason"][rs] = n
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[WalletScoring] coverage unavailable: %s", e)
        out["state"] = "UNAVAILABLE"
        out["detail"] = str(e)[:200]
        return out

    st = out["by_analysis_status"]
    out["never_analysed"] = st.get("NEVER_ANALYSED", 0)
    out["failed"] = st.get("FAILED", 0)
    out["insufficient_evidence"] = (st.get("INSUFFICIENT", 0)
                                    + st.get("NO_VERIFIED_TRADES", 0))
    if out["registry_wallets"]:
        out["coverage_pct"] = round(
            100.0 * out["scored"] / out["registry_wallets"], 2)
    return out


def score_wallets(addresses, *, db=None, limit: int = 50,
                  transfers_fn=None) -> dict:
    """Rescore NAMED wallets. Bounded, and the only ones that changed.

    The sweep below selects a population by status; this selects by
    address, so a cycle can rescore exactly the wallets whose evidence
    moved instead of walking 1,086 registry rows every fifteen minutes.
    Same model, same thresholds, same version — see `_score_one`.
    """
    from app.database import WalletRegistry, get_db

    wanted = [a for a in dict.fromkeys(addresses or []) if a]
    stats = _empty_stats()
    stats["requested"] = len(wanted)
    stats["skipped_not_in_registry"] = 0
    stats["population"] = SCORE_BOOTSTRAP_POPULATION
    stats["score_version"] = SCORE_VERSION
    if not wanted:
        return stats

    cap = max(1, min(int(limit), 200))
    wanted = wanted[:cap]

    if transfers_fn is None:
        from lib.helius_client import transfers as transfers_fn

    def _run(session):
        rows = (session.query(WalletRegistry)
                .filter(WalletRegistry.address.in_(wanted))
                .filter(~WalletRegistry.status.in_(
                    ("EXCLUDED_ENTITY", "ARCHIVED")))
                .all())
        found = {r.address for r in rows}
        stats["skipped_not_in_registry"] = len(
            [a for a in wanted if a not in found])
        for w in rows:
            _score_one(w, session=session, transfers_fn=transfers_fn,
                       stats=stats)
        return stats

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


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
        stats = _empty_stats()
        stats["population"] = SCORE_BOOTSTRAP_POPULATION
        stats["score_version"] = SCORE_VERSION
        for w in rows:
            _score_one(w, session=session, transfers_fn=transfers,
                       stats=stats)
        return stats

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
