"""
Job: Fetch market data — prices + OHLCV cache warm-up.
v6.2: Now also fetches multi-timeframe OHLCV bars into ohlcv_cache.db
      so signal gen can read from cache instantly without live API calls.
"""
import logging, uuid, time
from datetime import datetime, timezone
from app.database import get_db, MarketAsset
from lib.alpaca_client import get_alpaca_creds, normalize_symbol, is_crypto
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, CryptoLatestBarRequest

logger = logging.getLogger(__name__)

EQUITY_WATCHLIST = [
    'SPY','QQQ','IWM','NVDA','AMD','MSFT','GOOGL','AAPL','META','AMZN',
    'AVGO','TSM','PLTR','COIN','MSTR','TSLA','RTX','LMT','NOC','GD','BA',
    'XOM','CVX','COP','GLD','SLV','TLT','GDX','SOXX','ARM','HOOD',
    'ANET','INTC','QCOM','SMCI','VRT','CRWV','NBIS','FANG','CEG','USO',
    'UNG','GDXJ','IWM',
]
CRYPTO_WATCHLIST = [
    'BTC/USD','ETH/USD','SOL/USD','XRP/USD','BNB/USD','AVAX/USD',
    'LINK/USD','DOGE/USD','ADA/USD','AAVE/USD','DOT/USD','ATOM/USD',
    'SUI/USD','RENDER/USD','INJ/USD','NEAR/USD','OP/USD','ARB/USD',
]
ALL_SYMBOLS = list(dict.fromkeys(EQUITY_WATCHLIST + CRYPTO_WATCHLIST))


def _warm_ohlcv_cache(symbols: list, stock_client, crypto_client):
    """
    Fetch 1H, 4H, 1D bars for all symbols and store in ohlcv_cache.db.
    This is what signal gen reads from — no live fetch needed during signal gen.
    """
    from lib.ohlcv_cache import init_cache_db, _store_bars
    from lib.ohlcv import _fetch_alpaca_single, RATE_LIMIT_DELAY
    init_cache_db()

    timeframes = ['1H', '4H', '1D']
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
                    # Try yfinance fallback
                    try:
                        from lib.ohlcv_cache import _yf_fetch, TF_CONFIG
                        from datetime import timedelta
                        cfg = TF_CONFIG.get(tf, TF_CONFIG['1D'])
                        end = datetime.now(timezone.utc)
                        start = end - timedelta(days=cfg['lookback_days'])
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

    # ── 1. Latest price snapshot (fast — single batch request) ───────────────
    try:
        req = StockLatestBarRequest(symbol_or_symbols=EQUITY_WATCHLIST)
        bars = stock_client.get_stock_latest_bar(req)
        for sym, bar in bars.items():
            results[sym] = {'price': float(bar.close), 'volume': float(bar.volume or 0), 'asset_class': 'Equity', 'name': sym}
        logger.info(f"[Market] Got {len(bars)} equity prices")
    except Exception as e:
        logger.error(f"[Market] Equity price error: {e}")

    try:
        req = CryptoLatestBarRequest(symbol_or_symbols=CRYPTO_WATCHLIST)
        bars = crypto_client.get_crypto_latest_bar(req)
        for sym, bar in bars.items():
            results[sym] = {'price': float(bar.close), 'volume': float(bar.volume or 0), 'asset_class': 'Crypto', 'name': sym}
        logger.info(f"[Market] Got {len(bars)} crypto prices")
    except Exception as e:
        logger.error(f"[Market] Crypto price error: {e}")

    # ── 2. Save prices to MarketAsset DB ─────────────────────────────────────
    with get_db() as db:
        for sym, data in results.items():
            existing = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
            if existing:
                existing.price = data['price']
                existing.volume = data['volume']
                existing.last_updated = now_iso
                existing.updated_date = now_iso
            else:
                db.add(MarketAsset(
                    id=str(uuid.uuid4()),
                    symbol=sym, name=data['name'],
                    asset_class=data['asset_class'],
                    price=data['price'], volume=data['volume'],
                    last_updated=now_iso,
                    created_date=now_iso, updated_date=now_iso,
                ))
    logger.info(f"[Market] Saved {len(results)} asset prices to DB")

    # ── 3. Warm OHLCV cache (so signal gen doesn't need to fetch live) ────────
    try:
        cached = _warm_ohlcv_cache(ALL_SYMBOLS, stock_client, crypto_client)
    except Exception as e:
        logger.error(f"[Market] OHLCV cache warm-up error: {e}")
        cached = 0

    return {'prices_updated': len(results), 'ohlcv_cached': cached}
