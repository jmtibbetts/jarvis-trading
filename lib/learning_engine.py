"""
lib/learning_engine.py
──────────────────────
Tier 1: Record trade outcomes when positions close.
Tier 2: Aggregate signal accuracy stats per symbol/timeframe.
         Inject historical win-rate context into LLM prompts.
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _new_id():
    return str(uuid.uuid4())


# ── Tier 1: Record a closed trade outcome ─────────────────────────────────────

def record_trade_outcome(
    *,
    symbol: str,
    asset_class: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    exit_reason: str,       # HARD_STOP | TAKE_PROFIT | LLM_EXIT | MANUAL | TIMEOUT
    signal_id: str = None,
    timeframe: str = None,
    signal_confidence: float = None,
    signal_score: float = None,
    signal_reasoning: str = None,
    ta_summary: str = None,
    market_regime: str = None,
    entered_at: str = None,
    paper_mode: bool = False,
):
    """
    Call this every time a position is closed (real or paper).
    Automatically computes P&L, outcome, hold duration, and writes to trade_outcomes.
    Then refreshes signal_accuracy for this symbol/timeframe.
    """
    from app.database import engine
    from sqlalchemy import text

    try:
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0.0
        # For short direction, invert
        if direction and direction.upper() in ("SELL", "SHORT"):
            pnl_pct = -pnl_pct
        pnl_usd = pnl_pct / 100.0 * entry_price * (qty or 0)

        if pnl_pct > 0.1:
            outcome = "WIN"
        elif pnl_pct < -0.1:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        # Hold duration
        hold_duration_m = None
        if entered_at:
            try:
                entry_dt = datetime.fromisoformat(entered_at.replace("Z", "+00:00"))
                hold_duration_m = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
            except Exception:
                pass

        row_id = _new_id()
        exited_at = _now_iso()

        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trade_outcomes (
                    id TEXT PRIMARY KEY, signal_id TEXT, symbol TEXT, asset_class TEXT,
                    direction TEXT, timeframe TEXT, entry_price REAL, exit_price REAL,
                    qty REAL, pnl_usd REAL, pnl_pct REAL, outcome TEXT, exit_reason TEXT,
                    hold_duration_m REAL, signal_confidence REAL, signal_score REAL,
                    signal_reasoning TEXT, ta_summary TEXT, market_regime TEXT,
                    paper_mode INTEGER DEFAULT 0, entered_at TEXT, exited_at TEXT
                )
            """))
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

        logger.info(f"[Learning] Recorded outcome: {symbol} {outcome} {pnl_pct:+.2f}% via {exit_reason}")

        # Tier 2: refresh accuracy stats for this symbol
        _refresh_signal_accuracy(symbol, asset_class, timeframe)

        return {"outcome": outcome, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

    except Exception as e:
        logger.error(f"[Learning] record_trade_outcome failed for {symbol}: {e}")
        return None


# ── Tier 2: Refresh aggregated win-rate stats ─────────────────────────────────

def _refresh_signal_accuracy(symbol: str, asset_class: str, timeframe: str):
    """Recompute win-rate stats for a symbol/timeframe from trade_outcomes."""
    from app.database import engine
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS signal_accuracy (
                    id TEXT PRIMARY KEY, symbol TEXT, asset_class TEXT, timeframe TEXT,
                    total_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0, win_rate REAL DEFAULT 0.0,
                    avg_pnl_pct REAL DEFAULT 0.0, avg_hold_min REAL DEFAULT 0.0,
                    best_pnl_pct REAL DEFAULT 0.0, worst_pnl_pct REAL DEFAULT 0.0,
                    last_updated TEXT
                )
            """))

            rows = conn.execute(text("""
                SELECT outcome, pnl_pct, hold_duration_m
                FROM trade_outcomes
                WHERE symbol = :sym AND paper_mode = 0
            """), {"sym": symbol}).fetchall()

            if not rows:
                return

            total = len(rows)
            wins = sum(1 for r in rows if r[0] == "WIN")
            losses = sum(1 for r in rows if r[0] == "LOSS")
            pnls = [r[1] for r in rows if r[1] is not None]
            holds = [r[2] for r in rows if r[2] is not None]

            stats = {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total, 4) if total else 0.0,
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
                "avg_hold_min": round(sum(holds) / len(holds), 1) if holds else 0.0,
                "best_pnl_pct": round(max(pnls), 4) if pnls else 0.0,
                "worst_pnl_pct": round(min(pnls), 4) if pnls else 0.0,
                "last_updated": _now_iso(),
            }

            # Upsert — update if exists, insert if not
            existing = conn.execute(text(
                "SELECT id FROM signal_accuracy WHERE symbol=:sym AND (timeframe=:tf OR (timeframe IS NULL AND :tf IS NULL))"
            ), {"sym": symbol, "tf": timeframe}).fetchone()

            if existing:
                conn.execute(text("""
                    UPDATE signal_accuracy SET
                        total_trades=:total_trades, wins=:wins, losses=:losses,
                        win_rate=:win_rate, avg_pnl_pct=:avg_pnl_pct,
                        avg_hold_min=:avg_hold_min, best_pnl_pct=:best_pnl_pct,
                        worst_pnl_pct=:worst_pnl_pct, last_updated=:last_updated
                    WHERE symbol=:sym AND (timeframe=:tf OR (timeframe IS NULL AND :tf IS NULL))
                """), {**stats, "sym": symbol, "tf": timeframe})
            else:
                conn.execute(text("""
                    INSERT INTO signal_accuracy
                    (id, symbol, asset_class, timeframe, total_trades, wins, losses,
                     win_rate, avg_pnl_pct, avg_hold_min, best_pnl_pct, worst_pnl_pct, last_updated)
                    VALUES
                    (:id, :sym, :asset_class, :tf, :total_trades, :wins, :losses,
                     :win_rate, :avg_pnl_pct, :avg_hold_min, :best_pnl_pct, :worst_pnl_pct, :last_updated)
                """), {**stats, "id": _new_id(), "sym": symbol, "asset_class": asset_class, "tf": timeframe})

        logger.info(f"[Learning] Updated accuracy for {symbol}: {wins}/{total} wins ({wins/total*100:.0f}%)")

    except Exception as e:
        logger.error(f"[Learning] _refresh_signal_accuracy failed for {symbol}: {e}")


# ── Tier 2: Get win-rate context block for LLM prompt injection ───────────────

def get_accuracy_context(symbol: str, timeframe: str = None, lookback_days: int = 30) -> str:
    """
    Returns a compact text block summarizing historical performance for this symbol.
    Inject this into LLM signal generation prompts.
    """
    from app.database import engine
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            # Recent trade outcomes for this symbol
            cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
            rows = conn.execute(text("""
                SELECT outcome, pnl_pct, exit_reason, hold_duration_m, exited_at
                FROM trade_outcomes
                WHERE symbol = :sym AND paper_mode = 0 AND exited_at >= :cutoff
                ORDER BY exited_at DESC LIMIT 20
            """), {"sym": symbol, "cutoff": cutoff}).fetchall()

            # Overall accuracy stats
            acc = conn.execute(text("""
                SELECT total_trades, wins, losses, win_rate, avg_pnl_pct, avg_hold_min,
                       best_pnl_pct, worst_pnl_pct
                FROM signal_accuracy WHERE symbol = :sym
            """), {"sym": symbol}).fetchone()

        if not rows and not acc:
            return ""  # No history yet — don't inject anything

        lines = [f"\n📊 HISTORICAL PERFORMANCE FOR {symbol} (last {lookback_days}d):"]

        if acc:
            wr = acc[3] * 100
            lines.append(
                f"  Overall: {acc[0]} trades | {wr:.0f}% win rate | "
                f"avg P&L {acc[4]:+.2f}% | avg hold {acc[5]:.0f}min | "
                f"best {acc[6]:+.2f}% | worst {acc[7]:+.2f}%"
            )

        if rows:
            lines.append(f"  Recent trades ({len(rows)}):")
            for r in rows[:5]:
                outcome_icon = "✅" if r[0] == "WIN" else ("❌" if r[0] == "LOSS" else "➖")
                lines.append(f"    {outcome_icon} {r[0]} {r[1]:+.2f}% via {r[2]} ({r[3]:.0f}min)" if r[3] else
                             f"    {outcome_icon} {r[0]} {r[1]:+.2f}% via {r[2]}")

        lines.append("")
        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[Learning] get_accuracy_context failed for {symbol}: {e}")
        return ""


# ── API helpers ───────────────────────────────────────────────────────────────

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
                FROM trade_outcomes
                WHERE paper_mode = :pm
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
