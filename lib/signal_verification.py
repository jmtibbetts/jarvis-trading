"""
Signal double-check — user-initiated re-verification of a trade setup
against fresh data. Deterministic: every check is arithmetic over fetched
prices; no LLM anywhere in the verdict.

Price sources by asset class:
  crypto   live exchange price via lib/crypto_market_data (free, cached ~30s)
  equity   Massive REST previous-session data (lib/massive_data — respects
           the plan's 5-calls/min budget and 5-min cache; note this is
           end-of-day data, and the verdict says so via price_asof)

Verdicts:
  CONFIRMED         current price still inside the setup's validity: entry
                    not run away, stop not breached, target not already hit
  STALE_ENTRY       price has drifted more than STALE_ENTRY_PCT from entry —
                    the fill the signal assumed no longer exists. A
                    suggested_update re-anchors entry to the current price
                    and shifts stop/target by the SAME ABSOLUTE DISTANCES
                    (preserving the original risk/reward geometry). Applied
                    only when the user explicitly clicks apply — never
                    automatically.
  INVALIDATED       stop already breached or target already reached at the
                    checked price — the trade as written is over
  DATA_UNAVAILABLE  no fresh price could be fetched; nothing is guessed

STALE_ENTRY_PCT is deliberately generous (1.5%): normal drift within a bar
shouldn't nag; a real run-away should.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STALE_ENTRY_PCT = 1.5


def fetch_current_price(symbol: str, asset_class: str | None) -> tuple[float | None, str | None, str | None]:
    """(price, source, asof) — asof 'live' for exchange quotes, 'previous_session'
    for Massive end-of-day data."""
    is_crypto = "/" in (symbol or "") or (asset_class or "").lower() == "crypto"
    if is_crypto:
        try:
            from lib.crypto_market_data import fetch_crypto_prices
            prices = fetch_crypto_prices([symbol])
            row = prices.get(symbol) or next(iter(prices.values()), None)
            if row and row.get("price"):
                return float(row["price"]), row.get("source", "exchange"), "live"
        except Exception as e:
            logger.debug(f"[Verify] Crypto price fetch failed for {symbol}: {e}")
    try:
        from lib.massive_data import get_market_summary
        summary = get_market_summary(symbol, days=3)
        prev = (summary or {}).get("previous_close")
        if prev and prev.get("close"):
            return float(prev["close"]), "massive", "previous_session"
    except Exception as e:
        logger.debug(f"[Verify] Massive price fetch failed for {symbol}: {e}")
    # Last resort: the app's own stored asset price (age unknown but real).
    try:
        from app.database import get_db, MarketAsset
        with get_db() as db:
            row = db.query(MarketAsset).filter(MarketAsset.symbol == symbol).first()
            if row and row.price:
                return float(row.price), "market_assets_cache", row.last_updated or "unknown"
    except Exception:
        pass
    return None, None, None


def verify_levels(direction: str, entry: float, target: float, stop: float,
                  current: float) -> dict:
    """Pure arithmetic verdict for one setup at one observed price."""
    short = str(direction or "").lower().startswith("short")
    checks = []

    stop_breached = current >= stop if short else current <= stop
    target_reached = current <= target if short else current >= target
    drift_pct = abs(current - entry) / entry * 100 if entry else None

    checks.append({
        "check": "stop_not_breached",
        "ok": not stop_breached,
        "detail": f"price {current:g} vs stop {stop:g}",
    })
    checks.append({
        "check": "target_not_already_reached",
        "ok": not target_reached,
        "detail": f"price {current:g} vs target {target:g}",
    })
    checks.append({
        "check": "entry_still_near",
        "ok": drift_pct is not None and drift_pct <= STALE_ENTRY_PCT,
        "detail": f"price {current:g} is {drift_pct:.2f}% from entry {entry:g}" if drift_pct is not None else "no entry",
    })

    if stop_breached or target_reached:
        verdict = "INVALIDATED"
    elif drift_pct is not None and drift_pct > STALE_ENTRY_PCT:
        verdict = "STALE_ENTRY"
    else:
        verdict = "CONFIRMED"

    suggested = None
    if verdict == "STALE_ENTRY":
        # Re-anchor to current price, PRESERVING the original absolute
        # stop/target distances — same risk geometry, fresh anchor.
        stop_dist = stop - entry
        target_dist = target - entry
        suggested = {
            "entry_price": round(current, 8),
            "stop_loss": round(current + stop_dist, 8),
            "target_price": round(current + target_dist, 8),
            "basis": "entry re-anchored to checked price; stop/target shifted by the original absolute distances",
        }

    return {"verdict": verdict, "checks": checks, "drift_pct": round(drift_pct, 3) if drift_pct is not None else None,
            "suggested_update": suggested}


def verify_signal(signal: dict) -> dict:
    """Full verification for one signal dict (needs asset_symbol, asset_class,
    direction, entry_price, target_price, stop_loss)."""
    symbol = signal.get("asset_symbol") or ""
    entry = float(signal.get("entry_price") or 0)
    target = float(signal.get("target_price") or 0)
    stop = float(signal.get("stop_loss") or 0)
    if not (entry and target and stop):
        return {"verdict": "DATA_UNAVAILABLE", "checks": [],
                "detail": "signal is missing price levels", "current_price": None}

    current, source, asof = fetch_current_price(symbol, signal.get("asset_class"))
    if current is None:
        return {"verdict": "DATA_UNAVAILABLE", "checks": [],
                "detail": "no fresh price available from any source", "current_price": None}

    result = verify_levels(signal.get("direction") or "Long", entry, target, stop, current)
    result.update({
        "current_price": current,
        "price_source": source,
        "price_asof": asof,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Deterministic re-check against fresh data. previous_session prices "
            "are end-of-day (Massive plan has no live quotes) — a CONFIRMED "
            "verdict on session-old data means 'was still valid at last close'."
        ),
    })
    return result


# --------------------------------------------------------------------------
# Deep verification — deterministic verdict PLUS fresh Python TA, MCP market
# data, and MCP news, all fed into the LLM for a second opinion. The LLM
# NEVER overrides the arithmetic verdict above; it adds an assessment layer
# (does the setup still make sense given fresh TA + news?) that the user
# reads alongside the deterministic result.
# --------------------------------------------------------------------------

def _fresh_ta_block(symbol: str) -> tuple[str | None, dict | None]:
    """Recompute multi-timeframe TA right now (same engine signals use)."""
    try:
        from lib.ohlcv import fetch_multi_timeframe
        from lib.ta_engine import analyze_symbol, build_ta_prompt_block
        bars = fetch_multi_timeframe(symbol, ["1H", "4H", "1D"])
        ta = analyze_symbol(bars)
        return build_ta_prompt_block(symbol, ta), ta
    except Exception as e:
        logger.debug(f"[DeepVerify] TA recompute failed for {symbol}: {e}")
        return None, None


def _mcp_news_block(symbol: str) -> str | None:
    """Fresh symbol news for the LLM: tavily first (keyed, structured
    title+content per story), exa as keyless fallback. Both verified live."""
    query = f"{symbol} price news today"
    try:
        from lib.mcp_client import call_tool
        import json as _json
        raw = call_tool("tavily", "tavily_search", {"query": query, "max_results": 5})
        if raw:
            try:
                data = _json.loads(raw) if isinstance(raw, str) else raw
                lines = []
                for r in (data.get("results") or [])[:5]:
                    title = str(r.get("title") or "").strip()[:110]
                    content = str(r.get("content") or "").strip()[:250]
                    if title:
                        lines.append(f"- {title}: {content}" if content else f"- {title}")
                if lines:
                    return "FRESH NEWS (tavily web search — unverified):\n" + "\n".join(lines)[:1800]
            except Exception:
                return f"FRESH NEWS (tavily web search — unverified):\n{str(raw)[:1800]}"
        raw = call_tool("exa", "web_search_exa", {"query": query, "numResults": 5})
        if raw:
            return f"FRESH NEWS (exa web search — unverified):\n{str(raw)[:1800]}"
    except Exception as e:
        logger.debug(f"[DeepVerify] MCP news failed for {symbol}: {e}")
    return None


def _market_data_block(symbol: str, asset_class: str | None) -> str | None:
    """Massive REST summary (equity) or crypto derivatives snapshot."""
    is_crypto = "/" in (symbol or "") or (asset_class or "").lower() == "crypto"
    try:
        if is_crypto:
            parts = []
            from lib.crypto_derivatives import fetch_derivatives_snapshot
            snap = fetch_derivatives_snapshot(symbol)
            if snap:
                import json as _json
                parts.append("CRYPTO DERIVATIVES (live, free exchange APIs):\n" + _json.dumps(snap, default=str)[:1200])
            try:
                from lib.mcp_client import coingecko_snapshot
                cg = coingecko_snapshot([symbol])
                if cg:
                    parts.append("COINGECKO MARKET DATA (live, keyless MCP):\n" + str(cg)[:1000])
            except Exception as e:
                logger.debug(f"[DeepVerify] CoinGecko snapshot failed for {symbol}: {e}")
            if parts:
                return "\n\n".join(parts)
        else:
            # Forex first — AllRatesToday has live interbank rates, which beat
            # Massive's previous-session data for currency pairs.
            try:
                from lib.allrates_data import fx_summary_block
                fx = fx_summary_block(symbol)
                if fx:
                    return fx
            except Exception as e:
                logger.debug(f"[DeepVerify] FX block failed for {symbol}: {e}")
            from lib.massive_data import get_market_summary
            summary = get_market_summary(symbol, days=5)
            if summary:
                import json as _json
                return "MARKET DATA (Massive REST, previous sessions — not live quotes):\n" + _json.dumps(summary, default=str)[:1500]
    except Exception as e:
        logger.debug(f"[DeepVerify] Market data block failed for {symbol}: {e}")
    return None


def deep_verify_signal(signal: dict) -> dict:
    """Deterministic verify + LLM second opinion over fresh TA, market data,
    and news. Returns the deterministic result with an `llm_assessment` key
    added ({assessment, confidence, reasoning} or an honest unavailability
    note). Costs: 1 tavily call, up to 1 Massive call, 1 LLM call."""
    base = verify_signal(signal)
    symbol = signal.get("asset_symbol") or ""

    ta_block, ta = _fresh_ta_block(symbol)
    news_block = _mcp_news_block(symbol)
    md_block = _market_data_block(symbol, signal.get("asset_class"))

    blocks = [b for b in (ta_block, md_block, news_block) if b]
    context_used = {
        "fresh_ta": ta_block is not None,
        "market_data": md_block is not None,
        "web_news": news_block is not None,
    }
    if not blocks:
        base["llm_assessment"] = {
            "assessment": "UNAVAILABLE",
            "reasoning": "No fresh context (TA, market data, or news) could be gathered — skipping LLM opinion rather than guessing.",
            "context_used": context_used,
        }
        return base

    import json as _json
    prompt = (
        f"You are double-checking an EXISTING trade signal. Do not invent prices.\n\n"
        f"SIGNAL: {symbol} {signal.get('direction')} | entry {signal.get('entry_price')} | "
        f"target {signal.get('target_price')} | stop {signal.get('stop_loss')} | "
        f"timeframe {signal.get('timeframe')} | original reasoning: {str(signal.get('reasoning') or '')[:400]}\n\n"
        f"DETERMINISTIC RE-CHECK (arithmetic, trust it): {_json.dumps({k: base.get(k) for k in ('verdict', 'current_price', 'price_asof', 'drift_pct')})}\n\n"
        + "\n\n".join(blocks)
        + "\n\nGiven the FRESH data above, does this trade still make sense? "
          "Answer JSON only: {\"assessment\": \"AGREE|DISAGREE|UNCERTAIN\", "
          "\"confidence\": 0-100, \"reasoning\": \"2-3 sentences citing the fresh data\", "
          "\"key_change\": \"what changed since the signal was generated, or 'nothing material'\"}"
    )
    try:
        from lib.lmstudio import call_lm_studio, parse_json
        text = call_lm_studio(prompt, system="You are a rigorous trade verification analyst. JSON only.",
                              max_tokens=400, request_timeout=90)
        parsed = parse_json(text) or {}
        base["llm_assessment"] = {
            "assessment": str(parsed.get("assessment") or "UNCERTAIN").upper(),
            "confidence": parsed.get("confidence"),
            "reasoning": parsed.get("reasoning"),
            "key_change": parsed.get("key_change"),
            "context_used": context_used,
        }
        # A confidently-rejected thesis is a tradeable observation: offer the
        # flipped setup with computed levels (never model-invented prices).
        base["reversal_proposal"] = propose_reversal(
            signal, base.get("current_price") or 0.0, ta, base["llm_assessment"]
        )
    except Exception as e:
        base["llm_assessment"] = {
            "assessment": "UNAVAILABLE",
            "reasoning": f"LLM unavailable ({str(e)[:80]}) — deterministic verdict above still stands.",
            "context_used": context_used,
        }
    return base


# --------------------------------------------------------------------------
# Reversal proposals — when deep verify's AI DISAGREES with a live signal,
# the setup failing is itself information: the thesis broke, and the
# opposite side often has the better odds. This proposes the flipped trade
# with DETERMINISTIC levels (ATR-derived, horizon-capped) so the LLM never
# invents a price. It is a PROPOSAL only — nothing trades until the user
# accepts it.
# --------------------------------------------------------------------------

REVERSAL_MIN_CONFIDENCE = 70   # AI must be this sure the original is wrong
DEFAULT_RR = 2.0


def _atr_from_ta(ta: dict | None, timeframe: str | None) -> float | None:
    """ATR for the signal's own timeframe, falling back to any available."""
    if not isinstance(ta, dict):
        return None
    order = [timeframe] if timeframe else []
    order += ["1H", "4H", "1D"]
    for tf in order:
        block = ta.get(tf) if tf else None
        if isinstance(block, dict):
            atr = (block.get("atr") or {}).get("value")
            if atr and float(atr) > 0:
                return float(atr)
    return None


def propose_reversal(signal: dict, current_price: float, ta: dict | None,
                     assessment: dict | None) -> dict | None:
    """Build the opposite-direction setup from the same symbol.

    Levels are computed, never generated: stop sits one ATR beyond entry
    (clamped by the horizon cap — 3% scalp / 10% longer), target at
    DEFAULT_RR times that risk. Returns None when the AI did not clearly
    reject the original, or when no ATR is available to size risk honestly.
    """
    a = assessment or {}
    if str(a.get("assessment", "")).upper() != "DISAGREE":
        return None
    try:
        conf = float(a.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < REVERSAL_MIN_CONFIDENCE:
        return None
    if not current_price or current_price <= 0:
        return None

    was_short = str(signal.get("direction") or "").lower().startswith("short")
    new_direction = "Long" if was_short else "Short"

    atr = _atr_from_ta(ta, signal.get("timeframe"))
    if not atr:
        return None

    from lib.trading_preferences import horizon_for_timeframe
    horizon = horizon_for_timeframe(signal.get("timeframe"))
    cap_frac = 0.03 if horizon == "scalp" else 0.10
    risk = min(atr, current_price * cap_frac)
    if risk <= 0:
        return None

    entry = current_price
    if new_direction == "Short":
        stop = entry + risk
        target = entry - risk * DEFAULT_RR
    else:
        stop = entry - risk
        target = entry + risk * DEFAULT_RR

    digits = 8 if entry < 1 else 4
    return {
        "direction": new_direction,
        "entry_price": round(entry, digits),
        "stop_loss": round(stop, digits),
        "target_price": round(target, digits),
        "rr_ratio": DEFAULT_RR,
        "risk_per_unit": round(risk, digits),
        "basis": (
            f"Reversal of a failing {signal.get('direction')} setup. Entry at the checked price; "
            f"stop one ATR ({atr:.6g}) away, capped at {cap_frac:.0%} for a {horizon} horizon; "
            f"target at {DEFAULT_RR:g}x risk. Levels are computed from ATR, not model output."
        ),
        "ai_reasoning": a.get("reasoning"),
        "ai_confidence": conf,
        "warning": (
            "A failing thesis does not guarantee the opposite works — the market may simply "
            "be ranging. This is a proposal; review it before accepting."
        ),
    }
