"""
Alpaca client wrapper — uses alpaca-py SDK.
Reads credentials from PlatformConfig DB first, falls back to .env.
"""
import os, re
from functools import lru_cache
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOrdersRequest,
    StopOrderRequest
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
    """
    Get credentials — priority order:
    1. DB PlatformConfig (any row where platform contains 'alpaca')
    2. .env / environment variables
    Paper mode: DB extra_field_1 == 'paper' OR env ALPACA_PAPER=true OR ALPACA_BASE_URL contains 'paper'
    """
    # ── 1. Try DB ──────────────────────────────────────────────────────────────
    try:
        from app.database import get_db, PlatformConfig
        with get_db() as db:
            configs = db.query(PlatformConfig).filter(
                PlatformConfig.is_active == True
            ).all()
            alpaca_configs = [c for c in configs
                              if c.platform and 'alpaca' in c.platform.lower()]
            cfg = next((c for c in alpaca_configs if c.is_default), None) \
                  or (alpaca_configs[0] if alpaca_configs else None)

            if cfg and cfg.api_key and cfg.api_secret:
                url = (cfg.api_url or '').lower()
                ef1 = (cfg.extra_field_1 or '').lower()
                paper = (ef1 == 'paper') or ('paper' in url) or (ef1 not in ('live', 'prod', 'production'))
                logger.debug(f"[Alpaca] Creds from DB — paper={paper} platform={cfg.platform}")
                return cfg.api_key, cfg.api_secret, paper
    except Exception as e:
        logger.debug(f"[Alpaca] DB creds lookup failed: {e}")

    # ── 2. Fall back to environment ────────────────────────────────────────────
    key    = os.getenv('ALPACA_API_KEY', '').strip()
    secret = os.getenv('ALPACA_API_SECRET', '').strip()
    base   = os.getenv('ALPACA_BASE_URL', '').lower()
    paper_env = os.getenv('ALPACA_PAPER', os.getenv('ALPACA_MODE', 'paper')).lower()
    paper  = (paper_env in ('true', '1', 'paper')) or ('paper' in base)

    if key and secret:
        logger.debug(f"[Alpaca] Creds from .env — paper={paper}")
    else:
        logger.warning("[Alpaca] No credentials found in DB or .env")
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
    """
    Submit an order with take-profit and stop-loss protection.

    Equities: standard bracket order (limit entry + TP limit + SL stop).
    Crypto:   market entry first, then two separate protective limit orders:
              - A stop-loss static limit-sell at stop_loss price
              - A take-profit limit-sell at take_profit price
              Alpaca does NOT support bracket orders or trailing stops on
              crypto — submitting either raises a 40010001 API error.
              These are NOT linked (no OCO), so the position manager is
              responsible for cancelling the orphan order after a leg fills.
    """
    sym, crypto = normalize_symbol(symbol)
    client = get_trading_client()
    order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL

    if crypto:
        # ── Step 0: cancel any existing open sell orders for this symbol ────
        # Alpaca rejects new sell orders as "wash trade" if a prior sell
        # order (orphaned SL/TP from a previous entry) is still open.
        try:
            from alpaca.trading.requests import GetOrdersRequest as _GOR
            from alpaca.trading.enums import QueryOrderStatus as _QOS
            sym_clean = sym.replace("/", "")
            existing_sells = client.get_orders(_GOR(status=_QOS.OPEN, symbols=[sym_clean]))
            cancelled = 0
            for o in existing_sells:
                try:
                    client.cancel_order_by_id(o.id)
                    cancelled += 1
                except Exception:
                    pass
            if cancelled:
                logger.info(f"[Alpaca] Cleared {cancelled} orphan order(s) for {sym} before new entry")
        except Exception as ce:
            logger.debug(f"[Alpaca] Pre-entry cancel check failed for {sym}: {ce}")

        # ── Step 1: market entry ────────────────────────────────────────────
        market_req = MarketOrderRequest(
            symbol=sym,
            qty=round(qty, 8),
            side=order_side,
            time_in_force=TimeInForce.GTC,
        )
        entry_order = client.submit_order(market_req)
        logger.info(f"[Alpaca] Crypto market entry submitted — {sym} x{qty} | order_id={entry_order.id}")

        result = {
            'id': str(entry_order.id),
            'symbol': sym,
            'qty': qty,
            'status': str(entry_order.status),
            'type': 'market',
            'side': str(entry_order.side),
            'crypto': True,
            'sl_order_id': None,
            'tp_order_id': None,
        }

        # ── Step 2a: static stop-loss limit-sell ────────────────────────────
        # Alpaca crypto does NOT support trailing stops or bracket orders.
        # We use a plain limit-sell at the stop_loss price instead.
        if stop_loss and stop_loss > 0:
            try:
                sl_req = LimitOrderRequest(
                    symbol=sym,
                    qty=round(qty, 8),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    limit_price=round(stop_loss, 8),
                )
                sl_order = client.submit_order(sl_req)
                result['sl_order_id'] = str(sl_order.id)
                logger.info(
                    f"[Alpaca] Crypto SL order placed — {sym} limit-sell @ ${stop_loss:.6g} | order_id={sl_order.id}"
                )
            except Exception as e:
                logger.warning(f"[Alpaca] Crypto SL order failed for {sym}: {e} — entry still filled, no SL protection")

        # ── Step 2b: take-profit limit-sell ─────────────────────────────────
        if take_profit and take_profit > 0:
            try:
                tp_req = LimitOrderRequest(
                    symbol=sym,
                    qty=round(qty, 8),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    limit_price=round(take_profit, 8),
                )
                tp_order = client.submit_order(tp_req)
                result['tp_order_id'] = str(tp_order.id)
                logger.info(
                    f"[Alpaca] Crypto TP order placed — {sym} limit-sell @ ${take_profit:.6g} | order_id={tp_order.id}"
                )
            except Exception as e:
                logger.warning(f"[Alpaca] Crypto TP order failed for {sym}: {e} — entry still filled, no TP protection")

        return result

    else:
        # ── Equity: standard bracket (limit entry) ──────────────────────────
        qty = max(1, int(qty))
        req = LimitOrderRequest(
            symbol=sym,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(entry_price, 2),
            order_class=OrderClass.BRACKET,
            take_profit={"limit_price": str(round(take_profit, 2))},
            stop_loss={"stop_price": str(round(stop_loss, 2))},
        )
        order = client.submit_order(req)
        return {
            'id': str(order.id),
            'symbol': sym,
            'qty': qty,
            'status': str(order.status),
            'type': str(order.type),
            'side': str(order.side),
            'crypto': False,
        }

def close_position(symbol: str):
    """
    Close 100% of a position by symbol.
    Calling client.close_position(symbol) with NO ClosePositionRequest options is
    Alpaca's fully-reliable 100% liquidation path — no fractional math, no dust errors.
    NOTE: percentage="1" was a bug — Alpaca treats that as 1%, not 100%.
    Alpaca REST requires no-slash for crypto (BTCUSD not BTC/USD).
    """
    client = get_trading_client()
    s = symbol.upper().strip().replace("/", "")
    try:
        # No ClosePositionRequest = Alpaca default = close 100% of position
        return client.close_position(s)
    except Exception as e:
        err_str = str(e)
        # If the position is already flat or not found, treat as success
        if "position does not exist" in err_str.lower() or "404" in err_str:
            logger.warning(f"[Alpaca] close_position {s}: already flat or not found — skipping")
            return None
        raise


def cancel_open_orders_for_symbol(symbol: str):
    """Cancel all open orders for a symbol — needed before closing a position that has bracket legs."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        client = get_trading_client()
        s = symbol.upper().strip().replace("/", "")
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[s]))
        cancelled = 0
        for o in open_orders:
            try:
                client.cancel_order_by_id(o.id)
                cancelled += 1
            except Exception:
                pass
        return cancelled
    except Exception:
        return 0

