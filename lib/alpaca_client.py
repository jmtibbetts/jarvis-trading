"""
Alpaca client wrapper — uses alpaca-py SDK.
Reads credentials from PlatformConfig DB first, falls back to .env.
"""
import os, re
from functools import lru_cache
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)

CRYPTO_BASES = {
    'BTC','ETH','SOL','XRP','BNB','AVAX','AAVE','LINK','DOGE','SHIB',
    'SUI','RENDER','LTC','BCH','DOT','ADA','MATIC','UNI','ARB','OP',
    'APT','INJ','NEAR','ATOM','FIL','PEPE','GRT','MKR','CRV','SNX',
    'COMP','LDO','IMX','BONK','WIF','JUP','STRK','GALA','SAND','MANA',
}

def is_crypto(symbol: str) -> bool:
    s = symbol.upper().strip()
    if '/' in s:
        return s.split('/')[0] in CRYPTO_BASES
    if s.endswith('USD'):
        return s[:-3] in CRYPTO_BASES
    return s in CRYPTO_BASES

def normalize_symbol(symbol: str) -> tuple[str, bool]:
    """Returns (alpaca_symbol, is_crypto)"""
    s = symbol.upper().strip()
    crypto = is_crypto(s)
    if crypto:
        base = s.split('/')[0] if '/' in s else (s[:-3] if s.endswith('USD') else s)
        return f"{base}/USD", True
    return s, False

def get_alpaca_creds():
    """Get credentials from DB or environment."""
    try:
        from app.database import get_db, PlatformConfig
        with get_db() as db:
            configs = db.query(PlatformConfig).filter(
                PlatformConfig.platform.like('alpaca%'),
                PlatformConfig.is_active == True
            ).all()
            cfg = next((c for c in configs if c.is_default), None) or (configs[0] if configs else None)
            if cfg:
                return cfg.api_key, cfg.api_secret, cfg.extra_field_1 == 'paper'
    except Exception as e:
        logger.debug(f"DB creds lookup failed: {e}")
    
    key = os.getenv('ALPACA_API_KEY', '')
    secret = os.getenv('ALPACA_API_SECRET', '')
    paper = os.getenv('ALPACA_MODE', 'paper').lower() == 'paper'
    return key, secret, paper

def get_trading_client() -> TradingClient:
    key, secret, paper = get_alpaca_creds()
    if not key or not secret:
        raise ValueError("No Alpaca credentials configured")
    return TradingClient(api_key=key, secret_key=secret, paper=paper)

def get_stock_data_client() -> StockHistoricalDataClient:
    key, secret, _ = get_alpaca_creds()
    return StockHistoricalDataClient(api_key=key, secret_key=secret)

def get_crypto_data_client() -> CryptoHistoricalDataClient:
    key, secret, _ = get_alpaca_creds()
    return CryptoHistoricalDataClient(api_key=key, secret_key=secret)

def get_account():
    client = get_trading_client()
    return client.get_account()

def get_positions():
    client = get_trading_client()
    return client.get_all_positions()

def get_open_orders():
    client = get_trading_client()
    return client.get_orders(GetOrdersRequest(status=OrderStatus.OPEN))

def submit_bracket_order(symbol: str, qty: float, entry_price: float,
                          take_profit: float, stop_loss: float,
                          side: str = 'buy') -> dict:
    """Submit a bracket order with take-profit and stop-loss legs."""
    sym, crypto = normalize_symbol(symbol)
    client = get_trading_client()
    
    order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
    
    if crypto:
        # Crypto: market order with TP/SL (alpaca-py handles bracket for crypto)
        from alpaca.trading.requests import MarketOrderRequest
        req = MarketOrderRequest(
            symbol=sym,
            qty=round(qty, 8),
            side=order_side,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit={"limit_price": str(round(take_profit, 4))},
            stop_loss={"stop_price": str(round(stop_loss, 4))}
        )
    else:
        # Equity: limit entry bracket order (whole shares)
        from alpaca.trading.requests import LimitOrderRequest
        qty = max(1, int(qty))
        req = LimitOrderRequest(
            symbol=sym,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(entry_price, 2),
            order_class=OrderClass.BRACKET,
            take_profit={"limit_price": str(round(take_profit, 2))},
            stop_loss={"stop_price": str(round(stop_loss, 2))}
        )
    
    order = client.submit_order(req)
    return {
        'id': str(order.id),
        'symbol': sym,
        'qty': qty,
        'status': str(order.status),
        'type': str(order.type),
        'side': str(order.side)
    }

def close_position(symbol: str):
    sym, _ = normalize_symbol(symbol)
    client = get_trading_client()
    return client.close_position(sym)
