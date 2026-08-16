"""Trading & execution routes — Phase 7 split of app/routes.py. Bodies are
verbatim; shared helpers live in app.routers.common."""
from fastapi import APIRouter

from app.routers.common import *  # noqa: F401,F403
from app.routers.common import _build_fx_rates, _context_terms, _is_pending_equity_candidate, _position_dict, _related_signal_context, _sig_dict, _signal_evaluation_dict  # noqa: E501

router = APIRouter()


@router.get("/preferences/trading")
def get_trading_preference():
    from lib.trading_preferences import get_user_preference
    return get_user_preference()


@router.put("/preferences/trading")
def update_trading_preference(body: TradingPreferenceRequest):
    from lib.trading_preferences import set_trade_mode
    try:
        return set_trade_mode(body.trade_mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/preferences/execution")
def update_execution_criteria(body: ExecutionCriteriaRequest):
    """Live (Alpaca) auto-execution gates. Auto Sim mirrors every approved
    signal unconditionally; these criteria decide which of them also reach
    the broker account."""
    from lib.trading_preferences import set_execution_criteria
    return set_execution_criteria(
        live_min_score=body.live_min_score,
        live_min_rr=body.live_min_rr,
        live_min_confidence=body.live_min_confidence,
    )


@router.get("/auto-paper/summary")
def auto_paper_summary():
    from lib.auto_simulator import get_auto_sim_summary
    return get_auto_sim_summary()


@router.post("/auto-paper/run")
def auto_paper_run():
    from lib.auto_simulator import run_auto_simulator
    return run_auto_simulator()

@router.get("/signals")
def get_signals(status: str = None, limit: int = 150):
    with get_db() as db:
        q = db.query(TradingSignal)
        if status:
            q = q.filter(TradingSignal.status == status)
        else:
            q = q.filter(
                TradingSignal.status.notin_(["Superseded", "Rejected"])
            )
        rows = q.order_by(TradingSignal.generated_at.desc()).limit(limit).all()
        out = [_sig_dict(s) for s in rows]
        # Attach the stored v8 gate verdict (decision/net R/reason) from the
        # candidate ledger — recorded at signal birth, so the card can lead
        # with the DECISION instead of the composite score, at zero extra
        # computation. Signals predating the experiment simply carry none.
        try:
            from app.database import CandidateSignal
            ids = [s.id for s in rows if s.id]
            if ids:
                gates = {c.signal_id: c for c in db.query(CandidateSignal).filter(
                    CandidateSignal.signal_id.in_(ids),
                    CandidateSignal.gate_v8_decision.isnot(None)).all()}
                for d in out:
                    g = gates.get(d.get("id"))
                    if g is not None:
                        d["gate_decision"] = g.gate_v8_decision
                        d["gate_net_r"] = g.gate_v8_net_r
                        d["gate_reason"] = g.gate_v8_reason
                        d["gate_legacy_take"] = bool(g.gate_legacy_take)
        except Exception as e:
            logger.debug(f"[Signals] gate verdict join failed: {e}")
        return out


@router.get("/signals/performance")
def get_signal_performance(asset_class: str = None, direction: str = None,
                           timeframe: str = None, signal_version: str = None):
    from lib.signal_evaluation import summarize_evaluations
    with get_db() as db:
        query = db.query(SignalEvaluation)
        if asset_class:
            query = query.filter(SignalEvaluation.asset_class == asset_class)
        if direction:
            query = query.filter(SignalEvaluation.direction == direction)
        if timeframe:
            query = query.filter(SignalEvaluation.timeframe == timeframe)
        if signal_version:
            query = query.filter(SignalEvaluation.signal_version == signal_version)
        rows = [_signal_evaluation_dict(row) for row in query.all()]

    grouped = {}
    for field in ("asset_class", "direction", "timeframe", "signal_version"):
        values = sorted({row.get(field) or "Unknown" for row in rows})
        grouped[field] = {
            value: summarize_evaluations([row for row in rows if (row.get(field) or "Unknown") == value])
            for value in values
        }
    return {"summary": summarize_evaluations(rows), "groups": grouped, "evaluations": rows[-100:]}


@router.get("/signals/{signal_id}/analysis")
def get_signal_analysis(signal_id: str):
    with get_db() as db:
        row = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not row:
            raise HTTPException(404, "Signal not found")
        signal = _sig_dict(row)
        news, threats = _related_signal_context(db, signal)
    try:
        from lib.signal_analysis import build_signal_analysis
        return build_signal_analysis(signal, news, threats)
    except Exception as exc:
        logger.exception("[API] Signal analysis failed for %s", signal_id)
        raise HTTPException(500, str(exc))

@router.delete("/signals/clear/expired")
def clear_expired():
    with get_db() as db:
        n = db.query(TradingSignal).filter(TradingSignal.status.in_(["Expired","Rejected"])).delete()
    return {"ok":True,"deleted":n}

@router.delete("/signals/{signal_id}")
def delete_signal(signal_id: str):
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        db.delete(sig)
    return {"ok":True}

@router.post("/signals/{signal_id}/notes")
def save_signal_notes(signal_id: str, body: NotesRequest):
    """Trade journal note — freeform, attached to the signal for its whole lifecycle."""
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        sig.notes = body.notes
        sig.updated_date = datetime.now(timezone.utc).isoformat()
    return {"ok": True}

@router.post("/signals/{signal_id}/execute")
def manual_execute(signal_id: str, body: ExecuteRequest = ExecuteRequest()):
    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        raise HTTPException(423, f"Live trading is paused: {kill_state.get('paused_reason') or 'manually paused'}")
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        if bool(getattr(sig, "paper_mode", False)):
            raise HTTPException(400, "Paper-only signal cannot be sent to Alpaca live execution")
        if getattr(sig, "expires_at", None):
            try:
                expiry = datetime.fromisoformat(sig.expires_at.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    sig.status = "Expired"
                    raise HTTPException(409, "Signal expired; refresh analysis before execution")
            except ValueError:
                pass
        if getattr(sig, "data_quality_score", None) is not None and sig.data_quality_score < 35:
            raise HTTPException(409, "Signal data quality is below the execution threshold")
        try:
            from lib.alpaca_client import submit_bracket_order, normalize_symbol, is_crypto
            sym, crypto = normalize_symbol(sig.asset_symbol)
            entry  = float(sig.entry_price or 100)
            if body.qty and body.qty > 0:
                qty = float(body.qty)
            elif crypto:
                # Fractional qty for crypto — $1000 notional
                qty = round(1000.0 / entry, 6) if entry > 0 else 0.01
            else:
                qty = max(1, int(1000 / entry)) if entry > 0 else 1
            result = submit_bracket_order(symbol=sym, qty=qty, entry_price=sig.entry_price,
                                          take_profit=sig.target_price, stop_loss=sig.stop_loss)
            sig.status = "Executed"; sig.updated_date = datetime.now(timezone.utc).isoformat()
            # Store Alpaca order ID on the signal for position linking
            try:
                order_id = result.get("id") or result.get("order_id") if isinstance(result, dict) else getattr(result, "id", None)
                if order_id:
                    sig.alpaca_order_id = str(order_id)
            except Exception as link_err:
                logger.warning(f"[API] Could not link Alpaca order id for signal {signal_id}: {link_err}")
            return {"ok":True,"order":result,"qty":qty,"crypto":crypto}
        except Exception as e:
            raise HTTPException(500, str(e))

@router.post("/signals/save")
def save_signal(body: SaveSignalRequest):
    """Save a manually-scanned signal to the DB."""
    import uuid as _uuid
    now_iso = datetime.now(timezone.utc).isoformat()
    rr = None
    if body.entry_price and body.target_price and body.stop_loss and body.entry_price > body.stop_loss:
        try: rr = round((body.target_price - body.entry_price) / (body.entry_price - body.stop_loss), 2)
        except: pass
    with get_db() as db:
        existing = db.query(TradingSignal).filter(
            TradingSignal.asset_symbol == body.asset_symbol,
            TradingSignal.status == "Active"
        ).first()
        if existing:
            return {"error": f"Active signal for {body.asset_symbol} already exists"}
        sig = TradingSignal(
            id           = str(_uuid.uuid4()),
            asset_symbol = body.asset_symbol,
            asset_name   = body.asset_name or body.asset_symbol,
            asset_class  = body.asset_class or "Equity",
            direction    = body.direction or "Long",
            confidence   = body.confidence or 65,
            composite_score = body.confidence or 65,
            timeframe    = body.timeframe or "4H",
            entry_price  = body.entry_price,
            target_price = body.target_price,
            stop_loss    = body.stop_loss,
            reasoning    = body.reasoning or "",
            key_risks    = body.key_risks or "",
            momentum     = body.momentum or "",
            signal_source = "scanner",
            rr_ratio     = rr,
            status       = "Active",
            generated_at = now_iso,
            created_date = now_iso,
            updated_date = now_iso,
        )
        db.add(sig)
        sig_id = sig.id  # capture before session closes
    return {"ok": True, "id": sig_id}

@router.post("/signals/{signal_id}/reverse")
def reverse_signal(signal_id: str, body: ReverseSignalRequest):
    """Turn a failing signal into its opposite-side trade.

    Runs a fresh deep verify; only proceeds when the AI DISAGREES with the
    original at >= REVERSAL_MIN_CONFIDENCE and an ATR-derived level set can
    be computed. The new signal enters as a normal candidate (Active for
    crypto, PendingApproval for equities) so every downstream gate — score
    floor, execution criteria, risk sizing — still applies."""
    from lib.signal_verification import deep_verify_signal

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if sig.status not in ("Active", "PendingApproval"):
            # Signals regenerate on a schedule — HYPE/USD supersedes itself
            # every 15 minutes — so a card rendered a few minutes ago holds
            # an id that is already stale. The operator's intent ("flip this
            # symbol") is still perfectly valid; only the id died. Point
            # them at the live successor instead of a dead end.
            successor = (
                db.query(TradingSignal)
                .filter(
                    TradingSignal.asset_symbol == sig.asset_symbol,
                    TradingSignal.user_id == sig.user_id,
                    TradingSignal.status.in_(("Active", "PendingApproval")),
                )
                .order_by(TradingSignal.created_date.desc())
                .first()
            )
            if successor:
                raise HTTPException(
                    409,
                    {
                        "message": (
                            f"That {sig.asset_symbol} signal was superseded by a newer "
                            f"one while the page was open — reversing the current signal "
                            f"instead."
                        ),
                        "successor_id": successor.id,
                        "symbol": sig.asset_symbol,
                    },
                )
            raise HTTPException(
                400,
                f"Signal is {sig.status} and no live {sig.asset_symbol} signal has "
                f"replaced it — nothing to reverse.",
            )
        sig_dict = {
            "asset_symbol": sig.asset_symbol, "asset_class": sig.asset_class,
            "direction": sig.direction, "entry_price": sig.entry_price,
            "target_price": sig.target_price, "stop_loss": sig.stop_loss,
            "status": sig.status, "timeframe": sig.timeframe, "reasoning": sig.reasoning,
            "asset_name": sig.asset_name, "confidence": sig.confidence,
            "composite_score": sig.composite_score, "paper_mode": sig.paper_mode,
        }

    result = deep_verify_signal(sig_dict)
    proposal = result.get("reversal_proposal")
    if not proposal:
        a = result.get("llm_assessment") or {}
        raise HTTPException(
            400,
            f"No reversal justified right now (AI says {a.get('assessment', 'UNKNOWN')}"
            f"{f" at {a.get('confidence')}%" if a.get('confidence') else ''}). "
            "A reversal needs a confident DISAGREE plus an ATR to size risk.",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    _, is_crypto_sym = normalize_symbol(sig_dict["asset_symbol"] or "")
    new_id_val = str(uuid.uuid4())
    with get_db() as db:
        original = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not original or original.status not in ("Active", "PendingApproval"):
            raise HTTPException(409, "Signal changed state while the reversal was being computed")

        db.add(TradingSignal(
            id=new_id_val,
            asset_symbol=original.asset_symbol,
            asset_name=original.asset_name,
            asset_class=original.asset_class,
            direction=proposal["direction"],
            confidence=proposal.get("ai_confidence") or original.confidence,
            composite_score=original.composite_score,
            timeframe=original.timeframe,
            entry_price=proposal["entry_price"],
            target_price=proposal["target_price"],
            stop_loss=proposal["stop_loss"],
            rr_ratio=proposal["rr_ratio"],
            reasoning=(
                f"REVERSAL of failing {original.direction} setup. "
                f"{proposal.get('ai_reasoning') or ''} [{proposal['basis']}]"
            )[:2000],
            key_risks=proposal["warning"],
            signal_source="reversal",
            setup_type="reversal",
            trade_horizon=original.trade_horizon,
            signal_version=original.signal_version,
            paper_mode=bool(original.paper_mode),
            paper_direction=proposal["direction"] if original.paper_mode else None,
            status="Active" if is_crypto_sym else "PendingApproval",
            generated_at=now_iso,
            market_data_at=now_iso,
            trigger_event=f"Deep verify reversal of {signal_id[:8]}",
            trigger_event_id=signal_id,
        ))
        if body.supersede_original:
            original.status = "Superseded"
            note_line = "\n[reversal] flipped to {} at {}".format(proposal["direction"], now_iso)
            original.notes = ((original.notes or "") + note_line).strip()
            original.updated_date = now_iso

    logger.info(
        f"[Reverse] {sig_dict['asset_symbol']} {sig_dict['direction']} -> {proposal['direction']} "
        f"@ {proposal['entry_price']} (new signal {new_id_val[:8]})"
    )
    return {
        "ok": True,
        "new_signal_id": new_id_val,
        "proposal": proposal,
        "original_superseded": body.supersede_original,
        "verification": {k: result.get(k) for k in ("verdict", "current_price", "price_asof", "llm_assessment")},
    }


@router.get("/signals/{signal_id}/sizing")
def get_signal_sizing(signal_id: str):
    """What this signal would actually trade: capital committed, leverage,
    resulting exposure, and what a stop-out costs. Read-only preview — the
    same maths open_paper_position uses, so the card can't disagree with
    the engine."""
    from lib.paper_engine import score_leverage, size_position, TRADE_MARGIN_PCT
    from app.database import PaperPosition, PaperPortfolio

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        entry = float(sig.entry_price or 0)
        stop = float(sig.stop_loss or 0)
        target = float(sig.target_price or 0)
        score = sig.composite_score or sig.confidence
        direction = sig.direction or "Long"
        # Read every attribute needed AFTER the session closes while it is
        # still open. Touching sig.asset_symbol below the `with` raised
        # DetachedInstanceError on every call, and the caller swallows sizing
        # errors per-card — so all 40 cards silently lost their capital /
        # leverage / exposure line with nothing visible but a 500 in the
        # network tab.
        symbol = sig.asset_symbol

        pf = db.query(PaperPortfolio).first()
        cash = float(pf.cash if pf else 100_000.0)
        equity = cash + sum(
            float(r.margin_used or 0)
            for r in db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        )

    # An explicit 5x/10x/20x in the direction is an instruction, not a guess.
    explicit = None
    import re as _re
    m = _re.search(r"(\d+)x", str(direction), _re.I)
    if m:
        explicit = float(m.group(1))
    leverage = explicit or score_leverage(score)

    # The concentration headroom the OPEN path applies. Without it the
    # preview sized against equity alone and advertised positions the book
    # would then refuse — a card promising $67k of exposure that could
    # never be taken.
    notional_cap = None
    try:
        from lib.concentration import headroom_for_book
        notional_cap = headroom_for_book(symbol, equity, book="paper").get("max_notional")
    except Exception as e:
        logger.debug(f"[Sizing] no concentration headroom for {symbol}: {e}")

    sizing = size_position(equity, entry, stop, leverage, cash, symbol=symbol,
                           notional_cap_usd=notional_cap)
    if not sizing.get("ok"):
        return {"ok": False, "reason": sizing.get("reason"), "leverage": leverage}

    gain_at_target = sizing["qty"] * abs(target - entry) if target > 0 else None
    return {
        "ok": True,
        # The leverage ACTUALLY used, not the one requested. Echoing the
        # request made every card read "1x" while the engine sized at 25x,
        # so margin x leverage never matched the exposure printed beside it.
        "leverage": round(float(sizing.get("leverage") or leverage), 2),
        "leverage_requested": leverage,
        "leverage_source": "explicit in direction" if explicit else f"conviction score {float(score or 0):.0f}",
        "margin": round(sizing["margin"], 2),
        "notional": round(sizing["notional"], 2),
        "qty": round(sizing["qty"], 8),
        "loss_at_stop": round(sizing["loss_at_stop"], 2),
        "loss_pct_of_margin": round(sizing["loss_pct_of_margin"], 1),
        "gain_at_target": round(gain_at_target, 2) if gain_at_target is not None else None,
        "gain_pct_of_margin": round(gain_at_target / sizing["margin"] * 100, 1) if gain_at_target and sizing["margin"] else None,
        "capped_by_cash": sizing["capped_by_cash"],
        "equity_basis": round(equity, 2),
        "note": (
            f"{TRADE_MARGIN_PCT}% of equity committed per position. Leverage multiplies "
            f"exposure, not the capital at risk — the committed amount is the most that can be lost."
        ),
    }


@router.post("/signals/{signal_id}/verify")
def verify_signal_route(signal_id: str, apply_update: bool = False, deep: bool = False):
    """User-initiated double-check of a signal against fresh data —
    lib/signal_verification.py. Deterministic verdict (CONFIRMED /
    STALE_ENTRY / INVALIDATED / DATA_UNAVAILABLE); equity prices come from
    Massive within its 5-calls/min budget, crypto from live exchange feeds.

    apply_update=true additionally applies the suggested level re-anchor,
    ONLY when the verdict is STALE_ENTRY and only for signals still awaiting
    action — an executed trade's levels are history, not editable."""
    from lib.signal_verification import verify_signal, deep_verify_signal

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        sig_dict = {
            "asset_symbol": sig.asset_symbol, "asset_class": sig.asset_class,
            "direction": sig.direction, "entry_price": sig.entry_price,
            "target_price": sig.target_price, "stop_loss": sig.stop_loss,
            "status": sig.status, "timeframe": sig.timeframe,
            "reasoning": sig.reasoning,
        }

    # deep=true adds fresh Python TA + MCP market data + MCP news, all fed
    # into the LLM for a second opinion layered ON TOP of the deterministic
    # verdict (never replacing it). Costs 1 tavily + up to 1 Massive + 1 LLM call.
    result = deep_verify_signal(sig_dict) if deep else verify_signal(sig_dict)

    applied = False
    if (apply_update and result.get("verdict") == "STALE_ENTRY"
            and result.get("suggested_update")
            and sig_dict["status"] in ("Active", "PendingApproval")):
        upd = result["suggested_update"]
        with get_db() as db:
            sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
            if sig and sig.status in ("Active", "PendingApproval"):
                sig.entry_price = upd["entry_price"]
                sig.stop_loss = upd["stop_loss"]
                sig.target_price = upd["target_price"]
                sig.notes = ((sig.notes or "") + f"\n[verify] levels re-anchored at {result['verified_at']}").strip()
                sig.updated_date = datetime.now(timezone.utc).isoformat()
                applied = True
        result["update_applied"] = applied

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if sig:
            sig.verification_json = json.dumps(result, default=str)
            sig.verified_at = result.get("verified_at")

    return {"signal_id": signal_id, **result, "update_applied": applied}


@router.get("/venue/fee-comparison")
def get_fee_comparison(notional: float = 10_000.0, contracts: float = 1.0):
    """What a round trip costs at each venue for a given trade size.

    Exists because the venues price on incompatible SHAPES: spot charges a
    percentage of notional (scale-neutral), while US perpetuals and CME
    products charge per contract (regressive). Which venue is cheapest
    therefore depends entirely on size, and the answer flips — so a single
    "our fee is X" figure would be misleading at some size no matter which
    number was chosen.
    """
    import os
    from lib.venues import (fee_for, us_perpetual_fee, us_futures_fee,
                            futures_fee_for, us_fee_as_pct_of_notional)

    region = (os.getenv("VENUE_REGION") or "international").lower()
    rows = []

    def add(label, dollars, note=""):
        rows.append({
            "venue": label,
            "round_trip_usd": round(dollars, 4),
            "pct_of_notional": round(us_fee_as_pct_of_notional(notional, dollars) * 100, 5),
            "note": note,
        })

    for v in ("alpaca", "kraken"):
        rate, why = fee_for(v, asset_class="crypto")
        add(f"{v} spot", notional * rate * 2.0, why)

    if region == "us":
        fee, why = us_perpetual_fee(contracts)
        add("kraken US perpetual", fee, why)
        for sym in ("MES=F", "ES=F"):
            f, why2 = us_futures_fee(sym, contracts)
            if f is not None:
                add(f"kraken CME {sym}", f, why2)
    else:
        rate, why = futures_fee_for("BTC/USD")
        if rate is not None:
            add("kraken perpetual", notional * rate * 2.0, why)

    rows.sort(key=lambda r: r["round_trip_usd"])
    return {
        "notional": notional,
        "contracts": contracts,
        "region": region,
        "cheapest": rows[0]["venue"] if rows else None,
        "rows": rows,
        "note": (
            "Percentage venues are scale-neutral; per-contract venues are regressive. "
            "The cheapest venue therefore CHANGES with trade size — compare at the size "
            "you actually trade, not at a default."
        ),
    }


@router.get("/venue/kraken")
def get_kraken_venue():
    """Everything the desk knows about the Kraken venue, for the UI.

    Deliberately one endpoint rather than four: these numbers only make
    sense together. A tight spread means little without the fee that also
    has to be paid, and leverage limits mean little without knowing what a
    round trip costs at that leverage.
    """
    import os
    out = {"venue": "kraken", "paper_venue": os.getenv("PAPER_VENUE", "kraken")}

    # Account connection and the fee actually charged
    try:
        from lib.kraken_account import check_connection, is_configured
        out["account"] = check_connection() if is_configured() else {
            "connected": False, "reason": "no credentials configured"}
    except Exception as e:
        out["account"] = {"connected": False, "reason": str(e)[:80]}

    try:
        from lib.venues import fee_for, account_fee
        measured = account_fee("kraken")
        taker, why = fee_for("kraken")
        maker, _ = fee_for("kraken", maker=True)
        out["fees"] = {
            "taker_pct": round(taker * 100, 4),
            "maker_pct": round(maker * 100, 4),
            "source": "measured from account" if measured else "published schedule",
            "volume_30d": measured.get("volume_30d") if measured else None,
            "note": why,
        }
    except Exception as e:
        out["fees"] = {"error": str(e)[:80]}

    # Live book + tape, per streamed symbol
    try:
        from lib.kraken_stream import status as stream_status, live_spread_pct, trade_flow
        st = stream_status()
        rows = []
        for sym in st.get("symbols", []):
            spread, spread_why = live_spread_pct(sym)
            flow = trade_flow(sym)
            rows.append({
                "symbol": sym,
                "spread_pct": round(spread * 100, 5) if spread is not None else None,
                "spread_age": spread_why,
                "flow_imbalance": flow["flow_imbalance"] if flow else None,
                "prints": flow["prints"] if flow else 0,
                "buy_count": flow["buy_count"] if flow else 0,
                "sell_count": flow["sell_count"] if flow else 0,
                "largest_print": flow["largest"] if flow else None,
            })
        rows.sort(key=lambda r: abs(r["flow_imbalance"] or 0), reverse=True)
        out["stream"] = {"connected": st.get("connected"), "error": st.get("error"),
                         "since": st.get("since"), "symbols": rows}
    except Exception as e:
        out["stream"] = {"connected": False, "error": str(e)[:80], "symbols": []}

    return out


@router.get("/fx/rates")
def get_fx_rates():
    """Live interbank FX rates + 30d history (AllRatesToday). Serve-stale-
    while-revalidate: instant from the persisted cache even right after a
    restart; a background refresh keeps it within 15 min of live."""
    from lib.api_cache import serve_with_refresh
    payload, stale = serve_with_refresh("fx_rates", 900, _build_fx_rates)
    if payload is None:
        return {"pairs": [], "as_of": None, "note": "FX rates unavailable (no key or upstream down)"}
    return {**payload, "stale": stale}


@router.get("/kraken/account")
def kraken_account():
    """The operator's REAL Kraken account, read-only: balances, open
    positions/orders, and the measured fee tier that replaces the assumed
    one in the cost model. Requires only the read scopes already granted."""
    from lib.kraken_sync import account_snapshot
    return account_snapshot()


@router.get("/kraken/fills")
def kraken_fills():
    """What the synced trades history says about the operator's actual
    trading — the ground truth the execution model will train against."""
    from lib.kraken_sync import fills_summary
    return fills_summary()


@router.post("/kraken/sync")
def kraken_sync_now():
    """Manual sync trigger — same read-only pull the 30-minute job does."""
    from lib.kraken_sync import sync_trades
    return sync_trades()


@router.get("/portfolio/risk")
def get_portfolio_risk():
    """Returns-based portfolio risk over CURRENT positions: correlation
    matrix, hidden-concentration flags, and 1-day historical-simulation VaR —
    lib/portfolio_risk.py. Price history comes from the OHLCV cache only (no
    provider calls on request); symbols without cached history are listed as
    uncovered rather than silently dropped."""
    from datetime import datetime as dt
    from lib.portfolio_risk import (
        concentration_summary, correlation_matrix, historical_var, returns_frame,
    )
    from lib.ohlcv_cache import get_cached_range

    try:
        from lib.alpaca_client import get_positions
        positions = get_positions()
    except Exception as e:
        raise HTTPException(503, f"Positions unavailable: {e}")
    if not positions:
        return {"positions": 0, "note": "No open positions — nothing to measure."}

    weights: dict = {}
    for p in positions:
        try:
            sym = p.symbol
            # Alpaca reports crypto positions in suffix form ("SOLUSD") that the
            # cache doesn't recognize as crypto — normalize to the app-native
            # BASE/USD key or the cache lookup silently misses (observed live:
            # every crypto position landed in uncovered_symbols).
            if str(getattr(p, "asset_class", "")).lower().endswith("crypto"):
                from lib.crypto_market_data import normalize_crypto_symbol
                sym = normalize_crypto_symbol(sym)
            weights[sym] = float(p.market_value)
        except (TypeError, ValueError):
            continue

    end = dt.now(timezone.utc)
    start = end - timedelta(days=400)
    closes = {}
    uncovered = []
    for sym in weights:
        df = get_cached_range(sym, "1D", start, end)
        if df is not None and len(df) >= 2:
            closes[sym] = df["close"]
        else:
            uncovered.append(sym)

    rf = returns_frame(closes)
    matrix = correlation_matrix(rf) if not rf.empty else {}
    gross = sum(abs(v) for v in weights.values())
    # VaR must be scaled to the gross it actually measured. With partial cache
    # coverage, scaling var_pct by the FULL book's gross would present a
    # 3-symbol VaR as whole-portfolio risk (the first live run did exactly
    # that: 3 of 12 positions covered, var_usd quoted against the full gross).
    covered_gross = sum(abs(weights[s]) for s in closes)
    var = historical_var(rf, weights, gross_value=covered_gross) if not rf.empty else None
    if var is not None:
        var["covered_gross_usd"] = round(covered_gross, 2)
        var["coverage_pct_of_gross"] = round(covered_gross / gross * 100, 1) if gross else None
    return {
        "positions": len(weights),
        "gross_value_usd": round(gross, 2),
        "correlation_matrix": matrix,
        "concentration": concentration_summary(matrix, weights) if matrix else None,
        "var": var,
        "uncovered_symbols": uncovered,
        "note": (
            "Cache-only price history; uncovered symbols had no cached daily "
            "bars and are excluded from every statistic above. VaR is 1-day "
            "historical simulation, scaled ONLY to the covered gross (see "
            "coverage_pct_of_gross), and abstains below its sample floor."
        ),
    }


@router.get("/positions/threat-exposure")
def get_positions_threat_exposure():
    """Which currently-held symbols (live + paper) are directly named in an
    active geopolitical threat — reuses the same term-matching logic as
    per-signal threat linking (_context_terms/_related_signal_context),
    scoped to direct symbol/name mentions only so this doesn't just flag
    every position against generic market-wide threats."""
    symbols = set()
    try:
        from lib.alpaca_client import get_positions
        for p in get_positions():
            symbols.add(str(p.symbol).upper())
    except Exception:
        pass
    with get_db() as db:
        from app.database import PaperPosition
        for row in db.query(PaperPosition.symbol).filter(PaperPosition.status == "Open").all():
            if row[0]:
                symbols.add(row[0].upper())

        threat_rows = db.query(ThreatEvent).filter(ThreatEvent.status == "Active").order_by(
            ThreatEvent.created_date.desc()
        ).limit(200).all()

        exposure = {}
        for sym in symbols:
            terms = _context_terms({"asset_symbol": sym, "asset_name": ""})
            if not terms:
                continue
            matches = []
            for t in threat_rows:
                haystack = f"{t.title or ''} {t.description or ''}".upper()
                if any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in terms):
                    matches.append({"id": t.id, "title": t.title, "severity": t.severity, "country": t.country, "region": t.region})
            if matches:
                exposure[sym] = matches[:5]
    return {"exposure": exposure, "symbols_checked": len(symbols), "symbols_exposed": len(exposure)}


@router.get("/positions/with-signals")
def get_positions_with_signals():
    """Positions enriched with their originating signal data."""
    try:
        from lib.alpaca_client import get_positions, get_account
        positions = get_positions()
        account   = get_account()
        equity    = float(account.equity)
        mv_total  = sum(float(p.market_value or 0) for p in positions)
        pl_total  = sum(float(p.unrealized_pl or 0) for p in positions)

        # Build symbol → signal map — serialize to dicts INSIDE session
        with get_db() as db:
            db_sigs = db.query(TradingSignal).filter(
                TradingSignal.status.in_(["Executed", "Active", "Closed"])
            ).order_by(TradingSignal.generated_at.desc()).all()
            # Convert to plain dicts while session is still open
            sig_dicts = [_sig_dict(s) for s in db_sigs]

        sig_map = {}
        for s in sig_dicts:
            sym = s.get("asset_symbol", "")
            if not sym:
                continue
            # Index by every form: "BTC/USD", "BTC", "BTCUSD"
            for key in [sym, sym.replace("/USD",""), sym.replace("/",""), sym.upper(), sym.lower()]:
                if key and key not in sig_map:
                    sig_map[key] = s

        # Filter out zero-qty and dust positions (Alpaca retains sub-cent crypto leftovers)
        positions = [p for p in positions if abs(float(p.qty or 0)) >= 0.0001 and abs(float(p.market_value or 0)) >= 1.0]
        result = []
        for p in positions:
            sym = str(p.symbol)
            pos_dict = _position_dict(p)
            # Alpaca returns crypto as "BTCUSD", DB stores as "BTC/USD" — try all forms
            sym_slash = (sym[:-3] + "/USD") if (len(sym) > 3 and sym.endswith("USD") and "/" not in sym) else sym
            sig = (sig_map.get(sym) or
                   sig_map.get(sym_slash) or
                   sig_map.get(sym.replace("/USD","")) or
                   sig_map.get(sym.replace("/","")) or
                   sig_map.get(sym + "/USD"))
            if sig:
                entry  = float(sig.get("entry_price") or 0)
                target = float(sig.get("target_price") or 0)
                stop   = float(sig.get("stop_loss") or 0)
                curr   = float(p.current_price or 0)
                rr     = round((target - entry) / (entry - stop), 2) if entry > stop and target > entry else None
                progress = round((curr - entry) / (target - entry) * 100, 1) if target > entry and curr else None
                pos_dict["signal"] = dict(sig, rr=rr, progress_pct=progress)
            else:
                # No DB signal — build synthetic context from Alpaca position data
                avg = float(p.avg_entry_price or 0)
                curr = float(p.current_price or 0)
                cost_basis = float(p.cost_basis or 0)
                pos_dict["signal"] = {
                    "asset_symbol": sym,
                    "direction": "Long" if float(p.qty or 0) > 0 else "Short",
                    "entry_price": avg,
                    "target_price": None,
                    "stop_loss": None,
                    "confidence": None,
                    "composite_score": None,
                    "timeframe": None,
                    "rr": None,
                    "progress_pct": None,
                    "reasoning": f"Position entered manually or via external order. Cost basis: ${cost_basis:,.2f}",
                    "key_risks": None,
                    "momentum": None,
                    "signal_source": "manual",
                    "generated_at": None,
                    "_manual": True,
                }
            result.append(pos_dict)

        return {
            "positions": result,
            "account": {
                "equity":         equity,
                "cash":           float(account.cash),
                "buying_power":   float(account.buying_power),
                "market_value":   mv_total,
                "unrealized_pl":  pl_total,
                "unrealized_plpc": (pl_total / (equity - pl_total) * 100) if (equity - pl_total) > 0 else 0,
                "day_trade_count": int(account.daytrade_count or 0),
            }
        }
    except Exception as e:
        raise HTTPException(500, f"Alpaca error: {e}")


@router.get("/positions")
def get_positions_live():
    try:
        from lib.alpaca_client import get_positions, get_account
        positions = get_positions(); account = get_account()
        equity = float(account.equity); mv = sum(float(p.market_value or 0) for p in positions)
        pl     = sum(float(p.unrealized_pl or 0) for p in positions)
        # Filter out zero-qty and dust positions (Alpaca retains sub-cent crypto leftovers)
        positions = [p for p in positions if abs(float(p.qty or 0)) >= 0.0001 and abs(float(p.market_value or 0)) >= 1.0]
        return {"positions":[_position_dict(p) for p in positions],
                "account":{"equity":equity,"cash":float(account.cash),
                            "buying_power":float(account.buying_power),"market_value":mv,
                            "unrealized_pl":pl,"unrealized_plpc":(pl/(equity-pl)*100) if (equity-pl)>0 else 0,
                            "day_trade_count":int(account.daytrade_count or 0)}}
    except Exception as e:
        raise HTTPException(500, f"Alpaca error: {e}")

@router.post("/positions/{symbol}/close")
def close_pos(symbol: str):
    """Close a position, cancelling its protective orders first.

    A working stop-limit SELL RESERVES the entire holding at the broker, so
    a bare close request fails with 40310000 "insufficient balance for ARB
    (requested: 12437.65, available: 0.000000175)" — the coins are all
    committed to the stop. Cancel, let the release settle, then sell."""
    import time as _t
    from lib.alpaca_client import close_position, cancel_open_orders_for_symbol
    try:
        cancelled = cancel_open_orders_for_symbol(symbol)
        if cancelled:
            _t.sleep(1.5)   # broker needs a beat to release the reserved qty
        try:
            close_position(symbol)
        except Exception as first_error:
            # Occasionally the release is slower than 1.5s; one patient retry
            # beats handing the user a raw balance error.
            if "insufficient balance" not in str(first_error).lower():
                raise
            logger.info(f"[Close] {symbol} balance still reserved — retrying in 3s")
            _t.sleep(3.0)
            close_position(symbol)
        return {"ok": True, "symbol": symbol, "orders_cancelled": cancelled}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/portfolio/equity")
def get_equity(hours: int = 24):
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
    with get_db() as db:
        snaps = db.query(PortfolioSnapshot).filter(PortfolioSnapshot.snapshot_at>=cutoff).order_by(PortfolioSnapshot.snapshot_at.asc()).all()
        return [{"time":s.snapshot_at,"equity":s.equity,"cash":s.cash,"market_value":s.market_value,"unrealized_pl":s.unrealized_pl,"position_count":s.position_count} for s in snaps]

@router.get("/execution/slippage")
def get_slippage_summary(limit: int = 200):
    """Execution quality: gap between a live signal's intended entry price and
    the broker's actual fill price, recorded the first time each position is
    observed by manage_positions. Paper fills are never included — they always
    fill at the requested price by construction."""
    with get_db() as db:
        rows = db.query(TradingSignal).filter(
            TradingSignal.slippage_pct.is_not(None),
            TradingSignal.paper_mode.is_not(True),
        ).order_by(TradingSignal.fill_recorded_at.desc()).limit(min(max(limit, 1), 1000)).all()
        trades = [{
            "symbol": r.asset_symbol,
            "asset_class": r.asset_class,
            "entry_price": r.entry_price,
            "actual_fill_price": r.actual_fill_price,
            "slippage_pct": r.slippage_pct,
            "fill_recorded_at": r.fill_recorded_at,
        } for r in rows]
    if not trades:
        return {"count": 0, "avg_slippage_pct": None, "median_slippage_pct": None,
                "worst_slippage_pct": None, "trades": []}
    values = sorted(t["slippage_pct"] for t in trades)
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    worst = max(values, key=abs)
    return {
        "count": n,
        "avg_slippage_pct": round(sum(values) / n, 4),
        "median_slippage_pct": round(median, 4),
        "worst_slippage_pct": round(worst, 4),
        "trades": trades,
    }


@router.get("/alpaca/orders")
def get_orders():
    try:
        from lib.alpaca_client import get_open_orders
        orders=get_open_orders()
        return [{"id":str(o.id),"symbol":str(o.symbol),"qty":float(o.qty or 0),"side":str(o.side),"status":str(o.status),"type":str(o.order_type)} for o in orders]
    except Exception as e: raise HTTPException(500,str(e))

@router.delete("/alpaca/orders/{order_id}")
def cancel_order(order_id: str):
    try:
        from lib.alpaca_client import get_trading_client
        get_trading_client().cancel_order_by_id(order_id)
        return {"ok":True}
    except Exception as e: raise HTTPException(500,str(e))

@router.delete("/alpaca/orders")
def cancel_all_orders():
    """Cancel ALL open orders on Alpaca and reset their signals back to Active."""
    try:
        from lib.alpaca_client import get_trading_client
        client = get_trading_client()
        client.cancel_orders()  # cancels all open orders
        # Also reset any PendingApproval signals back to Active so they can re-queue
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            pending = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").all()
            for s in pending:
                s.status = "Active"
                s.updated_date = now_iso
            cancelled_count = len(pending)
        return {"ok": True, "orders_cancelled": True, "signals_reset": cancelled_count}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/signals/pending")
def get_pending_signals():
    """Get live non-crypto signals queued for the next equity-market session."""
    with get_db() as db:
        rows = db.query(TradingSignal).filter(
            TradingSignal.status == "PendingApproval"
        ).order_by(TradingSignal.confidence.desc()).all()
        sigs = [s for s in rows if _is_pending_equity_candidate(s)]
        return [_sig_dict(s) for s in sigs]

@router.post("/signals/{signal_id}/approve")
def approve_signal(signal_id: str):
    """Approve a pending signal — immediately submit the order to Alpaca."""
    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        raise HTTPException(423, f"Live trading is paused: {kill_state.get('paused_reason') or 'manually paused'}")
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if sig.status != "PendingApproval":
            raise HTTPException(400, f"Signal is {sig.status}, not PendingApproval")
        if bool(getattr(sig, "paper_mode", False)):
            raise HTTPException(400, "Paper-only signal cannot be sent to Alpaca live execution")
        sym_raw = sig.asset_symbol
        entry  = float(sig.entry_price or 0)
        target = float(sig.target_price or 0)
        stop   = float(sig.stop_loss or 0)

    # The broker call runs outside the session above: raising an HTTPException from
    # within that `with get_db()` block rolls back the ENTIRE transaction (including
    # any status write made in the same except block), so a failed submission was
    # silently leaving the signal stuck at PendingApproval forever. Each status write
    # below now commits in its own session, independent of whether we raise after it.
    try:
        from lib.alpaca_client import submit_bracket_order, normalize_symbol, is_crypto, get_account
        sym, crypto = normalize_symbol(sym_raw)
        if not entry or not target or not stop:
            raise ValueError("Signal missing price levels")
        account = get_account()
        buying_power = float(account.buying_power)
        qty = max(1, int(min(1500, buying_power * 0.2) / entry)) if not crypto else round(min(1000, buying_power * 0.1) / entry, 6)
        if qty <= 0:
            raise ValueError(f"Insufficient buying power ${buying_power:.0f}")
        result = submit_bracket_order(symbol=sym, qty=qty, entry_price=entry, take_profit=target, stop_loss=stop)
    except Exception as e:
        with get_db() as db:
            sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
            if sig:
                sig.status = "Rejected"
                sig.updated_date = datetime.now(timezone.utc).isoformat()
        raise HTTPException(500, str(e))

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if sig:
            sig.status = "Executed"
            sig.updated_date = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "order": result, "qty": qty, "symbol": sym}

@router.post("/signals/{signal_id}/reject")
def reject_signal(signal_id: str):
    """Reject a pending signal — discard without trading."""
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404)
        sig.status = "Rejected"
        sig.updated_date = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

@router.post("/signals/approve-all")
def approve_all_signals():
    """Approve ALL pending signals — submit all to Alpaca."""
    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        raise HTTPException(423, f"Live trading is paused: {kill_state.get('paused_reason') or 'manually paused'}")
    from lib.alpaca_client import submit_bracket_order, normalize_symbol, get_account
    account = get_account()
    buying_power = float(account.buying_power)
    now_iso = datetime.now(timezone.utc).isoformat()
    approved = rejected = 0
    with get_db() as db:
        rows = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").order_by(TradingSignal.confidence.desc()).all()
        sigs = [s for s in rows if _is_pending_equity_candidate(s)]
        for sig in sigs:
            if buying_power < 100:
                break
            try:
                sym, crypto = normalize_symbol(sig.asset_symbol)
                entry  = float(sig.entry_price or 0)
                target = float(sig.target_price or 0)
                stop   = float(sig.stop_loss or 0)
                if not entry or not target or not stop or stop >= entry or target <= entry:
                    sig.status = "Rejected"; sig.updated_date = now_iso; rejected += 1; continue
                trade_budget = min(buying_power * 0.15, 1500)
                qty = max(1, int(trade_budget / entry)) if not crypto else round(trade_budget / entry, 6)
                submit_bracket_order(symbol=sym, qty=qty, entry_price=entry, take_profit=target, stop_loss=stop)
                sig.status = "Executed"; sig.updated_date = now_iso
                buying_power -= qty * entry
                approved += 1
            except Exception as e:
                sig.status = "Rejected"; sig.updated_date = now_iso; rejected += 1
    return {"ok": True, "approved": approved, "rejected": rejected, "buying_power_remaining": round(buying_power, 2)}

@router.post("/signals/reject-all")
def reject_all_pending():
    """Reject all pending signals."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        rows = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").all()
        sigs = [s for s in rows if _is_pending_equity_candidate(s)]
        for s in sigs:
            s.status = "Rejected"; s.updated_date = now_iso
        return {"ok": True, "rejected": len(sigs)}

@router.get("/paper/summary")
def get_paper_summary_route():
    """Full paper portfolio summary — positions, trades, equity curve."""
    try:
        from lib.paper_engine import get_paper_summary
        return get_paper_summary()
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/paper/open")
def paper_open(body: PaperOpenRequest):
    """Manually open a paper position."""
    try:
        from lib.paper_engine import open_paper_position
        from app.database import MarketAsset
        price = body.entry_price
        if not price:
            with get_db() as db:
                a = db.query(MarketAsset).filter(MarketAsset.symbol == body.symbol).first()
                if a: price = float(a.price)
        if not price:
            raise HTTPException(400, f"No price available for {body.symbol}")
        signal = {
            "id": body.signal_id, "asset_symbol": body.symbol, "asset_class": body.asset_class,
            "paper_direction": body.paper_direction, "direction": body.paper_direction,
            "entry_price": price, "target_price": body.target_price, "stop_loss": body.stop_loss,
        }
        result = open_paper_position(signal, current_price=price)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return result
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/paper/close/{pos_id}")
def paper_close(pos_id: str, price: Optional[float] = None):
    """Close a paper position at current market price."""
    try:
        from lib.paper_engine import close_paper_position
        from app.database import PaperPosition, MarketAsset
        close_price = price
        if not close_price:
            with get_db() as db:
                pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
                if pos:
                    sym = pos.symbol
                    a = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
                    if a: close_price = float(a.price or pos.current_price or pos.entry_price)
        if not close_price:
            raise HTTPException(400, "No close price available")
        return close_paper_position(pos_id, close_price, reason="manual")
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/autosim/reset")
def autosim_reset(hard: bool = False, starting_cash: float = 100000.0):
    """Reset the auto-sim funds. Default is a SOFT reset: open positions are
    closed into history, the wallet refills, and every trade row survives —
    the sim's books are learning data. `hard=true` is the old destructive
    wipe, kept only for destroying corrupt data."""
    if hard:
        from lib.auto_simulator import reset_auto_simulator
        reset_auto_simulator()
        return {"ok": True, "message": "Auto-sim HARD reset — trade history deleted"}
    from lib.auto_simulator import soft_reset_auto_simulator
    return soft_reset_auto_simulator(starting_cash=starting_cash)


@router.post("/trading/flatten")
def flatten_trading(body: FlattenRequest):
    """Close every open position, cancel every working order, and reject all
    pending signals in the chosen scope. Requires confirm="FLATTEN" so a
    stray click can never liquidate a book.

    live  — Alpaca: cancel all orders, close all positions (market), reject
            pending live signals. Alpaca offers no API to reset the paper
            account's equity to a fixed $100k — that is one click on the
            Alpaca dashboard (Account → Reset); flattening returns the
            account to all-cash at its current equity.
    paper — internal engine: close every open paper position at the latest
            known price, reject paper-pending signals, and close all open
            Auto Sim virtual positions (history and win/loss record kept).
            Cash is preserved; use /paper/reset and /autosim/reset
            afterwards for a clean $100k each.
    """
    if body.confirm != "FLATTEN":
        raise HTTPException(400, 'Confirmation required: pass confirm="FLATTEN"')
    scope = body.scope.lower()
    if scope not in ("live", "paper", "all"):
        raise HTTPException(400, "scope must be live, paper, or all")

    now_iso = datetime.now(timezone.utc).isoformat()
    out: dict = {"ok": True, "scope": scope}

    if scope in ("live", "all"):
        live_res = {"orders_cancelled": 0, "positions_closed": 0, "errors": []}
        try:
            from lib.alpaca_client import get_trading_client
            client = get_trading_client()
            try:
                cancelled = client.cancel_orders()
                live_res["orders_cancelled"] = len(cancelled or [])
            except Exception as e:
                live_res["errors"].append(f"cancel orders: {str(e)[:80]}")
            try:
                closed = client.close_all_positions(cancel_orders=True)
                live_res["positions_closed"] = len(closed or [])
            except Exception as e:
                live_res["errors"].append(f"close positions: {str(e)[:80]}")
        except Exception as e:
            live_res["errors"].append(f"alpaca client: {str(e)[:80]}")
        out["live"] = live_res

    if scope in ("paper", "all"):
        paper_res = {"positions_closed": 0, "errors": []}
        try:
            from lib.paper_engine import close_paper_position
            from app.database import PaperPosition
            with get_db() as db:
                open_ids = [
                    (row.id, row.symbol, float(row.current_price or row.entry_price or 0))
                    for row in db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
                ]
            for pid, sym, px in open_ids:
                try:
                    if px > 0:
                        close_paper_position(pid, px, reason="manual flatten")
                        paper_res["positions_closed"] += 1
                    else:
                        paper_res["errors"].append(f"{sym}: no price to close at")
                except Exception as e:
                    paper_res["errors"].append(f"{sym}: {str(e)[:60]}")
        except Exception as e:
            paper_res["errors"].append(str(e)[:80])
        out["paper"] = paper_res
        try:
            from lib.auto_simulator import flatten_auto_simulator
            out["autosim"] = flatten_auto_simulator()
        except Exception as e:
            out["autosim"] = {"closed": 0, "error": str(e)[:80]}

    # Pending signals: reject everything still awaiting action in scope.
    with get_db() as db:
        q = db.query(TradingSignal).filter(TradingSignal.status.in_(["Active", "PendingApproval"]))
        if scope == "live":
            q = q.filter(TradingSignal.paper_mode.is_(False))
        elif scope == "paper":
            q = q.filter(TradingSignal.paper_mode.is_(True))
        rejected = 0
        for sig in q.all():
            sig.status = "Rejected"
            sig.updated_date = now_iso
            rejected += 1
    out["signals_rejected"] = rejected
    logger.warning(f"[Flatten] scope={scope}: {out}")
    return out


@router.post("/paper/reset")
def paper_reset(hard: bool = False, starting_cash: float = 100000.0):
    """Reset the paper funds. Default is a SOFT reset: open positions are
    closed into history at their last price (tagged 'reset'), the wallet
    refills, and every PaperTrade row survives — outcomes, calibration and
    the postmortems all read those rows. `hard=true` is the old destructive
    wipe, kept only for destroying corrupt data."""
    try:
        if hard:
            from lib.paper_engine import reset_paper_portfolio
            r = reset_paper_portfolio()
            r["message"] = "Paper account HARD reset — trade history deleted"
            return r
        from lib.paper_engine import soft_reset_paper_portfolio
        return soft_reset_paper_portfolio(starting_cash=starting_cash)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/concentration/status")
def concentration_status():
    """Live exposure against the limits that actually block an open.

    Exists because the guard was inert for a week and nothing on screen
    could have told anyone: the operator was reading combined live+paper
    equity off the Positions tab while the limit was per-book and matching
    an empty set. This surfaces what the guard itself sees, book by book.

    Equity per book is computed the way that book's own summary computes
    it, so this panel and the P&L header cannot disagree.
    """
    from lib.concentration import POSITION_BOOKS, book_status

    def _paper_equity() -> float:
        from lib.paper_engine import get_paper_summary
        return float((get_paper_summary().get("portfolio") or {}).get("equity") or 0)

    def _auto_sim_equity() -> float:
        from lib.auto_simulator import get_auto_sim_summary
        return float((get_auto_sim_summary().get("summary") or {}).get("equity") or 0)

    equity_for = {"paper": _paper_equity, "auto_sim": _auto_sim_equity}
    books = []
    for name in POSITION_BOOKS:
        try:
            equity = equity_for[name]()
        except KeyError:
            # A registered book with no equity source is a wiring gap, not
            # a reason to drop it silently off the panel.
            books.append({"book": name, "error": "no equity source wired",
                          "symbols": [], "positions": 0})
            continue
        except Exception as e:
            books.append({"book": name, "symbols": [], "positions": 0,
                          "error": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        books.append(book_status(name, equity))

    return {"books": books,
            "any_over_limit": any(b.get("symbols_over_limit") or
                                  b.get("gross_over_limit") for b in books)}


@router.post("/reset/all")
def reset_all_virtual_books(starting_cash: float = 100000.0,
                            pause_autotrading: bool = True):
    """One reset, one clean slate — every virtual book at once.

    Reset was the only Danger Zone action without an EVERYTHING scope:
    flatten has one, reset had a button per book. So "reset the sim" meant
    the paper book only, Auto Sim kept its open positions, and the combined
    equity the Positions tab displays still showed the old concentration —
    which reads exactly like a reset that refilled cash and left the orders
    open. It isn't; it is two books and one button.

    Soft by design, like the per-book resets it calls: positions close into
    history at their last mark, every trade row survives. The live broker
    account is NOT touched — Alpaca has no reset API (see /trading/flatten).

    PAUSES automatic opening by default (§140.1). Reported six times as
    "reset never works": the cash did reset, and the books refilled inside
    one scan cycle, which from the operator's chair is the same thing as
    nothing happening. Auto Sim takes EVERY approved signal by design, so a
    reset it can immediately undo is not a clean slate — it is a flicker.
    Resuming is deliberately a separate action.
    """
    out: dict = {"ok": True, "books": {}, "errors": []}
    for name, fn in (("paper", "lib.paper_engine:soft_reset_paper_portfolio"),
                     ("auto_sim", "lib.auto_simulator:soft_reset_auto_simulator")):
        mod, _, attr = fn.partition(":")
        try:
            import importlib
            out["books"][name] = getattr(importlib.import_module(mod), attr)(
                starting_cash=starting_cash)
        except Exception as e:
            # One book failing must not leave the other unreset and the
            # operator unaware — report per book rather than 500-ing.
            out["ok"] = False
            out["errors"].append(f"{name}: {str(e)[:120]}")
            logger.error(f"[ResetAll] {name} failed: {e}")
    out["positions_closed"] = sum(
        int((r or {}).get("positions_closed") or 0)
        for r in out["books"].values())

    if pause_autotrading:
        try:
            from app.database import DEFAULT_USER_ID, UserPreference, now_iso
            with get_db() as db:
                pref = db.query(UserPreference).first()
                if pref is None:
                    pref = UserPreference(user_id=DEFAULT_USER_ID)
                    db.add(pref)
                pref.auto_sim_enabled = False
                pref.paper_auto_trade_enabled = False
                pref.updated_at = now_iso()
            out["autotrading_paused"] = True
            out["resume_with"] = "POST /api/trading/autotrading {\"enabled\": true}"
        except Exception as e:
            # A reset that silently failed to pause would refill and look
            # exactly like the bug this is fixing, so it is reported.
            out["ok"] = False
            out["autotrading_paused"] = False
            out["errors"].append(f"pause: {str(e)[:120]}")
            logger.error(f"[ResetAll] pause failed: {e}")
    else:
        out["autotrading_paused"] = False

    logger.warning(f"[ResetAll] {out}")
    return out


@router.get("/trading/autotrading")
def get_autotrading_state():
    """Whether the two SIMULATED books open positions on their own.

    Separate from the kill switch, which governs the live broker account
    only. These are what a reset pauses.
    """
    from app.database import UserPreference
    with get_db() as db:
        pref = db.query(UserPreference).first()
        auto_sim = bool(pref.auto_sim_enabled) if pref else True
        paper = bool(pref.paper_auto_trade_enabled) if pref else True
    return {"auto_sim_enabled": auto_sim, "paper_auto_trade_enabled": paper,
            "any_enabled": auto_sim or paper}


@router.post("/trading/autotrading")
def set_autotrading_state(enabled: bool | None = Body(None, embed=True),
                          paper: bool | None = Body(None, embed=True),
                          auto_sim: bool | None = Body(None, embed=True)):
    """Pause or resume automatic opening, PER BOOK.

    They are governed separately because they are in different states:
    paper mirrors the broker's criteria and is ready to run, while Auto Sim
    is being rebuilt from "take every approved signal" into a full virtual
    exchange. Running the two together would keep pouring untrustworthy
    outcomes into a book the operator eventually wants to train on.

    `enabled` still sets both, for existing callers; an explicit `paper` or
    `auto_sim` overrides it.

    SIMULATED books only. The live broker account is behind the kill switch
    and is deliberately unreachable from here — one endpoint that can arm
    real orders alongside simulated ones is a mistake waiting for a typo.
    """
    from app.database import DEFAULT_USER_ID, UserPreference, now_iso
    if enabled is None and paper is None and auto_sim is None:
        raise HTTPException(400, "pass `enabled`, or `paper` / `auto_sim`")

    want_paper = paper if paper is not None else enabled
    want_sim = auto_sim if auto_sim is not None else enabled

    with get_db() as db:
        # Filtered by user_id, matching how both jobs READ it. `.first()`
        # would drift onto a different row the moment a second user exists,
        # and the flag would silently stop governing anything.
        pref = db.query(UserPreference).filter(
            UserPreference.user_id == DEFAULT_USER_ID).first()
        if pref is None:
            pref = UserPreference(user_id=DEFAULT_USER_ID)
            db.add(pref)
        if want_paper is not None:
            pref.paper_auto_trade_enabled = bool(want_paper)
        if want_sim is not None:
            pref.auto_sim_enabled = bool(want_sim)
        pref.updated_at = now_iso()
        out = {"auto_sim_enabled": bool(pref.auto_sim_enabled),
               "paper_auto_trade_enabled": bool(pref.paper_auto_trade_enabled)}
    out["any_enabled"] = out["auto_sim_enabled"] or out["paper_auto_trade_enabled"]
    logger.warning(f"[AutoTrading] paper="
                   f"{'ON' if out['paper_auto_trade_enabled'] else 'PAUSED'} "
                   f"auto_sim={'ON' if out['auto_sim_enabled'] else 'PAUSED'}")
    return out


@router.post("/paper/run-mtm")
def paper_run_mtm():
    """Manually trigger mark-to-market cycle."""
    try:
        from lib.paper_engine import mark_to_market
        with get_db() as db:
            assets = db.query(MarketAsset).all()
            prices = {a.symbol: float(a.price) for a in assets if a.price}
        result = mark_to_market(prices)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/paper/debug")
def paper_debug():
    """Debug endpoint — returns raw DB counts and first few records for diagnosis."""
    try:
        from app.database import PaperPosition, PaperTrade, PaperPortfolio
        with get_db() as db:
            portfolio = db.query(PaperPortfolio).first()
            all_positions = db.query(PaperPosition).all()
            open_positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
            all_trades = db.query(PaperTrade).all()
            
            return {
                "portfolio": {
                    "cash": float(portfolio.cash) if portfolio else None,
                    "total_trades": portfolio.total_trades if portfolio else None,
                    "winning_trades": portfolio.winning_trades if portfolio else None,
                    "realized_pnl": float(portfolio.realized_pnl) if portfolio else None,
                } if portfolio else None,
                "position_counts": {
                    "total": len(all_positions),
                    "open": len(open_positions),
                    "by_status": {s: sum(1 for p in all_positions if p.status == s) 
                                  for s in set(p.status for p in all_positions)},
                },
                "trade_count": len(all_trades),
                "sample_open_positions": [
                    {
                        "id": p.id, "symbol": p.symbol, "status": p.status,
                        "direction": p.direction, "side": p.side,
                        "entry_price": float(p.entry_price) if p.entry_price else None,
                        "qty": float(p.qty) if p.qty else None,
                        "margin_used": float(p.margin_used) if p.margin_used else None,
                        "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl is not None else None,
                        "opened_at": p.opened_at,
                    }
                    for p in open_positions[:5]
                ],
                "sample_trades": [
                    {
                        "id": t.id, "symbol": t.symbol, "direction": t.direction,
                        "realized_pnl": float(t.realized_pnl) if t.realized_pnl else None,
                        "close_reason": t.close_reason, "closed_at": t.closed_at,
                    }
                    for t in all_trades[:5]
                ],
            }
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/paper/positions")
def get_paper_positions(status: str = "Open"):
    """List paper positions filtered by status."""
    try:
        from app.database import PaperPosition
        with get_db() as db:
            q = db.query(PaperPosition)
            if status != "all": q = q.filter(PaperPosition.status == status)
            positions = q.order_by(PaperPosition.opened_at.desc()).limit(100).all()
            return [
                {
                    "id": p.id, "symbol": p.symbol, "asset_class": p.asset_class,
                    "direction": p.direction, "side": p.side, "leverage": p.leverage,
                    "qty": float(p.qty), "entry_price": float(p.entry_price),
                    "current_price": float(p.current_price or p.entry_price),
                    "target_price": float(p.target_price or 0),
                    "stop_loss": float(p.stop_loss or 0),
                    "notional": float(p.notional or 0),
                    "unrealized_pnl": float(p.unrealized_pnl or 0),
                    "unrealized_pct": float(p.unrealized_pct or 0),
                    "status": p.status, "opened_at": p.opened_at,
                }
                for p in positions
            ]
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/paper/trades")
def get_paper_trades(limit: int = 100):
    """List completed paper trades."""
    try:
        from app.database import PaperTrade
        with get_db() as db:
            trades = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(limit).all()
            return [
                {
                    "id": t.id, "symbol": t.symbol, "direction": t.direction,
                    "leverage": t.leverage, "entry_price": float(t.entry_price),
                    "exit_price": float(t.exit_price),
                    "realized_pnl": round(float(t.realized_pnl), 2),
                    "pnl_pct": round(float(t.pnl_pct), 2),
                    "close_reason": t.close_reason, "opened_at": t.opened_at, "closed_at": t.closed_at,
                    "asset_class": t.asset_class,
                }
                for t in trades
            ]
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/signals/{signal_id}/paper-execute")
def paper_execute_signal(signal_id: str, direction: str = "Long"):
    """Send an existing signal to the paper engine with specified direction."""
    try:
        from lib.paper_engine import open_paper_position
        from app.database import MarketAsset
        with get_db() as db:
            sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
            if not sig: raise HTTPException(404, "Signal not found")
            sym = sig.asset_symbol
            a = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
            price = float(a.price) if a and a.price else float(sig.entry_price or 0)
            sig_data = {
                "id": sig.id, "asset_symbol": sym, "asset_class": sig.asset_class,
                "paper_direction": direction, "entry_price": price,
                "target_price": sig.target_price, "stop_loss": sig.stop_loss,
            }

        result = open_paper_position(sig_data, current_price=price)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return result
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ── AI Decision Log ────────────────────────────────────────────────────────────

@router.get("/futures/prices")
def get_futures_prices(paper_only: bool = False):
    """Return latest futures/forex/commodity prices."""
    try:
        from lib.futures_data import fetch_all_futures_prices, PAPER_FUTURES, FUTURES_UNIVERSE
        syms = PAPER_FUTURES if paper_only else list(FUTURES_UNIVERSE.keys())
        return fetch_all_futures_prices(syms)
    except Exception as e:
        logger.error(f"[API] /futures/prices: {e}")
        return {}

@router.get("/futures/news")
def get_futures_news(limit: int = 30):
    """Return recent futures/commodity/forex news articles."""
    try:
        from lib.futures_data import fetch_futures_news
        return fetch_futures_news(max_total=limit)
    except Exception as e:
        logger.error(f"[API] /futures/news: {e}")
        return []

@router.get("/futures/universe")
def get_futures_universe():
    """Return the full futures symbol registry."""
    try:
        from lib.futures_data import FUTURES_UNIVERSE, CATEGORY_ICONS
        return [
            {"symbol": sym, **meta, "icon": CATEGORY_ICONS.get(meta.get("category",""), "📊")}
            for sym, meta in FUTURES_UNIVERSE.items()
        ]
    except Exception as e:
        logger.error(f"[API] /futures/universe: {e}")
        return []



