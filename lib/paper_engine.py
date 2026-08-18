"""
Paper Trading Engine v2.0
Supports: Long, Long Leveraged, Short, Short Leveraged
Tracks a virtual account with P&L, mark-to-market, and margin simulation.

v2.0 Fixes:
- open_paper_position: direction key normalization is now exhaustive (handles LLM variants)
- open_paper_position: asset_class auto-detected from symbol if not provided
- mark_to_market: improved symbol lookup covers slash/no-slash variants
- mark_to_market: SHORT stop/target logic was inverted (stop ABOVE entry, target BELOW)
  — now correctly closes shorts at stop when price >= stop_loss
- mark_to_market: added missing margin_used fallback to prevent $0 margin positions
- DEFAULT_POSITION_SIZE raised to $3,000 for better trade visibility
"""
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from app.database import get_db, PaperPosition, PaperTrade, PaperPortfolio
from lib.learning_engine import record_trade_outcome as _record_outcome

logger = logging.getLogger(__name__)

PAPER_STARTING_CAPITAL = 100_000.0   # $100k virtual account
MAX_LEVERAGE           = 25.0         # hard cap — matches the operator's target broker (1-25x)
MARGIN_CALL_THRESHOLD  = 0.15         # Liquidate if equity < 15% of margin (lost 85% of capital)
DEFAULT_POSITION_SIZE  = 3_000.0      # legacy fallback when risk sizing is impossible

# ── Margin-first sizing (the trade amount IS the committed capital) ──────
# A $10 trade at 10x controls $100 of exposure but is still a $10 trade:
# $10 leaves the account and $10 is the most that can be lost. An earlier
# risk-first version inverted this — it solved for the notional needed to
# risk 1% at the stop, which produced a $125,000 position on a $100,000
# account. Exposure is now bounded by construction: commit a fixed slice of
# equity, let conviction (2x-20x) decide how far that slice reaches, and
# let the stop govern the loss WITHIN it.
TRADE_MARGIN_PCT       = 1.0    # % of equity COMMITTED per position
# Per-trade sizing alone does not bound a portfolio: 1% each x 86 positions
# committed 99.7% of the account and left $281 of cash, after which every
# new entry failed on funds. These are the PORTFOLIO-level limits that were
# missing — deployed capital and position count, checked before opening.
# The two limits are set to bind at the SAME point: at 1% per trade, 60
# positions is exactly 60% deployed. A count cap that trips first would make
# the deployment cap decorative (and vice versa) — matching them means
# whichever runs out first is the one that genuinely matters, and a
# cash-capped smaller position lets a few more trades through honestly.
MAX_DEPLOYED_PCT       = 60.0   # total margin across open positions, % of equity
MAX_OPEN_POSITIONS     = 60
MAX_MARGIN_PCT_OF_CASH = 15.0   # one position may tie up at most this % of free cash

# A round trip costing this much of notional does not exist on any venue this
# desk trades, so a fee above it means the model is misapplied rather than the
# venue expensive. Kept as a backstop over whatever schedule is selected.
FEE_SANITY_CEILING = 0.05

# Quantity is persisted and authorized at this precision, so every risk
# figure derived from it is meaningful only to within one step. Named
# rather than inlined because the tolerance of the last risk gate has to be
# expressed in the SAME units as the rounding that produced the number.
QTY_DECIMALS = 6
QTY_STEP = 10.0 ** -QTY_DECIMALS


def perp_base_rate() -> tuple[float, str]:
    """Fallback rate for a LEVERAGED position, per side.

    A leveraged crypto position is a perpetual, and perps are a different
    PRODUCT from spot with a different fee schedule — not a variant of it.
    Kraken's measured spot taker is 0.80%/side; the perp taker is
    0.05%/side. Falling back to spot when a perp schedule was missing
    therefore billed 16x the real cost, which is what made a $1,000-margin
    position at 8.9x look like it cost $142 to trade instead of ~$9.

    Overcharging by 16x is not the safe direction. It vetoes sound trades
    and teaches the model that leverage is unaffordable, which is exactly
    the wrong lesson for a desk whose whole plan is leveraged perps.
    """
    from lib.venues import KRAKEN_PERP_BASE_TAKER
    return (KRAKEN_PERP_BASE_TAKER,
            f"kraken perpetual base taker {KRAKEN_PERP_BASE_TAKER * 100:.3g}% "
            f"- no live schedule for this symbol")
DEFAULT_SCORE_FLOOR    = 55.0   # used only when no criteria are configured


def _configured_floor() -> float:
    """The operator's own minimum score, from Ops -> Execution Criteria.

    The leverage curve is anchored to THIS, not a constant: raise the floor
    to 70 and a 70 becomes 1x while 100 still earns the maximum, so the
    ladder always spans the range actually being traded."""
    try:
        from lib.trading_preferences import get_user_preference
        return float(get_user_preference().get("live_min_score") or DEFAULT_SCORE_FLOOR)
    except Exception:
        return DEFAULT_SCORE_FLOOR


def _historical_edge(score: float | None, asset_class: str | None,
                     direction: str | None) -> tuple[float | None, int]:
    """Realized win rate for this score band / class / direction bucket."""
    try:
        from lib.ev_model import compute_ev_buckets, _score_band
        from app.database import get_db, SignalEvaluation
        with get_db() as db:
            rows = [
                {"composite_score": r.composite_score, "asset_class": r.asset_class,
                 "direction": r.direction, "outcome": r.outcome,
                 "entry_price": r.entry_price, "exit_price": getattr(r, "exit_price", None)}
                for r in db.query(SignalEvaluation).limit(4000).all()
            ]
        band = _score_band(score)
        for b in compute_ev_buckets(rows):
            if (b["score_band"] == band
                    and str(b["asset_class"]).lower() == str(asset_class or "").lower()
                    and str(b["direction"]).lower() == str(direction or "").lower()):
                decided = int(b.get("decided") or 0)
                if decided:
                    return (int(b.get("wins") or 0) / decided), decided
                return None, 0
    except Exception as e:
        logger.debug(f"[Paper] Historical edge lookup failed: {e}")
    return None, 0


def _consecutive_losses() -> int:
    """Losing streak on the paper book — the account's own warning signal."""
    try:
        from app.database import get_db, PaperTrade
        with get_db() as db:
            recent = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(20).all()
        streak = 0
        for t in recent:
            if float(t.realized_pnl or 0) < 0:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


# The stop must sit inside this fraction of the liquidation distance. A
# position at leverage L is wiped by a 1/L adverse move; a stop at 100% of
# that distance fills at the same moment the venue force-closes you.
LIQ_STOP_BUFFER = 0.80


def max_safe_leverage(entry: float, stop: float, symbol: str = "",
                      requested: float | None = None,
                      notional_hint: float = 10_000.0) -> dict:
    """Leverage DERIVED from the stop and the venue — never the reverse.

    The old order was backwards (P0.6): the composite score chose leverage
    (2-20x by conviction), and the stop was then TIGHTENED to fit inside
    that leverage's liquidation distance — the score literally moved the
    risk decision. Here the stop is an input that never moves:

        liq cap    = LIQ_STOP_BUFFER / stop_fraction
        venue cap  = what the real venue permits at this size
        hard cap   = MAX_LEVERAGE
        cap        = min(all three)
        selected   = min(requested, cap)   when the direction carried an
                                            explicit instruction (Long_10x)
                     cap                    otherwise — the caller sizes
                                            qty from risk and derives the
                                            margin actually needed

    0 risk distance yields 1x, and callers reject such setups upstream.
    """
    try:
        entry, stop = float(entry or 0), float(stop or 0)
    except (TypeError, ValueError):
        return {"leverage": 1.0, "cap": 1.0, "why": "non-numeric levels"}
    if entry <= 0 or stop <= 0 or abs(entry - stop) <= 0:
        return {"leverage": 1.0, "cap": 1.0, "why": "no risk distance"}
    stop_frac = abs(entry - stop) / entry
    liq_cap = LIQ_STOP_BUFFER / stop_frac
    venue_cap, venue_why = venue_max_leverage(symbol, notional_hint)
    cap = max(1.0, min(liq_cap, venue_cap, MAX_LEVERAGE))
    if requested and requested > 1.0:
        lev = min(float(requested), cap)
        why = (f"explicit {requested:g}x, capped to {cap:.1f}x "
               f"(liq {liq_cap:.1f}x, venue {venue_cap:g}x)" if lev < requested
               else f"explicit {requested:g}x within the {cap:.1f}x cap")
    else:
        lev, why = cap, (f"cap: min(liq {liq_cap:.1f}x at {stop_frac:.2%} stop, "
                         f"venue {venue_cap:g}x, hard {MAX_LEVERAGE:g}x)")
    return {"leverage": round(lev, 2), "cap": round(cap, 2), "why": why,
            "liq_cap": round(liq_cap, 2), "venue_cap": venue_cap}


def score_leverage(score: float | None, *, asset_class: str | None = None,
                   direction: str | None = None, atr_pct: float | None = None,
                   explain: bool = False):
    """DEPRECATED FOR SIZING (P0.6, invariant #3): the composite score is
    measured inverted against outcomes, so "conviction earns leverage" was
    granting the most leverage to the worst setups. No order path may call
    this; it survives only for UI explain endpoints until they migrate to
    max_safe_leverage. See lib/leverage_policy.py."""
    from lib.leverage_policy import decide
    regime = None
    try:
        from lib.market_regime import get_regime
        regime = get_regime()
    except Exception:
        pass
    win_rate, sample = _historical_edge(score, asset_class, direction)
    result = decide(
        score, _configured_floor(),
        regime=regime, win_rate=win_rate, sample=sample,
        consecutive_losses=_consecutive_losses(), atr_pct=atr_pct,
    )
    return result if explain else result["leverage"]


def venue_round_trip_fee(symbol: str, notional: float, leverage: float = 1.0,
                         entry_price: float = 0.0,
                         product: str | None = None) -> tuple[float, str]:
    """Dollar cost of opening AND closing this position at the real venue.

    Paper trading charged nothing, so every simulated result was optimistic
    by the full fee. Practice that ignores cost teaches the wrong lesson.

    PRODUCT, not leverage, selects the schedule. Spot and perpetuals are
    different products — you trade one or the other — and a perpetual at 1x
    is still a perpetual. Inferring the product from `leverage > 1` meant
    that whenever the conviction ladder bottomed out at 1x, the position was
    billed as a spot trade: 1.6% round trip instead of 0.10%, a 16x
    overcharge on a book whose entire premise is leveraged perps.

    Pass product="spot" or "perp" to state it. When omitted it resolves from
    CRYPTO_PRODUCT, which defaults to perp for this desk.
    """
    fee, why = _round_trip_fee_uncapped(symbol, notional, leverage,
                                        entry_price, product)
    # One backstop over every path. Each schedule below has now been wrong in
    # BOTH directions at least once, so the ceiling is enforced here rather
    # than inside whichever branch happened to fail last.
    #
    # EXCEPT for exact per-contract arithmetic on a rulebook contract size.
    # That is a measurement, and capping a measurement UNDERSTATES cost —
    # the one direction this model must never fail in. A genuinely expensive
    # instrument (SHIB's contract is $4.47 and costs $0.30 to trade, 6.7%)
    # has to report its real cost so the gate at signal construction can
    # refuse the trade on economics rather than be handed a flattering number.
    if "/side all-in" in why and "ESTIMATED" not in why:
        return fee, why
    ceiling = abs(notional) * FEE_SANITY_CEILING
    if fee > ceiling > 0:
        logger.warning(
            f"[Fees] ${fee:,.2f} on ${abs(notional):,.2f} notional for {symbol} "
            f"exceeds {FEE_SANITY_CEILING:.0%} — capping. Basis was: {why}"
        )
        return ceiling, f"CAPPED at {FEE_SANITY_CEILING:.0%} of notional — {why}"
    return fee, why


def _round_trip_fee_uncapped(symbol: str, notional: float, leverage: float,
                             entry_price: float,
                             product: str | None) -> tuple[float, str]:
    # The paper book rehearses a venue that may not be the one live orders
    # go to. Alpaca fills the live account today while the practice target
    # is Kraken, and pricing rehearsal at Alpaca's cheaper fee would teach
    # a trade that does not survive at the real destination.
    import os
    venue = os.getenv("PAPER_VENUE") or os.getenv("DEFAULT_CRYPTO_VENUE") or "kraken"
    try:
        # Route by what the instrument IS. This used to price everything at
        # the Kraken crypto taker rate, so a GOOGL trade was charged 0.8%
        # round trip at a venue that does not list it — a 60-position book
        # was carrying ~$1,200 of fees that no equity broker would bill.
        from lib.transaction_costs import is_crypto_symbol
        from lib.instruments import is_futures
        if not is_crypto_symbol(symbol) and not is_futures(symbol):
            # Equities, at ANY leverage. Leverage on a stock is a MARGIN loan,
            # not a perpetual — there is no perp fee to pay, so pricing a 3.2x
            # ANET position as a crypto perp charged 0.8% for a product that
            # does not exist. What leverage actually costs here is margin
            # INTEREST, which accrues per day held rather than per trade and
            # is charged by the holding-cost model, not here.
            from lib.venues import equity_regulatory_fee
            shares = abs(notional) / entry_price if entry_price else 0.0
            fee, why = equity_regulatory_fee(abs(notional), shares)
            if leverage > 1.0:
                why += f" - {leverage:g}x is a margin loan; interest accrues per day held"
            return fee, why
        # CME products are priced per contract by the exchange, regardless of
        # leverage — an ES trade is never a Kraken crypto taker fill.
        from lib.venues import US_FUTURES_COMMISSION, us_futures_fee
        if symbol in US_FUTURES_COMMISSION:
            fee, why = us_futures_fee(symbol)
            if fee is not None:
                contracts = max(1.0, round(abs(notional) / entry_price)) if entry_price else 1.0
                return fee * contracts, why
        # Perpetual or spot — the PRODUCT decides, not the leverage.
        # These are different products, not variants of one: you trade spot
        # OR you trade perps with leverage. Inferring the product from
        # `leverage > 1` meant that whenever the conviction ladder bottomed
        # out at 1x, the position was billed as a spot trade — 1.6% round
        # trip instead of 0.10%, a 16x overcharge on a perp book.
        prod = (product or os.getenv("CRYPTO_PRODUCT") or "perp").lower()
        if leverage > 1.0 or prod == "perp":
            # A US account trades Bitnomial-listed perpetuals, priced per
            # contract:  cost = contracts x per_contract_all_in_fee x 2 sides.
            #
            # The rate was never wrong. The CONTRACT COUNT was: it came from
            # Kraken's INTERNATIONAL flexible futures, where contractSize 1
            # means one TOKEN, so a $0.089 coin needed 20,157 "contracts" and
            # billed $6,047 to trade $1,800. Bitnomial contracts are sized in
            # units of the underlying (BUI = 0.1 BTC, BUS = 1 BTC), and
            # us_perp_contracts() returns None rather than inventing a size
            # for a symbol Bitnomial does not list.
            # KRAKEN PRO US ONLY. The per-contract schedule, the Bitnomial
            # contract sizes and the NO_TRADE gate are Kraken's US
            # derivatives pricing and describe no other venue — Alpaca
            # charges a percentage on spot, BTCC has its own schedule.
            # Applying a per-contract model elsewhere would repeat, at a
            # different address, the mistake that started all of this.
            from lib.venues import us_perp_venue_applies
            if us_perp_venue_applies(venue):
                from lib.venues import us_perp_fee
                fee, why = us_perp_fee(symbol, abs(notional), entry_price)
                if fee is not None:
                    return fee, why
                # Contract size not on file, so contracts cannot be counted.
                # Everything on the Kraken US exchange IS a US perpetual and
                # is priced per contract; what is missing here is the size,
                # not the eligibility. Price it at the percentage schedule
                # and SAY the basis is estimated, so the gap is visible
                # rather than hidden inside a plausible-looking number.
                from lib.venues import futures_fee_for
                rate, irate_why = futures_fee_for(symbol, maker=False,
                                                  region="international")
                if rate is None:
                    rate, irate_why = perp_base_rate()
                return (abs(notional) * rate * 2.0,
                        f"ESTIMATED — {why}. Using a percentage stand-in "
                        f"({irate_why}) until the contract size is known")
            from lib.venues import futures_fee_for
            rate, why = futures_fee_for(symbol, maker=False,
                                        region="international")
            if rate is None:
                rate, why = perp_base_rate()
            return abs(notional) * rate * 2.0, why
        from lib.venues import fee_for
        rate, why = fee_for(venue, maker=False, asset_class="crypto")
        return abs(notional) * rate * 2.0, why
    except Exception as e:
        # A failed lookup must never make the trade FREE. This returned 0.0,
        # which is the most dangerous possible answer: the one path where the
        # cost model breaks is the one path that reports no cost at all.
        from lib.venues import VENUE_FEES
        rate = VENUE_FEES["alpaca"]["crypto"]["taker"][0][1]
        return (abs(notional) * rate * 2.0,
                f"fee lookup failed ({e}) — charging the retail taker rate "
                f"{rate * 100:.2g}% both sides as a conservative stand-in")


def venue_max_leverage(symbol: str, notional: float) -> tuple[float, str]:
    """The most leverage the real venue would permit for this position.

    Practising at 20x on a pair the venue caps at 5x is rehearsing a trade
    that cannot be placed. Kraken publishes both: spot margin limits per
    pair, and tiered futures margin where the cap FALLS as size grows.
    """
    try:
        from lib.venues import kraken_pair_specs, max_leverage_at_size
        futures_lev, why = max_leverage_at_size(symbol, notional)
        if futures_lev > 1.0:
            return futures_lev, why
        spec = kraken_pair_specs(symbol)
        if spec and spec.get("max_leverage", 0) > 1:
            return float(spec["max_leverage"]), f"kraken spot margin caps {symbol} at {spec['max_leverage']}x"
    except Exception:
        pass
    return float("inf"), "no venue leverage limit known"


def size_position(equity: float, entry: float, stop: float, leverage: float,
                  free_cash: float, margin_override: float = 0.0,
                  symbol: str = "", notional_cap_usd: float | None = None,
                  execution_instrument=None) -> dict:
    """Margin-first sizing.

        margin   = equity * TRADE_MARGIN_PCT   (or an explicit override)
        notional = margin * leverage
        qty      = notional / entry

    The returned loss_at_stop says what a stop-out actually costs out of
    that committed margin — the number that matters once leverage is high.
    """
    if entry <= 0 or equity <= 0:
        return {"ok": False, "reason": "cannot size: missing entry or equity"}

    margin = margin_override if margin_override > 0 else equity * (TRADE_MARGIN_PCT / 100.0)
    cap = free_cash * (MAX_MARGIN_PCT_OF_CASH / 100.0)
    capped = False
    if cap > 0 and margin > cap:
        margin = cap
        capped = True
    if margin <= 0:
        return {"ok": False, "reason": "no free cash to commit"}

    from lib.instruments import (UnexecutableQuantity, get_spec, is_futures,
                                 whole_contracts, margin_required,
                                 normalize_quantity_down, suggest_micro)
    # An exact execution instrument is the authority for what one unit IS.
    # Falling back to get_spec(symbol) here would answer "1.0 coin" for a
    # contract worth 0.01 BTC, and the wrapper would then undo the correct
    # answer solve_position had just produced.
    exact = execution_instrument is not None
    if exact:
        mult = float(execution_instrument.multiplier or 1.0)
    else:
        spec = get_spec(symbol) if symbol else None
        mult = float(spec.multiplier if spec else 1.0)
    unit_value = entry * mult

    # ── Risk-first for EVERY asset class (P0.8 / Phase 1 §5) ─────────────
    # The ARITHMETIC lives in lib/risk_engine.solve_position — the single
    # authority live, paper, and Auto Sim all call, so no book can drift
    # back into margin-first sizing alone. This wrapper supplies paper's
    # POLICY: the equity slice as risk budget, the free-cash financing
    # fraction, and the venue fee model.
    stop_distance = abs(entry - stop) if stop > 0 else 0.0
    if margin_override > 0 and not ((not exact) and symbol and is_futures(symbol)):
        # EXPLICIT operator margin ("commit $435 at 9.6x") is a deliberate
        # instruction, like Long_10x — the manual-trade path keeps
        # margin-first semantics. The venue cap still applies, and the
        # liquidation-safe cap still bounds leverage against the stop.
        safe = max_safe_leverage(entry, stop, symbol,
                                 requested=leverage if leverage > 1.0 else None,
                                 notional_hint=margin * max(1.0, leverage))
        # The operator's leverage passes through, capped by safety — never
        # inflated: a manual 1x stays 1x.
        leverage = max(1.0, min(float(leverage or 1.0), safe["cap"]))
        notional = margin * max(1.0, leverage)
        # The concentration cap binds the manual path too. An explicit
        # "commit $435 at 9.6x" is an instruction about SIZE, not a waiver on
        # book exposure, and letting it through would leave one unguarded
        # door into the same book the automatic path is careful about.
        if notional_cap_usd and notional > notional_cap_usd > 0:
            notional = notional_cap_usd
            margin = notional / max(1.0, leverage)
        qty = notional / unit_value if unit_value > 0 else 0.0
        if exact:
            # OPERATOR MARGIN IS A MAXIMUM COMMITMENT, NOT AN OBLIGATION TO
            # SPEND IT. "Commit $435 at 9.6x" against a contract worth $640
            # buys 6 contracts, not 6.53 — and the $435 becomes whatever 6
            # contracts actually cost. The manual path bypasses
            # solve_position, so it needs the same executable-quantity rule
            # rather than an exemption from it.
            try:
                qty = normalize_quantity_down(qty, execution_instrument)
            except UnexecutableQuantity as e:
                return {"ok": False, "reason": str(e)}
            _min = execution_instrument.minimum_quantity
            if _min is not None and qty < float(_min):
                return {"ok": False, "reason": (
                    f"manual margin ${margin:,.2f} at {leverage:g}x buys less "
                    f"than the {float(_min):g} "
                    f"{execution_instrument.quantity_unit} minimum")}
            notional = qty * unit_value
            margin = notional / max(1.0, leverage)
    else:
        # The shared engine solves everything else — futures included.
        # `notional_cap_usd` is the concentration headroom: solve_position has
        # always accepted it and nothing ever passed one, so risk-parity sizing
        # ran unbounded. A tight stop then produced a position that breached
        # the book cap by construction — proposed, then refused, every time.
        from lib.risk_engine import solve_position
        decision = solve_position(
            entry=entry, stop=stop, risk_budget_usd=margin,
            free_cash=free_cash, symbol=symbol,
            # Deliberately None when the score asks for 1.0. Forcing 1x here
            # looks safer and breaks risk parity: with a tight stop, reaching
            # the risk budget needs a large notional, and an unleveraged
            # account cannot fund it, so the position shrinks and risks LESS
            # than budget. tests/test_risk_first_paper.py pins that — wide
            # and tight stops must risk the same dollars.
            #
            # Leverage here is financing, not risk. Loss at stop is bounded
            # by the budget whichever leverage is used; what leverage changes
            # is the cash committed. The real defect was the endpoint
            # REPORTING the requested leverage instead of the one used, so a
            # 25x position printed "1x" beside a $67k exposure.
            requested_leverage=leverage if leverage > 1.0 else None,
            max_margin_frac_of_cash=MAX_MARGIN_PCT_OF_CASH / 100.0,
            notional_cap_usd=notional_cap_usd,
            execution_instrument=execution_instrument,
        )
        if decision.rejected:
            return {"ok": False, "reason": decision.rejection_reason}
        qty = decision.qty
        notional = decision.notional
        margin = decision.margin
        leverage = decision.leverage
        capped = capped or decision.limiting_constraint == "cash"

    stop_distance = abs(entry - stop) if stop > 0 else 0.0
    loss_at_stop = qty * stop_distance * mult
    fees, fee_why = venue_round_trip_fee(symbol, notional, leverage, entry)
    return {
        "ok": True,
        "qty": qty,
        "margin": margin,
        "notional": notional,
        "leverage": leverage,
        "round_trip_fees": round(fees, 2),
        "fees_pct_of_margin": round(fees / margin * 100, 2) if margin else 0.0,
        "fee_basis": fee_why,
        "loss_at_stop": loss_at_stop,
        "loss_pct_of_margin": (loss_at_stop / margin * 100) if margin else 0.0,
        "capped_by_cash": capped,
    }

# A candidate close/mark price implying more than this multiple away from
# entry, in either direction, is rejected as an implausible single-interval
# move rather than trusted. No genuine mark-to-market tick (run every few
# minutes) legitimately moves a price 50x, let alone the 74,000x seen in the
# incident this guards against: a BEAT/USD crypto position (entry $0.000039)
# got marked-to-market against NASDAQ-listed BEAT's equity price ($2.87)
# when the crypto quote briefly went missing and an upstream symbol-lookup
# fallback fell through to the unrelated equity's bare ticker (see
# jobs/paper_trading.py's _get_all_prices/_get_current_price). That single
# bad tick inflated the paper portfolio's realized P&L by ~$148M. This is
# the last line of defense — it protects portfolio integrity from *any*
# upstream price-source bug, not just this specific collision.
MAX_PLAUSIBLE_PRICE_MULTIPLE = 50.0


def _price_move_is_plausible(entry: float, price: float) -> bool:
    if entry is None or price is None or entry <= 0 or price <= 0:
        return False
    ratio = price / entry
    return (1.0 / MAX_PLAUSIBLE_PRICE_MULTIPLE) <= ratio <= MAX_PLAUSIBLE_PRICE_MULTIPLE

# Leverage by asset class — futures get tighter margin than equity
ASSET_CLASS_MARGIN = {
    "futures":  1_500.0,   # Futures use smaller margin (higher leverage)
    "forex":    1_000.0,   # Forex pip-based — smaller notional per pip
    "crypto":   2_000.0,
    "equity":   3_000.0,
}

DIRECTION_LEVERAGE = {
    "Long":               (1,   1.0),
    "Bounce":             (1,   1.0),
    "Long_Leveraged":     (1,   2.0),
    "Long_5x":            (1,   5.0),
    "Long_10x":           (1,  10.0),
    "Long_20x":           (1,  20.0),
    "Short":              (-1,  1.0),
    "Short_Leveraged":    (-1,  2.0),
    "Short_5x":           (-1,  5.0),
    "Short_10x":          (-1, 10.0),
    "Short_20x":          (-1, 20.0),
}

def _normalize_direction(raw: str) -> str:
    """Canonical DIRECTION_LEVERAGE key from the TWO facts a direction
    string actually carries — side and leverage — both extracted by
    lib/trade_side, the sole parsing authority (Phase 1 dedup). This
    replaces a 40-line alias table that was a third independent
    implementation of the same two regexes, one variant spelling away
    from disagreeing with the others."""
    from lib import trade_side
    raw_str = str(raw or "").strip()
    side = trade_side.parse_side_strict(raw_str) or trade_side.normalize_side(raw_str)
    prefix = "Short" if side == trade_side.SHORT else "Long"
    lev = trade_side.leverage_from_direction(raw_str)
    if lev is None or lev <= 1.0:
        # Bounce keeps its own ledger key — it is a setup taxonomy the
        # UI and history rely on, not a side or a leverage.
        return "Bounce" if raw_str.lower() == "bounce" else prefix
    if lev <= 2.0:
        return f"{prefix}_Leveraged"
    if lev <= 5.0:
        return f"{prefix}_5x"
    if lev <= 10.0:
        return f"{prefix}_10x"
    return f"{prefix}_20x"


def _now(): return datetime.now(timezone.utc).isoformat()


def _get_portfolio_cash(db):
    """Fetch the paper portfolio record. init_db() guarantees it exists."""
    p = db.query(PaperPortfolio).first()
    if not p:
        from app.database import new_id
        p = PaperPortfolio(
            id=new_id(),
            cash=PAPER_STARTING_CAPITAL,
            total_trades=0,
            winning_trades=0,
            realized_pnl=0.0,
            updated_at=_now()
        )
        db.add(p)
        db.flush()
        logger.warning("[Paper] Portfolio row was missing — created with $100k starting capital")
    return p


def _funding_cost_usd(symbol: str, notional: float, side: int,
                      opened_at) -> float:
    """Perpetual funding accrued over the time the position was held.

    Funding is a TRANSFER, not a fee: longs pay shorts when the rate is
    positive and shorts pay longs when it is negative, so this can
    legitimately come back negative and REDUCE the cost of a short. Returns
    0 for anything that is not a crypto perpetual.

    It exists only once a trade is held, which is why it is charged at close
    rather than reserved at open like the round-trip fee. Negligible on a
    scalp; material on the 1D setups this book runs, whose own estimate is a
    1-4 week hold.
    """
    try:
        from lib.trade_horizon import age_minutes
        held_min = age_minutes(opened_at)
        if not held_min or held_min <= 0 or not notional:
            return 0.0
        from lib.transaction_costs import funding_cost_pct
        pct, _src = funding_cost_pct(symbol, held_min / 60.0, is_short=(side == -1))
        return float(pct) * abs(float(notional))
    except Exception as e:
        logger.debug(f"[Paper] funding cost unavailable for {symbol}: {e}")
        return 0.0


def _calc_pnl(entry: float, close_price: float, qty: float, side: int, leverage: float,
              margin: float, symbol: str = ""):
    """
    Unified P&L calculation.
    - qty = notional / entry  (notional = margin * leverage)
    - raw_pnl = price_move * qty * MULTIPLIER * side
    - pnl_pct uses MARGIN (capital at risk) as the base, which gives the correct ROI

    The multiplier is why this needed changing: one point of ES is $50 per
    contract, not $1. Without it every futures P&L in the learning data was
    understated by 5x (YM) to 1000x (CL). Shares and coins have a
    multiplier of 1.0, so this is a no-op for them.

    UNITS CONVENTION — the multiplier makes this load-bearing. For futures,
    `qty` MUST be CONTRACTS, because the multiplier converts contracts to
    underlying units. Passing units instead double-counts by the whole
    multiplier. That is not theoretical: a single HG=F position sized the
    units way (749.57 = notional/price, rather than 0.03 contracts) recorded
    -$440,371 on a -0.35% copper move. The true loss was -$17.61. That one
    trade was 104% of the paper book's entire -$422,504 deficit, and it took
    the account to -$341,681 cash, which then made every signal card refuse
    to size.

    So the arithmetic is bounded by what the position could actually lose.
    """
    from lib.instruments import get_spec
    multiplier = get_spec(symbol).multiplier if symbol else 1.0
    multiplier = _effective_multiplier(multiplier, entry, qty, margin, leverage, symbol)
    raw_pnl = (close_price - entry) * qty * side * multiplier
    raw_pnl = _bound_loss_to_margin(raw_pnl, margin, symbol, qty, multiplier)
    pnl_pct = (raw_pnl / margin) * 100 if margin else 0.0
    return raw_pnl, pnl_pct


def _effective_multiplier(multiplier: float, entry: float, qty: float,
                          margin: float, leverage: float, symbol: str) -> float:
    """Apply the contract multiplier only when `qty` is actually CONTRACTS.

    The multiplier converts contracts to underlying units, so it is correct
    for a contract-sized qty and catastrophic for a unit-sized one. Which
    convention a stored position uses is recoverable from its own numbers,
    because notional is margin x leverage:

        qty x entry              ~ notional  ->  qty is UNITS
        qty x entry x multiplier ~ notional  ->  qty is CONTRACTS

    Every futures position in the paper book turned out to be unit-sized
    while P&L multiplied anyway, and the damage ran BOTH ways — the bound on
    losses alone would have left the winners inflated:

        SI=F  +0.07% move  ->  recorded +$17,028   true +$3.41
        HG=F  -0.35% move  ->  recorded -$440,371  true -$17.61

    Reading the convention off the position is better than bounding the
    result, because it gives the right number rather than a survivable one.
    """
    if multiplier <= 1 or entry <= 0 or qty <= 0:
        return multiplier
    notional = abs(margin) * (leverage or 1.0)
    if notional <= 0:
        return multiplier
    # Only an EXACT units match disarms the multiplier — not merely "closer
    # than the contract reading". The affected rows were sized as
    # notional/price, so qty x entry reproduces the notional to the cent.
    # Demanding that precision means ambiguous or synthetic data keeps the
    # declared convention: a heuristic that fires on a maybe would corrupt
    # correctly-sized positions to fix incorrectly-sized ones, which is a
    # worse trade than leaving the bad rows to the margin bound.
    if abs(qty * entry - notional) / notional <= UNITS_MATCH_TOLERANCE:
        logger.warning(
            f"[Paper] {symbol}: qty={qty:g} is UNITS of the underlying "
            f"(qty x entry = ${qty * entry:,.0f} = the notional), not contracts. "
            f"Not applying the {multiplier:g}x contract multiplier — doing so "
            f"would overstate this position's P&L by {multiplier:g}x."
        )
        return 1.0
    return multiplier


# A position cannot lose more than the capital committed to it — that is what
# liquidation means, and it is a property of the instrument, not a risk
# preference. The broker closes you out when margin is exhausted; it does not
# hand you a bill for 440x the position.
#
# This is the backstop for the units/contracts mismatch above. Sizing and P&L
# are two code paths that must agree about what `qty` means, and when they
# disagree the error is silent and enormous. Bounding here means the worst a
# disagreement can cost is the margin, and the log names the likely cause.
LOSS_MISMATCH_FACTOR = 1.5   # above this multiple of margin, suspect a unit bug

# How exactly `qty x entry` must reproduce the notional before the position is
# declared unit-sized. The affected rows match to the cent because they were
# sized as notional/price; anything looser is ambiguous and keeps the declared
# convention.
UNITS_MATCH_TOLERANCE = 0.001   # 0.1%


def _bound_loss_to_margin(raw_pnl: float, margin: float, symbol: str,
                          qty: float, multiplier: float) -> float:
    if margin <= 0 or raw_pnl >= -margin:
        return raw_pnl
    # A loss slightly past margin is ordinary — the position gapped through
    # its stop and liquidated. Only a loss that dwarfs the margin indicates
    # the sizing and P&L paths disagree about what qty means, and only then
    # is the multiplier worth naming.
    overshoot = abs(raw_pnl) / margin
    if overshoot >= LOSS_MISMATCH_FACTOR:
        cause = (
            f" qty={qty:g}, multiplier={multiplier:g} — for futures qty must be "
            f"CONTRACTS, and passing units double-counts by the multiplier."
            if multiplier > 1 else ""
        )
        logger.error(
            f"[Paper] {symbol or '?'}: computed P&L {raw_pnl:,.2f} against "
            f"${margin:,.2f} of margin — {overshoot:,.0f}x the capital at risk, "
            f"which is not possible.{cause} Bounding at the margin (liquidation)."
        )
    return -margin


@dataclass(frozen=True)
class EntryAuthorization:
    """What risk approved, priced at a REFERENCE. Nothing has been mutated.

    This is the output of PREPARE. It is deliberately a record rather than a
    scratchpad: an authorization that can be edited in place is one that can
    be quietly enlarged between the approval and the order, which is the
    whole failure mode `OrderPlan.check` exists to catch.

    `reference_price` is NOT a fill. It is the price the size was solved
    against — the screen price a desk sizes on before it learns what it
    actually paid. Settlement records the real fill.
    """
    signal: dict
    symbol: str
    asset_class: str
    direction: str                 # a DIRECTION_LEVERAGE key
    side: int                      # +1 long, -1 short
    reference_price: float
    target: float
    stop: float
    qty: float
    margin: float
    notional: float
    leverage: float
    loss_at_stop: float
    equity: float
    sizing: dict

    @property
    def stop_distance(self) -> float:
        return abs(float(self.reference_price) - float(self.stop))

    @property
    def risk_quantum(self) -> float:
        """The dollar risk of ONE quantity-rounding step.

        `qty` is rounded to QTY_DECIMALS, so every risk figure derived from
        it is only meaningful to within one step. Comparing two such figures
        at a tighter tolerance than the rounding that produced them asks the
        arithmetic a question it cannot answer, and the answer comes back as
        "$863.46 exceeds $863.46" — a refusal with no readable cause.

        WHICH STEP, THOUGH. QTY_STEP is the CONTINUOUS step, and reading it
        for a discrete instrument understates the quantum by six orders of
        magnitude. A perpetual rounds to ONE CONTRACT: re-pricing a 3-lot at
        a fill that sits further from the stop cannot answer "hold slightly
        less" — the only sizes available are 3 and 2. So the same 3 contracts
        legitimately risk $981.57 where the mid said $966.00, and comparing
        that at a 1e-6 quantum refused an order whose size never changed and
        whose loss stayed inside the approved budget. The budget itself is
        still enforced exactly, in `solve_position`; this tolerance only ever
        governs the precision of the comparison.
        """
        mult = float(self.sizing.get("multiplier") or 1.0)
        step = float(self.sizing.get("quantity_step") or QTY_STEP)
        return step * self.stop_distance * mult

    def risk_decision(self):
        """The approval, in the canonical type the risk gate understands."""
        from lib.decision_types import RiskDecision
        _unit = self.sizing.get("quantity_unit")
        return RiskDecision(
            allowed_risk_usd=float(self.loss_at_stop),
            stop_distance=self.stop_distance,
            qty=float(self.qty), notional=float(self.notional),
            margin=float(self.margin), leverage=float(self.leverage),
            limiting_constraint=("cash" if self.sizing.get("capped_by_cash")
                                 else "risk"),
            # THE BASIS TRAVELS OR THE GATE GUESSES. Rebuilding the decision
            # without it left the last gate to re-resolve the bare symbol,
            # which answered 1.0 coin for a quantity approved in 0.01-BTC
            # contracts — and it then refused the very order it had approved,
            # by a factor of exactly 100.
            quantity_unit=_unit,
            multiplier=(float(self.sizing.get("multiplier") or 1.0)
                        if _unit else None))

    def shrunk_to(self, qty: float) -> "EntryAuthorization":
        """A SMALLER authorization. Refuses to enlarge, by construction.

        Execution may shrink an order — a venue minimum, a rounded contract,
        an adverse fill that costs more risk per unit than the size was
        solved against. It may never grow one, so this raises rather than
        clamps: a silent enlargement is exactly the defect that would make
        every downstream ceiling decorative.
        """
        q = float(qty)
        if not (q > 0):
            raise ValueError(f"cannot shrink {self.symbol} to {q!r} units")
        if q > float(self.qty) + 1e-12:
            raise ValueError(
                f"shrunk_to({q}) would ENLARGE {self.symbol} beyond the "
                f"authorized {self.qty} — execution may shrink an order, "
                f"never enlarge one")
        scale = q / float(self.qty)
        return replace(self, qty=q,
                       notional=float(self.notional) * scale,
                       margin=float(self.margin) * scale,
                       loss_at_stop=float(self.loss_at_stop) * scale)


def prepare_entry(signal: dict, reference_price: float = None,
                  execution_instrument=None) -> dict:
    """PREPARE — read, calculate, AUTHORIZE. No mutation, no cash moves.

    Everything that decides whether and how large this trade may be: side
    parsing, delivery risk, stop and target discipline, the horizon cap,
    sizing, leverage, book concentration and headroom. It ends holding an
    authorized quantity and touches nothing.

    This exists because the order was backwards. `canonical_entry` used to
    send a NOMINAL 1 unit through the venue purely to discover a price, and
    only then size — so `ExecutionResult.spread_cost_usd` and `slippage_usd`
    described one unit of an order nobody placed, while the position settled
    at a different size entirely. The sequence has to be

        signal -> risk/sizing -> AUTHORIZED QTY -> execute -> fill -> settle

    and the attribution has to describe the order that was actually
    simulated. Splitting the authorization out is what lets a caller size
    first and execute second WITHOUT a second risk engine: there is still
    exactly one, and it lives here.

    Returns {"ok": True, "authorization": EntryAuthorization} or an error
    dict — the same error dicts `open_paper_position` has always returned.
    """
    # EVIDENCE_ONLY forbids economic mutation AT THE MUTATION, not at the
    # caller — a caller that forgets to ask is exactly the one that would
    # open a position in a mode that forbade it.
    from lib.runtime_mode import forbid_economic_mutation
    forbid_economic_mutation("prepare_entry")
    sym = signal.get("asset_symbol", "").upper().strip()
    if not sym:
        return {"error": "No asset_symbol provided"}

    # Strict side first: an order path never assumes. A missing or
    # unparseable direction used to default to "Long", which meant garbage
    # input BOUGHT things. Unknown is a rejection with a reason.
    from lib.trade_side import parse_side_strict
    raw_dir = signal.get("paper_direction") or signal.get("direction")
    if parse_side_strict(raw_dir) is None:
        return {"error": f"unparseable direction {raw_dir!r} — refusing to "
                         "assume a side for an order"}
    dir_key = _normalize_direction(raw_dir)

    side, leverage = DIRECTION_LEVERAGE[dir_key]

    # Delivery-risk hard block (Phase 4B): the continuous symbol never
    # expires, but the contract a real account would hold does. An entry
    # the real market would refuse (front month inside its notice/expiry
    # margin) must not exist in the learning ledger either — futures are
    # paper-only today, and that is exactly why the ledger must be right.
    try:
        from lib.futures_contracts import delivery_risk, root_of
        if root_of(sym) is not None:
            risk = delivery_risk(sym)
            if risk["level"] == "blocked":
                return {"error": f"delivery risk: {risk['reason']}"}
            if risk["level"] == "roll_window":
                logger.warning(f"[Paper] {sym} entering inside roll window: "
                               f"{risk['reason']}")
    except Exception as e:
        logger.debug(f"[Paper] delivery-risk check skipped for {sym}: {e}")

    entry = float(reference_price or signal.get("entry_price") or 0)
    # Try futures price source if still no price
    if (not entry or entry <= 0):
        try:
            from lib.futures_data import get_cached_futures_price, FUTURES_UNIVERSE
            if sym in FUTURES_UNIVERSE:
                fd = get_cached_futures_price(sym)
                if fd:
                    entry = float(fd.get("price") or 0)
        except Exception:
            pass
    if not entry or entry <= 0:
        return {"error": f"No valid entry price for {sym} (got: {reference_price}, signal entry: {signal.get('entry_price')})"}

    # Auto-detect asset class (Equity | Crypto | Futures | Forex)
    asset_class_raw = (signal.get("asset_class") or "").lower()
    if "futures" in asset_class_raw or "commodity" in asset_class_raw:
        asset_class = "Futures"
    elif "forex" in asset_class_raw or "currency" in asset_class_raw:
        asset_class = "Forex"
    elif "/" in sym or sym.upper().endswith("USD"):
        asset_class = "Crypto"
    else:
        # Check the futures universe
        try:
            from lib.futures_data import FUTURES_UNIVERSE
            if sym in FUTURES_UNIVERSE:
                cat = FUTURES_UNIVERSE[sym]["category"]
                asset_class = "Forex" if cat == "Forex" else "Futures"
            else:
                asset_class = "Equity"
        except Exception:
            asset_class = "Equity"

    target = float(signal.get("target_price") or 0)
    stop   = float(signal.get("stop_loss") or 0)

    # Ensure stop/target are on the correct side of entry
    if side == 1:  # Long / Bounce / Long_Leveraged
        if not target or target <= entry:
            target = round(entry * 1.05, 4 if entry < 1 else 2)
        if not stop or stop >= entry:
            stop = round(entry * 0.97, 4 if entry < 1 else 2)
    else:  # Short / Short_Leveraged — stop ABOVE entry, target BELOW entry
        if not target or target >= entry:
            target = round(entry * 0.95, 4 if entry < 1 else 2)
        if not stop or stop <= entry:
            stop = round(entry * 1.03, 4 if entry < 1 else 2)

    # ── Stop discipline before sizing ────────────────────────────────────
    # Two ceilings, whichever is tighter: the horizon cap (3% scalp / 10%
    # longer) and the liquidation bound (a position at leverage L is wiped
    # out by a 1/L adverse move, so the stop must sit well inside that).
    try:
        from lib.trading_preferences import horizon_for_timeframe
        _horizon = horizon_for_timeframe(signal.get("timeframe"))
    except Exception:
        _horizon = "all"
    # The horizon cap is Jarvis's OWN risk policy (scalps risk <=3% of
    # entry, longer trades <=10%) and may clamp the stop. What may NOT
    # clamp the stop anymore is leverage: the old code picked leverage
    # from the composite score and then tightened the stop to fit inside
    # that leverage's liquidation distance — the score literally moved the
    # risk decision (P0.6). Now the stop is fixed first, and leverage is
    # derived from it inside size_position (max_safe_leverage).
    _horizon_cap = 0.03 if _horizon == "scalp" else 0.10
    _max_move = entry * _horizon_cap
    if side == 1:
        _floor = entry - _max_move
        if stop < _floor:
            logger.info(f"[Paper] {sym} stop {stop:g} -> {_floor:g} "
                        f"({_horizon_cap:.0%} {_horizon} horizon cap)")
            stop = round(_floor, 8)
    else:
        _ceil = entry + _max_move
        if stop > _ceil:
            logger.info(f"[Paper] {sym} stop {stop:g} -> {_ceil:g} "
                        f"({_horizon_cap:.0%} {_horizon} horizon cap)")
            stop = round(_ceil, 8)

    # ── Sizing: risk decides quantity, the stop decides leverage ─────────
    # An explicit direction like Long_10x is a deliberate instruction and
    # survives as a CEILING passed into sizing; conviction-derived leverage
    # is gone (invariant #3 — the score is measured inverted, so
    # "conviction earns leverage" granted the most leverage to the worst
    # setups).
    ac_lower = asset_class.lower()
    override_margin = float(signal.get("margin_override") or 0)

    try:
        with get_db() as _db:
            _pf = _get_portfolio_cash(_db)
            _equity = float(_pf.cash or 0) + sum(
                float(r.margin_used or 0)
                for r in _db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
            )
            # Ask how much room is left BEFORE sizing, so the sizer fits
            # inside the cap instead of proposing something that will be
            # refused. Same module and same constants as the guard below —
            # the check that follows is a verification, not a second opinion.
            from lib.concentration import headroom_for_book
            _room = headroom_for_book(sym, _equity, _db, book="paper")
            # NOT `or None`: zero room must stay zero. `solve_position`
            # treats a falsy cap as "no cap", so `0.0 or None` would hand an
            # exhausted symbol UNLIMITED size — which is how this call sized
            # DOGE at 70% of equity with no headroom left, in testing.
            _cap = _room["max_notional"]
            if _cap <= 0:
                _sizing_blocked = _room["reason"]
                sizing = {"ok": False, "reason": _room["reason"]}
            else:
                _sizing_blocked = None
                sizing = size_position(_equity, entry, stop, leverage, float(_pf.cash or 0),
                                       margin_override=override_margin, symbol=sym,
                                       notional_cap_usd=_cap,
                                       execution_instrument=execution_instrument)
    except Exception as e:
        # FAIL CLOSED (P0.3's paper twin): a sizing crash is not a license
        # to open a flat-size position.
        logger.error(f"[Paper] sizing crashed for {sym} — refusing to open: {e}")
        return {"error": f"sizing unavailable for {sym}: {e}"}

    # No room at all is a BOOK state, and saying so in those terms beats
    # reporting it as a sizing failure — the trade is fine, the book is full.
    if _sizing_blocked:
        logger.info(f"[Paper] {sym} NOT opened — {_sizing_blocked}")
        return {"error": f"concentration limit: {_sizing_blocked}",
                "concentration": _room}

    if not sizing.get("ok"):
        # A deterministic rejection stays a rejection (P0.7). The old code
        # fell back to a flat ASSET_CLASS_MARGIN position here, silently
        # overriding reasons like "one contract exceeds the risk budget"
        # and "no free cash to commit".
        logger.info(f"[Paper] {sym} NOT opened — sizing rejected: {sizing.get('reason')}")
        return {"error": f"paper sizing rejected: {sizing.get('reason')}"}

    # Concentration is a property of the BOOK, not of this trade, so it is
    # judged here against what is already open — not inside solve_position,
    # where a notional ceiling collides with risk parity (measured
    # 2026-08-16: any cap tight enough to bind a 146%-exposure XAUT also
    # strangles an ordinary 1x/5%-stop trade at 20% notional).
    from lib.concentration import check_against_book
    conc = check_against_book(sym, sizing["notional"],
                              sizing.get("loss_at_stop") or 0.0, _equity)
    if not conc.get("ok"):
        logger.info(f"[Paper] {sym} NOT opened — concentration: {conc['reason']}")
        return {"error": f"concentration limit: {conc['reason']}",
                "concentration": conc}

    qty = round(sizing["qty"], QTY_DECIMALS)
    margin = round(sizing["margin"], 2)
    notional = sizing["notional"]
    leverage = float(sizing.get("leverage") or leverage or 1.0)
    logger.info(
        f"[Paper] {sym}: ${margin:,.0f} committed @ {leverage:g}x = ${notional:,.0f} exposure | "
        f"qty={qty:g} | stop-out costs ${sizing['loss_at_stop']:,.0f} "
        f"({sizing['loss_pct_of_margin']:.0f}% of the ${margin:,.0f} committed)"
        + (" [capped by free cash]" if sizing.get("capped_by_cash") else "")
    )

    # THE AUTHORIZATION MUST DESCRIBE THE QUANTITY IT AUTHORIZES.
    #
    # `qty` is rounded to 6dp above, and rounding can go UP: a solved
    # 0.29411764... becomes 0.294118, which risks $1,000.0012 against a
    # $1,000.00 budget. `sizing["loss_at_stop"]` was computed from the
    # UNROUNDED quantity, so the record said 1,000.00 while the size it
    # carried risked fractionally more — and the last risk gate, comparing
    # the two honestly, refused the order it had itself approved.
    #
    # Tiny in dollars and structural in kind: a risk figure must be derived
    # from the size that will actually be sent, not from the one that was
    # solved before rounding.
    #
    # And the MULTIPLIER has to come from the same place the sizing did. An
    # exact instrument that reached solve_position and then had its loss
    # recomputed against get_spec(sym) would authorize a contract quantity
    # and describe its risk in coins — the 100x seam, reopened one line
    # before the authorization is built.
    if execution_instrument is not None:
        _mult = float(execution_instrument.multiplier or 1.0)
        _unit = execution_instrument.quantity_unit
        _step = execution_instrument.quantity_step
        # `round()` above can go UP, and up is the one direction a quantity
        # may never move. Harmless at an integer step, structural at any
        # other, so it is re-normalised rather than trusted.
        from lib.instruments import normalize_quantity_down
        qty = normalize_quantity_down(qty, execution_instrument)
    else:
        _mult, _unit, _step = 1.0, None, None
        try:
            from lib.instruments import get_spec
            _spec = get_spec(sym) if sym else None
            _mult = float(getattr(_spec, "multiplier", 1.0) or 1.0)
        except Exception:
            pass
    loss_at_stop = qty * abs(entry - stop) * _mult
    # `quantity_unit` is set ONLY when an exact instrument stated it. It is
    # what tells the last risk gate that the basis is already established and
    # need not be re-derived; a legacy authorization leaves it None so that
    # gate keeps resolving — and keeps refusing instruments whose units are
    # unknown — exactly as before.
    sizing = dict(sizing, multiplier=_mult, quantity_unit=_unit,
                  quantity_step=_step)

    return {"ok": True, "authorization": EntryAuthorization(
        signal=signal, symbol=sym, asset_class=asset_class, direction=dir_key,
        side=side, reference_price=entry, target=target, stop=stop,
        qty=qty, margin=margin, notional=notional, leverage=leverage,
        loss_at_stop=loss_at_stop, equity=_equity, sizing=sizing)}


def settle_position_entry(auth: EntryAuthorization, *, fill_price: float,
                 execution_provenance: dict | None = None,
                 canonical_entry_fee_usd: float | None = None,
                 observation_id: str | None = None,
                 execution_id: str | None = None) -> dict:
    """SETTLE — ONE transaction: revalidate the book, create, debit, commit.

    The authorization arrives already decided; nothing here may enlarge it.
    What this re-checks are the facts that can have changed since PREPARE
    read them — a duplicate open, the slot count, the deployment cap, free
    cash — because a book is shared state and an authorization is a snapshot.

    PROVENANCE IS SETTLED HERE, NOT STAMPED AFTERWARDS.

    `get_db()` commits on context exit, so a caller that opened the position
    and then persisted provenance in a SECOND session was running two
    transactions. When the second failed, the first had already committed:
    the position existed, cash was debited, provenance was NULL — and a NULL
    provenance position is invisible to `is_canonical()`, so the fail-closed
    exit guard would let it straight through the legacy close path. Measured
    on a disposable book: $1,250.17 debited, one orphan position, and an
    exception that unwound nothing.

    Passing the document in means position, cash and provenance share one
    transaction and roll back together.

    `canonical_entry_fee_usd`, when given, switches this position to the
    per-leg cost model: the fee is DEBITED NOW from actual executed exposure,
    and `fees` is left at 0 rather than carrying the legacy deferred
    round-trip estimate that the close path would later charge again.

    `fill_price` is what the position is RECORDED at. For the legacy path it
    equals the reference the size was solved against; for the canonical path
    it is the executed fill, and the two differ by exactly the spread and
    slippage that crossing the book actually cost.

    `observation_id` / `execution_id` are the CANONICAL evidence linkage and
    are OPTIONAL by design. The legacy and manual callers — Telegram, the
    trading routes, `open_paper_position` — price their own entries and have
    no decision observation; requiring one would force them to manufacture
    evidence about a decision path they never went through. When they ARE
    supplied, the observation is advanced to SETTLED inside this same
    transaction, so the ledger and the evidence chain cannot disagree.
    """
    # EVIDENCE_ONLY forbids economic mutation AT THE MUTATION, not at the
    # caller — a caller that forgets to ask is exactly the one that would
    # open a position in a mode that forbade it.
    from lib.runtime_mode import forbid_economic_mutation
    forbid_economic_mutation("settle_position_entry")
    sym = auth.symbol
    asset_class = auth.asset_class
    dir_key = auth.direction
    side = auth.side
    entry = float(fill_price)
    target, stop = auth.target, auth.stop
    qty, margin = round(auth.qty, QTY_DECIMALS), round(auth.margin, 2)
    notional, leverage = auth.notional, auth.leverage
    sizing = auth.sizing

    # ── B1: CANONICAL INTENT IS THE CAUSAL PAIR, STATED OR ABSENT ────────
    #
    # observation_id and execution_id arrive together or not at all. The
    # individual companion arguments (`execution_provenance`,
    # `canonical_entry_fee_usd`) have each existed independently, so their
    # presence proves nothing about intent — but the PAIR is minted only by
    # the canonical entry path. Half a pair is a broken causal chain, and a
    # broken chain FAILS CLOSED rather than quietly settling as legacy: a
    # canonical trade that loses its evidence linkage must not become a
    # legacy-shaped position nobody can explain later.
    canonical = bool(observation_id) and bool(execution_id)
    if bool(observation_id) != bool(execution_id):
        return {"error": "INCOMPLETE_CANONICAL_LINKAGE",
                "detail": (f"canonical settlement requires BOTH "
                           f"observation_id and execution_id; got "
                           f"observation_id={'set' if observation_id else 'missing'}, "
                           f"execution_id={'set' if execution_id else 'missing'} "
                           f"— refusing rather than downgrading to legacy")}

    ledger_facts = None
    if canonical:
        # Canonical intent requires the full canonical invocation, validated
        # as ONE self-consistent fact set BEFORE any mutation. The validator
        # is pure: it re-prices nothing and opens no session.
        from lib.settlement_ledger import (LedgerValidationError,
                                           validate_entry_ledger_facts)
        if execution_provenance is None or canonical_entry_fee_usd is None:
            return {"error": "INCOMPLETE_CANONICAL_LINKAGE",
                    "detail": ("canonical settlement requires execution "
                               "provenance and the exact entry fee; refusing "
                               "rather than settling half a canonical entry "
                               "as legacy")}
        try:
            ledger_facts = validate_entry_ledger_facts(
                auth, settled_qty=qty, settled_margin=margin,
                fill_price=entry,
                canonical_entry_fee_usd=float(canonical_entry_fee_usd),
                execution_provenance=execution_provenance,
                observation_id=observation_id, execution_id=execution_id)
        except LedgerValidationError as e:
            logger.error("[Paper] %s canonical ledger validation refused: %s",
                         sym, e)
            return {"error": "CANONICAL_LEDGER_VALIDATION_FAILED",
                    "detail": str(e)}

    # ONE settlement time for the whole entry transaction (§24). This is
    # ACCOUNT SETTLEMENT TIME, not the venue execution event — venue event
    # time stays in provenance.
    settlement_time = _now()

    # NOTE on the duplicate-open race: the "already open?" check below and the
    # INSERT further down happen in the same SQLAlchemy session/transaction,
    # but that only protects against races *within* this process — a second
    # process/thread (e.g. a concurrent scheduler cycle vs. a Telegram
    # callback) using its own session can still pass the SELECT before this
    # transaction commits. The real guard is the partial unique index
    # `uq_paper_position_open_symbol` on paper_positions(user_id, symbol)
    # WHERE status='Open' (see app/database.py:_ensure_paper_position_unique_open_index),
    # which turns that race into an IntegrityError we catch below instead of
    # a silent duplicate position. On a pre-existing DB with duplicate open
    # rows the index creation is skipped (logged at startup) and this
    # residual race remains until those duplicates are cleaned up.
    try:
        with get_db() as db:
            existing = db.query(PaperPosition).filter(
                PaperPosition.symbol == sym,
                PaperPosition.status == "Open"
            ).first()
            if existing:
                return {"error": f"Paper position already open for {sym}"}

            # B1: one entry execution cannot create two canonical positions.
            # Named refusal here; the UNIQUE constraint on the header is the
            # backstop for the race this SELECT cannot see.
            if canonical:
                from app.database import PaperPositionSettlement
                dup = db.query(PaperPositionSettlement).filter(
                    PaperPositionSettlement.entry_execution_id == execution_id
                ).first()
                if dup:
                    return {"error": "DUPLICATE_CANONICAL_EXECUTION",
                            "detail": (f"execution {execution_id} already "
                                       f"settled into position "
                                       f"{dup.position_id}; one execution is "
                                       f"one entry")}

            portfolio = _get_portfolio_cash(db)

            # ── Portfolio-level capacity checks ──────────────────────────
            open_rows = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
            deployed = sum(float(r.margin_used or 0) for r in open_rows)
            equity_now = float(portfolio.cash or 0) + deployed
            if len(open_rows) >= MAX_OPEN_POSITIONS:
                return {"error": (f"Paper book full: {len(open_rows)}/{MAX_OPEN_POSITIONS} positions open. "
                                  f"Close something before adding risk.")}
            deploy_cap = equity_now * (MAX_DEPLOYED_PCT / 100.0)
            if deployed + margin > deploy_cap:
                return {"error": (f"Paper deployment cap reached: ${deployed:,.0f} of ${deploy_cap:,.0f} "
                                  f"({MAX_DEPLOYED_PCT:.0f}% of ${equity_now:,.0f} equity) already committed.")}

            logger.info(
                f"[Paper] Cash ${portfolio.cash:,.2f} | deployed ${deployed:,.0f}/${deploy_cap:,.0f} "
                f"({len(open_rows)}/{MAX_OPEN_POSITIONS} slots) | this trade ${margin:,.2f}"
            )
            if portfolio.cash < margin:
                logger.warning(f"[Paper] Insufficient cash — have ${portfolio.cash:.2f}, need ${margin:.2f}")
                return {"error": f"Insufficient paper cash (${portfolio.cash:.0f}) for margin ${margin:.0f}. Use /api/paper/reset to restore $100k."}

            # The catastrophic backstop is written onto the position as a
            # PRICE the moment it opens, so it is enforced by the ordinary
            # stop machinery rather than discovered by a 15-minute poll. The
            # old dollar cap was only ever a comparison made when the job
            # happened to look, which is why a "$15 limit" exited at -$55 on
            # average and -$379 at worst.
            #
            # It only ever WIDENS nothing: the signal's own stop governs
            # whenever it is tighter, and this catches the tail.
            try:
                from jobs.paper_trading import catastrophic_stop_price
                hard = catastrophic_stop_price(entry, qty, margin, side == -1)
                if hard and hard > 0:
                    if side == -1:
                        stop = min(stop, hard) if stop and stop > 0 else hard
                    else:
                        stop = max(stop, hard) if stop and stop > 0 else hard
            except Exception as e:
                logger.debug(f"[Paper] catastrophic stop unavailable for {sym}: {e}")

            from app.database import new_id
            pos = PaperPosition(
                id            = new_id(),
                symbol        = sym,
                asset_class   = asset_class,
                direction     = dir_key,
                side          = "long" if side == 1 else "short",
                leverage      = leverage,
                qty           = qty,
                entry_price   = entry,
                current_price = entry,
                target_price  = target,
                stop_loss     = stop,
                # As placed at open, never trailed: the R denominator.
                initial_stop_loss = stop,
                notional      = notional,
                margin_used   = margin,
                # The round trip is reserved at OPEN, so an untouched position
                # immediately shows what unwinding it costs. size_position
                # already computed this; it was only ever displayed, never
                # charged — `cash -= margin` alone let the book trade free.
                # LEGACY: a deferred round-trip ESTIMATE, charged at close.
                # CANONICAL (per_leg_v2): the entry leg is charged now, so
                # this stays 0 — leaving the estimate here would let the
                # close path bill the same economics a second time.
                fees          = (0.0 if canonical_entry_fee_usd is not None
                                 else round(float(sizing.get("round_trip_fees") or 0.0), 6)),
                fee_basis     = ("per_leg_v2_entry" if canonical_entry_fee_usd is not None
                                 else sizing.get("fee_basis")),
                execution_provenance = (json.dumps(execution_provenance)
                                        if execution_provenance else None),
                unrealized_pnl= 0.0,
                unrealized_pct= 0.0,
                signal_id     = auth.signal.get("id"),
                status        = "Open",
                opened_at     = settlement_time,
                updated_at    = settlement_time,
            )
            db.add(pos)
            # ONE debit, inside ONE transaction. The canonical entry fee is
            # charged here rather than deferred, which is the whole of the
            # per-leg model at entry.
            entry_fee = float(canonical_entry_fee_usd or 0.0)
            if entry_fee and portfolio.cash < (margin + entry_fee):
                raise ValueError(
                    f"cash {portfolio.cash:.2f} cannot cover margin {margin:.2f} "
                    f"plus entry fee {entry_fee:.2f} at settlement")
            portfolio.cash    -= (margin + entry_fee)
            portfolio.updated_at = settlement_time

            pos_id   = pos.id

            # ── A.1.1: THE EVIDENCE CHAIN CLOSES IN THIS TRANSACTION ─────
            #
            # Linking the decision observation AFTERWARDS was a smaller
            # version of the A7 mistake. `get_db()` commits on context exit,
            # so a third transaction that failed would leave the ledger
            # perfectly correct — position created, margin and fee debited,
            # provenance stamped — while the observation still said
            # SIMULATED_FILLED with no position_id. The economics would be
            # right and the causal chain
            #
            #     observation -> execution -> settled position
            #
            # would have a hole in the middle, which is exactly the state
            # this evidence subsystem exists to make impossible.
            #
            # Verified against the row rather than assumed: a mismatch means
            # this settlement does not belong to this decision, and raising
            # rolls back the position, the margin, the fee and the
            # provenance together.
            if observation_id:
                from lib import decision_observation as DO
                from app.database import DecisionObservation
                obs = db.query(DecisionObservation).filter(
                    DecisionObservation.observation_id == observation_id).first()
                if obs is None:
                    raise ValueError(
                        f"no decision observation {observation_id} to settle "
                        f"against — refusing to create a position with no "
                        f"record of why it was allowed")
                if obs.final_decision != DO.TRADE:
                    raise ValueError(
                        f"observation {observation_id} decided "
                        f"{obs.final_decision}, not TRADE")
                if execution_id and obs.execution_id != execution_id:
                    raise ValueError(
                        f"observation {observation_id} names execution "
                        f"{obs.execution_id!r}, settlement carries "
                        f"{execution_id!r}")
                if obs.execution_state != DO.EXEC_SIMULATED_FILLED:
                    raise ValueError(
                        f"observation {observation_id} is "
                        f"{obs.execution_state}, not {DO.EXEC_SIMULATED_FILLED}")
                if obs.position_id:
                    raise ValueError(
                        f"observation {observation_id} already settled into "
                        f"position {obs.position_id}")
                obs.execution_state = DO.EXEC_SETTLED
                obs.position_id = pos_id
                obs.settlement_at = settlement_time

            # ── B1: THE LEDGER COMMITS WITH THE POSITION OR NOT AT ALL ───
            # Header and ENTRY leg are added to THIS session, inside THIS
            # transaction — no second session, no post-commit stamping. If
            # anything from the position insert to this point fails, the
            # position, the cash, the observation linkage and the ledger all
            # unwind together, which is the entire reason B1 exists.
            if canonical:
                from lib.settlement_ledger import persist_entry_ledger
                persist_entry_ledger(db, position=pos, facts=ledger_facts,
                                     settlement_time=settlement_time)

            pos_data = {
                "id": pos_id, "symbol": sym, "direction": dir_key,
                "side": "long" if side == 1 else "short", "leverage": leverage, "qty": qty,
                "entry_price": entry, "target": target, "stop": stop,
                "notional": notional, "margin_required": margin, "asset_class": asset_class,
                "entry_fee_usd": float(canonical_entry_fee_usd or 0.0),
            }
    except IntegrityError as e:
        # WHICH constraint fired matters: the same exception class covers
        # two different races, and answering both with "already open" would
        # misreport a duplicated canonical execution as a symbol collision.
        detail = str(e)
        if ("paper_position_settlements" in detail
                or "paper_settlement_legs" in detail):
            logger.warning(f"[Paper] Duplicate canonical settlement race for "
                           f"{sym}: {detail}")
            return {"error": "DUPLICATE_CANONICAL_EXECUTION",
                    "detail": ("another settlement committed this execution "
                               "concurrently; one execution is one entry")}
        # Lost the race: another session committed an open position for this
        # symbol between our SELECT above and this transaction's commit.
        logger.warning(f"[Paper] Duplicate-open race detected for {sym} — a position was already opened concurrently")
        return {"error": f"Paper position already open for {sym}"}

    logger.info(
        f"[Paper] ✅ Opened {dir_key} on {sym} ({asset_class}) @ ${entry:.4f} | "
        f"qty={qty:.4f} | notional=${notional:.0f} | margin=${margin:.0f} | "
        f"target=${target:.4f} | stop=${stop:.4f}"
    )
    return {"ok": True, "position": pos_data}


def open_paper_position(signal: dict, current_price: float = None,
                        execution_provenance: dict | None = None,
                        canonical_entry_fee_usd: float | None = None) -> dict:
    """Open a new paper position from a trading signal: PREPARE then SETTLE.

    direction can be: Long, Bounce, Long_Leveraged, Short, Short_Leveraged

    THE PRICE IS USED TWICE HERE, AND THAT IS THE LEGACY SHAPE. `current_price`
    is both the reference the size is solved against and the price the
    position is recorded at, because this path has no execution step between
    the two — whatever it is handed becomes the fill. That is precisely the
    mark-as-fill behaviour `lib/canonical_entry` exists to replace; it stays
    intact for the manual and legacy callers that still price their own
    entries (Telegram, the trading routes), and it is not what the autonomous
    scheduler uses.
    """
    prep = prepare_entry(signal, reference_price=current_price)
    if "authorization" not in prep:
        return prep                       # an error dict, unchanged in shape
    return settle_position_entry(prep["authorization"],
                        fill_price=prep["authorization"].reference_price,
                        execution_provenance=execution_provenance,
                        canonical_entry_fee_usd=canonical_entry_fee_usd)


# ── Development-checkpoint guard: canonical positions have no exit yet ────
#
# Pass A wired canonical ENTRY. Canonical EXIT is Pass B. Until it exists, a
# position opened by the venue-book executor must not fall through the close
# arithmetic below, which settles at whatever price it is handed — the exact
# mark-as-fill behaviour canonical entry was built to remove. Half-honest
# economics are worse than uniformly optimistic ones, because they stop being
# diagnosable.
#
# Fails CLOSED: refuse and mutate nothing.
CANONICAL_REQUIRES_EXECUTION_SETTLEMENT = "CANONICAL_POSITION_REQUIRES_EXECUTION_SETTLEMENT"


def _refuse_legacy_close(pos) -> dict | None:
    """A refusal dict when `pos` was filled by the venue book, else None.

    KEYED ON THE WIDER CONDITION, DELIBERATELY. `is_canonical` was tightened
    in A2 to require per-leg costs, a canonical epoch and an entry execution
    id in addition to the venue-book fill — the right test for CLASSIFYING a
    position, and the wrong one for this guard. What makes the legacy path
    unsafe is that it settles at whatever price it is handed, so everything
    with a venue-book fill must be refused, including a hybrid that has one
    without the rest. Narrowing this to track the classifier would have
    silently reopened the exact hole the guard exists to close.

    A guard fails CLOSED by refusing MORE, never less.
    """
    try:
        from lib.canonical_entry import has_canonical_fill
    except Exception:
        return None
    if not has_canonical_fill(pos):
        return None
    return {
        "error": CANONICAL_REQUIRES_EXECUTION_SETTLEMENT,
        "detail": ("this position was opened by the venue-book executor and "
                   "must be closed through canonical execution settlement; "
                   "the legacy path would settle it at the price handed in, "
                   "which is the mark-as-fill behaviour canonical entry "
                   "exists to remove"),
        "ok": False,
    }

def close_paper_position(pos_id: str, close_price: float, reason: str = "manual") -> dict:
    """Close a paper position and record the trade."""
    # EVIDENCE_ONLY forbids economic mutation AT THE MUTATION, not at the
    # caller — a caller that forgets to ask is exactly the one that would
    # open a position in a mode that forbade it.
    from lib.runtime_mode import forbid_economic_mutation
    forbid_economic_mutation("close_paper_position")
    with get_db() as _db:
        _pos = _db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        _refusal = _refuse_legacy_close(_pos) if _pos is not None else None
    if _refusal:
        logger.warning("[Paper] refusing legacy close of canonical position %s", pos_id)
        return _refusal

    result = {}
    log_symbol = ""
    log_direction = ""
    log_pnl = 0.0
    log_pct = 0.0

    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}

        pos_symbol    = pos.symbol
        pos_direction = pos.direction
        pos_side      = pos.side
        pos_asset_cls = pos.asset_class
        pos_signal_id = pos.signal_id
        pos_opened_at = pos.opened_at
        pos_notional  = float(pos.notional or 0)
        pos_margin    = float(pos.margin_used or DEFAULT_POSITION_SIZE)

        entry  = float(pos.entry_price)
        qty    = float(pos.qty)
        lev    = float(pos.leverage or 1.0)
        side   = 1 if pos_side == "long" else -1

        if not _price_move_is_plausible(entry, close_price):
            logger.error(
                f"[Paper] Rejected close for {pos_symbol}: entry=${entry:.6g} candidate_close=${close_price:.6g} "
                f"is a {close_price / entry if entry else 0:.1f}x move — almost certainly a bad price (symbol "
                f"collision or stale/wrong data source), not a real market move. Position left open."
            )
            return {"error": f"Rejected implausible close price for {pos_symbol}: ${close_price:.6g} vs entry ${entry:.6g}"}

        gross, _ = _calc_pnl(entry, close_price, qty, side, lev, pos_margin, symbol=pos_symbol)
        # The round trip reserved at open is charged here. Without it the book
        # kept the losing side of the ledger optional.
        pos_fees = float(getattr(pos, "fees", 0.0) or 0.0)
        # Perpetual funding, which only exists once a trade is HELD. It is
        # rounding error on a scalp and a real cost on the 1D setups this
        # book runs, whose own estimate is a 1-4 week hold: at the standard
        # 0.01%/8h that is 0.84% of notional over four weeks, and notional is
        # leveraged. Charged at close because it accrues with time, unlike
        # the fee, which is known at open.
        funding = _funding_cost_usd(pos_symbol, qty * entry, side,
                                    getattr(pos, "opened_at", None))
        pos_fees += funding
        pnl = gross - pos_fees
        pnl_pct = (pnl / pos_margin * 100) if pos_margin else 0.0

        portfolio = _get_portfolio_cash(db)
        portfolio.cash         += pos_margin + pnl
        portfolio.realized_pnl  = (portfolio.realized_pnl or 0) + pnl
        portfolio.total_trades  = (portfolio.total_trades or 0) + 1
        if pnl > 0:
            portfolio.winning_trades = (portfolio.winning_trades or 0) + 1
        portfolio.updated_at = _now()

        from app.database import new_id
        trade = PaperTrade(
            id           = new_id(),
            position_id  = pos_id,
            symbol       = pos_symbol,
            asset_class  = pos_asset_cls,
            direction    = pos_direction,
            side         = pos_side,
            leverage     = lev,
            qty          = qty,
            entry_price  = entry,
            exit_price   = close_price,
            notional     = pos_notional,
            gross_pnl    = round(gross, 6),
            fees         = round(pos_fees, 6),
            fee_basis    = getattr(pos, "fee_basis", None),
            realized_pnl = pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos_signal_id,
            opened_at    = pos_opened_at,
            closed_at    = _now(),
        )
        db.add(trade)

        pos.status         = "Closed"
        pos.current_price  = close_price
        pos.unrealized_pnl = pnl
        pos.updated_at     = _now()

        result = {
            "ok": True, "symbol": pos_symbol, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price
        }
        log_symbol     = pos_symbol
        log_direction  = pos_direction
        log_asset_cls  = pos_asset_cls
        log_entry      = entry
        log_qty        = qty
        log_pnl        = pnl
        log_pct        = pnl_pct
        # Pull signal metadata for learning engine
        log_timeframe  = "4H"
        log_confidence = None
        log_reasoning  = None
        if pos_signal_id:
            try:
                from app.database import TradingSignal
                sig_row = db.query(TradingSignal).filter(TradingSignal.id == pos_signal_id).first()
                if sig_row:
                    log_timeframe  = sig_row.timeframe or "4H"
                    log_confidence = float(sig_row.confidence) if sig_row.confidence else None
                    log_reasoning  = sig_row.reasoning or None
            except Exception:
                pass

    # ── Record to Learning Engine (Tiers 1-5) ───────────────────────────
    try:
        _record_outcome(
            symbol=log_symbol,
            asset_class=log_asset_cls,
            direction=log_direction,
            entry_price=log_entry,
            exit_price=close_price,
            qty=log_qty,
            exit_reason=reason,
            timeframe=log_timeframe,
            signal_confidence=log_confidence,
            signal_reasoning=log_reasoning,
            ta_profile=None,   # TA not re-fetched at paper close — populated next cycle
            market_regime=None,
            # THE LINK. `pos_signal_id` was read at the top of this function
            # and then not passed, so every paper outcome arrived at learning
            # detached from the signal that produced it. Without it an
            # outcome cannot be attributed to a strategy, timeframe, setup,
            # signal source or model version — it becomes an anonymous
            # win/loss that can only teach an aggregate.
            signal_id=pos_signal_id,
            paper_mode=True,
        )
    except Exception as _le:
        logger.warning(f"[Paper][Learning] record_outcome failed: {_le}")
    logger.info(f"[Paper] Closed {log_symbol} ({log_direction}) @ ${close_price:.4f} | P&L=${log_pnl:.2f} ({log_pct:.1f}%) | {reason}")
    return result


def partial_close_paper_position(pos_id: str, close_fraction: float, close_price: float, reason: str = "scale_out") -> dict:
    """Realize P&L on a fraction of an open paper position and keep the rest
    open with reduced size — mirrors close_paper_position but for a partial
    exit (e.g. locking in profit at an intermediate target). The remaining
    qty/notional/margin are reduced proportionally; the position stays Open."""
    # EVIDENCE_ONLY forbids economic mutation AT THE MUTATION, not at the
    # caller — a caller that forgets to ask is exactly the one that would
    # open a position in a mode that forbade it.
    from lib.runtime_mode import forbid_economic_mutation
    forbid_economic_mutation("partial_close_paper_position")
    if not (0 < close_fraction < 1):
        return {"error": "close_fraction must be between 0 and 1 (exclusive)"}

    with get_db() as _db:
        _pos = _db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        _refusal = _refuse_legacy_close(_pos) if _pos is not None else None
    if _refusal:
        logger.warning("[Paper] refusing legacy partial close of canonical position %s", pos_id)
        return _refusal

    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}
        if bool(pos.scaled_out):
            return {"error": "Position has already been scaled out"}

        pos_symbol    = pos.symbol
        pos_direction = pos.direction
        pos_side      = pos.side
        pos_asset_cls = pos.asset_class
        pos_signal_id = pos.signal_id
        pos_opened_at = pos.opened_at

        entry = float(pos.entry_price)
        qty   = float(pos.qty)
        lev   = float(pos.leverage or 1.0)
        side  = 1 if pos_side == "long" else -1
        notional = float(pos.notional or 0)
        margin   = float(pos.margin_used or DEFAULT_POSITION_SIZE)

        close_qty   = qty * close_fraction
        remain_qty  = qty - close_qty
        close_margin   = margin * close_fraction
        remain_margin  = margin - close_margin
        close_notional = notional * close_fraction
        remain_notional = notional - close_notional
        if close_qty <= 0 or remain_qty <= 0:
            return {"error": "Position too small to split"}

        if not _price_move_is_plausible(entry, close_price):
            logger.error(
                f"[Paper] Rejected partial close for {pos_symbol}: entry=${entry:.6g} candidate_close=${close_price:.6g} "
                f"is a {close_price / entry if entry else 0:.1f}x move — almost certainly a bad price, not a real "
                f"market move. Position left open, unmodified."
            )
            return {"error": f"Rejected implausible close price for {pos_symbol}: ${close_price:.6g} vs entry ${entry:.6g}"}

        gross, _ = _calc_pnl(entry, close_price, close_qty, side, lev, close_margin, symbol=pos_symbol)
        # Only the fraction being closed pays its share of the round trip;
        # the remainder keeps its reserve for when the runner is closed.
        total_qty = float(pos.qty or 0) or close_qty
        pos_fees_all = float(getattr(pos, "fees", 0.0) or 0.0)
        share = (close_qty / total_qty) if total_qty else 1.0
        close_fees = pos_fees_all * share
        pnl = gross - close_fees
        pnl_pct = (pnl / close_margin * 100) if close_margin else 0.0

        portfolio = _get_portfolio_cash(db)
        portfolio.cash        += close_margin + pnl
        portfolio.realized_pnl = (portfolio.realized_pnl or 0) + pnl
        portfolio.total_trades = (portfolio.total_trades or 0) + 1
        if pnl > 0:
            portfolio.winning_trades = (portfolio.winning_trades or 0) + 1
        portfolio.updated_at = _now()

        from app.database import new_id
        db.add(PaperTrade(
            id           = new_id(),
            position_id  = pos_id,
            symbol       = pos_symbol,
            asset_class  = pos_asset_cls,
            direction    = pos_direction,
            side         = pos_side,
            leverage     = lev,
            qty          = close_qty,
            entry_price  = entry,
            exit_price   = close_price,
            notional     = close_notional,
            gross_pnl    = round(gross, 6),
            fees         = round(close_fees, 6),
            fee_basis    = getattr(pos, "fee_basis", None),
            realized_pnl = pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos_signal_id,
            opened_at    = pos_opened_at,
            closed_at    = _now(),
        ))

        pos.qty            = remain_qty
        pos.notional        = remain_notional
        pos.margin_used     = remain_margin
        # The closed fraction's share of the round trip has been paid; the
        # runner carries only what is left, or closing it would charge the
        # same fee twice.
        pos.fees             = round(max(0.0, pos_fees_all - close_fees), 6)
        pos.scaled_out       = True
        pos.scaled_out_qty   = close_qty
        pos.updated_at       = _now()

        result = {
            "ok": True, "symbol": pos_symbol, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price,
            "closed_qty": close_qty, "remaining_qty": remain_qty,
        }

    try:
        _record_outcome(
            symbol=pos_symbol, asset_class=pos_asset_cls, direction=pos_direction,
            entry_price=entry, exit_price=close_price, qty=close_qty,
            exit_reason=reason,
            # A partial exit is still evidence about the SAME thesis.
            signal_id=pos_signal_id,
            paper_mode=True,
        )
    except Exception as _le:
        logger.warning(f"[Paper][Learning] partial-close record_outcome failed: {_le}")

    logger.info(
        f"[Paper] Scaled out {pos_symbol} ({pos_direction}): closed {close_qty:.6g} @ ${close_price:.4f} "
        f"| P&L=${pnl:.2f} ({pnl_pct:.1f}%) | {remain_qty:.6g} remaining"
    )
    return result


def mark_to_market(prices: dict) -> dict:
    """
    Update unrealized P&L for all open paper positions.
    prices = {symbol: current_price}
    Auto-triggers stop-loss / take-profit / margin-call checks.
    """
    closed  = []
    updated = []
    # Triggers whose settlement was REFUSED — most often a canonical
    # threshold the contract's own book did not confirm. Reported under its
    # own name so nothing downstream mistakes a refusal for a trade.
    refused = []

    with get_db() as db:
        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = [
            {
                "id":           p.id,
                "symbol":       p.symbol,
                "entry_price":  float(p.entry_price or 0),
                "qty":          float(p.qty or 0),
                "side":         p.side or "long",
                "leverage":     float(p.leverage or 1.0),
                "target_price": float(p.target_price or 0),
                "stop_loss":    float(p.stop_loss or 0),
                "notional":     float(p.notional or 0),
                "margin_used":  float(p.margin_used or DEFAULT_POSITION_SIZE),
                "direction":    p.direction or "Long",
            }
            for p in positions
            if p.entry_price and p.qty
        ]

    for pos in pos_list:
        sym = pos["symbol"]
        # Try multiple price lookup variants
        price = (
            prices.get(sym) or
            prices.get(sym.replace("/USD", "")) or
            prices.get(sym.replace("/", "") + "USD") or
            prices.get(sym.replace("/", ""))
        )
        # Futures/Forex fallback — covers GC=F, EURUSD=X, CL=F, etc.
        if not price:
            try:
                from lib.futures_data import get_cached_futures_price, FUTURES_UNIVERSE
                if sym in FUTURES_UNIVERSE:
                    fd = get_cached_futures_price(sym)
                    if fd and fd.get("price"):
                        price = float(fd["price"])
            except Exception:
                pass

        # Also check with and without = suffix (yfinance oddities)
        if not price:
            for alt in [sym.upper(), sym.replace("=X",""), sym.replace("=F","")]:
                if alt in prices and prices[alt]:
                    price = float(prices[alt])
                    break

        if not price:
            logger.debug(f"[Paper] No price in MTM for {sym}")
            continue

        entry  = pos["entry_price"]
        qty    = pos["qty"]
        lev    = pos["leverage"]
        margin = pos["margin_used"]
        side   = 1 if pos["side"] == "long" else -1

        if not _price_move_is_plausible(entry, price):
            logger.error(
                f"[Paper] Rejected MTM price for {sym}: entry=${entry:.6g} candidate=${price:.6g} "
                f"is a {price / entry if entry else 0:.1f}x move — almost certainly a symbol collision or bad "
                f"tick from an upstream price source, not a real move. Skipping this cycle; position stays open "
                f"at its last known-good price."
            )
            continue

        # THE UNIT BASIS DECIDES THE DOLLARS. `_calc_pnl` prices qty as
        # COINS, which is right for the legacy book and wrong by 100x for a
        # 26-contract PBTC position at a 0.01 multiplier. The canonical
        # basis comes from the frozen ledger; legacy keeps legacy
        # arithmetic exactly.
        from lib import paper_mark_economics as PME
        _basis = PME.basis_for_position(pos["id"])
        if _basis is not None and _basis.route == PME.CANONICAL:
            _gross = PME.gross_at_mark(_basis, price)
            if _gross is None:
                logger.debug("[Paper] %s: no canonical mark economics", sym)
                continue
            pnl = _gross
            pct = PME.return_pct_on_margin(_basis, _gross) or 0.0
        else:
            pnl, pct = _calc_pnl(entry, price, qty, side, lev, margin,
                                 symbol=sym)

        # Trigger checks — MUST respect side direction:
        # LONG:  stop when price falls BELOW stop_loss, profit when price rises ABOVE target
        # SHORT: stop when price rises ABOVE stop_loss, profit when price falls BELOW target
        reason = None
        stop   = pos["stop_loss"]
        target = pos["target_price"]

        if side == 1:   # LONG
            if stop  > 0 and price <= stop:    reason = "stop_loss"
            elif target > 0 and price >= target: reason = "take_profit"
        else:           # SHORT
            if stop  > 0 and price >= stop:    reason = "stop_loss"
            elif target > 0 and price <= target: reason = "take_profit"

        # Margin call: equity in position (margin + pnl) < 15% of original margin (lost 85%)
        equity_in_pos = margin + pnl
        if margin > 0 and equity_in_pos < margin * MARGIN_CALL_THRESHOLD:
            reason = "margin_call"

        if reason:
            # THE MARK GOT US HERE; IT DOES NOT SETTLE. Routed through the
            # dispatcher: a legacy position closes exactly as before, and a
            # canonical one re-confirms the threshold on its OWN executable
            # book before any order exists — a cross-market print must not
            # liquidate a contract whose book never reached the level.
            from lib.exit_dispatch import request_position_exit
            threshold = (stop if reason == "stop_loss"
                         else target if reason == "take_profit" else None)
            result = request_position_exit(
                pos["id"], caller_price=price, caller_reason=reason,
                caller_source="MARK_TO_MARKET", trigger_price=threshold)
            if result.get("ok"):
                closed.append({"symbol": sym, "reason": reason,
                               "pnl": result.get("pnl"),
                               "route": result.get("route")})
            else:
                # NOT CLOSED. Reporting an unconfirmed trigger as "closed
                # with pnl=None" is how a refusal becomes a phantom trade in
                # every dashboard downstream.
                refused.append({"symbol": sym, "reason": reason,
                                "error": result.get("error"),
                                "detail": result.get("detail")})
                logger.info("[Paper] %s %s not settled: %s", sym, reason,
                            result.get("error"))
        else:
            with get_db() as db:
                p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                if p:
                    p.current_price   = price
                    p.unrealized_pnl  = round(pnl, 2)
                    p.unrealized_pct  = round(pct, 2)
                    p.updated_at      = _now()
            updated.append(sym)

    return {"updated": len(updated), "closed": closed,
            "trigger_refused": refused}


def get_paper_summary() -> dict:
    """Return portfolio summary and open positions. Null-safe throughout."""
    with get_db() as db:
        portfolio = _get_portfolio_cash(db)
        p_data = {
            "cash":           round(float(portfolio.cash or 0), 2),
            "total_trades":   int(portfolio.total_trades or 0),
            "winning_trades": int(portfolio.winning_trades or 0),
            "realized_pnl":   round(float(portfolio.realized_pnl or 0), 2),
            "updated_at":     portfolio.updated_at,
        }

        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = []
        for p in positions:
            if not p.entry_price:
                continue
            try:
                pos_list.append({
                    "id":            p.id,
                    "symbol":        p.symbol or "",
                    "direction":     p.direction or "Long",
                    "side":          p.side or "long",
                    "leverage":      float(p.leverage or 1.0),
                    "qty":           float(p.qty or 0),
                    "entry_price":   float(p.entry_price or 0),
                    "current_price": float(p.current_price or p.entry_price or 0),
                    "target_price":  float(p.target_price or 0),
                    "stop_loss":     float(p.stop_loss or 0),
                    "notional":      float(p.notional or 0),
                    "margin_used":   float(p.margin_used or DEFAULT_POSITION_SIZE),
                    "unrealized_pnl":float(p.unrealized_pnl or 0),
                    "unrealized_pct":float(p.unrealized_pct or 0),
                    "opened_at":     p.opened_at.isoformat() if hasattr(p.opened_at, "isoformat") else (p.opened_at or ""),
                    "asset_class":   p.asset_class or "Equity",
                    "signal_id":     p.signal_id or "",
                })
            except Exception as e:
                logger.warning(f"[Paper] Skipping bad position row {p.id}: {e}")

        trades = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(50).all()
        trade_list = []
        for t in trades:
            try:
                trade_list.append({
                    "id":           t.id,
                    "symbol":       t.symbol or "",
                    "direction":    t.direction or "Long",
                    "side":         t.side or "long",
                    "leverage":     float(t.leverage or 1.0),
                    # How much was actually traded, and what it cost. Omitting
                    # qty left the closed-trades table with nothing to show
                    # for size, and the fee columns had no source at all.
                    "qty":          float(t.qty or 0),
                    "notional":     float(t.notional or 0),
                    "entry_price":  float(t.entry_price or 0),
                    "exit_price":   float(t.exit_price or 0),
                    "gross_pnl":    round(float(t.gross_pnl or 0), 2),
                    "fees":         round(float(t.fees or 0), 2),
                    "fee_basis":    t.fee_basis,
                    "realized_pnl": round(float(t.realized_pnl or 0), 2),
                    "pnl_pct":      round(float(t.pnl_pct or 0), 2),
                    "close_reason": t.close_reason or "manual",
                    "opened_at":    t.opened_at.isoformat() if hasattr(t.opened_at, "isoformat") else (t.opened_at or ""),
                    "closed_at":    t.closed_at.isoformat() if hasattr(t.closed_at, "isoformat") else (t.closed_at or ""),
                    "asset_class":  t.asset_class or "Equity",
                })
            except Exception as e:
                logger.warning(f"[Paper] Skipping bad trade row {t.id}: {e}")

    # Join signal data for paper positions (for TA/reasoning/news display)
    signal_ids = [p["signal_id"] for p in pos_list if p.get("signal_id")]
    signal_map = {}
    if signal_ids:
        try:
            from app.database import TradingSignal
            sigs = db.query(TradingSignal).filter(TradingSignal.id.in_(signal_ids)).all()
            for s in sigs:
                signal_map[s.id] = {
                    "id":             s.id,
                    "direction":      s.direction or "Long",
                    "confidence":     float(s.confidence or 0),
                    "composite_score":float(getattr(s, "composite_score", None) or s.confidence or 0),
                    "timeframe":      s.timeframe or "",
                    "reasoning":      s.reasoning or "",
                    "key_risks":      getattr(s, "key_risks", None) or "",
                    "momentum":       getattr(s, "momentum", None) or "",
                    "signal_source":  getattr(s, "signal_source", None) or "watchlist",
                    "generated_at":   s.generated_at.isoformat() if hasattr(s.generated_at, "isoformat") else (s.generated_at or ""),
                    "entry_price":    float(s.entry_price or 0),
                    "target_price":   float(s.target_price or 0),
                    "stop_loss":      float(s.stop_loss or 0),
                    "status":         s.status or "",
                    "trigger_event":  getattr(s, "trigger_event", None) or "",
                }
        except Exception as e:
            logger.warning(f"[Paper] Could not join signals: {e}")

    # Attach signal context to each position
    for p in pos_list:
        p["signal"] = signal_map.get(p.get("signal_id"), None)

    open_pnl  = sum(p["unrealized_pnl"] for p in pos_list)
    margin_in = sum(p["margin_used"] for p in pos_list)
    # Equity = cash on hand + margin deployed + any unrealized gains/losses
    # (margin is still your capital — just locked in positions, not lost)
    equity    = p_data["cash"] + margin_in + open_pnl
    total     = p_data["total_trades"]
    wins      = p_data["winning_trades"]
    win_rate  = round(wins / total * 100, 1) if total > 0 else 0.0

    return {
        "portfolio": {
            **p_data,
            "open_pnl":          round(open_pnl, 2),
            "equity":            round(equity, 2),
            "margin_in_use":     round(margin_in, 2),
            "win_rate":          win_rate,
            "starting_capital":  PAPER_STARTING_CAPITAL,
            "total_return_pct":  round((equity - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100, 2),
        },
        "positions": pos_list,
        "trades":    trade_list,
    }


def soft_reset_paper_portfolio(starting_cash: float = None) -> dict:
    """Reset the FUNDS while keeping every trade the book ever made.

    The hard reset below deletes PaperTrade rows — which are learning data:
    outcomes, calibration and the failure postmortems all read them. Wiping
    the ledger to refill the wallet destroys measurements to move a number.

    This version: every open position is closed through the normal close
    path at its last known price (so it lands in history as a real trade,
    tagged 'reset'), then the portfolio row alone is re-seeded. The stats
    counters restart at zero — they describe the NEW book — while the old
    book's rows remain queryable forever. The reset itself is recorded in
    the AI decision log so an equity curve that suddenly jumps to the
    starting balance has a visible, timestamped explanation.
    """
    cash = float(starting_cash or PAPER_STARTING_CAPITAL)
    closed = []
    with get_db() as db:
        # OPEN rows only. This walked every PaperPosition ever written — 516
        # rows to close 14 — and `close_paper_position` refuses non-Open
        # positions, so the other 502 were round-trips to the database to be
        # told no. Harmless, and it made a reset look hung.
        # Lowercased on both sides: every writer stores "Open" and SQLite's
        # `=` is case-sensitive, which is the exact bug that left this
        # guard's sibling inert for a week.
        open_ids = [(p.id, p.current_price or p.entry_price)
                    for p in db.query(PaperPosition).filter(
                        func.lower(PaperPosition.status) == "open").all()]
    from lib.exit_dispatch import request_position_exit
    failed = []
    for pos_id, price in open_ids:
        try:
            r = request_position_exit(pos_id, caller_price=float(price or 0),
                                      caller_reason="reset",
                                      caller_source="SOFT_RESET")
            ok = bool(r.get("ok"))
            closed.append({"id": pos_id, "ok": ok})
            if not ok:
                failed.append({"id": pos_id, "error": r.get("error"),
                               "detail": r.get("detail")})
        except Exception as e:
            logger.warning(f"[Paper] soft reset: close {pos_id} failed: {e}")
            closed.append({"id": pos_id, "ok": False})
            failed.append({"id": pos_id, "error": "EXCEPTION",
                           "detail": str(e)[:200]})

    # NEVER FRESH CASH + OLD OPEN EXPOSURE.
    #
    # Closing N positions against N live markets is not one transaction, and
    # pretending otherwise is not the safety property that matters. What
    # matters is that the RESEED — which invents capital — happens only when
    # nothing is still exposed. A canonical position whose book was stale
    # cannot be closed right now; reseeding around it would leave the book
    # holding real risk against a wallet that says it is flat.
    #
    # Positions that DID close stay closed. That is honest economic history,
    # and a later reset may finish the job.
    with get_db() as db:
        still_open = [p.id for p in db.query(PaperPosition).filter(
            func.lower(PaperPosition.status) == "open").all()]
    if still_open:
        logger.error("[Paper] soft reset INCOMPLETE: %d position(s) remain "
                     "open; refusing to reseed capital over live exposure",
                     len(still_open))
        return {"ok": False, "error": "RESET_INCOMPLETE",
                "detail": (f"{len(still_open)} position(s) could not be "
                           f"closed; capital was NOT reseeded because fresh "
                           f"cash beside open exposure is not a reset"),
                "successfully_closed": [c["id"] for c in closed if c["ok"]],
                "failed": failed, "still_open": still_open,
                "cash_reseeded": False}

    from app.database import new_id
    with get_db() as db:
        # Only the PORTFOLIO row is replaced. Trades stay.
        db.query(PaperPortfolio).delete()
        db.add(PaperPortfolio(
            id=new_id(), cash=cash, total_trades=0, winning_trades=0,
            realized_pnl=0.0, updated_at=_now(),
        ))
    try:
        from lib.learning_engine import log_decision
        log_decision("paper", "RESET",
                     f"Funds soft-reset to ${cash:,.0f}; {len(closed)} open "
                     f"position(s) closed into history; trade rows preserved",
                     thinking=False)
    except Exception:
        pass
    logger.info(f"[Paper] Soft reset: cash=${cash:,.0f}, "
                f"{len(closed)} positions closed, history preserved")
    return {"ok": True, "cash": cash, "positions_closed": len(closed),
            "history_preserved": True, "cash_reseeded": True}


CANONICAL_LEDGER_REQUIRES_EPOCH_RESET = "CANONICAL_LEDGER_REQUIRES_EPOCH_RESET"


def _canonical_book_present() -> bool:
    """Does this database hold any CANONICAL economic record at all?

    Four independent witnesses, because they can outlive one another: a
    settlement header, a settlement leg, a realized outcome, or a still-open
    position carrying a canonical fill. A closed canonical trade leaves no
    open position but still leaves legs and an outcome, so "the open book is
    empty" is not evidence the ledger is.

    Schema-absent is not canonical. On the operator's unmigrated database
    the tables do not exist, this answers False, and the legacy reset
    behaves exactly as it always has. The probe INSPECTS; it never creates a
    table and never calls init_db.
    """
    from lib import exit_dispatch as ED
    if not ED._canonical_ledger_available():
        return False
    from app.database import (PaperPosition, PaperPositionSettlement,
                              PaperRealizedOutcome, PaperSettlementLeg)
    from lib.canonical_entry import has_canonical_fill
    with get_db() as db:
        for model in (PaperPositionSettlement, PaperSettlementLeg,
                      PaperRealizedOutcome):
            if db.query(model).first() is not None:
                return True
        # has_canonical_fill is the WIDE claim on purpose: a hybrid whose
        # fill crossed a real book is exactly the record a legacy wipe
        # would silently destroy half of.
        for pos in db.query(PaperPosition).all():
            if has_canonical_fill(pos):
                return True
    return False


def reset_paper_portfolio() -> dict:
    """Reset the paper portfolio back to $100k starting capital.

    DESTRUCTIVE: deletes every PaperTrade and PaperPosition row — the
    learning data, not just the wallet. Prefer soft_reset_paper_portfolio,
    which refills the funds and keeps the history.

    IT REFUSES A CANONICAL BOOK. This deletes positions, trades and the
    portfolio and knows nothing about settlement headers, legs or realized
    outcomes. Run against a canonical ledger it would leave those rows
    pointing at positions that no longer exist — half an economic record,
    which is a worse state than either a clean book or an intact one. A
    canonical book is retired by an EPOCH RESET that closes the ledger it
    actually has, not by deleting one side of it.
    """
    from app.database import new_id
    if _canonical_book_present():
        logger.warning("[Paper] hard reset REFUSED: this book has a "
                       "canonical settlement ledger")
        return {"ok": False, "error": CANONICAL_LEDGER_REQUIRES_EPOCH_RESET,
                "detail": ("this database holds canonical settlement "
                           "records; the legacy hard reset deletes "
                           "positions and trades but not the ledger, "
                           "which would orphan it. Use the soft reset, or "
                           "a canonical epoch reset.")}
    with get_db() as db:
        db.query(PaperTrade).delete()
        db.query(PaperPosition).delete()
        db.query(PaperPortfolio).delete()
        db.flush()
        db.add(PaperPortfolio(
            id=new_id(),
            cash=PAPER_STARTING_CAPITAL,
            total_trades=0,
            winning_trades=0,
            realized_pnl=0.0,
            updated_at=_now()
        ))
    logger.info("[Paper] Portfolio hard reset to $100,000")
    return {"ok": True, "cash": PAPER_STARTING_CAPITAL}
