"""
Job: Fetch market data — prices + OHLCV cache warm-up.
v6.3: Added connect timeout + yfinance fallback for crypto price fetch.
      Crypto timeout no longer stalls the entire job.
v6.8.4: Equity price fetch now wrapped in ThreadPoolExecutor timeout guard
        (same as crypto) to prevent indefinite hang when data.alpaca.markets
        is unreachable. Added yfinance equity fallback for timeout/error cases.
"""
import logging, uuid, time, socket, os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from app.database import get_db, MarketAsset
from lib.alpaca_client import get_alpaca_creds, normalize_symbol, is_crypto
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, CryptoLatestBarRequest
from lib.crypto_market_data import (
    DEFAULT_CRYPTO_UNIVERSE,
    discover_crypto_universe,
    fetch_crypto_ohlcv,
    fetch_crypto_prices,
    is_crypto_symbol as is_crypto_data_symbol,
    normalize_crypto_symbol,
    to_dash_symbol,
)

logger = logging.getLogger(__name__)

EQUITY_WATCHLIST = [
    'SPY','QQQ','IWM','NVDA','AMD','MSFT','GOOGL','AAPL','META','AMZN',
    'AVGO','TSM','PLTR','COIN','MSTR','TSLA','RTX','LMT','NOC','GD','BA',
    'XOM','CVX','COP','GLD','SLV','TLT','GDX','SOXX','ARM','HOOD',
    'ANET','INTC','QCOM','SMCI','VRT','CRWV','NBIS','FANG','CEG','USO',
    'UNG','GDXJ','IWM',
]
CRYPTO_WATCHLIST = list(dict.fromkeys(DEFAULT_CRYPTO_UNIVERSE))
CRYPTO_DISCOVERY_LIMIT = int(os.getenv("CRYPTO_DISCOVERY_LIMIT", "150"))
CRYPTO_CACHE_LIMIT = int(os.getenv("CRYPTO_CACHE_LIMIT", "60"))
ALLOW_YFINANCE_CRYPTO_FALLBACK = os.getenv("ALLOW_YFINANCE_CRYPTO_FALLBACK", "false").lower() in ("1", "true", "yes")
ALL_SYMBOLS = list(dict.fromkeys(EQUITY_WATCHLIST + CRYPTO_WATCHLIST))

# Alpaca -> yfinance ticker mapping for crypto fallback
CRYPTO_YF_MAP = {
    'BTC/USD': 'BTC-USD', 'ETH/USD': 'ETH-USD', 'SOL/USD': 'SOL-USD',
    'XRP/USD': 'XRP-USD', 'BNB/USD': 'BNB-USD', 'AVAX/USD': 'AVAX-USD',
    'LINK/USD': 'LINK-USD', 'DOGE/USD': 'DOGE-USD', 'ADA/USD': 'ADA-USD',
    'AAVE/USD': 'AAVE-USD', 'DOT/USD': 'DOT-USD', 'ATOM/USD': 'ATOM-USD',
    'SUI/USD': 'SUI-USD', 'RENDER/USD': 'RNDR-USD', 'INJ/USD': 'INJ-USD',
    'NEAR/USD': 'NEAR-USD', 'OP/USD': 'OP-USD', 'ARB/USD': 'ARB11841-USD',
}

ALPACA_CONNECT_TIMEOUT = 10   # seconds
ALPACA_READ_TIMEOUT    = 12   # seconds — bail if SDK call is slow; fallback handles rest


def _check_alpaca_reachable(host='data.alpaca.markets', port=443, timeout=5) -> bool:
    """
    Deprecated: TCP probe was unreliable — port 443 being open doesn't mean the
    API endpoint itself is healthy. Always return True and let the SDK call
    handle failures with its own timeout guard.
    """
    return True


def _fetch_crypto_via_yfinance(symbols: list) -> dict:
    """Fallback: pull latest crypto prices from yfinance."""
    results = {}
    try:
        import yfinance as yf
        yf_tickers = [CRYPTO_YF_MAP.get(s, to_dash_symbol(s)) for s in symbols]
        tickers = yf.Tickers(' '.join(yf_tickers))
        for alpaca_sym, yf_sym in zip(symbols, yf_tickers):
            try:
                info = tickers.tickers[yf_sym].fast_info
                price = getattr(info, 'last_price', None) or getattr(info, 'regularMarketPrice', None)
                vol   = getattr(info, 'three_month_average_volume', 0) or 0
                if price:
                    norm_sym = normalize_crypto_symbol(alpaca_sym)
                    results[norm_sym] = {
                        'price': float(price),
                        'volume': float(vol),
                        'asset_class': 'Crypto',
                        'name': norm_sym,
                        'source': 'yfinance',
                    }
            except Exception as ie:
                logger.debug(f"[Market] yfinance {yf_sym}: {ie}")
        logger.info(f"[Market] yfinance crypto fallback: {len(results)}/{len(symbols)} prices")
    except Exception as e:
        logger.warning(f"[Market] yfinance fallback failed entirely: {e}")
    return results


def _discover_crypto_watchlist() -> tuple[list[str], dict[str, dict]]:
    """Build a broad crypto universe from exchange volume plus aggregator breadth."""
    meta = {}
    try:
        discovered = discover_crypto_universe(limit=CRYPTO_DISCOVERY_LIMIT)
        for item in discovered:
            sym = normalize_crypto_symbol(item.get("symbol", ""))
            meta[sym] = item
        symbols = list(dict.fromkeys(CRYPTO_WATCHLIST + list(meta.keys())))
        logger.info(f"[Market] Crypto universe: {len(symbols)} symbols ({len(meta)} discovered)")
        return symbols, meta
    except Exception as e:
        logger.warning(f"[Market] Crypto discovery failed, using static list: {e}")
        return CRYPTO_WATCHLIST, {}


def _fetch_crypto_multi_provider(symbols: list) -> dict:
    """Exchange-first crypto prices with aggregator/yfinance fallback."""
    results = {}
    try:
        results.update(fetch_crypto_prices(symbols))
        logger.info(f"[Market] Multi-provider crypto prices: {len(results)}/{len(symbols)}")
    except Exception as e:
        logger.warning(f"[Market] Multi-provider crypto fetch failed: {e}")

    missing = [s for s in symbols if normalize_crypto_symbol(s) not in results]
    if missing:
        yf_symbols = [
            s for s in missing
            if ALLOW_YFINANCE_CRYPTO_FALLBACK or normalize_crypto_symbol(s) in CRYPTO_YF_MAP
        ]
        yf_results = _fetch_crypto_via_yfinance(yf_symbols) if yf_symbols else {}
        for sym, data in yf_results.items():
            results.setdefault(normalize_crypto_symbol(sym), data)
    return results


def _fetch_equity_via_yfinance(symbols: list) -> dict:
    """Fallback: pull latest equity prices from yfinance when Alpaca is unreachable."""
    results = {}
    try:
        import yfinance as yf
        # yfinance uses the same tickers for equities
        tickers = yf.Tickers(' '.join(symbols))
        for sym in symbols:
            try:
                info = tickers.tickers[sym].fast_info
                price = getattr(info, 'last_price', None) or getattr(info, 'regularMarketPrice', None)
                vol   = getattr(info, 'three_month_average_volume', 0) or 0
                if price:
                    results[sym] = {
                        'price': float(price),
                        'volume': float(vol),
                        'asset_class': 'Equity',
                        'name': sym,
                    }
            except Exception as ie:
                logger.debug(f"[Market] yfinance equity {sym}: {ie}")
        logger.info(f"[Market] yfinance equity fallback: {len(results)}/{len(symbols)} prices")
    except Exception as e:
        logger.warning(f"[Market] yfinance equity fallback failed entirely: {e}")
    return results


def _warm_ohlcv_cache(symbols: list, stock_client, crypto_client):
    """
    Fetch 1H, 4H, 1D bars for all symbols and store in ohlcv_cache.db.
    This is what signal gen reads from -- no live fetch needed during signal gen.
    """
    from lib.ohlcv_cache import init_cache_db, _store_bars
    from lib.ohlcv import _fetch_alpaca_single, RATE_LIMIT_DELAY
    init_cache_db()

    configured = os.getenv("OHLCV_WARM_TIMEFRAMES", "15m,30m,1H,2H,4H,1D")
    timeframes = [tf.strip() for tf in configured.split(",") if tf.strip()]
    success = 0
    failed  = 0

    def alpaca_fn(sym, tf):
        return _fetch_alpaca_single(sym, tf, stock_client, crypto_client)

    for sym in symbols:
        for tf in timeframes:
            try:
                df = alpaca_fn(sym, tf)
                if df is not None and not df.empty:
                    _store_bars(sym, tf, df, source='alpaca')
                    success += 1
                    logger.debug(f"[Market] Cached {sym}/{tf}: {len(df)} bars")
                else:
                    try:
                        from lib.ohlcv_cache import _yf_fetch, TF_CONFIG
                        from datetime import timedelta
                        cfg = TF_CONFIG.get(tf, TF_CONFIG['1D'])
                        end = datetime.now(timezone.utc)
                        start = end - timedelta(days=cfg['lookback_days'])
                        if is_crypto_data_symbol(sym):
                            crypto_sym = normalize_crypto_symbol(sym)
                            crypto_df = fetch_crypto_ohlcv(sym, tf, limit=cfg['bar_count'])
                            if crypto_df is not None and not crypto_df.empty:
                                source = crypto_df.attrs.get("source", "crypto_api")
                                _store_bars(crypto_sym, tf, crypto_df, source=source)
                                success += 1
                                logger.debug(f"[Market] Cached {crypto_sym}/{tf}: {len(crypto_df)} bars ({source})")
                            elif ALLOW_YFINANCE_CRYPTO_FALLBACK:
                                yf_df = _yf_fetch(sym, tf, start, end)
                                if yf_df is not None and not yf_df.empty:
                                    _store_bars(crypto_sym, tf, yf_df, source='yfinance')
                                    success += 1
                                    logger.debug(f"[Market] Cached {crypto_sym}/{tf}: {len(yf_df)} bars (yfinance)")
                                else:
                                    failed += 1
                            else:
                                failed += 1
                        else:
                            yf_df = _yf_fetch(sym, tf, start, end)
                            if yf_df is not None and not yf_df.empty:
                                _store_bars(sym, tf, yf_df, source='yfinance')
                                success += 1
                                logger.debug(f"[Market] Cached {sym}/{tf}: {len(yf_df)} bars (yfinance)")
                            else:
                                failed += 1
                    except Exception as ye:
                        logger.debug(f"[Market] yfinance fallback failed {sym}/{tf}: {ye}")
                        failed += 1
            except Exception as e:
                logger.debug(f"[Market] Cache failed {sym}/{tf}: {e}")
                failed += 1
            time.sleep(RATE_LIMIT_DELAY)

    logger.info(f"[Market] OHLCV cache warm-up: {success} stored, {failed} failed")
    return success


def run():
    logger.info("[Market] Fetching market data + warming OHLCV cache...")

    key, secret, _ = get_alpaca_creds()
    if not key:
        logger.error("[Market] No Alpaca credentials")
        return {"error": "no_credentials"}

    stock_client  = StockHistoricalDataClient(api_key=key, secret_key=secret)
    crypto_client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    now_iso = datetime.now(timezone.utc).isoformat()
    results = {}
    crypto_symbols, crypto_meta = _discover_crypto_watchlist()

    # -- 1. Latest price snapshot -----------------------------------------------

    # Equity prices — wrapped in timeout guard to prevent indefinite hang
    def _do_equity_fetch():
        req = StockLatestBarRequest(symbol_or_symbols=EQUITY_WATCHLIST)
        return stock_client.get_stock_latest_bar(req)

    try:
        # NOTE: deliberately not using ThreadPoolExecutor as a context manager here —
        # `with` calls shutdown(wait=True) on exit, which blocks until the submitted
        # thread finishes even after future.result(timeout=...) has already given up,
        # negating the timeout. Shut down with wait=False instead so a stuck thread
        # doesn't stall the caller.
        ex = ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_do_equity_fetch)
        try:
            bars = future.result(timeout=ALPACA_READ_TIMEOUT)
            for sym, bar in bars.items():
                results[sym] = {
                    'price': float(bar.close),
                    'volume': float(bar.volume or 0),
                    'asset_class': 'Equity',
                    'name': sym,
                }
            logger.info(f"[Market] Got {len(bars)} equity prices (Alpaca)")
        except FuturesTimeout:
            logger.warning(f"[Market] Alpaca equity fetch timed out ({ALPACA_READ_TIMEOUT}s) — falling back to yfinance")
            results.update(_fetch_equity_via_yfinance(EQUITY_WATCHLIST))
        except Exception as te:
            logger.warning(f"[Market] Alpaca equity fetch error: {te} — falling back to yfinance")
            results.update(_fetch_equity_via_yfinance(EQUITY_WATCHLIST))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        logger.error(f"[Market] Equity price error: {e} — trying yfinance")
        results.update(_fetch_equity_via_yfinance(EQUITY_WATCHLIST))

    # Crypto prices — TCP probe first, then SDK call with timeout guard
    alpaca_reachable = _check_alpaca_reachable(timeout=ALPACA_CONNECT_TIMEOUT)
    if alpaca_reachable:
        try:
            def _do_crypto_fetch():
                req = CryptoLatestBarRequest(symbol_or_symbols=CRYPTO_WATCHLIST)
                return crypto_client.get_crypto_latest_bar(req)

            # See equity fetch above for why this isn't a `with ThreadPoolExecutor(...)` block.
            ex = ThreadPoolExecutor(max_workers=1)
            future = ex.submit(_do_crypto_fetch)
            try:
                bars = future.result(timeout=ALPACA_READ_TIMEOUT)
                for sym, bar in bars.items():
                    norm_sym = normalize_crypto_symbol(sym)
                    results[norm_sym] = {
                        'price': float(bar.close),
                        'volume': float(bar.volume or 0),
                        'asset_class': 'Crypto',
                        'name': norm_sym,
                        'source': 'alpaca',
                    }
                logger.info(f"[Market] Got {len(bars)} crypto prices (Alpaca)")
            except FuturesTimeout:
                logger.warning(f"[Market] Alpaca crypto fetch timed out ({ALPACA_READ_TIMEOUT}s) -- using public crypto providers")
            except Exception as te:
                logger.warning(f"[Market] Alpaca crypto fetch error: {te} -- using public crypto providers")
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.error(f"[Market] Crypto price error: {e} -- using public crypto providers")
    else:
        logger.warning("[Market] Alpaca unreachable -- using public crypto providers")

    missing_crypto = [s for s in crypto_symbols if normalize_crypto_symbol(s) not in results]
    if missing_crypto:
        results.update(_fetch_crypto_multi_provider(missing_crypto))

    for sym, meta in crypto_meta.items():
        if sym in results:
            results[sym]["name"] = meta.get("name") or results[sym].get("name") or sym
            results[sym]["change_percent"] = meta.get("change_pct", results[sym].get("change_percent"))
            results[sym]["market_cap"] = meta.get("market_cap", results[sym].get("market_cap"))

    # -- 2. Save prices to MarketAsset DB ----------------------------------------
    with get_db() as db:
        for sym, data in results.items():
            existing = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
            if existing:
                existing.price = data['price']
                existing.volume = data['volume']
                existing.change_percent = data.get('change_percent', existing.change_percent)
                existing.market_cap = data.get('market_cap', existing.market_cap)
                existing.last_updated = now_iso
                existing.updated_date = now_iso
            else:
                db.add(MarketAsset(
                    id=str(uuid.uuid4()),
                    symbol=sym, name=data['name'],
                    asset_class=data['asset_class'],
                    price=data['price'], volume=data['volume'],
                    change_percent=data.get('change_percent'),
                    market_cap=data.get('market_cap'),
                    last_updated=now_iso,
                    created_date=now_iso, updated_date=now_iso,
                ))
    logger.info(f"[Market] Saved {len(results)} asset prices to DB")

    # -- 3. Warm OHLCV cache (so signal gen doesn't need to fetch live) ----------
    try:
        cache_crypto_symbols = crypto_symbols[:CRYPTO_CACHE_LIMIT]
        all_symbols = list(dict.fromkeys(EQUITY_WATCHLIST + cache_crypto_symbols))
        cached = _warm_ohlcv_cache(all_symbols, stock_client, crypto_client)
    except Exception as e:
        logger.error(f"[Market] OHLCV cache warm-up error: {e}")
        cached = 0

    # Notify event bus — fresh market data means signal re-evaluation is worthwhile
    if len(results) > 0:
        try:
            from app.scheduler import notify_new_intelligence
            notify_new_intelligence()
        except Exception as e:
            logger.debug(f"[Market] Event notify failed: {e}")

    return {'prices_updated': len(results), 'ohlcv_cached': cached}
