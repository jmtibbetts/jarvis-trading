"""
Free/public crypto market data helpers.

Provider order is intentionally exchange-first, then aggregators:
  1. Binance, OKX, Bybit, Coinbase, Kraken, KuCoin, MEXC for tradable spot pairs and OHLCV
  2. CoinGecko, CoinPaprika, CoinMarketCap, CryptoCompare for breadth/fallback

Optional env keys:
  COINMARKETCAP_API_KEY
  CRYPTOCOMPARE_API_KEY
  COINGECKO_API_KEY
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = float(os.getenv("CRYPTO_API_TIMEOUT", "8"))
TICKER_CACHE_SECONDS = max(5, int(os.getenv("CRYPTO_TICKER_CACHE_SECONDS", "30")))
USER_AGENT = "JarvisTradingAI/6.8 (+local)"
ALLOW_AGGREGATOR_SYMBOL_FALLBACKS = os.getenv("ALLOW_AGGREGATOR_SYMBOL_FALLBACKS", "false").lower() in ("1", "true", "yes")

STABLE_QUOTES = ("USDT", "USDC", "USD")
EXCHANGE_SOURCES = {"binance", "okx", "bybit", "coinbase", "kraken", "kucoin", "mexc"}
SOURCE_PRIORITY = {
    "binance": 100,
    "okx": 95,
    "bybit": 92,
    "coinbase": 90,
    "kraken": 85,
    "kucoin": 83,
    "mexc": 81,
    "coingecko": 50,
    "coinmarketcap": 50,
    "coinpaprika": 45,
    "cryptocompare": 40,
    "default": 0,
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "4L", "4S", "5L", "5S")
KNOWN_BASE_ALIASES = {"XBT": "BTC"}
DEFAULT_CRYPTO_UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "BNB/USD", "AVAX/USD",
    "LINK/USD", "DOGE/USD", "ADA/USD", "AAVE/USD", "DOT/USD", "ATOM/USD",
    "SUI/USD", "RENDER/USD", "INJ/USD", "NEAR/USD", "OP/USD", "ARB/USD",
    "LTC/USD", "UNI/USD", "PEPE/USD", "WIF/USD", "BONK/USD", "JUP/USD",
]

COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "BNB": "binancecoin", "AVAX": "avalanche-2", "LINK": "chainlink",
    "DOGE": "dogecoin", "ADA": "cardano", "AAVE": "aave", "DOT": "polkadot",
    "ATOM": "cosmos", "SUI": "sui", "RENDER": "render-token", "RNDR": "render-token",
    "INJ": "injective-protocol", "NEAR": "near", "OP": "optimism",
    "ARB": "arbitrum", "LTC": "litecoin", "UNI": "uniswap",
    "PEPE": "pepe", "WIF": "dogwifcoin", "BONK": "bonk", "JUP": "jupiter-exchange-solana",
}


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


def _json(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    with httpx.Client(timeout=HTTP_TIMEOUT, headers=_headers(headers), follow_redirects=True) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _clean_base(base: str) -> str:
    base = (base or "").upper().strip()
    base = KNOWN_BASE_ALIASES.get(base, base)
    return re.sub(r"[^A-Z0-9]", "", base)


def _split_crypto_symbol(symbol: str) -> tuple[str, str | None]:
    """Return (base, requested_quote). USD means app-generic; USDT/USDC are explicit."""
    s = (symbol or "").upper().strip().replace(":", "-").replace("_", "-")
    if "/" in s:
        base, quote = s.split("/", 1)
        return _clean_base(base), quote if quote in STABLE_QUOTES else None
    if "-" in s:
        base, quote = s.split("-", 1)
        return _clean_base(base), quote if quote in STABLE_QUOTES else None
    for quote in STABLE_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            return _clean_base(s[:-len(quote)]), quote
    return _clean_base(s), None


def _quote_candidates(symbol: str) -> list[str]:
    _, quote = _split_crypto_symbol(symbol)
    if quote == "USDT":
        return ["USDT", "USDC", "USD"]
    if quote == "USDC":
        return ["USDC", "USDT", "USD"]
    # Treat BASE/USD as app-generic USD stable quote, not strictly a USD book.
    return ["USDT", "USDC", "USD"]


def normalize_crypto_symbol(symbol: str) -> str:
    """Return app-native BASE/USD, accepting BASE-USD, BASE/USD, BASEUSDT, etc."""
    base, _ = _split_crypto_symbol(symbol)
    return f"{base}/USD"


def to_dash_symbol(symbol: str) -> str:
    base = normalize_crypto_symbol(symbol).split("/")[0]
    return f"{base}-USD"


def is_crypto_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().strip()
    return "/" in s or s.endswith("-USD") or s in {x.split("/")[0] for x in DEFAULT_CRYPTO_UNIVERSE}


def _base(symbol: str) -> str:
    return _split_crypto_symbol(symbol)[0]


def _valid_base(base: str) -> bool:
    if not base or len(base) > 12:
        return False
    if any(base.endswith(sfx) and len(base) > len(sfx) + 2 for sfx in LEVERAGED_SUFFIXES):
        return False
    return bool(re.match(r"^[A-Z0-9]+$", base))


def _normalize_ohlcv(df: pd.DataFrame, source: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    cols = ["open", "high", "low", "close", "volume"]
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if not all(c in df.columns for c in cols):
        return None
    df = df[cols]
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.attrs["source"] = source
    return df if len(df) >= 3 else None


def _resample(df: pd.DataFrame, tf: str, source: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    rules = {"3m": "3min", "2H": "2h", "4H": "4h"}
    if tf in rules:
        df = df.resample(rules[tf]).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
        df.attrs["source"] = source
    return df


def _ticker_cache_bucket() -> int:
    return int(time.monotonic() // TICKER_CACHE_SECONDS)


@lru_cache(maxsize=4)
def _binance_tickers_cached(_bucket: int) -> list[dict[str, Any]]:
    return _json("https://api.binance.com/api/v3/ticker/24hr")


def _binance_tickers() -> list[dict[str, Any]]:
    return _binance_tickers_cached(_ticker_cache_bucket())


def _binance_symbol(symbol: str) -> Optional[str]:
    base = _base(symbol)
    tickers = _binance_tickers()
    candidates = [f"{base}{q}" for q in _quote_candidates(symbol)]
    available = {t.get("symbol") for t in tickers}
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


def _binance_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    try:
        by_symbol = {t.get("symbol"): t for t in _binance_tickers()}
        results = {}
        for sym in symbols:
            base = _base(sym)
            pair = _binance_symbol(sym)
            row = by_symbol.get(pair) if pair else None
            price = _safe_float((row or {}).get("lastPrice"))
            if price:
                results[normalize_crypto_symbol(sym)] = {
                    "price": price,
                    "volume": _safe_float(row.get("quoteVolume")),
                    "change_percent": _safe_float(row.get("priceChangePercent")),
                    "asset_class": "Crypto",
                    "name": normalize_crypto_symbol(sym),
                    "source": "binance",
                }
        return results
    except Exception as e:
        logger.debug(f"[CryptoData] Binance prices failed: {e}")
        return {}


def _binance_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    interval = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1H": "1h", "2H": "2h", "4H": "4h", "1D": "1d",
    }.get(tf, "1d")
    try:
        pair = _binance_symbol(symbol)
        if not pair:
            return None
        rows = _json("https://api.binance.com/api/v3/klines", {"symbol": pair, "interval": interval, "limit": limit})
        df = pd.DataFrame(rows, columns=[
            "ts", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ])
        df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return _normalize_ohlcv(df, "binance")
    except Exception as e:
        logger.debug(f"[CryptoData] Binance OHLCV failed {symbol}/{tf}: {e}")
        return None


def _coinbase_pair(symbol: str) -> list[str]:
    base = _base(symbol)
    return [f"{base}-{quote}" for quote in _quote_candidates(symbol)]


def _coinbase_price(symbol: str) -> Optional[dict[str, Any]]:
    for pair in _coinbase_pair(symbol):
        try:
            row = _json(f"https://api.exchange.coinbase.com/products/{pair}/ticker")
            price = _safe_float(row.get("price"))
            if price:
                return {
                    "price": price, "volume": _safe_float(row.get("volume")),
                    "asset_class": "Crypto", "name": normalize_crypto_symbol(symbol),
                    "source": "coinbase",
                }
        except Exception:
            continue
    return None


def _coinbase_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    granularity = {
        "1m": 60, "3m": 60, "5m": 300, "15m": 900, "30m": 300,
        "1H": 3600, "2H": 3600, "4H": 21600, "1D": 86400,
    }.get(tf, 86400)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=granularity * min(limit, 300))
    params = {"granularity": granularity, "start": start.isoformat(), "end": end.isoformat()}
    for pair in _coinbase_pair(symbol):
        try:
            rows = _json(f"https://api.exchange.coinbase.com/products/{pair}/candles", params)
            if not rows:
                continue
            df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
            df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
            normalized = _normalize_ohlcv(df, "coinbase")
            if normalized is not None and tf in ("3m", "2H", "30m"):
                rule = {"3m": "3min", "2H": "2h", "30m": "30min"}[tf]
                normalized = normalized.resample(rule).agg({
                    "open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum",
                }).dropna(subset=["close"])
                normalized.attrs["source"] = "coinbase"
            return normalized.tail(limit) if normalized is not None else None
        except Exception:
            continue
    return None


def _kraken_pairs(symbol: str) -> list[str]:
    base = _base(symbol)
    b = "XBT" if base == "BTC" else base
    return [f"{b}{quote}" for quote in _quote_candidates(symbol)]


def _kraken_price(symbol: str) -> Optional[dict[str, Any]]:
    for pair in _kraken_pairs(symbol):
        try:
            row = _json("https://api.kraken.com/0/public/Ticker", {"pair": pair})
            result = row.get("result") or {}
            data = next(iter(result.values()), None)
            price = _safe_float((data or {}).get("c", [0])[0])
            if price:
                return {
                    "price": price,
                    "volume": _safe_float((data or {}).get("v", [0, 0])[1]),
                    "asset_class": "Crypto",
                    "name": normalize_crypto_symbol(symbol),
                    "source": "kraken",
                }
        except Exception:
            continue
    return None


def _kraken_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    interval = {
        "1m": 1, "3m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1H": 60, "2H": 60, "4H": 240, "1D": 1440,
    }.get(tf, 1440)
    since = int((datetime.now(timezone.utc) - timedelta(minutes=interval * limit * 2)).timestamp())
    for pair in _kraken_pairs(symbol):
        try:
            row = _json("https://api.kraken.com/0/public/OHLC", {"pair": pair, "interval": interval, "since": since})
            result = row.get("result") or {}
            result.pop("last", None)
            rows = next(iter(result.values()), [])
            if not rows:
                continue
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vwap", "volume", "count"])
            df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
            df = _normalize_ohlcv(df, "kraken")
            return _resample(df, tf, "kraken") if tf in ("3m", "2H") else df
        except Exception:
            continue
    return None


@lru_cache(maxsize=4)
def _okx_tickers_cached(_bucket: int) -> list[dict[str, Any]]:
    data = _json("https://www.okx.com/api/v5/market/tickers", {"instType": "SPOT"})
    return data.get("data") or []


def _okx_tickers() -> list[dict[str, Any]]:
    return _okx_tickers_cached(_ticker_cache_bucket())


def _okx_inst(symbol: str) -> Optional[str]:
    base = _base(symbol)
    wanted = [f"{base}-{q}" for q in _quote_candidates(symbol)]
    available = {r.get("instId") for r in _okx_tickers()}
    for inst in wanted:
        if inst in available:
            return inst
    return None


def _okx_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    try:
        rows = {r.get("instId"): r for r in _okx_tickers()}
        results = {}
        for sym in symbols:
            inst = _okx_inst(sym)
            row = rows.get(inst) if inst else None
            price = _safe_float((row or {}).get("last"))
            if price:
                results[normalize_crypto_symbol(sym)] = {
                    "price": price,
                    "volume": _safe_float(row.get("volCcy24h") or row.get("vol24h")),
                    "change_percent": 0,
                    "asset_class": "Crypto",
                    "name": normalize_crypto_symbol(sym),
                    "source": "okx",
                }
        return results
    except Exception as e:
        logger.debug(f"[CryptoData] OKX prices failed: {e}")
        return {}


def _okx_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    bar = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1H": "1H", "2H": "2H", "4H": "4H", "1D": "1Dutc",
    }.get(tf, "1Dutc")
    try:
        inst = _okx_inst(symbol)
        if not inst:
            return None
        data = _json("https://www.okx.com/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": limit}).get("data") or []
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume", "vol_ccy", "vol_quote", "confirm"])
        df.index = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms", utc=True)
        return _normalize_ohlcv(df, "okx")
    except Exception as e:
        logger.debug(f"[CryptoData] OKX OHLCV failed {symbol}/{tf}: {e}")
        return None


@lru_cache(maxsize=4)
def _bybit_tickers_cached(_bucket: int) -> list[dict[str, Any]]:
    data = _json("https://api.bybit.com/v5/market/tickers", {"category": "spot"})
    return ((data.get("result") or {}).get("list") or []) if isinstance(data, dict) else []


def _bybit_tickers() -> list[dict[str, Any]]:
    return _bybit_tickers_cached(_ticker_cache_bucket())


def _bybit_pair(symbol: str) -> Optional[str]:
    base = _base(symbol)
    wanted = [f"{base}{quote}" for quote in _quote_candidates(symbol)]
    available = {row.get("symbol") for row in _bybit_tickers()}
    return next((pair for pair in wanted if pair in available), None)


def _bybit_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    try:
        rows = {row.get("symbol"): row for row in _bybit_tickers()}
        results = {}
        for symbol in symbols:
            pair = _bybit_pair(symbol)
            row = rows.get(pair) if pair else None
            price = _safe_float((row or {}).get("lastPrice"))
            if price:
                normalized = normalize_crypto_symbol(symbol)
                results[normalized] = {
                    "price": price,
                    "volume": _safe_float(row.get("turnover24h") or row.get("volume24h")),
                    "change_percent": _safe_float(row.get("price24hPcnt")) * 100,
                    "asset_class": "Crypto", "name": normalized, "source": "bybit",
                }
        return results
    except Exception as e:
        logger.debug(f"[CryptoData] Bybit prices failed: {e}")
        return {}


def _bybit_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    interval = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1H": "60", "2H": "120", "4H": "240", "1D": "D",
    }.get(tf, "D")
    try:
        pair = _bybit_pair(symbol)
        if not pair:
            return None
        payload = _json("https://api.bybit.com/v5/market/kline", {
            "category": "spot", "symbol": pair, "interval": interval, "limit": min(limit, 1000),
        })
        rows = ((payload.get("result") or {}).get("list") or [])
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df.index = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms", utc=True)
        return _normalize_ohlcv(df, "bybit")
    except Exception as e:
        logger.debug(f"[CryptoData] Bybit OHLCV failed {symbol}/{tf}: {e}")
        return None


@lru_cache(maxsize=4)
def _kucoin_tickers_cached(_bucket: int) -> list[dict[str, Any]]:
    data = _json("https://api.kucoin.com/api/v1/market/allTickers")
    return ((data.get("data") or {}).get("ticker") or []) if isinstance(data, dict) else []


def _kucoin_tickers() -> list[dict[str, Any]]:
    return _kucoin_tickers_cached(_ticker_cache_bucket())


def _kucoin_pair(symbol: str) -> Optional[str]:
    base = _base(symbol)
    wanted = [f"{base}-{quote}" for quote in _quote_candidates(symbol)]
    available = {row.get("symbol") for row in _kucoin_tickers()}
    return next((pair for pair in wanted if pair in available), None)


def _kucoin_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    try:
        rows = {row.get("symbol"): row for row in _kucoin_tickers()}
        results = {}
        for symbol in symbols:
            pair = _kucoin_pair(symbol)
            row = rows.get(pair) if pair else None
            price = _safe_float((row or {}).get("last"))
            if price:
                normalized = normalize_crypto_symbol(symbol)
                results[normalized] = {
                    "price": price,
                    "volume": _safe_float(row.get("volValue") or row.get("vol")),
                    "change_percent": _safe_float(row.get("changeRate")) * 100,
                    "asset_class": "Crypto", "name": normalized, "source": "kucoin",
                }
        return results
    except Exception as e:
        logger.debug(f"[CryptoData] KuCoin prices failed: {e}")
        return {}


def _kucoin_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    interval = {
        "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1H": "1hour", "2H": "2hour", "4H": "4hour", "1D": "1day",
    }.get(tf, "1day")
    seconds = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400,
    }.get(tf, 86400)
    try:
        pair = _kucoin_pair(symbol)
        if not pair:
            return None
        end = int(datetime.now(timezone.utc).timestamp())
        payload = _json("https://api.kucoin.com/api/ua/v1/market/kline", {
            "tradeType": "SPOT", "symbol": pair, "interval": interval,
            "startAt": end - seconds * min(limit, 1500), "endAt": end,
        })
        data = payload.get("data") or []
        rows = data.get("list", []) if isinstance(data, dict) else data
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df.index = pd.to_datetime(pd.to_numeric(df["ts"]), unit="s", utc=True)
        return _normalize_ohlcv(df, "kucoin")
    except Exception as e:
        logger.debug(f"[CryptoData] KuCoin OHLCV failed {symbol}/{tf}: {e}")
        return None


@lru_cache(maxsize=4)
def _mexc_tickers_cached(_bucket: int) -> list[dict[str, Any]]:
    data = _json("https://api.mexc.com/api/v3/ticker/24hr")
    return data if isinstance(data, list) else []


def _mexc_tickers() -> list[dict[str, Any]]:
    return _mexc_tickers_cached(_ticker_cache_bucket())


def _mexc_pair(symbol: str) -> Optional[str]:
    base = _base(symbol)
    wanted = [f"{base}{quote}" for quote in _quote_candidates(symbol)]
    available = {row.get("symbol") for row in _mexc_tickers()}
    return next((pair for pair in wanted if pair in available), None)


def _mexc_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    try:
        rows = {row.get("symbol"): row for row in _mexc_tickers()}
        results = {}
        for symbol in symbols:
            pair = _mexc_pair(symbol)
            row = rows.get(pair) if pair else None
            price = _safe_float((row or {}).get("lastPrice"))
            if price:
                normalized = normalize_crypto_symbol(symbol)
                change = _safe_float(row.get("priceChangePercent"))
                results[normalized] = {
                    "price": price,
                    "volume": _safe_float(row.get("quoteVolume") or row.get("volume")),
                    "change_percent": change * 100 if abs(change) <= 1 else change,
                    "asset_class": "Crypto", "name": normalized, "source": "mexc",
                }
        return results
    except Exception as e:
        logger.debug(f"[CryptoData] MEXC prices failed: {e}")
        return {}


def _mexc_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    interval = {
        "1m": "1m", "3m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1H": "60m", "2H": "60m", "4H": "4h", "1D": "1d",
    }.get(tf, "1d")
    multiplier = 3 if tf == "3m" else 2 if tf == "2H" else 1
    try:
        pair = _mexc_pair(symbol)
        if not pair:
            return None
        rows = _json("https://api.mexc.com/api/v3/klines", {
            "symbol": pair, "interval": interval, "limit": min(limit * multiplier, 1000),
        })
        if not rows:
            return None
        width = len(rows[0])
        columns = ["ts", "open", "high", "low", "close", "volume", "close_time", "quote_volume"]
        columns.extend(f"extra_{idx}" for idx in range(max(0, width - len(columns))))
        df = pd.DataFrame(rows, columns=columns[:width])
        df.index = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms", utc=True)
        normalized = _normalize_ohlcv(df, "mexc")
        return _resample(normalized, tf, "mexc") if tf in ("3m", "2H") else normalized
    except Exception as e:
        logger.debug(f"[CryptoData] MEXC OHLCV failed {symbol}/{tf}: {e}")
        return None


def _cryptocompare_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    key = os.getenv("CRYPTOCOMPARE_API_KEY", "")
    results = {}
    try:
        bases = [_base(s) for s in symbols]
        params = {"fsyms": ",".join(bases), "tsyms": "USD"}
        if key:
            params["api_key"] = key
        rows = _json("https://min-api.cryptocompare.com/data/pricemultifull", params)
        raw = (rows.get("RAW") or {})
        for base, data in raw.items():
            usd = data.get("USD") or {}
            price = _safe_float(usd.get("PRICE"))
            if price:
                sym = f"{base}/USD"
                results[sym] = {
                    "price": price,
                    "volume": _safe_float(usd.get("VOLUME24HOURTO")),
                    "change_percent": _safe_float(usd.get("CHANGEPCT24HOUR")),
                    "asset_class": "Crypto",
                    "name": sym,
                    "source": "cryptocompare",
                }
        return results
    except Exception as e:
        logger.debug(f"[CryptoData] CryptoCompare prices failed: {e}")
        return results


def _cryptocompare_ohlcv(symbol: str, tf: str, limit: int) -> Optional[pd.DataFrame]:
    if not ALLOW_AGGREGATOR_SYMBOL_FALLBACKS:
        return None
    key = os.getenv("CRYPTOCOMPARE_API_KEY", "")
    base = _base(symbol)
    aggregate = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1H": 1, "2H": 2, "4H": 4, "1D": 1,
    }.get(tf, 1)
    endpoint = "histoday" if tf == "1D" else "histominute" if tf.endswith("m") else "histohour"
    params = {"fsym": base, "tsym": "USD", "limit": limit, "aggregate": aggregate, "e": "CCCAGG"}
    if key:
        params["api_key"] = key
    try:
        data = _json(f"https://min-api.cryptocompare.com/data/v2/{endpoint}", params)
        rows = ((data.get("Data") or {}).get("Data") or [])
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df = df.rename(columns={"time": "ts", "volumefrom": "volume"})
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        return _normalize_ohlcv(df, "cryptocompare")
    except Exception as e:
        logger.debug(f"[CryptoData] CryptoCompare OHLCV failed {symbol}/{tf}: {e}")
        return None


def _coingecko_key_headers() -> dict[str, str]:
    key = os.getenv("COINGECKO_API_KEY", "")
    return {"x-cg-demo-api-key": key} if key else {}


def _coingecko_discover(limit: int) -> list[dict[str, Any]]:
    out = []
    try:
        pages = max(1, min(4, (limit + 249) // 250))
        for page in range(1, pages + 1):
            rows = _json(
                "https://api.coingecko.com/api/v3/coins/markets",
                {
                    "vs_currency": "usd", "order": "volume_desc", "per_page": min(250, limit),
                    "page": page, "sparkline": "false", "price_change_percentage": "24h",
                },
                _coingecko_key_headers(),
            )
            for r in rows:
                base = _clean_base(r.get("symbol", ""))
                if not _valid_base(base):
                    continue
                COINGECKO_IDS.setdefault(base, r.get("id"))
                out.append({
                    "symbol": f"{base}/USD",
                    "name": r.get("name") or base,
                    "price": _safe_float(r.get("current_price")),
                    "volume": _safe_float(r.get("total_volume")),
                    "change_pct": _safe_float(r.get("price_change_percentage_24h")),
                    "market_cap": _safe_float(r.get("market_cap")),
                    "source": "coingecko",
                })
        return out
    except Exception as e:
        logger.debug(f"[CryptoData] CoinGecko discover failed: {e}")
        return out


def _coingecko_ohlcv(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    coin_id = COINGECKO_IDS.get(_base(symbol))
    if not coin_id:
        return None
    days = "1" if tf.endswith("m") else "90" if tf in ("1H", "2H", "4H") else "365"
    try:
        rows = _json(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
            {"vs_currency": "usd", "days": days},
            _coingecko_key_headers(),
        )
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
        df["volume"] = 0
        df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return _normalize_ohlcv(df, "coingecko")
    except Exception as e:
        logger.debug(f"[CryptoData] CoinGecko OHLCV failed {symbol}/{tf}: {e}")
        return None


def _coinpaprika_discover(limit: int) -> list[dict[str, Any]]:
    out = []
    try:
        rows = _json("https://api.coinpaprika.com/v1/tickers", {"quotes": "USD"})
        for r in rows[: max(limit, 100)]:
            base = _clean_base(r.get("symbol", ""))
            quote = ((r.get("quotes") or {}).get("USD") or {})
            if not _valid_base(base):
                continue
            out.append({
                "symbol": f"{base}/USD",
                "name": r.get("name") or base,
                "price": _safe_float(quote.get("price")),
                "volume": _safe_float(quote.get("volume_24h")),
                "change_pct": _safe_float(quote.get("percent_change_24h")),
                "market_cap": _safe_float(quote.get("market_cap")),
                "source": "coinpaprika",
            })
        return out
    except Exception as e:
        logger.debug(f"[CryptoData] CoinPaprika discover failed: {e}")
        return out


def _coinmarketcap_discover(limit: int) -> list[dict[str, Any]]:
    key = os.getenv("COINMARKETCAP_API_KEY")
    url = "https://pro-api.coinmarketcap.com/v3/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": key} if key else None
    if not key:
        url = "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/listings/latest"
    try:
        data = _json(
            url,
            {"start": 1, "limit": min(limit, 500), "convert": "USD", "sort": "volume_24h"},
            headers,
        )
        out = []
        for r in data.get("data") or []:
            base = _clean_base(r.get("symbol", ""))
            quote_data = r.get("quote") or {}
            if isinstance(quote_data, list):
                quote = next((q for q in quote_data if (q.get("symbol") or "").upper() == "USD"), {})
            else:
                quote = quote_data.get("USD") or {}
            if not _valid_base(base):
                continue
            out.append({
                "symbol": f"{base}/USD",
                "name": r.get("name") or base,
                "price": _safe_float(quote.get("price")),
                "volume": _safe_float(quote.get("volume_24h")),
                "change_pct": _safe_float(quote.get("percent_change_24h")),
                "market_cap": _safe_float(quote.get("market_cap")),
                "source": "coinmarketcap",
            })
        return out
    except Exception as e:
        logger.debug(f"[CryptoData] CoinMarketCap discover failed: {e}")
        return []


def _cryptocompare_discover(limit: int) -> list[dict[str, Any]]:
    key = os.getenv("CRYPTOCOMPARE_API_KEY", "")
    params = {"limit": min(limit, 100), "tsym": "USD"}
    if key:
        params["api_key"] = key
    try:
        data = _json("https://min-api.cryptocompare.com/data/top/totalvolfull", params)
        out = []
        for r in data.get("Data") or []:
            coin = r.get("CoinInfo") or {}
            raw = (((r.get("RAW") or {}).get("USD")) or {})
            base = _clean_base(coin.get("Name") or coin.get("Internal") or "")
            if not _valid_base(base):
                continue
            out.append({
                "symbol": f"{base}/USD",
                "name": coin.get("FullName") or base,
                "price": _safe_float(raw.get("PRICE")),
                "volume": _safe_float(raw.get("VOLUME24HOURTO")),
                "change_pct": _safe_float(raw.get("CHANGEPCT24HOUR")),
                "market_cap": _safe_float(raw.get("MKTCAP")),
                "source": "cryptocompare",
            })
        return out
    except Exception as e:
        logger.debug(f"[CryptoData] CryptoCompare discover failed: {e}")
        return []


def discover_crypto_universe(limit: int = 150) -> list[dict[str, Any]]:
    """Discover high-volume crypto symbols from every available free source."""
    rows: list[dict[str, Any]] = []

    try:
        for r in _binance_tickers():
            symbol = r.get("symbol") or ""
            quote = next((q for q in STABLE_QUOTES if symbol.endswith(q)), "")
            if not quote:
                continue
            base = _clean_base(symbol[:-len(quote)])
            if not _valid_base(base):
                continue
            rows.append({
                "symbol": f"{base}/USD",
                "name": base,
                "price": _safe_float(r.get("lastPrice")),
                "volume": _safe_float(r.get("quoteVolume")),
                "change_pct": _safe_float(r.get("priceChangePercent")),
                "source": "binance",
            })
    except Exception as e:
        logger.debug(f"[CryptoData] Binance discover failed: {e}")

    try:
        for r in _okx_tickers():
            inst = r.get("instId") or ""
            parts = inst.split("-")
            if len(parts) != 2 or parts[1] not in STABLE_QUOTES:
                continue
            base = _clean_base(parts[0])
            if not _valid_base(base):
                continue
            rows.append({
                "symbol": f"{base}/USD",
                "name": base,
                "price": _safe_float(r.get("last")),
                "volume": _safe_float(r.get("volCcy24h") or r.get("vol24h")),
                "change_pct": 0,
                "source": "okx",
            })
    except Exception as e:
        logger.debug(f"[CryptoData] OKX discover failed: {e}")

    try:
        for r in _bybit_tickers():
            symbol = r.get("symbol") or ""
            quote = next((q for q in STABLE_QUOTES if symbol.endswith(q)), "")
            if not quote:
                continue
            base = _clean_base(symbol[:-len(quote)])
            if not _valid_base(base):
                continue
            rows.append({
                "symbol": f"{base}/USD", "name": base,
                "price": _safe_float(r.get("lastPrice")),
                "volume": _safe_float(r.get("turnover24h") or r.get("volume24h")),
                "change_pct": _safe_float(r.get("price24hPcnt")) * 100,
                "source": "bybit",
            })
    except Exception as e:
        logger.debug(f"[CryptoData] Bybit discover failed: {e}")

    try:
        for r in _kucoin_tickers():
            parts = (r.get("symbol") or "").split("-")
            if len(parts) != 2 or parts[1] not in STABLE_QUOTES:
                continue
            base = _clean_base(parts[0])
            if not _valid_base(base):
                continue
            rows.append({
                "symbol": f"{base}/USD", "name": base,
                "price": _safe_float(r.get("last")),
                "volume": _safe_float(r.get("volValue") or r.get("vol")),
                "change_pct": _safe_float(r.get("changeRate")) * 100,
                "source": "kucoin",
            })
    except Exception as e:
        logger.debug(f"[CryptoData] KuCoin discover failed: {e}")

    try:
        for r in _mexc_tickers():
            symbol = r.get("symbol") or ""
            quote = next((q for q in STABLE_QUOTES if symbol.endswith(q)), "")
            if not quote:
                continue
            base = _clean_base(symbol[:-len(quote)])
            if not _valid_base(base):
                continue
            change = _safe_float(r.get("priceChangePercent"))
            rows.append({
                "symbol": f"{base}/USD", "name": base,
                "price": _safe_float(r.get("lastPrice")),
                "volume": _safe_float(r.get("quoteVolume") or r.get("volume")),
                "change_pct": change * 100 if abs(change) <= 1 else change,
                "source": "mexc",
            })
    except Exception as e:
        logger.debug(f"[CryptoData] MEXC discover failed: {e}")

    rows.extend(_coingecko_discover(limit))
    rows.extend(_cryptocompare_discover(limit))
    rows.extend(_coinmarketcap_discover(limit))
    rows.extend(_coinpaprika_discover(limit))

    merged: dict[str, dict[str, Any]] = {}
    for item in rows:
        sym = normalize_crypto_symbol(item.get("symbol", ""))
        base = _base(sym)
        if not _valid_base(base):
            continue
        existing = merged.get(sym)
        item_priority = SOURCE_PRIORITY.get(item.get("source"), 0)
        existing_priority = SOURCE_PRIORITY.get((existing or {}).get("source"), 0)
        should_replace = (
            not existing
            or item_priority > existing_priority
            or (
                item_priority == existing_priority
                and _safe_float(item.get("volume")) > _safe_float(existing.get("volume"))
            )
        )
        if should_replace:
            item["symbol"] = sym
            merged[sym] = item

    for sym in DEFAULT_CRYPTO_UNIVERSE:
        merged.setdefault(sym, {"symbol": sym, "name": sym, "volume": 0, "price": 0, "change_pct": 0, "source": "default"})

    discovered = sorted(merged.values(), key=lambda x: (_safe_float(x.get("volume")), _safe_float(x.get("market_cap"))), reverse=True)
    return discovered[:limit]


def fetch_crypto_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch latest prices, merging all providers without overwriting earlier hits."""
    normalized = list(dict.fromkeys(normalize_crypto_symbol(s) for s in symbols))
    results: dict[str, dict[str, Any]] = {}
    for provider in (_binance_prices, _okx_prices, _bybit_prices, _kucoin_prices, _mexc_prices):
        for sym, data in provider(normalized).items():
            results.setdefault(sym, data)

    for sym in normalized:
        if sym in results:
            continue
        data = _coinbase_price(sym) or _kraken_price(sym)
        if data:
            results[sym] = data

    missing = [s for s in normalized if s not in results]
    if missing and ALLOW_AGGREGATOR_SYMBOL_FALLBACKS:
        for sym, data in _cryptocompare_prices(missing).items():
            results.setdefault(sym, data)

    return results


def fetch_crypto_ohlcv(symbol: str, tf: str = "1H", limit: int = 120, allow_aggregators: bool = False) -> Optional[pd.DataFrame]:
    """Fetch OHLCV bars from exchange APIs, then aggregator fallbacks."""
    raw_symbol = symbol
    normalized = normalize_crypto_symbol(symbol)
    providers = (
        _binance_ohlcv,
        _okx_ohlcv,
        _bybit_ohlcv,
        _coinbase_ohlcv,
        _kraken_ohlcv,
        _kucoin_ohlcv,
        _mexc_ohlcv,
        _cryptocompare_ohlcv,
    )
    for provider in providers:
        df = provider(raw_symbol, tf, limit)
        if df is not None and not df.empty:
            return df.tail(limit)

    if allow_aggregators or ALLOW_AGGREGATOR_SYMBOL_FALLBACKS:
        df = _coingecko_ohlcv(normalized, tf)
        if df is not None and not df.empty:
            return df.tail(limit)
    return None
