"""Deep-history backfill through Twelve Data.

Usage:
    python -m jobs.backfill_history --timeframes 15m,1H --years 3
    python -m jobs.backfill_history --symbols BTC/USD,NVDA --timeframes 15m --years 5

Run manually, not scheduled: it is a one-time (per depth target) data
acquisition that budgets against the 800-credit day, not a recurring job.
Interrupted runs resume by re-running — the cache upserts are idempotent
and the credit floor stops the run before the account drains.

Why this exists: the path model was rejected on 9,246 labels spanning one
usable day. Every downstream ambition — path model, outcome model, analog
retrieval, honest walk-forward — is gated on calendar SPAN, and this is
the only connected source with years of intraday depth (verified: NVDA
15min to 2019, BTC/USD 15min to 2020).
"""
from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

# Standalone entrypoint: unlike jobs launched inside the server process,
# nothing has loaded .env before this runs. main.py does exactly this.
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Priority order: the symbols the book actually trades, most-traded first,
# so a credit-floor stop leaves the most useful history behind.
def default_universe(limit: int = 40) -> list[str]:
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT symbol, COUNT(*) n FROM trade_outcomes
            WHERE symbol IS NOT NULL
            GROUP BY symbol ORDER BY n DESC LIMIT :lim
        """), {"lim": limit}).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", help="comma-separated; default: top traded symbols")
    ap.add_argument("--timeframes", default="15m,1H",
                    help="comma-separated Jarvis timeframes (default 15m,1H)")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--limit", type=int, default=40,
                    help="universe size when --symbols is not given")
    args = ap.parse_args()

    from lib.twelvedata import (CreditFloorReached, TwelveDataError,
                                backfill_symbol, credits_remaining)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else default_universe(args.limit))
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    # Crypto rows cache under their normalized symbol so fetch_with_cache
    # finds them; equities cache as-is.
    def cache_name(sym: str) -> str:
        try:
            from lib.crypto_market_data import is_crypto_symbol, normalize_crypto_symbol
            return normalize_crypto_symbol(sym) if is_crypto_symbol(sym) else sym
        except Exception:
            return sym

    logger.info(f"backfill: {len(symbols)} symbols x {timeframes}, "
                f"{args.years}y target, {credits_remaining()} credits available")

    done = failed = 0
    for sym in symbols:
        for tf in timeframes:
            try:
                r = backfill_symbol(sym, tf, years=args.years,
                                    cache_symbol=cache_name(sym))
                logger.info(f"  {sym}/{tf}: {r['bars_stored']} bars in "
                            f"{r['pages']} calls, earliest {r['earliest']}")
                done += 1
            except CreditFloorReached as e:
                logger.warning(f"  {e}")
                logger.info(f"done for today: {done} series complete, "
                            f"{failed} failed. Re-run tomorrow to continue.")
                return 0
            except TwelveDataError as e:
                # One unsupported symbol must not end the run — log which,
                # and move on. (Twelve Data lists most but not all of the
                # crypto universe.)
                logger.warning(f"  {sym}/{tf}: {e}")
                failed += 1

    logger.info(f"backfill complete: {done} series, {failed} failed, "
                f"{credits_remaining()} credits left today")
    return 0


if __name__ == "__main__":
    sys.exit(main())
