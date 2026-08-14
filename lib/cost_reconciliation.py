"""Modeled costs vs realized costs — the reconciliation instrument.

The gate's NO_TRADE on the desk's biggest cell (crypto/4H, ~3,900
samples, gross +0.07R at the lower bound) is decided entirely by the
cost model. A cost model wrong in either direction is expensive: too
cheap and the desk bleeds fees it didn't price; too dear and it refuses
edges it actually has. So the model is reconciled against the two
ground truths the desk owns:

  Kraken fills   the operator's REAL account, synced read-only — fee
                 divided by cost is the realized per-side rate, no
                 assumptions anywhere (measured 2026-08-15: exactly
                 0.4000%/side spot taker, matching the model)
  Alpaca fills   recorded actual_fill_price vs intended entry — the
                 realized slippage distribution per asset class

And one sensitivity the reconciliation exposed on arrival: unleveraged
crypto signals are costed on the SPOT schedule (0.40%/side) while the
operator's stated venue class is a 1-25x perpetual broker, which the
model itself prices ~16x cheaper (0.05% taker). Whether crypto/4H is
tradeable after costs depends on which schedule the fill will actually
pay — so both nets are computed side by side, labelled, and left for
the operator's venue decision to select between. Nothing here changes
any verdict; it shows what the verdict is made of.
"""
from __future__ import annotations

import logging
import statistics

logger = logging.getLogger(__name__)


def kraken_realized_fees() -> dict:
    """Per-side fee rates actually paid, from the synced fill ledger."""
    from sqlalchemy import text

    from app.database import engine

    rates = []
    by_pair: dict = {}
    with engine.connect() as c:
        for pair, cost, fee in c.execute(text(
                "SELECT pair, cost, fee FROM kraken_trades "
                "WHERE cost > 0 AND fee IS NOT NULL")):
            r = fee / cost
            rates.append(r)
            by_pair.setdefault(pair, []).append(r)
    if not rates:
        return {"n": 0, "note": "no synced fills"}
    return {
        "n": len(rates),
        "median_pct_per_side": round(statistics.median(rates) * 100, 4),
        "max_pct_per_side": round(max(rates) * 100, 4),
        "by_pair": {p: {"n": len(v),
                        "median_pct": round(statistics.median(v) * 100, 4)}
                    for p, v in sorted(by_pair.items())},
    }


def alpaca_realized_slippage() -> dict:
    """Recorded fill-vs-intent slippage, per asset class. Signed median
    tells direction (negative = filled worse); median-absolute tells
    magnitude the spread model should cover."""
    from sqlalchemy import text

    from app.database import engine

    by_class: dict = {}
    with engine.connect() as c:
        for cls, slip in c.execute(text(
                "SELECT COALESCE(asset_class,'unknown'), slippage_pct "
                "FROM trading_signals WHERE slippage_pct IS NOT NULL")):
            by_class.setdefault(str(cls).lower(), []).append(float(slip))
    out = {}
    for cls, vals in by_class.items():
        out[cls] = {
            "n": len(vals),
            "median_signed_pct": round(statistics.median(vals), 4),
            "median_abs_pct": round(statistics.median(
                [abs(v) for v in vals]), 4),
            "worst_pct": round(min(vals), 4),
        }
    return out or {"note": "no recorded fills"}


def _median_stop_distance_pct(asset_class: str, timeframe: str) -> float | None:
    """Representative stop distance for a cell, from its own signals —
    the number that converts %-of-notional costs into R."""
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        rows = [r for (r,) in c.execute(text("""
            SELECT ABS(entry_price - stop_loss) / entry_price
            FROM trading_signals
            WHERE LOWER(asset_class) = :cls AND timeframe = :tf
              AND entry_price > 0 AND stop_loss IS NOT NULL
              AND ABS(entry_price - stop_loss) / entry_price BETWEEN 0.001 AND 0.5
            ORDER BY generated_at DESC LIMIT 500"""),
            {"cls": asset_class, "tf": timeframe})]
    return statistics.median(rows) if len(rows) >= 20 else None


def cell_fee_sensitivity(asset_class: str = "crypto",
                         timeframe: str = "4H") -> dict:
    """The headline cell's net R under both fee schedules it might pay.

    Spot vs perpetual is a VENUE decision, not a model estimate — so both
    are computed against the cell's own measured gross lower bound and
    its own median stop distance, and labelled. The verdict column shows
    what the gate would say under each schedule at MIN_NET_R.
    """
    from lib.expectancy import MIN_NET_R, MIN_SAMPLE, _summarise, build_table
    from lib.transaction_costs import estimate_costs, net_expected_r

    table = build_table()
    cell = table.get((("asset_class", "timeframe"), (asset_class, timeframe)))
    if not cell or cell["n"] < MIN_SAMPLE:
        return {"note": f"no {asset_class}/{timeframe} cell with sample >= {MIN_SAMPLE}"}
    stats = _summarise(cell, ("asset_class", "timeframe"),
                       (asset_class, timeframe))

    stop_pct = _median_stop_distance_pct(asset_class, timeframe)
    if stop_pct is None:
        return {"note": "not enough signals to measure a stop distance"}

    # A representative symbol routes fee/spread lookups; BTC is the
    # deepest book, so its spread flatters slightly — stated, not hidden.
    symbol = "BTC/USD" if asset_class == "crypto" else "SPY"
    entry = 100.0
    stop = entry * (1 - stop_pct)
    out_rows = []
    for label, leveraged in (("spot", False), ("perpetual", True)):
        costs = estimate_costs(symbol, entry, stop, hold_hours=24.0,
                               is_short=False, leveraged=leveraged)
        net = net_expected_r(stats["gross_expected_r_lower"], costs or {})
        nr = net.get("net_expected_r")
        out_rows.append({
            "schedule": label,
            "cost_r": net.get("expected_cost_r"),
            "net_lower_r": nr,
            "gate_would_say": ("TRADE" if nr is not None and nr >= MIN_NET_R
                               else "NO_TRADE" if nr is not None else "UNKNOWN"),
        })
    return {
        "cell": f"{asset_class}/{timeframe}",
        "sample": stats["sample"],
        "gross_lower_r": stats["gross_expected_r_lower"],
        "median_stop_distance_pct": round(stop_pct * 100, 3),
        "representative_symbol": symbol,
        "schedules": out_rows,
        "note": ("which schedule applies is a venue decision, not a model "
                 "estimate — unleveraged signals are costed as spot today"),
    }


def reconciliation_summary() -> dict:
    return {
        "kraken_realized": kraken_realized_fees(),
        "alpaca_slippage": alpaca_realized_slippage(),
        "cell_sensitivity": cell_fee_sensitivity(),
        "note": ("modeled vs realized: Kraken fills are the fee ground "
                 "truth, recorded fills the slippage ground truth; the "
                 "sensitivity block shows what the headline cell's verdict "
                 "is made of. Read-only — nothing here changes a gate."),
    }
