"""Where the edge is, where the cost eats it, and which of the two is the fault.

`lib/expectancy.py` measures gross edge per bucket. `lib/transaction_costs.py`
prices a round trip. Nothing put them in the same table, so the desk could
see that a bucket lost money and could not see WHY — and the two whys call
for opposite responses:

    gross 0.42R − cost 0.09R = +0.33R   the setup works; trade it
    gross 0.42R − cost 0.50R = −0.08R   the setup works; the VENUE eats it
    gross 0.02R − cost 0.09R = −0.07R   the setup does not work

Retiring a strategy for the second reason throws away a working thesis
because of a routing decision. That is the mistake this matrix exists to
make visible, one cell at a time, across strategy × product × timeframe
× venue.

TWO ARMS, TWO COST PROVENANCES, NEVER POOLED SILENTLY.

    CEX   costs are ESTIMATED — lib.transaction_costs priced against the
          entry and the stop as PLACED. The book records fills, not the
          spread it crossed.
    DEX   costs are REALIZED — dex_trades itemises pool fee, price impact
          and network fee per closed swap, because on-chain they are
          observable.

An estimate and a measurement are different claims about the same word.
Averaging them into one "cost" column would make the DEX arm look as
uncertain as the CEX arm, or the CEX arm look as solid as the DEX one, and
the reader could not tell which had happened. Every row carries its basis.

PRODUCT IS RESOLVED, NOT RENAMED. `asset_class` on an outcome row is
"equity" or "crypto"; the product axis asks a question those cannot answer
(a spot BTC long and a BTC perpetual are different economics), so the
product comes from lib.instruments — the same authority the sizer uses.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Cost provenance. The distinction is load-bearing; see the module docstring.
COST_ESTIMATED = "ESTIMATED"
COST_REALIZED = "REALIZED"

# A cell below this has not earned a verdict. Deliberately the same bar
# lib.venue_expectancy uses, so the matrix and the venue comparison cannot
# disagree about what counts as evidence.
MIN_CELL_SAMPLE = 8

# Net R a cell must clear to be called tradeable — also shared, same reason.
MIN_NET_R = 0.05

# Why a cell fails. The whole point of the matrix.
LIMIT_EVIDENCE = "EVIDENCE"     # too few closed trades to say anything
LIMIT_EDGE = "EDGE"             # gross edge is absent; cost is not the issue
LIMIT_COST = "COST"             # real gross edge, consumed by the round trip
LIMIT_NONE = "NONE"             # clears

# An outcome worse than -3R means the stop gapped or was never honoured.
# Clipped rather than dropped, exactly as lib.expectancy does it: the loss
# was real, and one -40R fill would otherwise make the cell describe a
# single bad print instead of the strategy.
R_FLOOR, R_CEILING = -3.0, 10.0


def _f(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _r_of(entry, stop, exit_price, direction) -> float | None:
    entry, stop, exit_price = _f(entry), _f(stop), _f(exit_price)
    if entry is None or stop is None or exit_price is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    short = str(direction or "").lower().startswith("short")
    move = (entry - exit_price) if short else (exit_price - entry)
    return max(R_FLOOR, min(R_CEILING, move / risk))


class _Memo:
    """Per-build caches for the two expensive lookups.

    `estimate_costs` can reach the live Kraken book, a cached REST spread
    measurement and the funding-rate table — once per call. Over several
    thousand closed trades that is thousands of round trips to price a
    handful of distinct instruments.

    The saving is exact rather than approximate: total cost as a FRACTION
    OF NOTIONAL does not depend on the stop at all, so it is computed once
    per (symbol, side, hold) and converted per trade by

        cost_r = total_pct * entry / |entry - stop|

    which is the same arithmetic estimate_costs performs internally. The
    one exception is futures, whose fee is a flat charge per contract and
    therefore does depend on entry — so entry joins the key there.
    """

    def __init__(self):
        self.cost: dict = {}
        self.product: dict = {}

    def product_of(self, symbol: str) -> tuple[str, str]:
        """(product, asset_class) from the one instrument authority."""
        key = str(symbol or "").upper()
        if key not in self.product:
            try:
                from lib.instruments import resolve
                ident = resolve(symbol)
                self.product[key] = (ident.product, ident.asset_class)
            except Exception:
                self.product[key] = ("UNKNOWN", "UNKNOWN")
        return self.product[key]

    def cost_pct(self, symbol: str, *, is_short: bool, hold_hours: float,
                 entry: float) -> tuple[float | None, str]:
        """(round-trip cost as a fraction of notional, source label)."""
        from lib.instruments import is_futures
        from lib.transaction_costs import estimate_costs

        # Hold is bucketed to the hour: funding and borrow accrue over it,
        # and pricing 61 minutes separately from 60 would defeat the cache
        # for no measurable difference.
        hold_key = round(max(0.0, float(hold_hours or 0.0)))
        try:
            futures = is_futures(symbol)
        except Exception:
            futures = False
        key = (str(symbol or "").upper(), bool(is_short), hold_key,
               round(float(entry), 4) if futures else None)
        if key in self.cost:
            return self.cost[key]

        try:
            # Reference levels only. total_pct is independent of them for
            # everything except the futures fee, which is why entry is in
            # the key above and is passed through here.
            ref_entry = float(entry) if futures else 1.0
            ref_stop = ref_entry * 0.5
            c = estimate_costs(symbol, ref_entry, ref_stop,
                               hold_hours=float(hold_key), is_short=is_short)
            out = ((_f(c.get("total_pct")), "estimated")
                   if c.get("ok") else (None, str(c.get("reason") or "unpriceable")))
        except Exception as e:                                # pragma: no cover
            logger.debug(f"[EdgeCost] cost estimate failed for {symbol}: {e}")
            out = (None, f"cost estimate failed: {e}")
        self.cost[key] = out
        return out


def _cex_rows(days: int, memo: _Memo) -> list[dict]:
    """Closed CEX outcomes, in R, with an ESTIMATED round-trip cost each.

    The stop used is the one AS PLACED, never the live stop: the manager
    trails, so by close the live stop often sits at breakeven and anything
    dividing by |entry − stop| divides by pennies. A ^VIX short that lost
    $19.29 reported R = −1,063,462 that way.
    """
    from app.database import PaperPosition, TradeOutcome, TradingSignal, get_db
    from lib.calibration import CURRENT_EPOCH
    from lib.trade_horizon import expected_hold_minutes

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    with get_db() as db:
        placed = {}
        try:
            placed = {sid: float(stop) for sid, stop in db.query(
                PaperPosition.signal_id, PaperPosition.initial_stop_loss).filter(
                PaperPosition.signal_id.isnot(None),
                PaperPosition.initial_stop_loss.isnot(None)).all() if stop}
        except Exception as e:                                # pragma: no cover
            logger.debug(f"[EdgeCost] placed stops unavailable: {e}")

        q = (db.query(TradeOutcome.symbol, TradeOutcome.timeframe,
                      TradeOutcome.direction, TradeOutcome.asset_class,
                      TradeOutcome.entry_price, TradeOutcome.exit_price,
                      TradeOutcome.hold_duration_m, TradeOutcome.outcome_source,
                      TradeOutcome.signal_id, TradeOutcome.exited_at,
                      TradingSignal.stop_loss, TradingSignal.strategy)
             .outerjoin(TradingSignal, TradingSignal.id == TradeOutcome.signal_id)
             .filter(TradeOutcome.engine_epoch == CURRENT_EPOCH)
             .filter(TradeOutcome.exited_at >= cutoff))
        raw = q.all()

    for (sym, tf, direction, klass, entry, exit_price, hold_m, src,
         sig_id, exited_at, sig_stop, strategy) in raw:
        stop = placed.get(sig_id) or sig_stop
        r = _r_of(entry, stop, exit_price, direction)
        if r is None:
            continue
        is_short = str(direction or "").lower().startswith("short")
        if hold_m:
            hold_hours = float(hold_m) / 60.0
        else:
            lo, hi = expected_hold_minutes(tf)
            hold_hours = (lo + hi) / 2.0 / 60.0
        pct, _src = memo.cost_pct(sym, is_short=is_short,
                                  hold_hours=hold_hours, entry=float(entry))
        distance = abs(float(entry) - float(stop))
        cost_r = (pct * float(entry) / distance) if (pct is not None and distance > 0) else None
        product, resolved_class = memo.product_of(sym)
        rows.append({
            "venue_type": "CEX",
            "symbol": sym,
            "strategy": strategy or "unclassified",
            "asset_class": (klass or resolved_class or "unknown").lower(),
            "product": product,
            "timeframe": tf or "unknown",
            "direction": "short" if is_short else "long",
            "gross_r": r,
            "cost_r": cost_r,
            "cost_basis": COST_ESTIMATED,
            "outcome_source": src or "live",
            "closed_at": exited_at,
        })
    return rows


def _dex_rows(days: int, memo: _Memo) -> list[dict]:
    """Closed DEX swaps, in R, with REALIZED itemised costs.

    On-chain the costs are observable — pool fee, price impact and network
    fee are all on the row — so this arm does not estimate anything. A swap
    with no stop on file has no risk denominator and is skipped rather than
    given a substitute one; inventing a stop would manufacture an R.
    """
    from app.database import DexPosition, DexTrade, get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    try:
        with get_db() as db:
            # Read into plain tuples INSIDE the session. ORM instances go
            # detached the moment the context manager exits, and touching
            # an unloaded attribute afterwards raises rather than
            # returning None — which would have looked like an empty book.
            trades = [{
                "position_id": t.position_id, "symbol": t.symbol,
                "mint": t.mint, "entry_price_usd": t.entry_price_usd,
                "qty_tokens": t.qty_tokens, "gross_pnl_usd": t.gross_pnl_usd,
                "total_costs_usd": t.total_costs_usd, "closed_at": t.closed_at,
            } for t in db.query(DexTrade).filter(DexTrade.closed_at >= cutoff).all()]
            pos_ids = [t["position_id"] for t in trades if t["position_id"]]
            stops: dict = {}
            if pos_ids:
                stops = {p_id: stop for p_id, stop in db.query(
                    DexPosition.id, DexPosition.stop_price_usd).filter(
                    DexPosition.id.in_(pos_ids)).all()}
    except Exception as e:                                    # pragma: no cover
        logger.debug(f"[EdgeCost] DEX book unavailable: {e}")
        return rows

    for t in trades:
        entry = _f(t["entry_price_usd"])
        qty = _f(t["qty_tokens"])
        stop = _f(stops.get(t["position_id"]))
        if entry is None or stop is None or qty is None:
            continue
        risk_usd = abs(entry - stop) * qty
        if risk_usd <= 0:
            continue
        gross = _f(t["gross_pnl_usd"])
        costs = _f(t["total_costs_usd"])
        if gross is None:
            continue
        product, _klass = memo.product_of(t["symbol"] or "")
        rows.append({
            "venue_type": "DEX",
            "symbol": t["symbol"] or t["mint"],
            # DEX swaps carry no named strategy today. "unclassified" is
            # the truthful label; borrowing the CEX strategy of the same
            # symbol would attribute an on-chain result to a setup that
            # never ran there.
            "strategy": "unclassified",
            "asset_class": "crypto",
            "product": product if product != "UNKNOWN" else "DEX_SPOT",
            "timeframe": "unknown",
            "direction": "long",
            "gross_r": max(R_FLOOR, min(R_CEILING, gross / risk_usd)),
            "cost_r": (abs(costs) / risk_usd) if costs is not None else None,
            "cost_basis": COST_REALIZED,
            "outcome_source": "live",
            "closed_at": t["closed_at"],
        })
    return rows


def _cell(rows: list[dict]) -> dict:
    """One (strategy, product, timeframe, venue) cell, and why it fails."""
    from lib import learning_population as LP

    # Replayed fills assumed perfect execution and that both a bar's high
    # and low were reachable, so they are systematically optimistic and are
    # weighted below live evidence rather than pooled with it. Admission is
    # an allowlist (lib/learning_population): an operator-executed trade
    # carries THAT ACCOUNT'S costs, including any promotion, and must not
    # price JARVIS's routing decisions.
    admitted = [(r, LP.weight(r["outcome_source"],
                              profile=LP.JARVIS_EXECUTION)) for r in rows]
    admitted = [(r, w) for r, w in admitted if w is not None]
    rows = [r for r, _ in admitted]
    weights = [w for _, w in admitted]
    total_w = sum(weights)
    gross = (sum(r["gross_r"] * w for r, w in zip(rows, weights)) / total_w
             if total_w else None)

    priced = sorted(r["cost_r"] for r in rows if r["cost_r"] is not None)
    cost_median = statistics.median(priced) if priced else None
    cost_p90 = priced[min(len(priced) - 1, int(len(priced) * 0.9))] if priced else None
    net = (gross - cost_median) if (gross is not None and cost_median is not None) else None

    n = len(rows)
    if n < MIN_CELL_SAMPLE:
        limiting, verdict = LIMIT_EVIDENCE, "UNKNOWN"
    elif net is None:
        limiting, verdict = LIMIT_EVIDENCE, "UNKNOWN"
    elif net >= MIN_NET_R:
        limiting, verdict = LIMIT_NONE, "TRADEABLE"
    elif gross is not None and gross < MIN_NET_R:
        # No gross edge to protect. Cheaper routing would not save this.
        limiting, verdict = LIMIT_EDGE, "UNTRADEABLE"
    else:
        # Real gross edge, consumed by the round trip. THIS is the cell a
        # blended P&L would have retired as a bad setup.
        limiting, verdict = LIMIT_COST, "UNTRADEABLE"

    bases = {r["cost_basis"] for r in rows if r["cost_r"] is not None}
    # LIVE means live, not "not a replay": `rows` is already restricted to
    # the admitted populations, so this counts the reference population
    # itself rather than everything that failed to be a replay.
    n_live = sum(1 for r in rows if r["outcome_source"] != LP.REPLAY)
    # 7,740 samples reads as overwhelming evidence. 7,740 REPLAYED samples
    # and zero live fills is a different claim, and the replay weighting
    # cannot express it — in a cell where every row is a replay the weight
    # divides out of both sides and vanishes. So it is said in words.
    evidence = ("LIVE" if n_live == n else
                "REPLAY_ONLY" if n_live == 0 else "MIXED")
    return {
        "n": n,
        "n_live": n_live,
        "n_replay": n - n_live,
        "evidence": evidence,
        "n_priced": len(priced),
        "gross_r": round(gross, 4) if gross is not None else None,
        "cost_r_median": round(cost_median, 4) if cost_median is not None else None,
        "cost_r_p90": round(cost_p90, 4) if cost_p90 is not None else None,
        "net_r": round(net, 4) if net is not None else None,
        # How much edge exists per unit of cost. Near 1 means the venue eats
        # the whole trade even when the thesis is right.
        "edge_cost_ratio": (round(gross / cost_median, 3)
                            if (gross is not None and cost_median) else None),
        "verdict": verdict,
        "limiting": limiting,
        "cost_basis": (sorted(bases)[0] if len(bases) == 1
                       else ("MIXED" if bases else None)),
        "unpriced": n - len(priced),
    }


def matrix(*, days: int = 180, top: int = 120) -> dict:
    """The edge–cost matrix across strategy × product × timeframe × venue.

    Returns cells for every combination with at least one closed trade,
    each labelled with WHY it fails when it does. Cells below the sample
    bar are returned too — an unmeasured bucket is a fact about the desk's
    evidence, and dropping it would make the matrix look more complete
    than the data is.
    """
    memo = _Memo()
    rows: list[dict] = []
    errors: list[str] = []
    try:
        rows += _cex_rows(days, memo)
    except Exception as e:
        logger.warning(f"[EdgeCost] CEX arm failed: {e}")
        errors.append(f"CEX arm unavailable: {e}")
    try:
        rows += _dex_rows(days, memo)
    except Exception as e:
        logger.warning(f"[EdgeCost] DEX arm failed: {e}")
        errors.append(f"DEX arm unavailable: {e}")

    grouped: dict[tuple, list[dict]] = {}
    for r in rows:
        grouped.setdefault(
            (r["strategy"], r["product"], r["timeframe"], r["venue_type"]),
            []).append(r)

    cells = [{
        "strategy": s, "product": p, "timeframe": tf, "venue": v,
        **_cell(group),
    } for (s, p, tf, v), group in grouped.items()]
    # Worst-first among cells that have a verdict: a cell whose real edge
    # is being eaten is the most actionable thing on the page, and it would
    # sit at the bottom of an alphabetical list forever.
    cells.sort(key=lambda c: (
        {LIMIT_COST: 0, LIMIT_NONE: 1, LIMIT_EDGE: 2, LIMIT_EVIDENCE: 3}[c["limiting"]],
        -(c["n"] or 0)))

    # The venue comparison this codebase has had the machinery for since
    # Phase 20 and has never been wired to anything: bad signal, or bad
    # venue?
    #
    # It takes TWO arms to answer. With one venue on file, compare_venues
    # still returns a lesson — and its NO_EDGE_ANYWHERE text reads "the
    # cost model is not the problem here", which directly contradicts the
    # COST cells above. That contradiction is an artefact of asking a
    # comparison to judge a single arm, so the answer is marked
    # non-comparable rather than shown as a finding.
    from lib.venue_expectancy import compare_venues
    venues = compare_venues(rows)
    measured_venues = sorted({r["venue_type"] for r in rows})
    venues["comparable"] = len(measured_venues) > 1
    venues["venues_with_outcomes"] = measured_venues
    if not venues["comparable"]:
        venues["not_comparable_reason"] = (
            f"only {measured_venues[0] if measured_venues else 'no'} outcomes "
            f"are on file. 'Bad signal or bad venue' needs both arms — with "
            f"one venue the answer is not a routing finding, and the "
            f"limiting-factor column above is the answer that can be given.")

    by_limit: dict[str, int] = {}
    for c in cells:
        by_limit[c["limiting"]] = by_limit.get(c["limiting"], 0) + 1

    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_cell_sample": MIN_CELL_SAMPLE,
        "min_net_r": MIN_NET_R,
        "rows_considered": len(rows),
        "cells_total": len(cells),
        "cells": cells[:top],
        "truncated": max(0, len(cells) - top),
        "by_limiting_factor": by_limit,
        "venues": venues,
        "axes": {
            "strategy": sorted({c["strategy"] for c in cells}),
            "product": sorted({c["product"] for c in cells}),
            "timeframe": sorted({c["timeframe"] for c in cells}),
            "venue": sorted({c["venue"] for c in cells}),
        },
        "errors": errors,
        "note": (
            "CEX costs are ESTIMATED against the entry and the stop as "
            "placed; DEX costs are REALIZED from itemised on-chain fees. "
            "Every cell states which basis it used, and the two are never "
            "averaged into one column. LIMITING says why a cell fails: "
            "EDGE means the setup does not work and cheaper routing would "
            "not save it; COST means it does work and the round trip eats "
            "it, which is a ROUTING result rather than a verdict on the "
            "setup."
        ),
    }
