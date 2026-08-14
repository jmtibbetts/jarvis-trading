"""The full-catalog deep pull, sized to the Grow plan (377 credits/min).

Four sequential waves through ONE pacer — the per-minute budget is
account-wide, so parallel pullers would fight each other into 429s, not
add throughput:

  1. equity + ETF   the desk's learning universe + curated index/sector/
                    bond/commodity/vol/crypto-proxy ETFs; 15m+1H, 5y
  2. forex          majors and crosses, fetched as TD's EUR/USD but
                    CACHED under the desk's canonical EURUSD=X identity
  3. crypto deep    census pairs with >=3y of 15m history AND a real
                    venue listing, desk-traded first; 15m+1H, 5y
  4. crypto broad   the rest of the quality set at 1H only — market
                    structure and analog work, without drinking 30GB of
                    dead-coin 15m bars into the cache

Technical indicators are deliberately NOT pulled: the desk computes its
own from these bars (one TA engine — a purchased RSI that can silently
disagree with ours is a bug, not a feature).

Resumable: cache upserts are idempotent and each symbol re-checks its
own coverage. Interrupt and re-run freely.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402

from lib.twelvedata import backfill_symbol  # noqa: E402

YEARS = 5.0
CENSUS = Path("data/twelvedata_crypto_depth.json")
SUMMARY = Path("data/backfill_everything_summary.json")

ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLF", "XLK", "XLV", "XLI",
        "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC", "TLT", "IEF", "SHY",
        "HYG", "LQD", "GLD", "SLV", "USO", "UNG", "DBA", "VXX", "EEM",
        "EFA", "FXI", "EWJ", "EWZ", "IBIT", "FBTC", "ARKK", "SMH"]

# TD symbol -> desk canonical (=X keeps forex out of the crypto-shaped
# slash namespace; asset_class_of reads =X as Forex).
FOREX = {"EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
         "USD/CHF": "USDCHF=X", "AUD/USD": "AUDUSD=X", "USD/CAD": "USDCAD=X",
         "NZD/USD": "NZDUSD=X", "EUR/GBP": "EURGBP=X", "EUR/JPY": "EURJPY=X",
         "GBP/JPY": "GBPJPY=X", "AUD/JPY": "AUDJPY=X", "USD/MXN": "USDMXN=X"}

REAL_VENUES = {"Binance", "Coinbase Pro", "Kraken", "Gate.io", "Huobi",
               "Bitfinex", "OKEx", "Bybit"}
CRYPTO_DEEP_CAP = 120


def equity_universe(cap: int = 150) -> list[str]:
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT symbol, SUM(n) FROM (
                SELECT symbol, COUNT(*) n FROM trade_outcomes
                WHERE asset_class LIKE '%quit%' GROUP BY symbol
                UNION ALL
                SELECT symbol, COUNT(*) n FROM candidate_signals
                WHERE asset_class = 'Equity' GROUP BY symbol
            ) GROUP BY symbol ORDER BY SUM(n) DESC LIMIT :cap
        """), {"cap": cap}).fetchall()
    return [r[0] for r in rows if r[0] and "/" not in r[0]
            and not r[0].endswith(("=F", "=X")) and not r[0].startswith("^")]


def crypto_waves() -> tuple[list[str], list[str]]:
    census = json.loads(CENSUS.read_text(encoding="utf-8"))["pairs"]
    catalog = httpx.get("https://api.twelvedata.com/cryptocurrencies",
                        timeout=60.0).json().get("data") or []
    venues = {p["symbol"]: set(p.get("available_exchanges") or [])
              for p in catalog}
    now = datetime.now(timezone.utc)

    def years_back(d):
        try:
            return (now - datetime.fromisoformat(d).replace(
                tzinfo=timezone.utc)).days / 365.25
        except Exception:
            return 0.0

    quality = [s for s, d in census.items()
               if d and not str(d).startswith("error")
               and years_back(d) >= 3.0
               and venues.get(s, set()) & REAL_VENUES]

    # Desk-traded crypto first — their history joins labeled outcomes.
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        traded = [r[0] for r in c.execute(text("""
            SELECT symbol, COUNT(*) n FROM trade_outcomes
            WHERE symbol LIKE '%/USD' GROUP BY symbol
            ORDER BY n DESC""")).fetchall()]
    rank = {s: i for i, s in enumerate(traded)}
    quality.sort(key=lambda s: (rank.get(s, 10_000),
                                -len(venues.get(s, set()))))
    return quality[:CRYPTO_DEEP_CAP], quality[CRYPTO_DEEP_CAP:]


def run_wave(name: str, jobs: list[tuple[str, str, str | None]],
             results: dict) -> None:
    print(f"=== wave {name}: {len(jobs)} pulls ===", flush=True)
    done = failed = 0
    for i, (symbol, tf, cache_as) in enumerate(jobs):
        try:
            r = backfill_symbol(symbol, tf, years=YEARS,
                                cache_symbol=cache_as)
            done += 1
            if (i + 1) % 25 == 0:
                print(f"  [{name}] {i + 1}/{len(jobs)} "
                      f"(last: {symbol}/{tf} +{r.get('bars', 0)} bars)",
                      flush=True)
        except Exception as e:
            failed += 1
            print(f"  [{name}] {symbol}/{tf} FAILED: {str(e)[:80]}",
                  flush=True)
    results[name] = {"pulls": len(jobs), "ok": done, "failed": failed}
    _write(results)


def _write(results: dict) -> None:
    SUMMARY.write_text(json.dumps(
        {"updated_at": datetime.now(timezone.utc).isoformat(),
         "years": YEARS, "waves": results}, indent=1), encoding="utf-8")


def main() -> int:
    eq = equity_universe()
    deep, broad = crypto_waves()
    print(f"universe: {len(eq)} equities + {len(ETFS)} ETFs, "
          f"{len(FOREX)} forex, {len(deep)} crypto deep, "
          f"{len(broad)} crypto broad", flush=True)

    results: dict = {}
    run_wave("equity_etf",
             [(s, tf, None) for s in eq + ETFS for tf in ("15m", "1H")],
             results)
    run_wave("forex",
             [(td, tf, desk) for td, desk in FOREX.items()
              for tf in ("15m", "1H")],
             results)
    run_wave("crypto_deep",
             [(s, tf, None) for s in deep for tf in ("15m", "1H")],
             results)
    run_wave("crypto_broad", [(s, "1H", None) for s in broad], results)
    print("ALL WAVES COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
