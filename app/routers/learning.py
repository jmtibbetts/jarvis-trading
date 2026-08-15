"""Learning & evaluation routes — Phase 7 split of app/routes.py. Bodies are
verbatim; shared helpers live in app.routers.common."""
from fastapi import APIRouter

from app.routers.common import *  # noqa: F401,F403
from app.routers.common import _sig_dict  # noqa: E501

router = APIRouter()


@router.get("/learning/postmortems")
def get_postmortems(days: int = 45):
    """The failure memory: classified reasons for every terminally failed or
    cancelled signal, aggregated by reason and by symbol/setup bucket. This
    is what the scoring penalty (failure_penalty in score_breakdown) is
    computed from — see lib/postmortem.py for the taxonomy."""
    from lib.postmortem import aggregate_reasons, load_recent_postmortems

    rows = load_recent_postmortems(days=max(1, min(days, 365)))
    agg = aggregate_reasons(rows)
    worst_buckets = sorted(
        (
            {"symbol": k[0], "setup_type": k[1], "failures": sum(v.values()),
             "reasons": v, "dominant": max(v, key=v.get)}
            for k, v in agg["by_bucket"].items()
        ),
        key=lambda b: -b["failures"],
    )[:20]
    return {
        "window_days": days,
        "total_failures": agg["total"],
        "by_reason": agg["by_reason"],
        "worst_buckets": worst_buckets,
        "recent": rows[-50:],
        "note": (
            "Deterministic failure taxonomy — no LLM judgments. Buckets with "
            "5+ failures in the window actively penalize new signals of the "
            "same symbol/setup (failure_penalty in score_breakdown)."
        ),
    }


@router.get("/calibration")
def calibration_summary():
    """What the system has actually measured about its own accuracy.

    Exists so "conf 83%" can be challenged. Raw model confidence was
    INVERTED against outcomes — 90%+ signals won 28%, 60-69% signals won
    44% — so every number the UI shows is now the measured rate for the
    most specific bucket with enough evidence, and this endpoint is where
    that evidence can be read directly.
    """
    from lib.calibration import summary
    return summary()


@router.get("/score-variants")
def score_variants(gate: float = 55.0, timeframe: str | None = None):
    """How each shadow scoring variant would have selected, over resolved
    outcomes. A = the live composite (control), B = inverted diagnostic,
    C = component-calibrated from the 2026-08-13 decomposition.

    Retrospective and replay-weighted: this ranks variants for shadowing.
    Promotion requires the shadow rows accumulating on new signals plus
    outcomes that postdate the variant's definition.
    """
    from lib.score_variants import evaluate_variants
    return evaluate_variants(gate=gate, timeframe=timeframe)


@router.get("/gate-experiment")
def gate_experiment():
    """The legacy-vs-v8 scoreboard: both gates' picks over the SAME
    candidates, judged by the SAME counterfactual resolver. This is the
    evidence that decides keep-vs-revert at the end of the judgment
    window (HARDENING_PLAN: >=2 weeks or >=300 resolved per arm)."""
    from sqlalchemy import text as _t
    from app.database import engine

    # The window opened 2026-08-14 on a GENERATOR-ONLY candidate
    # population; the scanner began recording candidates 2026-08-16. Both
    # arms are reported on the original population by default so widening
    # coverage cannot move the goalposts mid-experiment — the wider view
    # is available beside it, and becomes the next window's baseline.
    WINDOW_SOURCE = "generator"

    def arm(col: str, source: str | None = WINDOW_SOURCE) -> dict:
        src_sql = ("AND COALESCE(source, 'generator') = :src"
                   if source else "")
        params = {"src": source} if source else {}
        # Stratified by timeframe because the arms COMPOSE differently:
        # measured 2026-08-15, v8's TRADE picks were 87% 1D futures while
        # its NO_TRADEs were mostly intraday — pooled arm stats would
        # compare daily corn replays against 15-minute crypto ones and
        # call it a verdict. effective_n counts distinct (symbol, day):
        # five same-day ZC=F rows are one market opinion, not five.
        with engine.connect() as c:
            row = c.execute(_t(f"""
                SELECT COUNT(*),
                       COUNT(DISTINCT symbol || '|' || date(created_at)),
                       SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END),
                       AVG(CASE WHEN resolved=1 AND pnl_pct > 0 THEN 100.0
                                WHEN resolved=1 THEN 0.0 END),
                       AVG(CASE WHEN resolved=1 THEN pnl_pct END),
                       AVG(CASE WHEN resolved=1 THEN mfe_r END)
                FROM candidate_signals
                WHERE {col} = 1 {src_sql}"""), params).fetchone()
            tf_rows = c.execute(_t(f"""
                SELECT timeframe, COUNT(*),
                       COUNT(DISTINCT symbol || '|' || date(created_at)),
                       SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END),
                       AVG(CASE WHEN resolved=1 AND pnl_pct > 0 THEN 100.0
                                WHEN resolved=1 THEN 0.0 END),
                       AVG(CASE WHEN resolved=1 THEN pnl_pct END)
                FROM candidate_signals
                WHERE {col} = 1 {src_sql}
                GROUP BY timeframe ORDER BY COUNT(*) DESC"""),
                params).fetchall()
        n, eff, res, wr, pnl, mfe = row
        return {"selected": int(n or 0), "effective_n": int(eff or 0),
                "resolved": int(res or 0),
                "win_rate": round(wr, 1) if wr is not None else None,
                "avg_pnl_pct": round(pnl, 3) if pnl is not None else None,
                "avg_mfe_r": round(mfe, 3) if mfe is not None else None,
                "by_timeframe": [
                    {"timeframe": tf, "selected": int(tn or 0),
                     "effective_n": int(teff or 0),
                     "resolved": int(tres or 0),
                     "win_rate": round(twr, 1) if twr is not None else None,
                     "avg_pnl_pct": round(tpnl, 3) if tpnl is not None else None}
                    for tf, tn, teff, tres, twr, tpnl in tf_rows]}

    with engine.connect() as c:
        overlap = c.execute(_t("""
            SELECT SUM(CASE WHEN gate_legacy_take=1 AND gate_v8_take=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN gate_legacy_take=1 AND gate_v8_take=0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN gate_legacy_take=0 AND gate_v8_take=1 THEN 1 ELSE 0 END),
                   COUNT(*)
            FROM candidate_signals
            WHERE gate_legacy_take IS NOT NULL AND gate_v8_take IS NOT NULL
        """)).fetchone()
        decisions = c.execute(_t("""
            SELECT gate_v8_decision, COUNT(*) FROM candidate_signals
            WHERE gate_v8_decision IS NOT NULL GROUP BY gate_v8_decision
        """)).fetchall()
        by_source = [
            {"source": s or "generator", "candidates": n,
             "with_gate_verdict": g or 0, "resolved": r or 0}
            for s, n, g, r in c.execute(_t("""
                SELECT COALESCE(source, 'generator'), COUNT(*),
                       SUM(CASE WHEN gate_v8_decision IS NOT NULL THEN 1 ELSE 0 END),
                       SUM(resolved)
                FROM candidate_signals
                GROUP BY COALESCE(source, 'generator')"""))]
    return {
        "note": ("both arms judged by the same counterfactual resolver; "
                 "non-executed picks resolve with perfect fills — the bias "
                 "applies to both arms equally. Compare arms WITHIN a "
                 "timeframe row, never across the pooled headline: the "
                 "arms compose differently by timeframe, and effective_n "
                 "(distinct symbol-days) is the honest sample size."),
        "window_population": WINDOW_SOURCE,
        "window_note": ("arms below cover the population the window OPENED "
                        "on (generator-only, 2026-08-14). The scanner began "
                        "recording candidates 2026-08-16; its rows appear in "
                        "by_source and become the next window's baseline "
                        "rather than moving this one's goalposts."),
        "by_source": by_source,
        "legacy_all_sources": arm("gate_legacy_take", source=None),
        "v8_all_sources": arm("gate_v8_take", source=None),
        "legacy": arm("gate_legacy_take"),
        "v8": arm("gate_v8_take"),
        "overlap": {"both_take": int(overlap[0] or 0),
                    "legacy_only": int(overlap[1] or 0),
                    "v8_only": int(overlap[2] or 0),
                    "candidates_with_both_verdicts": int(overlap[3] or 0)},
        "v8_decision_mix": {d: int(n) for d, n in decisions},
    }


@router.get("/candidates/selection-bias")
def candidates_selection_bias():
    """Rejected vs accepted candidates on resolved counterfactuals — the
    direct measurement of whether the filters discard winners. Sparse until
    the resolution job has had time to work through the backlog."""
    from lib.candidates import selection_bias_summary
    return selection_bias_summary()


@router.get("/promotion/status")
def promotion_status(gate: float = 55.0):
    """§4.3 scoreboard: every shadow variant vs the current champion on
    out-of-sample resolved candidates, criterion by criterion. INSUFFICIENT
    until the forward stream has enough span and sample — by design."""
    from lib.promotion import evaluate_promotion
    return evaluate_promotion(gate=gate)


@router.get("/promotion/champions")
def promotion_champions():
    """The append-only champion ledger — who held live-scoring authority,
    since when, and on what frozen evidence."""
    from lib.promotion import champion_history
    return {"champions": champion_history()}


@router.get("/cost-reconciliation")
def cost_reconciliation():
    """Modeled vs realized costs: Kraken fill ledger (fee ground truth),
    recorded slippage, and the headline cell's net R under both fee
    schedules it might pay. Read-only — shows what verdicts are made of."""
    from lib.cost_reconciliation import reconciliation_summary
    return reconciliation_summary()


@router.get("/learning/model-comparison")
def llm_model_comparison():
    """Outcomes grouped by the LLM that AUTHORED each signal (stamped
    from the response at generation). The question this answers: did
    swapping the LM Studio load change signal quality? Rows accumulate
    from the moment attribution landed; pre-attribution history is
    honestly 'unattributed', never guessed."""
    from sqlalchemy import text as _t

    from app.database import engine

    out = {"models": [], "note": ("attribution starts 2026-08-16; older "
                                  "signals are unattributed by design")}
    with engine.connect() as c:
        for model, n_sig, n_out, wr, pnl, mfe in c.execute(_t("""
            SELECT COALESCE(s.llm_model, 'unattributed'),
                   COUNT(DISTINCT s.id), COUNT(o.id),
                   ROUND(AVG(CASE WHEN o.pnl_pct > 0 THEN 100.0
                                  WHEN o.pnl_pct IS NOT NULL THEN 0.0 END), 1),
                   ROUND(AVG(o.pnl_pct), 3), ROUND(AVG(o.mfe_r), 3)
            FROM trading_signals s
            LEFT JOIN trade_outcomes o ON o.signal_id = s.id
            GROUP BY COALESCE(s.llm_model, 'unattributed')
            ORDER BY COUNT(DISTINCT s.id) DESC
        """)):
            out["models"].append({
                "model": model, "signals": n_sig, "outcomes": n_out,
                "win_rate": wr, "avg_pnl_pct": pnl, "avg_mfe_r": mfe})
    return out


@router.get("/context-ablation")
def context_ablation():
    """Does the macro context a candidate was born under predict how it
    resolved? Per-feature/bucket/direction stats over resolved
    candidates with stored context. Fills itself as the corpus matures;
    thin buckets are flagged, not hidden."""
    from lib.context_ablation import ablation_summary
    return ablation_summary()


@router.get("/feature-snapshots/summary")
def feature_snapshots_summary():
    """P4 corpus state: clock-driven snapshots by quality, labels by
    horizon and status. The unbiased training corpus, accumulating."""
    from lib.feature_snapshots import snapshot_summary
    return snapshot_summary()


@router.get("/scenarios/{symbol}")
def get_scenarios(symbol: str, timeframe: str = "1D"):
    """Deterministic long+short scenarios from computed TA levels (swings,
    Donchian channels, Supertrend) — lib/scenario_engine.py. Both directions
    are always evaluated; NO_TRADE is a valid state, and every referenced
    price is an actual computed level, never an invented one."""
    from lib.ohlcv import fetch_multi_timeframe
    from lib.ta_engine import compute_timeframe
    from lib.scenario_engine import build_scenarios

    bars = fetch_multi_timeframe(symbol.upper(), [timeframe])
    df = bars.get(timeframe)
    if df is None or len(df) < 10:
        raise HTTPException(503, f"No usable {timeframe} bars for {symbol}")
    ta = compute_timeframe(df, timeframe)
    scenarios = build_scenarios(ta)
    if not scenarios:
        raise HTTPException(503, f"TA computation failed for {symbol}")
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        **scenarios,
        "market_structure": ta.get("market_structure"),
        "supertrend": ta.get("supertrend"),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ev/summary")
def get_ev_summary():
    """Empirical win-probability / expected-value buckets over all evaluated
    signals — lib/ev_model.py. A probability is never shown without its
    sample size; buckets under the sample floor report counts only."""
    from lib.ev_model import MIN_DECIDED, compute_ev_buckets

    with get_db() as db:
        joined = (
            db.query(SignalEvaluation, TradingSignal.composite_score)
            .outerjoin(TradingSignal, TradingSignal.id == SignalEvaluation.signal_id)
            .all()
        )
        rows = [{
            "outcome": ev.outcome, "direction": ev.direction, "asset_class": ev.asset_class,
            "entry_price": ev.entry_price, "target_price": ev.target_price, "stop_loss": ev.stop_loss,
            "composite_score": score,
        } for ev, score in joined]

    return {
        "buckets": compute_ev_buckets(rows),
        "total_evaluations": len(rows),
        "min_decided_for_probability": MIN_DECIDED,
        "note": (
            "Empirical frequencies of past signal outcomes, bucketed by "
            "conditions at generation time. Undecided outcomes (open/expired/"
            "ambiguous) are counted but excluded from win-rate denominators. "
            "Realized moves come from each signal's own stored levels."
        ),
    }


@router.get("/psychology")
def get_market_psychology(persist: bool = True):
    """JARVIS Market Psychology Index — a fear/greed composite computed from
    data this system already collects (VIX history, tracked-universe breadth,
    crypto perp funding, long/short ratio, liquidation skew) rather than
    scraped from a third-party index.

    Components with no data ABSTAIN rather than scoring a neutral 50, so the
    response reports how many of the five actually contributed. See
    lib/market_psychology.py for each mapping and why it was chosen."""
    from lib.market_psychology import (
        breadth_component, compute_psychology_index, compute_rate_of_change,
        funding_component, liquidation_component, long_short_component, vix_component,
    )

    vix_now = vix_history = None
    try:
        from lib.futures_data import fetch_futures_ohlcv, get_cached_futures_price
        # get_cached_futures_price returns a dict ({"symbol", "price", ...}),
        # not a bare float.
        quote = get_cached_futures_price("^VIX")
        vix_now = quote.get("price") if isinstance(quote, dict) else quote
        df = fetch_futures_ohlcv("^VIX", "1D")
        if df is not None and not df.empty:
            vix_history = [float(v) for v in df["close"].tolist()]
            if vix_now is None:
                vix_now = vix_history[-1]
    except Exception as e:
        logger.debug(f"[Psychology] VIX unavailable: {e}")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_db() as db:
        rows_all = db.query(MarketAsset.asset_class, MarketAsset.change_percent).all()
        changes = [r[1] for r in rows_all if r[1] is not None]
        changes_by_market = {"stocks": [], "crypto": [], "futures": []}
        for ac, chg in rows_all:
            if chg is None:
                continue
            c = (ac or "").lower()
            if "crypto" in c:
                changes_by_market["crypto"].append(chg)
            elif "equity" in c or "etf" in c or "stock" in c:
                changes_by_market["stocks"].append(chg)
            else:
                changes_by_market["futures"].append(chg)

        # One snapshot per symbol — the newest — so a symbol polled more often
        # doesn't get extra weight in the averages.
        latest_by_symbol: dict = {}
        for row in (
            db.query(CryptoDerivativesSnapshot)
            .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
            .limit(200).all()
        ):
            latest_by_symbol.setdefault(row.symbol, row)
        funding_rates = [r.funding_rate for r in latest_by_symbol.values()]
        ls_ratios = [r.long_short_ratio for r in latest_by_symbol.values()]

        long_usd = short_usd = 0.0
        for liq in db.query(CryptoLiquidation).filter(CryptoLiquidation.liquidated_at >= cutoff).all():
            # notional_usd is computed at ingest; fall back to price*size only
            # if that column is empty on older rows.
            notional = liq.notional_usd if liq.notional_usd is not None else (liq.price or 0) * (liq.size or 0)
            if (liq.pos_side or "").lower() == "long":
                long_usd += notional
            elif (liq.pos_side or "").lower() == "short":
                short_usd += notional

        prior = (
            db.query(PsychologySnapshot)
            .order_by(PsychologySnapshot.created_at.desc()).first()
        )
        prior_score = prior.score if prior else None
        prior_at = prior.created_at if prior else None

    components = {
        "vix": vix_component(vix_now, vix_history),
        "breadth": breadth_component(changes),
        "funding": funding_component(funding_rates),
        "long_short": long_short_component(ls_ratios),
        "liquidations": liquidation_component(long_usd, short_usd),
    }
    result = compute_psychology_index(components)

    hours = None
    if prior_at:
        try:
            hours = (datetime.now(timezone.utc) - datetime.fromisoformat(prior_at)).total_seconds() / 3600
        except ValueError:
            hours = None
    result["rate_of_change"] = compute_rate_of_change(result["score"], prior_score, hours)

    # Per-market indexes: same components, market-scoped breadth, per-market
    # weighting (lib/market_psychology.MARKET_COMPONENT_MAP). Crypto uses its
    # derivatives components; stocks/futures use VIX + their own breadth.
    from lib.market_psychology import compute_market_index
    result["markets"] = {
        market: compute_market_index(market, {
            "vix": components["vix"],
            "breadth": breadth_component(changes_by_market[market]),
            "funding": components["funding"],
            "long_short": components["long_short"],
            "liquidations": components["liquidations"],
        })
        for market in ("stocks", "crypto", "futures")
    }
    result["computed_at"] = datetime.now(timezone.utc).isoformat()

    if persist and result["score"] is not None:
        with get_db() as db:
            db.add(PsychologySnapshot(
                score=result["score"], label=result["label"],
                components_available=result["components_available"],
                components_json=json.dumps(components, default=str),
            ))
    return result


@router.get("/regime")
def get_regime_endpoint():
    try:
        from lib.market_regime import get_regime
        return get_regime()
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/performance/r-multiples")
def get_r_multiples(limit: int = 200):
    """R-multiple (realized P&L / initial $ risk) for closed paper trades.
    Joins paper_trades back to paper_positions for the original stop_loss,
    since paper_trades itself doesn't store it."""
    from lib.performance_analytics import compute_r_multiples
    from app.database import PaperTrade, PaperPosition
    with get_db() as db:
        trade_rows = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(limit).all()
        position_ids = [t.position_id for t in trade_rows if t.position_id]
        stop_by_pos = {}
        if position_ids:
            positions = db.query(PaperPosition).filter(PaperPosition.id.in_(position_ids)).all()
            # initial_stop_loss, NEVER the live stop_loss: the manager trails
            # stops, so by close the live stop often sits at breakeven and
            # |entry - stop| measures the trail, not the risk. A ^VIX short
            # that lost $19.29 reported R = -1,063,462 this way.
            stop_by_pos = {p.id: (p.initial_stop_loss or p.stop_loss)
                           for p in positions}
        trades = [{
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "stop_loss": stop_by_pos.get(t.position_id),
            "exit_price": t.exit_price,
            "qty": t.qty,
            "realized_pnl": t.realized_pnl,
            "pnl_pct": t.pnl_pct,
            "close_reason": t.close_reason,
            "closed_at": t.closed_at,
        } for t in trade_rows]
    return compute_r_multiples(trades)


@router.get("/performance/analytics")
def get_performance_analytics(days: int = 90):
    """Real portfolio analytics computed from history: Sharpe ratio and max
    drawdown from daily equity snapshots, plus win-rate/avg P&L broken down
    by originating signal source (watchlist LLM vs ta_fallback vs scanner)."""
    from lib.performance_analytics import (
        daily_equity_curve, compute_max_drawdown, compute_sharpe_ratio, signal_source_breakdown,
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_db() as db:
        snapshots = [
            {"snapshot_at": s.snapshot_at, "equity": s.equity}
            for s in db.query(PortfolioSnapshot)
                .filter(PortfolioSnapshot.snapshot_at >= cutoff)
                .order_by(PortfolioSnapshot.snapshot_at.asc()).all()
        ]

        outcome_rows = db.query(TradeOutcome).filter(TradeOutcome.exited_at >= cutoff).all()
        signal_ids = [o.signal_id for o in outcome_rows if o.signal_id]
        source_by_id = {}
        if signal_ids:
            sigs = db.query(TradingSignal).filter(TradingSignal.id.in_(signal_ids)).all()
            source_by_id = {s.id: (s.signal_source or "watchlist") for s in sigs}
        outcomes = [{
            "signal_source": source_by_id.get(o.signal_id, "unknown"),
            "outcome": o.outcome,
            "pnl_pct": o.pnl_pct,
        } for o in outcome_rows]

    curve = daily_equity_curve(snapshots)
    drawdown = compute_max_drawdown(curve)
    sharpe = compute_sharpe_ratio(curve)

    return {
        "period_days": days,
        "equity_curve_points": len(curve),
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": drawdown["max_drawdown_pct"],
        "drawdown_peak_date": drawdown["peak_date"],
        "drawdown_trough_date": drawdown["trough_date"],
        "trades_analyzed": len(outcomes),
        "by_signal_source": signal_source_breakdown(outcomes),
    }

@router.get("/predictive/status")
def predictive_status():
    """Inference devices, loaded models, latency, and the placement policy.

    Named `predictive`, not `npu`: measured on this host the CPU runs these
    model sizes ~20x faster than the NPU, so the layer is device-independent
    and the NPU is used where its real advantage lies — sustained background
    work that costs the CPU nothing.
    """
    try:
        from lib.predictive import get_runtime
        from lib.predictive.schemas import (CURRENT_SCHEMA, dimension,
                                            schema_hash)
        st = get_runtime().status()
        st["schema"] = {"version": CURRENT_SCHEMA, "features": dimension(),
                        "hash": schema_hash()}
        return st
    except Exception as e:
        # openvino absent is normal — the trading path must run without it.
        return {"available": False, "reason": str(e), "devices": [], "models": {}}


@router.get("/expectancy")
def expectancy_summary():
    """What each bucket of setups is actually worth, in R.

    A win rate is not an edge: 45% wins averaging +2R beats 60% averaging
    +0.4R, and only one of those was visible before. Every row carries its
    sample size and the Wilson interval on the win rate, because a point
    estimate from 12 trades and one from 4,000 look identical otherwise.
    """
    try:
        from lib.expectancy import summary
        return summary()
    except Exception as e:
        return {"error": str(e), "buckets": []}


@router.get("/strategies/lifecycle")
def strategy_lifecycle():
    """Which strategies may trade, judged on data the ranking never saw.

    With fourteen strategies across several timeframes, one will look
    excellent by chance and gets selected precisely because it got lucky.
    Outcomes are split by TIME — older trains, newer validates — and the
    verdict comes from the newer portion alone. `overfitted` marks a
    strategy that was profitable in training and is not out of it.
    """
    try:
        from lib.strategy_lifecycle import evaluate_all
        return evaluate_all()
    except Exception as e:
        return {"error": str(e), "strategies": {}}


@router.get("/performance")
def get_performance(days: int = 30):
    """Trade performance statistics over the last N days."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Serialize everything INSIDE the session — avoids DetachedInstanceError
    with get_db() as db:
        all_trades = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Closed", "Executed", "Rejected"]),
            TradingSignal.updated_date >= cutoff
        ).order_by(TradingSignal.updated_date.desc()).all()

        total    = len(all_trades)
        executed = [_sig_dict(t) for t in all_trades if t.status in ("Closed", "Executed")]
        rejected = [t for t in all_trades if t.status == "Rejected"]
        rej_count = len(rejected)

    # All computation now works on plain dicts — no ORM access after session close
    rr_list, scores, classes = [], [], {}
    for t in executed:
        cl = t.get("asset_class") or "Equity"
        classes[cl] = classes.get(cl, 0) + 1
        ep = t.get("entry_price"); tp = t.get("target_price"); sl = t.get("stop_loss")
        if ep and tp and sl and ep > sl:
            rr = round((tp - ep) / (ep - sl), 2)
            rr_list.append(rr)
        sc = t.get("composite_score") or t.get("confidence")
        if sc:
            scores.append(sc)

    avg_rr    = round(sum(rr_list) / len(rr_list), 2) if rr_list else None
    avg_score = round(sum(scores)  / len(scores),  1) if scores  else None
    good_rr   = [r for r in rr_list if r >= 2.0]
    by_class  = [{"class": k, "count": v} for k, v in classes.items()]

    daily = {}
    for t in executed:
        day = (t.get("generated_at") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0) + 1
    daily_list = sorted([{"date": d, "count": c} for d, c in daily.items()], key=lambda x: x["date"])

    return {
        "period_days":    days,
        "total_signals":  total,
        "executed":       len(executed),
        "rejected":       rej_count,
        "avg_rr":         avg_rr,
        "avg_score":      avg_score,
        "good_rr_count":  len(good_rr),
        "by_class":       by_class,
        "daily_volume":   daily_list,
        "recent_trades":  executed[:50],
    }

@router.post("/backtest/run")
async def run_backtest_endpoint(request: Request):
    """Kick off a historical, no-LLM backtest in a background thread. Body:
    {symbols: list[str], start_date: str, end_date: str,
     timeframes: list[str] = None, trade_mode: str = "longer"}
    Returns immediately with a run_id; poll GET /api/backtest/{run_id}."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    symbols = body.get("symbols") or []
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    timeframes = body.get("timeframes")
    trade_mode = body.get("trade_mode", "longer")

    if not isinstance(symbols, list) or not symbols:
        return JSONResponse({"error": "symbols must be a non-empty list"}, status_code=400)
    MAX_BACKTEST_SYMBOLS = 10
    if len(symbols) > MAX_BACKTEST_SYMBOLS:
        return JSONResponse(
            {"error": f"Too many symbols — max {MAX_BACKTEST_SYMBOLS} per backtest run"},
            status_code=400,
        )
    if not start_date or not end_date:
        return JSONResponse({"error": "start_date and end_date are required"}, status_code=400)

    from app.database import BacktestRun, new_id, now_iso

    run_id = new_id()
    with get_db() as db:
        db.add(BacktestRun(
            id=run_id,
            symbols=json.dumps(symbols),
            timeframes=json.dumps(timeframes) if timeframes else json.dumps([]),
            trade_mode=trade_mode,
            start_date=start_date,
            end_date=end_date,
            status="running",
            created_at=now_iso(),
        ))

    import threading
    def _run():
        from app.database import BacktestRun as _BacktestRun
        try:
            from lib.backtester import run_backtest
            result = run_backtest(
                symbols=symbols, start_date=start_date, end_date=end_date,
                timeframes=timeframes, trade_mode=trade_mode,
            )
            with get_db() as db:
                row = db.query(_BacktestRun).filter(_BacktestRun.id == run_id).first()
                if row:
                    row.status = "completed"
                    row.result_json = json.dumps(result, default=str)
                    row.finished_at = now_iso()
        except Exception as e:
            logger.error(f"[Routes] Backtest run {run_id} failed: {e}")
            try:
                with get_db() as db:
                    row = db.query(_BacktestRun).filter(_BacktestRun.id == run_id).first()
                    if row:
                        row.status = "failed"
                        row.error = str(e)
                        row.finished_at = now_iso()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"run_id": run_id, "status": "started"}


@router.get("/backtest/{run_id}")
def get_backtest_run(run_id: str):
    """Return a backtest run's status, and its parsed result once completed."""
    from app.database import BacktestRun
    with get_db() as db:
        row = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if not row:
            raise HTTPException(404, "Backtest run not found")
        out = {
            "id": row.id,
            "symbols": json.loads(row.symbols) if row.symbols else [],
            "timeframes": json.loads(row.timeframes) if row.timeframes else [],
            "trade_mode": row.trade_mode,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "status": row.status,
            "error": row.error,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }
        if row.status == "completed" and row.result_json:
            try:
                out["result"] = json.loads(row.result_json)
            except Exception:
                out["result"] = None
        return out


@router.get("/backtest")
def list_backtest_runs():
    """List recent backtest runs (light — no full result payload)."""
    from app.database import BacktestRun
    with get_db() as db:
        rows = (
            db.query(BacktestRun)
            .order_by(BacktestRun.created_at.desc())
            .limit(50)
            .all()
        )
        return {
            "runs": [
                {
                    "id": r.id,
                    "symbols": json.loads(r.symbols) if r.symbols else [],
                    "trade_mode": r.trade_mode,
                    "start_date": r.start_date,
                    "end_date": r.end_date,
                    "status": r.status,
                    "created_at": r.created_at,
                    "finished_at": r.finished_at,
                }
                for r in rows
            ]
        }


@router.get("/decisions")
def get_decisions(limit: int = 200):
    """Return recent AI decisions newest-first — raw SQL so it works even if table is brand new."""
    from app.database import engine
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        conn.execute(_text("""
            CREATE TABLE IF NOT EXISTS ai_decisions (
                id TEXT PRIMARY KEY, source TEXT, symbol TEXT, action TEXT,
                reasoning TEXT, price REAL, pnl_pct REAL, score REAL,
                thinking INTEGER DEFAULT 1, created_at TEXT
            )
        """))
        try:
            conn.execute(_text("ALTER TABLE ai_decisions ADD COLUMN thinking INTEGER DEFAULT 1"))
        except Exception:
            pass
        rows = conn.execute(_text(
            "SELECT id, source, symbol, action, reasoning, price, pnl_pct, score, thinking, created_at "
            "FROM ai_decisions ORDER BY created_at DESC LIMIT :lim"
        ), {"lim": limit}).fetchall()
    return [
        {"id": r[0], "source": r[1], "symbol": r[2], "action": r[3],
         "reasoning": r[4], "price": r[5], "pnl_pct": r[6], "score": r[7],
         "thinking": r[8] if r[8] is not None else 1, "created_at": r[9]}
        for r in rows
    ]


@router.delete("/decisions/clear")
def clear_decisions():
    """Clear all AI decision log entries."""
    from app.database import engine
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        conn.execute(_text("CREATE TABLE IF NOT EXISTS ai_decisions (id TEXT PRIMARY KEY, source TEXT, symbol TEXT, action TEXT, reasoning TEXT, price REAL, pnl_pct REAL, score REAL, created_at TEXT)"))
        count = conn.execute(_text("SELECT COUNT(*) FROM ai_decisions")).scalar()
        conn.execute(_text("DELETE FROM ai_decisions"))
    return {"ok": True, "deleted": count}

# ── Learning Engine — Tier 1 & 2 ─────────────────────────────────────────────

@router.get("/learning/outcomes")
def get_outcomes(limit: int = 200, paper: str = "false"):
    """Return closed trade outcomes for the performance log.
    paper: 'true'=paper only, 'false'=live only, 'all'=both
    """
    if paper == "all":
        paper_mode = None  # None = no filter
    elif paper in ("true", "1", "yes"):
        paper_mode = True
    else:
        paper_mode = False
    return get_all_outcomes(limit=limit, paper_mode=paper_mode)

@router.get("/learning/accuracy")
def get_accuracy():
    """Return per-symbol signal accuracy stats."""
    return get_all_accuracy()

@router.get("/learning/summary")
def get_learning_summary(paper: str = "live"):
    """Return a portfolio-level learning summary.
    paper: 'live' = live only, 'paper' = paper only, 'all' = combined
    """
    from app.database import engine
    from sqlalchemy import text as _text
    if paper == "paper":
        where = "WHERE paper_mode = 1"
    elif paper == "all":
        where = ""
    else:
        where = "WHERE paper_mode = 0"
    with engine.connect() as conn:
        rows = conn.execute(_text(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
                ROUND(AVG(pnl_pct),3) as avg_pnl,
                ROUND(AVG(hold_duration_m),1) as avg_hold_min,
                ROUND(MAX(pnl_pct),3) as best_trade,
                ROUND(MIN(pnl_pct),3) as worst_trade,
                SUM(pnl_usd) as total_pnl_usd
            FROM trade_outcomes {where}
        """)).fetchone()
    if not rows or rows[0] == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_pnl": 0, "avg_hold_min": 0, "best_trade": 0,
                "worst_trade": 0, "total_pnl_usd": 0}
    total = rows[0] or 0
    wins  = rows[1] or 0
    return {
        "total": total, "wins": wins, "losses": rows[2] or 0,
        "win_rate": round(wins / total, 4) if total else 0,
        "avg_pnl": rows[3], "avg_hold_min": rows[4],
        "best_trade": rows[5], "worst_trade": rows[6],
        "total_pnl_usd": round(rows[7] or 0, 2),
    }

@router.get("/learning/patterns")
def get_patterns():
    """Return Tier 3 pattern memory — TA setup win/loss history."""
    return get_all_patterns()

@router.get("/learning/regimes")
def get_regimes():
    """Return Tier 4 regime performance — win rates per market regime."""
    return get_all_regime_stats()

@router.get("/learning/lessons")
def get_lessons(limit: int = 50):
    """Return Tier 5 LLM reasoning audit lessons."""
    return get_all_lessons(limit=limit)

@router.post("/learning/seed-test")
def seed_test_outcome():
    """Dev helper: inject a fake closed trade so the Learning Engine tables populate and the UI can be verified."""
    from lib.learning_engine import record_trade_outcome
    import random, datetime, uuid
    symbols = ["AAPL", "NVDA", "SPY", "BTC/USD", "ETH/USD", "GC=F"]
    sym = random.choice(symbols)
    direction = random.choice(["BUY", "SELL"])
    entry = round(random.uniform(50, 400), 2)
    pnl_pct = round(random.uniform(-8, 15), 2)
    exit_p = round(entry * (1 + pnl_pct / 100), 2)
    outcome = "WIN" if pnl_pct > 0.5 else "LOSS" if pnl_pct < -0.5 else "BREAKEVEN"
    entered_at = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=random.randint(1,48))).isoformat()
    exited_at  = datetime.datetime.now(datetime.timezone.utc).isoformat()
    hold_min   = round(random.uniform(15, 600), 1)
    exit_reasons = ["TAKE_PROFIT", "HARD_STOP", "LLM_EXIT", "TIMEOUT", "MANUAL"]
    regimes = ["Risk-On Bull", "Range-Bound", "Bear / Risk-Off", "Neutral"]
    record_trade_outcome(
        signal_id=str(uuid.uuid4()),
        symbol=sym,
        asset_class="crypto" if "/" in sym else ("futures" if "=" in sym else "equity"),
        direction=direction,
        timeframe="4H",
        entry_price=entry,
        exit_price=exit_p,
        qty=round(random.uniform(1, 20), 4),
        pnl_usd=round(entry * random.uniform(0.01, 0.5) * (1 if outcome == "WIN" else -1), 2),
        pnl_pct=pnl_pct,
        outcome=outcome,
        exit_reason=random.choice(exit_reasons),
        hold_duration_m=hold_min,
        signal_confidence=random.randint(55, 92),
        signal_score=round(random.uniform(60, 95), 1),
        signal_reasoning=f"Test seed: {sym} showed momentum setup on 4H.",
        ta_summary="RSI oversold, MACD crossover, above VWAP",
        market_regime=random.choice(regimes),
        paper_mode=True,
        entered_at=entered_at,
        exited_at=exited_at,
    )
    return {"ok": True, "seeded": sym, "outcome": outcome, "pnl_pct": pnl_pct}

# ─── Futures Data ─────────────────────────────────────────────────────────────


@router.post("/learning/backfill-paper")
def backfill_paper_outcomes():
    """One-time backfill: copy all closed PaperTrades into trade_outcomes so learning engine can process them."""
    from lib.learning_engine import backfill_paper_trades
    result = backfill_paper_trades()
    return result

