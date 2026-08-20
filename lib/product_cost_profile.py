"""Which costs actually exist for a product. LEVERAGE IS NOT BORROWING.

THE DEFECT THIS PREVENTS. A 50x perpetual posts ~2% margin. It is tempting
to read the other 98% as a loan and charge interest on it — brokers do
charge interest on margin loans, after all. But nobody lent anything: the
leverage lives in the CONTRACT. A perpetual short borrows no coin, a
futures short borrows no shares, and a CME contract's initial margin is
collateral, not a down payment on a loan. Charging borrow interest there
is inventing a fee the venue never charges — and the audit found the
planning path doing exactly that: `borrow_cost_pct` charged stock-borrow
on any short non-crypto symbol, futures included, and `funding_cost_pct`
charged perpetual funding on any crypto symbol, spot included.

THE RULE: a product pays only the costs that exist for that product, on
affirmative evidence. The forbidden inferences are structural here:

    leverage > 1                 does NOT imply borrowing
    notional - margin            is NOT a loan principal
    short                        does NOT imply the underlying is borrowed
    funding                      is NOT interest on borrowed notional
    "brokers normally charge X"  is NOT evidence

Financing exists where something is actually extended: a margined spot
long is financed quote currency, a margined spot short is lent the asset
it sells, a real equity short borrows real shares. Those keep their
financing models. Everything derivative pays contract costs — maker/taker
or commission, exchange/clearing/regulatory where the venue says so,
funding for perpetuals as a SIGNED TRANSFER — and no phantom loan.

Sources are recorded per profile. Kraken's own schedules are the concrete
evidence for the three Kraken shapes: spot margin carries opening and
rollover fees (something IS extended); its perpetuals carry maker/taker
plus funding, described separately from fees; its US futures carry
commission/exchange/NFA/clearing with funding as a perpetual-only payment.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

PRODUCT_COST_MODEL_VERSION = "product_cost_v1"

UNKNOWN = "UNKNOWN"

# ── Product taxonomy. Collapsing these into "LEVERAGED" destroys the
#    economics, so every class is its own name. ──────────────────────────
SPOT_CASH = "SPOT_CASH"
SPOT_MARGIN_LONG = "SPOT_MARGIN_LONG"
SPOT_MARGIN_SHORT = "SPOT_MARGIN_SHORT"
EQUITY_CASH = "EQUITY_CASH"
EQUITY_MARGIN_LONG = "EQUITY_MARGIN_LONG"
EQUITY_SHORT_BORROW = "EQUITY_SHORT_BORROW"
CRYPTO_PERPETUAL = "CRYPTO_PERPETUAL"
CRYPTO_FIXED_FUTURE = "CRYPTO_FIXED_FUTURE"
EQUITY_INDEX_FUTURE = "EQUITY_INDEX_FUTURE"
COMMODITY_FUTURE = "COMMODITY_FUTURE"
FX_FUTURE = "FX_FUTURE"
OPTION = "OPTION"
DEX_SPOT_SWAP = "DEX_SPOT_SWAP"

TAXONOMY = (SPOT_CASH, SPOT_MARGIN_LONG, SPOT_MARGIN_SHORT, EQUITY_CASH,
            EQUITY_MARGIN_LONG, EQUITY_SHORT_BORROW, CRYPTO_PERPETUAL,
            CRYPTO_FIXED_FUTURE, EQUITY_INDEX_FUTURE, COMMODITY_FUTURE,
            FX_FUTURE, OPTION, DEX_SPOT_SWAP)

# Cost model vocabulary per component.
NOT_APPLICABLE = "NOT_APPLICABLE"
MAKER_TAKER = "MAKER_TAKER"
COMMISSION_PER_CONTRACT = "COMMISSION_PER_CONTRACT"
POOL_FEE = "POOL_FEE"
PERPETUAL_FUNDING_TRANSFER = "PERPETUAL_FUNDING_TRANSFER"
MARGIN_OPEN_PLUS_ROLLOVER = "MARGIN_OPEN_PLUS_ROLLOVER"
STOCK_BORROW_ACCRUAL = "STOCK_BORROW_ACCRUAL"
BROKER_MARGIN_INTEREST = "BROKER_MARGIN_INTEREST"
EXCHANGE_NFA_CLEARING = "EXCHANGE_NFA_CLEARING"
NETWORK_GAS = "NETWORK_GAS"


@dataclass(frozen=True)
class ProductCostProfile:
    """The cost surface of one product class — what exists, what cannot."""
    product_class: str
    # THE LOAD-BEARING BIT. True only where an asset is actually extended,
    # lent or located. NEVER derived from leverage.
    actual_borrowing_required: bool | str
    financing_model: str = NOT_APPLICABLE
    trading_fee_model: str = MAKER_TAKER
    funding_model: str = NOT_APPLICABLE
    clearing_fee_model: str = NOT_APPLICABLE
    exchange_fee_model: str = NOT_APPLICABLE
    regulatory_fee_model: str = NOT_APPLICABLE
    settlement_fee_model: str = NOT_APPLICABLE
    liquidation_fee_model: str = NOT_APPLICABLE
    borrow_fee_model: str = NOT_APPLICABLE
    collateral_conversion_model: str = NOT_APPLICABLE
    network_fee_model: str = NOT_APPLICABLE
    source: str = UNKNOWN
    model_version: str = PRODUCT_COST_MODEL_VERSION
    notes: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


_PROFILES: dict[str, ProductCostProfile] = {p.product_class: p for p in (
    ProductCostProfile(
        SPOT_CASH, actual_borrowing_required=False,
        trading_fee_model=MAKER_TAKER,
        source="cash purchase of an owned asset; nothing extended"),
    ProductCostProfile(
        SPOT_MARGIN_LONG, actual_borrowing_required=True,
        financing_model=MARGIN_OPEN_PLUS_ROLLOVER,
        trading_fee_model=MAKER_TAKER,
        source="venue margin schedule (e.g. Kraken spot margin: opening "
               "fee + rollover); quote currency is actually extended",
        notes="the financed amount is the extension, not notional-minus-"
              "margin arithmetic on a derivative"),
    ProductCostProfile(
        SPOT_MARGIN_SHORT, actual_borrowing_required=True,
        financing_model=MARGIN_OPEN_PLUS_ROLLOVER,
        borrow_fee_model=STOCK_BORROW_ACCRUAL,
        trading_fee_model=MAKER_TAKER,
        source="venue margin schedule; the base asset is actually "
               "borrowed and sold"),
    ProductCostProfile(
        EQUITY_CASH, actual_borrowing_required=False,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        regulatory_fee_model="SEC_TAF_WHERE_APPLICABLE",
        source="cash equity purchase"),
    ProductCostProfile(
        EQUITY_MARGIN_LONG, actual_borrowing_required=True,
        financing_model=BROKER_MARGIN_INTEREST,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        regulatory_fee_model="SEC_TAF_WHERE_APPLICABLE",
        source="broker margin loan; cash is actually extended"),
    ProductCostProfile(
        EQUITY_SHORT_BORROW, actual_borrowing_required=True,
        borrow_fee_model=STOCK_BORROW_ACCRUAL,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        regulatory_fee_model="SEC_TAF_WHERE_APPLICABLE",
        source="real shares are located and borrowed; the lender's fee "
               "accrues over calendar days"),
    ProductCostProfile(
        CRYPTO_PERPETUAL, actual_borrowing_required=False,
        trading_fee_model=MAKER_TAKER,
        funding_model=PERPETUAL_FUNDING_TRANSFER,
        liquidation_fee_model="VENUE_LIQUIDATION_SCHEDULE",
        collateral_conversion_model="WHERE_VENUE_SPECIFIES",
        source="perpetual contract; exposure lives in the contract. "
               "Funding is a SIGNED TRANSFER (longs pay shorts or the "
               "reverse), not interest on a loan nobody made",
        notes="a 50x perpetual borrows nothing; a short perpetual borrows "
              "no coin"),
    ProductCostProfile(
        CRYPTO_FIXED_FUTURE, actual_borrowing_required=False,
        trading_fee_model=MAKER_TAKER,
        funding_model=NOT_APPLICABLE,
        settlement_fee_model="WHERE_CONTRACT_SPECIFIES",
        source="fixed-maturity contract: no perpetual funding unless the "
               "contract explicitly defines one"),
    ProductCostProfile(
        EQUITY_INDEX_FUTURE, actual_borrowing_required=False,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        exchange_fee_model=EXCHANGE_NFA_CLEARING,
        clearing_fee_model=EXCHANGE_NFA_CLEARING,
        regulatory_fee_model=EXCHANGE_NFA_CLEARING,
        source="listed future: commission + exchange + NFA + clearing. "
               "Initial margin is collateral, not a down payment on a "
               "loan; a short borrows no shares"),
    ProductCostProfile(
        COMMODITY_FUTURE, actual_borrowing_required=False,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        exchange_fee_model=EXCHANGE_NFA_CLEARING,
        clearing_fee_model=EXCHANGE_NFA_CLEARING,
        regulatory_fee_model=EXCHANGE_NFA_CLEARING,
        source="listed future; same shape as index futures"),
    ProductCostProfile(
        FX_FUTURE, actual_borrowing_required=False,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        exchange_fee_model=EXCHANGE_NFA_CLEARING,
        clearing_fee_model=EXCHANGE_NFA_CLEARING,
        regulatory_fee_model=EXCHANGE_NFA_CLEARING,
        source="listed FX future (note 6J=F itself remains UNSUPPORTED "
               "for execution until its contract spec is authoritative)"),
    ProductCostProfile(
        OPTION, actual_borrowing_required=False,
        trading_fee_model=COMMISSION_PER_CONTRACT,
        exchange_fee_model="WHERE_VENUE_SPECIFIES",
        source="premium instrument; no execution support wired"),
    ProductCostProfile(
        DEX_SPOT_SWAP, actual_borrowing_required=False,
        trading_fee_model=POOL_FEE,
        network_fee_model=NETWORK_GAS,
        source="AMM swap of owned assets: pool fee + network gas; price "
               "impact is the pool moving, and PRICE IMPACT IS NOT A FEE"),
)}

# Legacy JARVIS product strings -> taxonomy. Side matters for equities:
# the same "EQUITY_SHORT" product IS the borrow product.
_LEGACY_MAP = {
    "CRYPTO_SPOT": SPOT_CASH,
    "EQUITY_SPOT": EQUITY_CASH,
    "ETF_SPOT": EQUITY_CASH,
    "CRYPTO_PERP": CRYPTO_PERPETUAL,
    "EQUITY_SHORT": EQUITY_SHORT_BORROW,
    "INDEX_FUTURE": EQUITY_INDEX_FUTURE,
    "COMMODITY_FUTURE": COMMODITY_FUTURE,
    "FX_FUTURE": FX_FUTURE,
    "FX_SPOT": SPOT_CASH,
    "DEX_SPOT": DEX_SPOT_SWAP,
}


def profile_for(product: str, *, leverage: float | None = None,
                is_short: bool | None = None) -> ProductCostProfile:
    """Resolve a product to its cost surface.

    `leverage` is accepted ONLY so this function can refuse to use it: it
    is deliberately ignored in resolution, and the test suite pins that a
    50x and a 1x perpetual resolve to the identical profile. An unknown
    product resolves to an UNKNOWN profile that permits nothing — not to
    the nearest-looking one.
    """
    key = str(product or "").upper().strip()
    cls = key if key in _PROFILES else _LEGACY_MAP.get(key)
    if cls is None:
        return ProductCostProfile(
            product_class=UNKNOWN,
            actual_borrowing_required=UNKNOWN,
            trading_fee_model=UNKNOWN, source=f"unrecognised product "
            f"{product!r}; no cost may be charged and none waived on a "
            f"product nobody characterised")
    return _PROFILES[cls]


def borrowing_applies(product: str, *, is_short: bool = False) -> bool:
    """May a borrow/financing accrual be charged AT ALL for this product?

    False for every derivative regardless of side or leverage. UNKNOWN
    products return False — a fee with no evidenced basis is not charged,
    it is reported UNKNOWN by the carry authority.
    """
    prof = profile_for(product, is_short=is_short)
    return prof.actual_borrowing_required is True


def funding_applies(product: str) -> bool:
    """Is perpetual funding a real mechanism for this product?"""
    return profile_for(product).funding_model == PERPETUAL_FUNDING_TRANSFER
