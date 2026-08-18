"""THE single mapping from a JARVIS instrument to a Bitnomial perpetual.

WHY ONE MODULE. Product-code mappings were about to be needed by
`fee_authority`, `execution_snapshot`, `product_router` and the tests. Four
copies of a contract size is four chances to disagree, and the disagreement
shows up as a fee or a quantity that is wrong by an order of magnitude
rather than as an error. Everything that needs to know what a US perpetual
IS asks here.

THE CHAIN THIS COMPLETES:

    JARVIS symbol (BTC/USD)
      -> CRYPTO_PERP                      (lib/product_router, A9)
      -> Kraken Derivatives US expression (venue: kraken_derivatives_us)
      -> Bitnomial product               (PBTCUCZ50, product_id 5614)
      -> contract size, tick, price scale

Kraken Derivatives US does not run its own matching engine for these: US
perpetuals LIST ON BITNOMIAL, a CFTC-regulated exchange. So the execution
VENUE is Kraken Derivatives US and the market-data SOURCE is Bitnomial's
public book. Those are different facts and this module keeps them apart.

DISCOVERY, NOT A HAND-WRITTEN TABLE. `lib/reference/bitnomial_perp_specs.json`
is a captured snapshot of the public product-spec API
(`/exchange/api/v1/prod/product/specs/`, no authentication). It is refreshable
and the loader validates it; nothing here retypes a contract size that the
exchange already publishes.

It lives under `lib/` rather than `data/` because `data/` is gitignored —
it holds the operator's database — so a snapshot placed there would be
invisible to CI, and every perpetual would refuse on a fresh checkout for a
reason that had nothing to do with the exchange.

AUDIT RESULT, 2026-08-17. Every one of the 17 active Bitnomial perpetuals
matched the hand-written `venues.US_PERP_CONTRACTS` contract sizes exactly,
so that registry is CORRECT — it is now cross-checked against the exchange
rather than trusted.

THE PRICE SCALE IS THE DANGEROUS PART, and it is not fully derivable.

Book and product prices arrive as RAW INTEGERS. Measured against the public
book AND the REST product feed AND an independent spot reference, on
2026-08-17:

    price_usd = raw * price_increment

held for 16 of the 17 products to within 0.1%. It did NOT hold for SHIB:

    PSHBUNZ50  raw bid 2231 * 2e-06  =  $0.004462
    SHIB spot                        =  $0.00000447      ratio ~998

The published spec gives SHIB `price_increment` 2e-06 and
`price_quotation_unit` "USD per SHIB" — formally identical in shape to the
other sixteen — so the ~1000x discrepancy is NOT explained by anything the
exchange publishes. A guessed /1000 would be a fabricated constant sitting
in the middle of the price path.

So the scale is a VERIFIED, PER-PRODUCT claim. Anything not on the verified
list is refused, exactly as `venues.us_perp_contracts` refuses a contract
size it does not have. On a $0.0000045 coin a 1000x price error corrupts
notional, fees, leverage, stop distance and liquidation all at once, and
every one of those errors looks plausible on its own.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib

logger = logging.getLogger(__name__)

BITNOMIAL_PRODUCTS_VERSION = "bitnomial_products_v1"

# The execution VENUE (where a Kraken US client's order goes) and the
# market-data SOURCE (whose book priced it) are deliberately different names.
KRAKEN_US_VENUE = "kraken_derivatives_us"
BITNOMIAL_SOURCE = "bitnomial_public_book"

# Public, unauthenticated. Confirmed against the live service on 2026-08-17.
REST_BASE = "https://bitnomial.com/exchange/api/v1/prod"
WS_URL = "wss://bitnomial.com/exchange/ws"

_SPEC_SNAPSHOT = (pathlib.Path(__file__).parent
                  / "reference" / "bitnomial_perp_specs.json")

# Refusals.
NO_BITNOMIAL_PRODUCT = "NO_BITNOMIAL_PRODUCT"
MISSING_CONTRACT_SPEC = "MISSING_CONTRACT_SPEC"
UNVERIFIED_PRICE_SCALE = "UNVERIFIED_PRICE_SCALE"

# ── THE PRICE-SCALE AUDIT ────────────────────────────────────────────────
# Base symbols whose `raw * price_increment = USD` reading was verified
# against the live public book, the REST product feed and an independent
# spot reference on the date below. Adding a member is a claim that somebody
# repeated that measurement — not that it looked reasonable.
PRICE_SCALE_AUDIT_DATE = "2026-08-17"
_VERIFIED_PRICE_SCALE = frozenset({
    "BTCUC", "ETHUI", "SOLUS", "XRPUH", "AVEUS", "AVXUD", "BCHUS", "ADAUK",
    "LNKUD", "DOGUK", "HBRUK", "LTCUS", "DOTUH", "XLMUK", "XTZUK", "TRXUK",
})
# SHBUN (SHIB) is deliberately ABSENT. Its published increment implies a
# price ~1000x the observed market and the exchange publishes nothing that
# explains the difference. It is refused rather than guessed.
_UNVERIFIED_REASON = {
    "SHBUN": ("the published price_increment 2e-06 with quotation unit "
              "'USD per SHIB' implies ~$0.00446 against an observed market "
              "of ~$0.00000447 — a ~1000x discrepancy the spec does not "
              "explain; a fabricated divisor here would corrupt notional, "
              "fees, leverage, stop distance and liquidation at once"),
}

_CACHE: dict | None = None


def _load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        doc = json.loads(_SPEC_SNAPSHOT.read_text(encoding="utf-8"))
        rows = doc.get("products") or []
    except Exception as e:                       # pragma: no cover - io only
        logger.error("[Bitnomial] spec snapshot unreadable: %s", e)
        rows = []
    by_base, by_symbol = {}, {}
    for r in rows:
        base = str(r.get("base_symbol") or "")
        if not base:
            continue
        by_base[base] = r
        by_symbol[str(r.get("symbol") or "")] = r
    _CACHE = {"by_base": by_base, "by_symbol": by_symbol,
              "captured_at": (doc.get("captured_at") if rows else None)}
    return _CACHE


def underlying_of(symbol: str) -> str:
    """BTC from BTC/USD, XBT/USD, BTCUSD. Reuses the venues helper so there
    is one spelling of this rule, not two."""
    from lib.venues import _underlying
    return _underlying(symbol)


class BitnomialProduct:
    """One executable US perpetual, or a stated refusal."""

    __slots__ = ("ok", "reason", "detail", "symbol", "product_id", "base_symbol",
                 "product_code", "contract_size", "contract_size_unit",
                 "price_increment", "underlying", "funding_interval_hours",
                 "venue", "market_data_source")

    def __init__(self, ok, reason=None, detail=None, **kw):
        self.ok, self.reason, self.detail = ok, reason, detail
        for k in ("symbol", "product_id", "base_symbol", "product_code",
                  "contract_size", "contract_size_unit", "price_increment",
                  "underlying", "funding_interval_hours"):
            setattr(self, k, kw.get(k))
        self.venue = KRAKEN_US_VENUE
        self.market_data_source = BITNOMIAL_SOURCE

    def price_usd(self, raw: float) -> float:
        """Raw book integer -> USD per unit of the underlying.

        Only reachable on a product whose scale was verified; `resolve`
        refuses the others, so this can never silently apply the wrong one.
        """
        return float(raw) * float(self.price_increment)

    def contract_value_usd(self, price_usd: float) -> float:
        return float(price_usd) * float(self.contract_size)

    def __repr__(self):
        return (f"BitnomialProduct(ok={self.ok}, symbol={self.symbol!r}, "
                f"reason={self.reason!r})")


def _refuse(reason, detail, **kw):
    return BitnomialProduct(False, reason, detail, **kw)


def resolve(symbol: str) -> BitnomialProduct:
    """The Bitnomial perpetual for a JARVIS symbol, or why there is none.

    CAPABILITY BEFORE ECONOMICS. A spot listing does not prove a US
    perpetual exists — BANK and BEAT trade as spot pairs and have no
    Bitnomial contract — so an unlisted underlying is refused here rather
    than priced somewhere downstream.
    """
    base = underlying_of(symbol)
    if not base:
        return _refuse(NO_BITNOMIAL_PRODUCT, f"no underlying parsed from {symbol!r}")

    spec = None
    for row in _load()["by_base"].values():
        if str(row.get("contract_size_unit") or "").upper() == base.upper():
            spec = row
            break
    if spec is None:
        return _refuse(
            NO_BITNOMIAL_PRODUCT,
            f"{base} has no active Bitnomial perpetual; a spot listing does "
            f"not prove a US perpetual exists")

    base_sym = str(spec.get("base_symbol"))
    for field in ("contract_size", "price_increment", "product_id", "symbol"):
        if spec.get(field) in (None, ""):
            return _refuse(MISSING_CONTRACT_SPEC,
                           f"{base_sym} is missing {field}; it cannot be sized "
                           f"or priced and will not be guessed",
                           base_symbol=base_sym)

    if base_sym not in _VERIFIED_PRICE_SCALE:
        return _refuse(
            UNVERIFIED_PRICE_SCALE,
            f"{base_sym}: " + _UNVERIFIED_REASON.get(
                base_sym,
                "this product's raw-price scale has not been verified against "
                "the live book; it is refused rather than assumed"),
            base_symbol=base_sym, symbol=spec.get("symbol"),
            product_id=spec.get("product_id"))

    return BitnomialProduct(
        True,
        symbol=spec.get("symbol"),
        product_id=spec.get("product_id"),
        base_symbol=base_sym,
        product_code="P" + base_sym,
        contract_size=float(spec.get("contract_size")),
        contract_size_unit=spec.get("contract_size_unit"),
        price_increment=float(spec.get("price_increment")),
        underlying=base,
        funding_interval_hours=spec.get("funding_interval_hours"))


def active_symbols() -> list:
    """Every Bitnomial WS product code this desk may subscribe to."""
    return sorted(r["symbol"] for b, r in _load()["by_base"].items()
                  if b in _VERIFIED_PRICE_SCALE)


def all_discovered() -> list:
    """Every active perpetual discovered, verified or not — for diagnostics."""
    return sorted(_load()["by_base"])


def audit_against_venue_registry() -> dict:
    """Cross-check the exchange's published contract sizes against the
    hand-written `venues.US_PERP_CONTRACTS`.

    Kept as a callable rather than a one-off script because the registry is
    load-bearing for every US perp fee, and a silent divergence after a
    contract respec would be billed rather than raised.
    """
    from lib.venues import US_PERP_CONTRACTS
    agree, disagree, missing = [], [], []
    for under, row in US_PERP_CONTRACTS.items():
        prod = resolve(f"{under}/USD")
        if not prod.ok and prod.reason == NO_BITNOMIAL_PRODUCT:
            missing.append(under)
            continue
        spec = _load()["by_base"].get(prod.base_symbol or "")
        if spec is None:
            missing.append(under)
            continue
        if float(spec["contract_size"]) == float(row["contract_size"]):
            agree.append(under)
        else:
            disagree.append({"underlying": under,
                             "registry": row["contract_size"],
                             "exchange": spec["contract_size"]})
    return {"agree": sorted(agree), "disagree": disagree,
            "missing": sorted(missing),
            "captured_at": _load().get("captured_at"),
            "version": BITNOMIAL_PRODUCTS_VERSION}
