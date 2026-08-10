"""Realized-performance analytics: Sharpe ratio, max drawdown, and win-rate
breakdown by signal source — computed from real historical data (portfolio
equity snapshots and closed trade outcomes), not placeholders."""

from __future__ import annotations

import math
from statistics import mean, pstdev


def daily_equity_curve(snapshots: list[dict]) -> list[tuple[str, float]]:
    """Collapse frequent PortfolioSnapshot rows (every ~5 min) down to one
    point per UTC day (the last snapshot of that day), so drawdown/Sharpe
    reflect day-to-day portfolio moves instead of intraday noise. Expects
    snapshots sorted ascending by snapshot_at, each {"snapshot_at": iso, "equity": float}.
    """
    by_day: dict[str, float] = {}
    for row in snapshots:
        ts = row.get("snapshot_at") or ""
        equity = row.get("equity")
        if not ts or equity is None:
            continue
        day = ts[:10]
        by_day[day] = float(equity)  # later same-day rows overwrite — keeps the last
    return sorted(by_day.items())


def compute_max_drawdown(equity_curve: list[tuple[str, float]]) -> dict:
    """Largest peak-to-trough decline in an equity curve. Returns a real
    computation instead of a hardcoded stub."""
    if len(equity_curve) < 2:
        return {"max_drawdown_pct": 0.0, "peak_date": None, "trough_date": None}

    peak_value = equity_curve[0][1]
    peak_date = equity_curve[0][0]
    max_dd = 0.0
    max_dd_peak_date = peak_date
    max_dd_trough_date = peak_date

    for date, equity in equity_curve:
        if equity > peak_value:
            peak_value = equity
            peak_date = date
        if peak_value > 0:
            dd = (peak_value - equity) / peak_value * 100
            if dd > max_dd:
                max_dd = dd
                max_dd_peak_date = peak_date
                max_dd_trough_date = date

    return {
        "max_drawdown_pct": round(max_dd, 2),
        "peak_date": max_dd_peak_date,
        "trough_date": max_dd_trough_date,
    }


def compute_sharpe_ratio(equity_curve: list[tuple[str, float]], periods_per_year: float = 365.0) -> float | None:
    """Annualized Sharpe ratio from day-over-day equity curve returns
    (risk-free rate assumed 0). Returns None when there isn't enough data
    to compute a meaningful ratio (fewer than 3 points, or zero variance)."""
    if len(equity_curve) < 3:
        return None
    returns = []
    for (_, prev), (_, curr) in zip(equity_curve, equity_curve[1:]):
        if prev:
            returns.append((curr - prev) / prev)
    if len(returns) < 2:
        return None
    std = pstdev(returns)
    if std < 1e-9:  # effectively zero variance — float rounding, not real signal
        return None
    sharpe = (mean(returns) / std) * math.sqrt(periods_per_year)
    return round(sharpe, 3)


def signal_source_breakdown(trade_outcomes: list[dict]) -> list[dict]:
    """Win rate / avg P&L grouped by originating signal_source (llm-generated
    watchlist signals vs. deterministic ta_fallback vs. opportunistic scanner
    signals), so an operator can see which source is actually working.
    Each row in `trade_outcomes` is expected to have "signal_source" (already
    joined from TradingSignal) and "pnl_pct" / "outcome" fields.
    """
    groups: dict[str, list[dict]] = {}
    for row in trade_outcomes:
        source = row.get("signal_source") or "unknown"
        groups.setdefault(source, []).append(row)

    result = []
    for source, rows in groups.items():
        wins = sum(1 for r in rows if r.get("outcome") == "WIN")
        losses = sum(1 for r in rows if r.get("outcome") == "LOSS")
        pnl_pcts = [float(r.get("pnl_pct") or 0) for r in rows]
        decided = wins + losses
        result.append({
            "signal_source": source,
            "total": len(rows),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / decided * 100, 2) if decided else None,
            "avg_pnl_pct": round(sum(pnl_pcts) / len(pnl_pcts), 3) if pnl_pcts else 0.0,
        })
    result.sort(key=lambda r: r["total"], reverse=True)
    return result
