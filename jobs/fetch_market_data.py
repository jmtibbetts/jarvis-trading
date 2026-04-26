"""
Job: Fetch snapshot market data for watchlist assets.
"""
import logging, uuid
from datetime import datetime, timezone
from app.database import get_db, MarketAsset
from lib.alpaca_client import get_trading_client, normalize_symbol, is_crypto
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, CryptoLatestQuoteRequest
from lib.alpaca_client import get_alpaca_creds

logger = logging.getLogger(__name__)

EQUITY_WATCHLIST = [
    'SPY','QQQ','IWM','NVDA','AMD','MSFT','GOOGL','AAPL','META','AMZN',
    'AVGO','TSM','PLTR','COIN','MSTR','TSLA','RTX','LMT','NOC','GD','BA',
    'XOM','CVX','COP','GLD','SLV','TLT','GDX','SOXX','ARM','HOOD',
]
CRYPTO_WATCHLIST = [
    'BTC/USD','ETH/USD','SOL/USD','XRP/USD','BNB/USD','AVAX/USD',
    'LINK/USD','DOGE/USD','ADA/USD','AAVE/USD','DOT/USD','ATOM/USD',
]

def run():
    logger.info("[Market] Fetching market data snapshots...")
    
    key, secret, _ = get_alpaca_creds()
    if not key:
        logger.error("[Market] No Alpaca credentials")
        return
    
    stock_client  = StockHistoricalDataClient(api_key=key, secret_key=secret)
    crypto_client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    now_iso = datetime.now(timezone.utc).isoformat()
    
    results = {}
    
    # Equity quotes
    try:
        from alpaca.data.requests import StockLatestBarRequest
        req = StockLatestBarRequest(symbol_or_symbols=EQUITY_WATCHLIST)
        bars = stock_client.get_stock_latest_bar(req)
        for sym, bar in bars.items():
            results[sym] = {
                'price': float(bar.close),
                'volume': float(bar.volume or 0),
                'asset_class': 'Equity',
                'name': sym
            }
    except Exception as e:
        logger.error(f"[Market] Equity quote error: {e}")
    
    # Crypto quotes
    try:
        from alpaca.data.requests import CryptoLatestBarRequest
        crypto_syms = [s.replace('/USD', '/USD') for s in CRYPTO_WATCHLIST]
        req = CryptoLatestBarRequest(symbol_or_symbols=crypto_syms)
        bars = crypto_client.get_crypto_latest_bar(req)
        for sym, bar in bars.items():
            results[sym] = {
                'price': float(bar.close),
                'volume': float(bar.volume or 0),
                'asset_class': 'Crypto',
                'name': sym
            }
    except Exception as e:
        logger.error(f"[Market] Crypto quote error: {e}")
    
    # Save to DB
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
                    symbol=sym,
                    name=data['name'],
                    asset_class=data['asset_class'],
                    price=data['price'],
                    volume=data['volume'],
                    last_updated=now_iso,
                    created_date=now_iso,
                    updated_date=now_iso
                ))
    
    logger.info(f"[Market] Updated {len(results)} assets")
    return {'updated': len(results)}
