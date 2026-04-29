"""
lib/learning_engine.py
──────────────────────
Tier 1: Record trade outcomes when positions close.
Tier 2: Aggregate signal accuracy stats per symbol/timeframe.
         Inject historical win-rate context into LLM prompts.
Tier 3: Pattern Memory — fingerprint TA setups, track win/loss per pattern.
         Inject "This setup has won 8/12 times" into LLM prompts.
Tier 4: Regime Awareness — track strategy performance per market regime.
         Auto-weight signal confidence based on regime history.
Tier 5: LLM Reasoning Audit — when a trade loses, feed reasoning + outcome
         back to LLM, ask what it missed. Store reflections, inject top lessons.
"""
import logging
import uuid
import json
import hashlib
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _lazy_ensure():
    """Call once per process to guarantee all learning tables exist.
    Safe to call from any public function — no-ops after first call."""
    global _tables_ensured
    if _tables_ensured:
        return
    try:
        from app.database import engine
        with engine.begin() as conn:
            _ensure_tables(conn)
        _tables_ensured = True
    except Exception as _e:
        import logging
        logging.getLogger(__name__).debug(f"[Learning] lazy_ensure skipped: {_e}")


def _new_id():
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# DB BOOTSTRAP — ensure all learning tables exist (idempotent)
# ─────────────────────────────────────────────────────────────────────────────

_tables_ensured = False  # module-level flag — only run once per process

def _ensure_tables(conn):
    from sqlalchemy import text
    conn.execute(text("""CREATE TABLE IF NOT EXISTS trade_outcomes (
        id TEXT PRIMARY KEY, signal_id TEXT, symbol TEXT, asset_class TEXT,
        direction TEXT, timeframe TEXT, entry_price REAL, exit_price REAL,
        qty REAL, pnl_usd REAL, pnl_pct REAL, outcome TEXT, exit_reason TEXT,
        hold_duration_m REAL, signal_confidence REAL, signal_score REAL,
        signal_reasoning TEXT, ta_summary TEXT, market_regime TEXT,
        paper_mode INTEGER DEFAULT 0, entered_at TEXT, exited_at TEXT
    )"""))
    conn.execute(text("""CREATE TABLE IF NOT EXISTS signal_accuracy (
        id TEXT PRIMARY KEY, symbol TEXT, asset_class TEXT, timeframe TEXT,
        total_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0, win_rate REAL DEFAULT 0.0,
        avg_pnl_pct REAL DEFAULT 0.0, avg_hold_min REAL DEFAULT 0.0,
        best_pnl_pct REAL DEFAULT 0.0, worst_pnl_pct REAL DEFAULT 0.0,
        last_updated TEXT
    )"""))
    # Tier 3 — pattern memory
    conn.execute(text("""CREATE TABLE IF NOT EXISTS pattern_memory (
        id TEXT PRIMARY KEY,
        fingerprint TEXT UNIQUE,        -- stable hash of TA setup conditions
        pattern_desc TEXT,              -- human-readable description
        asset_class TEXT,               -- equity | crypto | all
        timeframe TEXT,
        total INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        win_rate REAL DEFAULT 0.0,
        avg_pnl_pct REAL DEFAULT 0.0,
        last_seen TEXT,
        last_updated TEXT
    )"""))
    # Tier 4 — regime performance
    conn.execute(text("""CREATE TABLE IF NOT EXISTS regime_performance (
        id TEXT PRIMARY KEY,
        regime TEXT UNIQUE,             -- Risk-On Bull | Range-Bound | Bear/Risk-Off | etc.
        total INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        win_rate REAL DEFAULT 0.0,
        avg_pnl_pct REAL DEFAULT 0.0,
        avg_confidence REAL DEFAULT 0.0,
        last_updated TEXT
    )"""))
    # Tier 5 — LLM reasoning audit / lessons
    conn.execute(text("""CREATE TABLE IF NOT EXISTS llm_lessons (
        id TEXT PRIMARY KEY,
        trade_outcome_id TEXT,
        symbol TEXT,
        outcome TEXT,                   -- WIN | LOSS
        original_reasoning TEXT,
        lesson TEXT,                    -- LLM's self-reflection
        lesson_category TEXT,           -- TA_MISS | REGIME_MISS | NEWS_MISS | TIMING | OTHER
        applied_count INTEGER DEFAULT 0,-- how many times injected into prompts
        created_at TEXT
    )"""))


# ─────────────────────────────────────────────────────────────────────────────
# TIER 3 — Pattern Fingerprinting
# ─────────────────────────────────────────────────────────────────────────────

def _fingerprint_ta(ta_profile: dict, direction: str) -> tuple[str, str]:
    """
    Extract key boolean TA conditions from the 4H timeframe (fallback 1H/1D),
    hash them into a stable fingerprint, and return (fingerprint, description).
    """
    # Prefer 4H, fallback chain
    tf_data = None
    for tf in ["4H", "1H", "2H", "1D"]:
        tf_data = ta_profile.get(tf)
        if tf_data and not tf_data.get("error"):
            break
    if not tf_data:
        return None, None

    rsi         = tf_data.get("rsi") or 50
    rsi_sig     = tf_data.get("rsi_signal", "neutral")
    macd        = tf_data.get("macd") or {}
    macd_trend  = macd.get("trend", "unknown")
    macd_cross  = macd.get("crossover", False)
    bb          = tf_data.get("bollinger_bands") or {}
    bb_pos      = bb.get("position", "mid")
    vol         = tf_data.get("volume") or {}
    vol_surge   = bool(vol.get("surge"))
    vol_dry     = bool(vol.get("dry"))
    adx         = tf_data.get("adx") or {}
    adx_strong  = bool(adx.get("strong"))
    bias        = tf_data.get("bias", "neutral").lower()
    obv         = tf_data.get("obv_trend", "neutral")
    stoch       = tf_data.get("stochastic") or {}
    stoch_sig   = stoch.get("signal", "neutral")
    vwap        = tf_data.get("vwap") or {}
    vwap_pos    = vwap.get("position", "unknown")

    # Bucket RSI
    if rsi < 30:    rsi_bucket = "oversold"
    elif rsi < 45:  rsi_bucket = "low"
    elif rsi < 55:  rsi_bucket = "mid"
    elif rsi < 70:  rsi_bucket = "high"
    else:           rsi_bucket = "overbought"

    conditions = {
        "dir":        direction.lower()[:4],   # long/shor
        "rsi":        rsi_bucket,
        "macd_trend": macd_trend,
        "macd_cross": macd_cross,
        "bb_pos":     bb_pos,
        "vol_surge":  vol_surge,
        "adx_strong": adx_strong,
        "bias":       bias,
        "vwap":       vwap_pos,
        "stoch":      stoch_sig,
    }

    fp_str    = json.dumps(conditions, sort_keys=True)
    fp_hash   = hashlib.md5(fp_str.encode()).hexdigest()[:12]

    # Human-readable description
    parts = [f"Dir={conditions['dir'].upper()}"]
    parts.append(f"RSI={rsi_bucket}")
    parts.append(f"MACD={macd_trend}" + (" ✗cross" if macd_cross else ""))
    parts.append(f"BB={bb_pos}")
    if vol_surge: parts.append("VOL_SURGE")
    if vol_dry:   parts.append("VOL_DRY")
    if adx_strong: parts.append("ADX_STRONG")
    parts.append(f"Bias={bias}")
    if vwap_pos not in ("unknown", ""):
        parts.append(f"VWAP={vwap_pos}")

    desc = " | ".join(parts)
    return fp_hash, desc


def _update_pattern_memory(fingerprint: str, desc: str, asset_class: str,
                            timeframe: str, outcome: str, pnl_pct: float, conn):
    from sqlalchemy import text
    if not fingerprint:
        return

    existing = conn.execute(text(
        "SELECT id, total, wins, losses, avg_pnl_pct FROM pattern_memory WHERE fingerprint=:fp"
    ), {"fp": fingerprint}).fetchone()

    now = _now_iso()
    win = 1 if outcome == "WIN" else 0
    loss = 1 if outcome == "LOSS" else 0

    if existing:
        row_id, total, wins, losses, avg_pnl = existing
        new_total  = total + 1
        new_wins   = wins + win
        new_losses = losses + loss
        new_avg    = round(((avg_pnl * total) + pnl_pct) / new_total, 4)
        new_wr     = round(new_wins / new_total, 4)
        conn.execute(text("""
            UPDATE pattern_memory SET total=:t, wins=:w, losses=:l,
            win_rate=:wr, avg_pnl_pct=:ap, last_seen=:ls, last_updated=:lu
            WHERE fingerprint=:fp
        """), {"t": new_total, "w": new_wins, "l": new_losses,
               "wr": new_wr, "ap": new_avg,
               "ls": now, "lu": now, "fp": fingerprint})
    else:
        wr = 1.0 if win else 0.0
        conn.execute(text("""
            INSERT INTO pattern_memory
            (id, fingerprint, pattern_desc, asset_class, timeframe,
             total, wins, losses, win_rate, avg_pnl_pct, last_seen, last_updated)
            VALUES
            (:id, :fp, :desc, :ac, :tf, 1, :w, :l, :wr, :ap, :ls, :lu)
        """), {"id": _new_id(), "fp": fingerprint, "desc": desc,
               "ac": asset_class, "tf": timeframe,
               "w": win, "l": loss, "wr": wr, "ap": round(pnl_pct, 4),
               "ls": now, "lu": now})


def get_pattern_context(ta_profile: dict, direction: str) -> str:
    """
    Returns a text block for LLM injection: pattern win-rate history.
    """
    _lazy_ensure()
    from app.database import engine
    from sqlalchemy import text
    try:
        fp, desc = _fingerprint_ta(ta_profile, direction)
        if not fp:
            return ""
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT total, wins, win_rate, avg_pnl_pct FROM pattern_memory WHERE fingerprint=:fp"
            ), {"fp": fp}).fetchone()
        if not row or row[0] < 3:
            return ""   # not enough data to be meaningful
        total, wins, wr, avg_pnl = row
        pct = round(wr * 100, 0)
        sign = "+" if avg_pnl >= 0 else ""
        return (f"\n🧠 PATTERN MEMORY: This exact TA setup has occurred {total} times — "
                f"{wins}/{total} wins ({pct:.0f}% win rate, avg {sign}{avg_pnl:.2f}% P&L). "
                f"Pattern: {desc}\n")
    except Exception as e:
        logger.warning(f"[Learning-T3] get_pattern_context error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# TIER 4 — Regime Awareness
# ─────────────────────────────────────────────────────────────────────────────

def _update_regime_performance(regime: str, outcome: str, pnl_pct: float,
                                confidence: float, conn):
    from sqlalchemy import text
    if not regime or regime == "Unknown":
        return

    existing = conn.execute(text(
        "SELECT id, total, wins, losses, avg_pnl_pct, avg_confidence FROM regime_performance WHERE regime=:r"
    ), {"r": regime}).fetchone()

    now = _now_iso()
    win  = 1 if outcome == "WIN" else 0
    loss = 1 if outcome == "LOSS" else 0
    conf = confidence or 0.0

    if existing:
        row_id, total, wins, losses, avg_pnl, avg_conf = existing
        new_total = total + 1
        new_wins  = wins + win
        new_losses = losses + loss
        new_avg   = round(((avg_pnl * total) + pnl_pct) / new_total, 4)
        new_wr    = round(new_wins / new_total, 4)
        new_conf  = round(((avg_conf * total) + conf) / new_total, 2)
        conn.execute(text("""
            UPDATE regime_performance SET total=:t, wins=:w, losses=:l,
            win_rate=:wr, avg_pnl_pct=:ap, avg_confidence=:ac, last_updated=:lu
            WHERE regime=:r
        """), {"t": new_total, "w": new_wins, "l": new_losses,
               "wr": new_wr, "ap": new_avg, "ac": new_conf,
               "lu": now, "r": regime})
    else:
        wr = 1.0 if win else 0.0
        conn.execute(text("""
            INSERT INTO regime_performance
            (id, regime, total, wins, losses, win_rate, avg_pnl_pct, avg_confidence, last_updated)
            VALUES (:id, :r, 1, :w, :l, :wr, :ap, :ac, :lu)
        """), {"id": _new_id(), "r": regime, "w": win, "l": loss,
               "wr": wr, "ap": round(pnl_pct, 4), "ac": round(conf, 2), "lu": now})


def get_regime_context(current_regime: str) -> str:
    """
    Returns a text block for LLM injection: how the bot has performed historically
    in this exact market regime.
    """
    _lazy_ensure()
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT regime, total, wins, win_rate, avg_pnl_pct, avg_confidence FROM regime_performance ORDER BY total DESC"
            )).fetchall()

        if not rows:
            return ""

        lines = ["\n📈 REGIME PERFORMANCE HISTORY:"]
        for r in rows:
            regime, total, wins, wr, avg_pnl, avg_conf = r
            if total < 2:
                continue
            marker = " ◄ CURRENT" if regime == current_regime else ""
            pct = round(wr * 100, 0)
            sign = "+" if avg_pnl >= 0 else ""
            lines.append(
                f"  {'→' if regime == current_regime else '  '} {regime}: "
                f"{wins}/{total} wins ({pct:.0f}%) | avg {sign}{avg_pnl:.2f}% | "
                f"avg confidence {avg_conf:.0f}{marker}"
            )

        if len(lines) == 1:
            return ""
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[Learning-T4] get_regime_context error: {e}")
        return ""


def get_confidence_adjustment(current_regime: str, base_confidence: float) -> float:
    """
    Adjust signal confidence based on historical win rate in this regime.
    Returns adjusted confidence (capped 10-99).
    """
    _lazy_ensure()
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT total, win_rate FROM regime_performance WHERE regime=:r"
            ), {"r": current_regime}).fetchone()
        if not row or row[0] < 5:
            return base_confidence
        total, wr = row
        # Scale: if win_rate > 0.6 → boost up to +5pts, < 0.4 → penalize up to -10pts
        if wr >= 0.6:
            adj = +5.0 * ((wr - 0.6) / 0.4)
        elif wr < 0.4:
            adj = -10.0 * ((0.4 - wr) / 0.4)
        else:
            adj = 0.0
        adjusted = max(10.0, min(99.0, base_confidence + adj))
        return round(adjusted, 1)
    except Exception as e:
        logger.warning(f"[Learning-T4] confidence_adjustment error: {e}")
        return base_confidence


# ─────────────────────────────────────────────────────────────────────────────
# TIER 5 — LLM Reasoning Audit
# ─────────────────────────────────────────────────────────────────────────────

AUDIT_PROMPT = """You are a trading AI performing a post-trade self-review.

A trade you generated has now closed. Review your original reasoning and the outcome, then identify exactly what you missed or got right.

ORIGINAL REASONING:
{reasoning}

TRADE RESULT:
Symbol: {symbol}
Direction: {direction}
Entry: ${entry_price}
Exit: ${exit_price}
P&L: {pnl_pct:+.2f}%
Outcome: {outcome}
Exit reason: {exit_reason}
Hold duration: {hold_min:.0f} minutes
Market regime at entry: {regime}

Your task:
1. Identify the PRIMARY factor that caused this {outcome} (one sentence).
2. State one specific thing you should watch for next time on {symbol} or similar setups.
3. Categorize your lesson: TA_MISS | REGIME_MISS | NEWS_MISS | TIMING | CORRECT_CALL | OTHER

Respond ONLY with this JSON:
{{"lesson": "your 1-2 sentence lesson", "category": "CATEGORY"}}"""


def _run_reasoning_audit(outcome_row: dict) -> dict | None:
    """
    Calls LLM with the original signal reasoning + outcome and stores a lesson.
    Only runs for LOSS trades (or big WIN outliers > 5%).
    """
    try:
        from lib.lmstudio import call_lm_studio, parse_json
        from app.database import engine
        from sqlalchemy import text

        reasoning = outcome_row.get("signal_reasoning") or ""
        if not reasoning or len(reasoning) < 20:
            return None

        prompt = AUDIT_PROMPT.format(
            reasoning=reasoning,
            symbol=outcome_row.get("symbol", ""),
            direction=outcome_row.get("direction", ""),
            entry_price=outcome_row.get("entry_price", 0),
            exit_price=outcome_row.get("exit_price", 0),
            pnl_pct=outcome_row.get("pnl_pct", 0),
            outcome=outcome_row.get("outcome", ""),
            exit_reason=outcome_row.get("exit_reason", ""),
            hold_min=outcome_row.get("hold_duration_m") or 0,
            regime=outcome_row.get("market_regime") or "Unknown",
        )

        raw = call_lm_studio(prompt, system="You are a self-reviewing trading AI.", max_tokens=200, temperature=0.1)
        result = parse_json(raw)

        if not result or not result.get("lesson"):
            return None

        lesson     = result.get("lesson", "")[:500]
        category   = result.get("category", "OTHER")
        now        = _now_iso()
        lesson_id  = _new_id()

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO llm_lessons
                (id, trade_outcome_id, symbol, outcome, original_reasoning,
                 lesson, lesson_category, applied_count, created_at)
                VALUES (:id, :oid, :sym, :out, :orig, :lesson, :cat, 0, :now)
            """), {
                "id":     lesson_id,
                "oid":    outcome_row.get("id"),
                "sym":    outcome_row.get("symbol"),
                "out":    outcome_row.get("outcome"),
                "orig":   reasoning[:1000],
                "lesson": lesson,
                "cat":    category,
                "now":    now,
            })

        logger.info(f"[Learning-T5] Lesson stored for {outcome_row.get('symbol')}: [{category}] {lesson[:80]}")
        return {"lesson": lesson, "category": category}

    except Exception as e:
        logger.error(f"[Learning-T5] reasoning audit failed: {e}")
        return None


def get_lessons_context(symbol: str = None, limit: int = 5) -> str:
    """
    Returns the most recent/relevant lessons for LLM prompt injection.
    Increments applied_count so we can track usage.
    """
    _lazy_ensure()
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            if symbol:
                rows = conn.execute(text("""
                    SELECT id, symbol, outcome, lesson, lesson_category
                    FROM llm_lessons
                    WHERE symbol=:sym
                    ORDER BY created_at DESC LIMIT :lim
                """), {"sym": symbol, "lim": limit}).fetchall()
                # Also grab global lessons if not enough symbol-specific ones
                if len(rows) < 3:
                    extra = conn.execute(text("""
                        SELECT id, symbol, outcome, lesson, lesson_category
                        FROM llm_lessons
                        WHERE symbol != :sym
                        ORDER BY created_at DESC LIMIT :lim
                    """), {"sym": symbol, "lim": limit - len(rows)}).fetchall()
                    rows = list(rows) + list(extra)
            else:
                rows = conn.execute(text("""
                    SELECT id, symbol, outcome, lesson, lesson_category
                    FROM llm_lessons
                    ORDER BY created_at DESC LIMIT :lim
                """), {"lim": limit}).fetchall()

            if not rows:
                return ""

            # Increment applied_count for fetched lessons
            ids = [r[0] for r in rows]
            for lid in ids:
                conn.execute(text(
                    "UPDATE llm_lessons SET applied_count = applied_count + 1 WHERE id=:id"
                ), {"id": lid})

        icons = {"LOSS": "❌", "WIN": "✅"}
        lines = [f"\n📝 RECENT LESSONS FROM PAST TRADES (top {len(rows)}):"]
        for r in rows:
            _, sym, outcome, lesson, category = r
            icon = icons.get(outcome, "➖")
            lines.append(f"  {icon} [{category}] {sym}: {lesson}")
        lines.append("")
        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[Learning-T5] get_lessons_context error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# TIER 1 — Record Trade Outcome (calls T2, T3, T4, T5 internally)
# ─────────────────────────────────────────────────────────────────────────────

def record_trade_outcome(
    *,
    symbol: str,
    asset_class: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    exit_reason: str,
    signal_id: str = None,
    timeframe: str = None,
    signal_confidence: float = None,
    signal_score: float = None,
    signal_reasoning: str = None,
    ta_profile: dict = None,       # raw TA profile dict for pattern fingerprinting
    ta_summary: str = None,
    market_regime: str = None,
    entered_at: str = None,
    paper_mode: bool = False,
):
    _lazy_ensure()
    from app.database import engine
    from sqlalchemy import text

    try:
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0.0
        if direction and direction.upper() in ("SELL", "SHORT", "SELL_SHORT"):
            pnl_pct = -pnl_pct
        pnl_usd = pnl_pct / 100.0 * entry_price * (qty or 0)

        if pnl_pct > 0.1:   outcome = "WIN"
        elif pnl_pct < -0.1: outcome = "LOSS"
        else:                 outcome = "BREAKEVEN"

        hold_duration_m = None
        if entered_at:
            try:
                entry_dt = datetime.fromisoformat(entered_at.replace("Z", "+00:00"))
                hold_duration_m = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
            except Exception:
                pass

        row_id    = _new_id()
        exited_at = _now_iso()

        with engine.begin() as conn:
            _ensure_tables(conn)

            conn.execute(text("""
                INSERT INTO trade_outcomes
                (id, signal_id, symbol, asset_class, direction, timeframe,
                 entry_price, exit_price, qty, pnl_usd, pnl_pct, outcome, exit_reason,
                 hold_duration_m, signal_confidence, signal_score,
                 signal_reasoning, ta_summary, market_regime, paper_mode, entered_at, exited_at)
                VALUES
                (:id, :signal_id, :symbol, :asset_class, :direction, :timeframe,
                 :entry_price, :exit_price, :qty, :pnl_usd, :pnl_pct, :outcome, :exit_reason,
                 :hold_duration_m, :signal_confidence, :signal_score,
                 :signal_reasoning, :ta_summary, :market_regime, :paper_mode, :entered_at, :exited_at)
            """), {
                "id": row_id, "signal_id": signal_id, "symbol": symbol,
                "asset_class": asset_class, "direction": direction, "timeframe": timeframe,
                "entry_price": entry_price, "exit_price": exit_price, "qty": qty,
                "pnl_usd": round(pnl_usd, 4), "pnl_pct": round(pnl_pct, 4),
                "outcome": outcome, "exit_reason": exit_reason,
                "hold_duration_m": round(hold_duration_m, 1) if hold_duration_m else None,
                "signal_confidence": signal_confidence, "signal_score": signal_score,
                "signal_reasoning": signal_reasoning, "ta_summary": ta_summary,
                "market_regime": market_regime,
                "paper_mode": 1 if paper_mode else 0,
                "entered_at": entered_at, "exited_at": exited_at,
            })

            # Tier 3 — Pattern Memory
            if ta_profile and not paper_mode:
                try:
                    fp, desc = _fingerprint_ta(ta_profile, direction)
                    _update_pattern_memory(fp, desc, asset_class, timeframe, outcome, pnl_pct, conn)
                except Exception as e:
                    logger.warning(f"[Learning-T3] pattern update failed: {e}")

            # Tier 4 — Regime performance
            if market_regime and not paper_mode:
                try:
                    _update_regime_performance(market_regime, outcome, pnl_pct, signal_confidence or 0, conn)
                except Exception as e:
                    logger.warning(f"[Learning-T4] regime update failed: {e}")

        logger.info(f"[Learning] Recorded: {symbol} {outcome} {pnl_pct:+.2f}% via {exit_reason}")

        # Tier 2 — signal accuracy (separate transaction)
        if not paper_mode:
            _refresh_signal_accuracy(symbol, asset_class, timeframe)

        # Tier 5 — LLM reasoning audit (async-ish: only for losses or big wins)
        if not paper_mode and signal_reasoning and outcome in ("LOSS",):
            outcome_row = {
                "id": row_id, "symbol": symbol, "direction": direction,
                "entry_price": entry_price, "exit_price": exit_price,
                "pnl_pct": round(pnl_pct, 4), "outcome": outcome,
                "exit_reason": exit_reason,
                "hold_duration_m": hold_duration_m,
                "market_regime": market_regime,
                "signal_reasoning": signal_reasoning,
            }
            try:
                _run_reasoning_audit(outcome_row)
            except Exception as e:
                logger.warning(f"[Learning-T5] audit failed: {e}")

        return {"outcome": outcome, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

    except Exception as e:
        logger.error(f"[Learning] record_trade_outcome failed for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TIER 2 — Signal Accuracy Aggregation
# ─────────────────────────────────────────────────────────────────────────────

def _refresh_signal_accuracy(symbol: str, asset_class: str, timeframe: str):
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT outcome, pnl_pct, hold_duration_m FROM trade_outcomes
                WHERE symbol=:sym AND paper_mode=0
            """), {"sym": symbol}).fetchall()

            if not rows:
                return

            total  = len(rows)
            wins   = sum(1 for r in rows if r[0] == "WIN")
            losses = sum(1 for r in rows if r[0] == "LOSS")
            pnls   = [r[1] for r in rows if r[1] is not None]
            holds  = [r[2] for r in rows if r[2] is not None]

            stats = {
                "total_trades": total, "wins": wins, "losses": losses,
                "win_rate":    round(wins / total, 4) if total else 0.0,
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
                "avg_hold_min":round(sum(holds) / len(holds), 1) if holds else 0.0,
                "best_pnl_pct":round(max(pnls), 4) if pnls else 0.0,
                "worst_pnl_pct":round(min(pnls), 4) if pnls else 0.0,
                "last_updated": _now_iso(),
            }

            existing = conn.execute(text(
                "SELECT id FROM signal_accuracy WHERE symbol=:sym"
            ), {"sym": symbol}).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE signal_accuracy SET
                        total_trades=:total_trades, wins=:wins, losses=:losses,
                        win_rate=:win_rate, avg_pnl_pct=:avg_pnl_pct,
                        avg_hold_min=:avg_hold_min, best_pnl_pct=:best_pnl_pct,
                        worst_pnl_pct=:worst_pnl_pct, last_updated=:last_updated
                    WHERE symbol=:sym
                """), {**stats, "sym": symbol})
            else:
                conn.execute(text("""
                    INSERT INTO signal_accuracy
                    (id, symbol, asset_class, timeframe, total_trades, wins, losses,
                     win_rate, avg_pnl_pct, avg_hold_min, best_pnl_pct, worst_pnl_pct, last_updated)
                    VALUES
                    (:id, :sym, :asset_class, :tf, :total_trades, :wins, :losses,
                     :win_rate, :avg_pnl_pct, :avg_hold_min, :best_pnl_pct, :worst_pnl_pct, :last_updated)
                """), {**stats, "id": _new_id(), "sym": symbol,
                       "asset_class": asset_class, "tf": timeframe})

        logger.info(f"[Learning-T2] Updated accuracy {symbol}: {wins}/{total} wins")
    except Exception as e:
        logger.error(f"[Learning-T2] _refresh_signal_accuracy failed: {e}")


def get_accuracy_context(symbol: str, timeframe: str = None, lookback_days: int = 30) -> str:
    _lazy_ensure()
    from app.database import engine
    from sqlalchemy import text
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT outcome, pnl_pct, exit_reason, hold_duration_m
                FROM trade_outcomes
                WHERE symbol=:sym AND paper_mode=0 AND exited_at>=:cutoff
                ORDER BY exited_at DESC LIMIT 20
            """), {"sym": symbol, "cutoff": cutoff}).fetchall()

            acc = conn.execute(text("""
                SELECT total_trades, wins, losses, win_rate, avg_pnl_pct,
                       avg_hold_min, best_pnl_pct, worst_pnl_pct
                FROM signal_accuracy WHERE symbol=:sym
            """), {"sym": symbol}).fetchone()

        if not rows and not acc:
            return ""

        lines = [f"\n📊 HISTORICAL PERFORMANCE — {symbol} (last {lookback_days}d):"]
        if acc:
            wr = acc[3] * 100
            lines.append(
                f"  Overall: {acc[0]} trades | {wr:.0f}% win rate | "
                f"avg P&L {acc[4]:+.2f}% | avg hold {acc[5]:.0f}min | "
                f"best {acc[6]:+.2f}% | worst {acc[7]:+.2f}%"
            )
        if rows:
            lines.append(f"  Recent ({len(rows)}):")
            for r in rows[:5]:
                icon = "✅" if r[0] == "WIN" else ("❌" if r[0] == "LOSS" else "➖")
                hold_str = f" ({r[3]:.0f}min)" if r[3] else ""
                lines.append(f"    {icon} {r[0]} {r[1]:+.2f}% via {r[2]}{hold_str}")
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[Learning-T2] get_accuracy_context error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# API helpers — used by routes.py
# ─────────────────────────────────────────────────────────────────────────────

def get_all_outcomes(limit: int = 200, paper_mode: bool = False) -> list:
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, signal_id, symbol, asset_class, direction, timeframe,
                       entry_price, exit_price, qty, pnl_usd, pnl_pct, outcome,
                       exit_reason, hold_duration_m, signal_confidence,
                       market_regime, paper_mode, entered_at, exited_at
                FROM trade_outcomes WHERE paper_mode=:pm
                ORDER BY exited_at DESC LIMIT :lim
            """), {"pm": 1 if paper_mode else 0, "lim": limit}).fetchall()
        keys = ["id","signal_id","symbol","asset_class","direction","timeframe",
                "entry_price","exit_price","qty","pnl_usd","pnl_pct","outcome",
                "exit_reason","hold_duration_m","signal_confidence",
                "market_regime","paper_mode","entered_at","exited_at"]
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.error(f"[Learning] get_all_outcomes error: {e}")
        return []


def get_all_accuracy() -> list:
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, symbol, asset_class, timeframe, total_trades, wins, losses,
                       win_rate, avg_pnl_pct, avg_hold_min, best_pnl_pct, worst_pnl_pct, last_updated
                FROM signal_accuracy ORDER BY total_trades DESC
            """)).fetchall()
        keys = ["id","symbol","asset_class","timeframe","total_trades","wins","losses",
                "win_rate","avg_pnl_pct","avg_hold_min","best_pnl_pct","worst_pnl_pct","last_updated"]
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.error(f"[Learning] get_all_accuracy error: {e}")
        return []


def get_all_patterns() -> list:
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, fingerprint, pattern_desc, asset_class, timeframe,
                       total, wins, losses, win_rate, avg_pnl_pct, last_seen
                FROM pattern_memory ORDER BY total DESC
            """)).fetchall()
        keys = ["id","fingerprint","pattern_desc","asset_class","timeframe",
                "total","wins","losses","win_rate","avg_pnl_pct","last_seen"]
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.error(f"[Learning] get_all_patterns error: {e}")
        return []


def get_all_regime_stats() -> list:
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, regime, total, wins, losses, win_rate,
                       avg_pnl_pct, avg_confidence, last_updated
                FROM regime_performance ORDER BY total DESC
            """)).fetchall()
        keys = ["id","regime","total","wins","losses","win_rate",
                "avg_pnl_pct","avg_confidence","last_updated"]
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.error(f"[Learning] get_all_regime_stats error: {e}")
        return []


def get_all_lessons(limit: int = 50) -> list:
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, trade_outcome_id, symbol, outcome, lesson,
                       lesson_category, applied_count, created_at
                FROM llm_lessons ORDER BY created_at DESC LIMIT :lim
            """), {"lim": limit}).fetchall()
        keys = ["id","trade_outcome_id","symbol","outcome","lesson",
                "lesson_category","applied_count","created_at"]
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.error(f"[Learning] get_all_lessons error: {e}")
        return []
