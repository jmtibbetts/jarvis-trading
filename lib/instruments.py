"""Contract specifications — what one unit of an instrument actually is.

Until this module existed, every book in the system assumed
`notional = qty * price`, which is true for shares and coins and WRONG for
every futures contract. A futures contract is

    notional = qty * price * MULTIPLIER

and the multipliers are large: 50 for E-mini S&P, 20 for E-mini Nasdaq,
1000 for Crude. So a 1-point ES move is $50 per contract while the paper
book recorded $1 — every futures trade in the learning data was wrong by
5x to 1000x. That matters more than it sounds: the win rates and R-multiples
being accumulated now are the training set for the EV engine, so bad data
today becomes bad decisions later.

Four things must be right for simulated fills to transfer to a real broker:

  multiplier      dollars per point per contract  -> P&L and notional
  tick_size       minimum price increment         -> a price off-tick cannot fill
  commission      charged PER CONTRACT, not as a % of notional
  initial_margin  set by the exchange in dollars, not derived from leverage

Micro contracts are listed as first-class instruments because they are the
correct size for a retail account: one E-mini S&P is ~$388k of exposure,
one Micro is ~$39k. CME created the micro complex for exactly this, and
every futures-capable broker (IBKR, Tradovate, AMP) carries them.

SOURCE AND CAVEAT: multipliers and tick sizes are CME/ICE contract
specifications and are stable. Margins move with volatility and are set by
the exchange and then raised at the broker's discretion — the values here
are typical day-margin figures for sizing sanity only. Before real money,
verify every line against the broker that will actually fill you.
"""
from __future__ import annotations

from dataclasses import dataclass


# ── Instrument identity (Phase 1 §6) ─────────────────────────────────────────
# One authority for "which instrument is this string", replacing three
# scattered implementations: jobs/execute_signals._both_formats,
# lib/alpaca_client._symbol_variants, and per-module slash heuristics. The
# LINK incident is why: Alpaca returns POSITIONS as "LINKUSD" and ORDERS as
# "LINK/USD", one module compared the wrong shape, and a protective
# stop-loss was nearly cancelled so a duplicate long could pyramid in.

def canonical(symbol: str | None) -> str:
    """The one spelling the rest of the system uses.

    crypto  BASE/QUOTE upper ("BTC/USD" — slash restored if a venue
            stripped it), routed through symbol_aliases first
    futures =F / =X / ^ formats upper, untouched
    equity  bare ticker upper
    """
    s = str(symbol or "").upper().strip()
    if not s:
        return s
    try:
        from lib.symbol_aliases import ALIASES
        s = ALIASES.get(s, s)
    except Exception:
        pass
    if s.endswith(("=F", "=X")) or s.startswith("^") or "/" in s:
        return s
    # Slashless crypto ("LINKUSD") -> slashed, but only when the base is a
    # known crypto asset — "SPCX" the equity must not become "SP/CX".
    if len(s) > 4 and s.endswith(("USD", "USDT", "USDC")):
        quote = "USDT" if s.endswith("USDT") else "USDC" if s.endswith("USDC") else "USD"
        base = s[: -len(quote)]
        try:
            from lib.crypto_market_data import is_crypto_symbol
            if base and is_crypto_symbol(base):
                return f"{base}/{quote}"
        except Exception:
            pass
    return s


def variants(symbol: str | None) -> set[str]:
    """Every spelling a venue might use for this instrument — for matching
    against broker state, which is inconsistent about the slash."""
    c = canonical(symbol)
    out = {c, str(symbol or "").upper().strip()}
    if "/" in c:
        out.add(c.replace("/", ""))
    elif len(c) > 3 and c.endswith("USD") and c[:-3].isalpha():
        out.add(f"{c[:-3]}/USD")
    out.discard("")
    return out


def is_stablecoin(symbol: str | None) -> bool:
    """True when the symbol's BASE has no USD price thesis to trade.

    A dollar-pegged asset's ATR is near zero BY DESIGN, so every
    volatility-derived stop collapses to something inside the spread.
    Measured on live candidates: DAI/USD produced a 0.0003% stop and a
    3,397R cost estimate, USDT/USD 72R — not thin trades, non-trades.

    Delegates to `lib.pegged_assets`, which is the one registry, because
    the answer is NOT a flat boolean over "stablecoins": PAXG and XAUT are
    pegged too, and they track gold, which is fully directional in USD.
    Base only — `BTC/USDT` is a bet on BTC and stays tradeable.
    """
    from lib.pegged_assets import is_non_directional
    return is_non_directional(canonical(symbol))


def asset_class_of(symbol: str | None) -> str:
    """Equity | Crypto | Futures | Forex — from the symbol's shape and the
    known-crypto registry, one rule for every module."""
    c = canonical(symbol)
    if not c:
        return "Equity"
    if c.endswith("=F") or c.startswith("^"):
        return "Futures"
    if c.endswith("=X"):
        return "Forex"
    if "/" in c:
        return "Crypto"
    return "Equity"


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    multiplier: float          # dollars per 1.0 of price movement, per contract
    tick_size: float           # minimum price increment
    commission: float          # dollars per contract, per side
    initial_margin: float      # dollars per contract (typical day margin)
    micro_of: str | None = None    # the full-size contract this is a micro of

    @property
    def tick_value(self) -> float:
        """Dollars per tick per contract."""
        return self.tick_size * self.multiplier


# ── Futures ──────────────────────────────────────────────────────────────
# Index, energy, metals, grains. Micros listed alongside their parents.
FUTURES_SPECS: dict[str, ContractSpec] = {
    # Index — E-mini
    "ES=F": ContractSpec("ES=F", "E-mini S&P 500", 50, 0.25, 2.25, 13_200),
    "NQ=F": ContractSpec("NQ=F", "E-mini Nasdaq 100", 20, 0.25, 2.25, 22_000),
    "YM=F": ContractSpec("YM=F", "E-mini Dow", 5, 1.0, 2.25, 9_900),
    "RTY=F": ContractSpec("RTY=F", "E-mini Russell 2000", 50, 0.10, 2.25, 7_700),
    # Index — Micro (1/10 size; the retail-appropriate tier)
    "MES=F": ContractSpec("MES=F", "Micro E-mini S&P 500", 5, 0.25, 0.52, 1_320, micro_of="ES=F"),
    "MNQ=F": ContractSpec("MNQ=F", "Micro E-mini Nasdaq", 2, 0.25, 0.52, 2_200, micro_of="NQ=F"),
    "MYM=F": ContractSpec("MYM=F", "Micro E-mini Dow", 0.5, 1.0, 0.52, 990, micro_of="YM=F"),
    "M2K=F": ContractSpec("M2K=F", "Micro E-mini Russell", 5, 0.10, 0.52, 770, micro_of="RTY=F"),
    # Energy
    "CL=F": ContractSpec("CL=F", "Crude Oil (WTI)", 1000, 0.01, 2.25, 6_600),
    "MCL=F": ContractSpec("MCL=F", "Micro Crude Oil", 100, 0.01, 0.52, 660, micro_of="CL=F"),
    "BZ=F": ContractSpec("BZ=F", "Brent Crude", 1000, 0.01, 2.25, 6_000),
    "NG=F": ContractSpec("NG=F", "Natural Gas", 10_000, 0.001, 2.25, 4_400),
    "RB=F": ContractSpec("RB=F", "RBOB Gasoline", 42_000, 0.0001, 2.25, 7_700),
    "HO=F": ContractSpec("HO=F", "Heating Oil", 42_000, 0.0001, 2.25, 7_700),
    # Metals
    "GC=F": ContractSpec("GC=F", "Gold", 100, 0.10, 2.25, 13_750),
    "MGC=F": ContractSpec("MGC=F", "Micro Gold", 10, 0.10, 0.52, 1_375, micro_of="GC=F"),
    "SI=F": ContractSpec("SI=F", "Silver", 5000, 0.005, 2.25, 16_500),
    "SIL=F": ContractSpec("SIL=F", "Micro Silver", 1000, 0.005, 0.52, 3_300, micro_of="SI=F"),
    "HG=F": ContractSpec("HG=F", "Copper", 25_000, 0.0005, 2.25, 6_600),
    "PL=F": ContractSpec("PL=F", "Platinum", 50, 0.10, 2.25, 3_300),
    "PA=F": ContractSpec("PA=F", "Palladium", 100, 0.50, 2.25, 12_000),
    # Grains
    "ZC=F": ContractSpec("ZC=F", "Corn", 50, 0.25, 2.25, 1_800),
    "ZW=F": ContractSpec("ZW=F", "Wheat", 50, 0.25, 2.25, 2_400),
    "ZS=F": ContractSpec("ZS=F", "Soybeans", 50, 0.25, 2.25, 3_300),
}

# Equities and crypto: one unit is one share/coin, no multiplier, and fees
# are charged as a percentage rather than per contract.
DEFAULT_EQUITY_SPEC = ContractSpec("EQUITY", "Equity share", 1.0, 0.01, 0.0, 0.0)
DEFAULT_CRYPTO_SPEC = ContractSpec("CRYPTO", "Crypto unit", 1.0, 0.0, 0.0, 0.0)
# FX trades in units of base currency at a 1.0 multiplier, but its tick is
# a pip — four decimals on most pairs, two on JPY crosses — and it is
# neither an equity nor a crypto unit.
DEFAULT_FX_SPEC = ContractSpec("FX", "FX unit", 1.0, 0.0001, 0.0, 0.0)


# ═════════════════════════════════════════════════════════════════════════
# CANONICAL INSTRUMENT IDENTITY
#
# BEFORE JARVIS CAN CALCULATE SIZE, NOTIONAL, MARGIN, P&L, FEES, RISK,
# LIQUIDATION OR LEARNING, IT MUST KNOW WHAT ONE UNIT OF THE INSTRUMENT
# ACTUALLY IS.
#
# Four concepts, resolved SEPARATELY, because collapsing them is how the
# arithmetic goes wrong:
#
#   ASSET CLASS   what market it belongs to
#   PRODUCT       what contract you actually hold. "Crypto" does not say
#                 spot vs perpetual vs DEX swap, and those have different
#                 margin, funding and liquidation mechanics.
#   QUANTITY UNIT what "qty = 3" means. Shares, coins, contracts, FX units
#                 and token units are not interchangeable.
#   EXECUTION SPEC the multiplier, tick and margin needed to simulate it.
#
# Two measured fail-opens this replaces:
#
#   get_spec("EUR/USD")   -> CRYPTO spec, because the string has a slash.
#   get_spec("UNKNOWN=F") -> EQUITY spec with multiplier 1, because the
#                            symbol was not in FUTURES_SPECS. A futures
#                            position sized on an equity multiplier is
#                            wrong by whatever the real multiplier is —
#                            50x for ES, 100x for gold.
#
# And EUR/USD vs EURUSD=X returned DIFFERENT answers for one instrument:
# Crypto+crypto-spec against Forex+equity-spec.
# ═════════════════════════════════════════════════════════════════════════

# Asset classes.
EQUITY = "EQUITY"
CRYPTO = "CRYPTO"
FUTURES = "FUTURES"
FOREX = "FOREX"

# Products. THE distinction asset_class cannot make.
EQUITY_SPOT = "EQUITY_SPOT"
EQUITY_SHORT = "EQUITY_SHORT"
ETF_SPOT = "ETF_SPOT"
INDEX_FUTURE = "INDEX_FUTURE"
COMMODITY_FUTURE = "COMMODITY_FUTURE"
FX_SPOT = "FX_SPOT"
CRYPTO_SPOT = "CRYPTO_SPOT"
CRYPTO_PERP = "CRYPTO_PERP"
DEX_SPOT = "DEX_SPOT"
PRODUCT_UNKNOWN = "UNKNOWN"

# Quantity units. What one unit of `qty` means.
SHARES = "SHARES"
COINS = "COINS"
CONTRACTS = "CONTRACTS"
FX_UNITS = "FX_UNITS"
TOKEN_UNITS = "TOKEN_UNITS"
UNIT_UNKNOWN = "UNKNOWN"

# Resolution status.
VERIFIED = "VERIFIED"            # execution spec confirmed
SUPPORTED = "SUPPORTED"          # simulatable on documented defaults
RESEARCH_ONLY = "RESEARCH_ONLY"  # observable, not executable
AMBIGUOUS = "AMBIGUOUS"          # more than one reading; needs a venue
UNSUPPORTED = "UNSUPPORTED"      # recognised, cannot be executed
STATUS_UNKNOWN = "UNKNOWN"

# FX majors and common crosses, with pip size. A JPY pair pips at the 2nd
# decimal and everything else at the 4th — pricing a JPY pair on a 0.0001
# pip overstates every FX cost by 100x.
FX_PAIRS: dict[str, float] = {
    "EUR/USD": 0.0001, "GBP/USD": 0.0001, "AUD/USD": 0.0001,
    "NZD/USD": 0.0001, "USD/CAD": 0.0001, "USD/CHF": 0.0001,
    "USD/JPY": 0.01,   "EUR/JPY": 0.01,   "GBP/JPY": 0.01,
    "AUD/JPY": 0.01,   "CHF/JPY": 0.01,   "CAD/JPY": 0.01,
    "EUR/GBP": 0.0001, "EUR/CHF": 0.0001, "EUR/AUD": 0.0001,
    "GBP/CHF": 0.0001, "AUD/NZD": 0.0001, "EUR/CAD": 0.0001,
}

# Standard FX lot sizes, in base-currency units.
FX_STANDARD_LOT = 100_000.0
FX_MINI_LOT = 10_000.0
FX_MICRO_LOT = 1_000.0

# Fiat currencies, so `EUR/USD` is not mistaken for a token pair.
_FIAT = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
                   "SEK", "NOK", "DKK", "SGD", "HKD", "MXN", "ZAR", "PLN",
                   "TRY", "CNH", "CNY"})

# Which futures roots are index rather than commodity. Both are FUTURES and
# both use CONTRACTS — the split exists for grouping and reporting, never
# for arithmetic.
_INDEX_ROOTS = frozenset({"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY",
                          "M2K", "VX", "ZB", "ZN", "ZF", "ZT"})


class UnsupportedInstrument(ValueError):
    """Raised when execution is attempted on an instrument whose units are
    unknown. Research may continue; simulation may not."""


@dataclass(frozen=True)
class InstrumentIdentity:
    instrument_id: str
    canonical_symbol: str
    display_symbol: str

    asset_class: str
    product: str
    quantity_unit: str

    base_asset: str | None = None
    quote_asset: str | None = None
    account_currency: str = "USD"

    venue_family: str | None = None

    multiplier: float = 1.0
    tick_size: float | None = None
    tick_value: float | None = None
    quantity_step: float | None = None
    minimum_quantity: float | None = None
    contract_size: float | None = None

    pip_size: float | None = None
    pip_value_rule: str | None = None

    initial_margin: float | None = None
    maintenance_margin: float | None = None

    expiry: str | None = None
    research_symbol: str | None = None
    executable_symbol: str | None = None

    status: str = STATUS_UNKNOWN
    reason: str | None = None
    provenance: str = "resolved"

    @property
    def executable(self) -> bool:
        """Whether a virtual venue may simulate this. RESEARCH_ONLY and
        UNSUPPORTED are observable but not tradeable."""
        return self.status in (VERIFIED, SUPPORTED)

    def require_executable(self) -> "InstrumentIdentity":
        if not self.executable:
            raise UnsupportedInstrument(
                f"{self.display_symbol}: {self.status}"
                f"{f' — {self.reason}' if self.reason else ''}")
        return self

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return {**asdict(self), "executable": self.executable}


def _split_pair(c: str) -> tuple[str | None, str | None]:
    if "/" in c:
        a, _, b = c.partition("/")
        return (a or None), (b or None)
    return None, None


def fx_canonical(symbol: str | None) -> str | None:
    """Any FX spelling -> `EUR/USD` form, or None if it is not FX.

    SYMBOL IS NOT IDENTITY. `EUR/USD`, `EURUSD=X` and `EURUSD` are three
    provider spellings of ONE instrument, and they used to resolve to three
    different answers — Crypto with a crypto spec, Forex with an equity
    spec, and equity respectively.
    """
    if not symbol:
        return None
    s = str(symbol).upper().strip()
    if s.endswith("=X"):
        s = s[:-2]
    if "/" in s:
        base, quote = _split_pair(s)
        if base in _FIAT and quote in _FIAT:
            return f"{base}/{quote}"
        return None
    if len(s) == 6 and s[:3] in _FIAT and s[3:] in _FIAT:
        return f"{s[:3]}/{s[3:]}"
    return None


def resolve(symbol: str | None, *, venue: str | None = None,
            product: str | None = None) -> InstrumentIdentity:
    """THE instrument authority. Never guesses a unit it does not know.

    `product` lets a caller state what it is actually trading when the
    symbol cannot: BTC/USD is spot on one venue and a perpetual on another,
    and leverage does NOT decide that — a 1x perpetual is still a
    perpetual, with funding and a liquidation price a spot position does
    not have.
    """
    raw = str(symbol or "").strip()
    c = canonical(raw)
    if not c:
        return InstrumentIdentity(
            instrument_id="", canonical_symbol="", display_symbol=raw,
            asset_class=EQUITY, product=PRODUCT_UNKNOWN,
            quantity_unit=UNIT_UNKNOWN, status=STATUS_UNKNOWN,
            reason="empty symbol")

    # ── FX, checked BEFORE the slash heuristic ───────────────────────────
    fx = fx_canonical(c)
    if fx:
        base, quote = _split_pair(fx)
        pip = FX_PAIRS.get(fx, 0.01 if quote == "JPY" else 0.0001)
        known = fx in FX_PAIRS
        return InstrumentIdentity(
            instrument_id=f"fx:{fx}", canonical_symbol=fx, display_symbol=fx,
            asset_class=FOREX, product=FX_SPOT, quantity_unit=FX_UNITS,
            base_asset=base, quote_asset=quote, account_currency="USD",
            venue_family=venue or "fx_spot",
            multiplier=1.0, pip_size=pip,
            pip_value_rule="pip_size * units, converted to account currency",
            quantity_step=FX_MICRO_LOT, minimum_quantity=FX_MICRO_LOT,
            contract_size=FX_STANDARD_LOT,
            research_symbol=f"{base}{quote}=X", executable_symbol=fx,
            status=VERIFIED if known else SUPPORTED,
            reason=None if known else "pip size inferred from quote currency",
            provenance="fx_registry")

    # ── Futures ──────────────────────────────────────────────────────────
    if c.endswith("=F") or c.startswith("^"):
        spec = FUTURES_SPECS.get(c)
        if spec is None:
            # NO FAIL-OPEN. This used to return an equity spec with
            # multiplier 1, so a futures position was sized as though one
            # contract moved one dollar per point — wrong by 50x on ES and
            # 100x on gold.
            return InstrumentIdentity(
                instrument_id=f"fut:{c}", canonical_symbol=c, display_symbol=c,
                asset_class=FUTURES, product=PRODUCT_UNKNOWN,
                quantity_unit=CONTRACTS, research_symbol=c,
                status=UNSUPPORTED, reason="MISSING_CONTRACT_SPEC",
                provenance="shape_only")
        root = c[:-2] if c.endswith("=F") else c.lstrip("^")
        is_index = root in _INDEX_ROOTS
        return InstrumentIdentity(
            instrument_id=f"fut:{c}", canonical_symbol=c, display_symbol=c,
            asset_class=FUTURES,
            product=INDEX_FUTURE if is_index else COMMODITY_FUTURE,
            quantity_unit=CONTRACTS, quote_asset="USD",
            venue_family=venue or "cme",
            multiplier=spec.multiplier, tick_size=spec.tick_size,
            tick_value=spec.tick_value, quantity_step=1.0,
            minimum_quantity=1.0, contract_size=spec.multiplier,
            initial_margin=spec.initial_margin,
            # A CONTINUOUS series is a research proxy, not an eternal
            # contract. Saying so keeps the simulation honest about what it
            # is standing in for.
            research_symbol=c, executable_symbol=None,
            status=VERIFIED, reason="continuous series used as research proxy",
            provenance="futures_specs")

    # ── Crypto ───────────────────────────────────────────────────────────
    base, quote = _split_pair(c)
    looks_crypto = ("/" in c and base and quote) or (
        len(c) > 3 and c.endswith("USD") and c[:-3].isalpha())
    if looks_crypto:
        if base is None:
            base, quote = c[:-3], "USD"
        # A slash alone proves nothing — that is how EUR/USD became crypto.
        # It is crypto when at least one leg is NOT fiat.
        if base in _FIAT and quote in _FIAT:
            return InstrumentIdentity(
                instrument_id=f"amb:{c}", canonical_symbol=c, display_symbol=c,
                asset_class=FOREX, product=PRODUCT_UNKNOWN,
                quantity_unit=UNIT_UNKNOWN, base_asset=base, quote_asset=quote,
                status=AMBIGUOUS, reason="fiat pair outside the FX registry",
                provenance="shape_only")
        prod = product or (DEX_SPOT if (venue or "").lower() == "dex"
                           else CRYPTO_SPOT)
        unit = TOKEN_UNITS if prod == DEX_SPOT else (
            CONTRACTS if prod == CRYPTO_PERP else COINS)
        return InstrumentIdentity(
            instrument_id=f"crypto:{base}/{quote}",
            canonical_symbol=f"{base}/{quote}", display_symbol=f"{base}/{quote}",
            asset_class=CRYPTO, product=prod, quantity_unit=unit,
            base_asset=base, quote_asset=quote,
            venue_family=venue or ("dex" if prod == DEX_SPOT else "cex"),
            multiplier=1.0, quantity_step=None, minimum_quantity=None,
            executable_symbol=f"{base}/{quote}",
            status=SUPPORTED, provenance="pair_shape")

    # ── Equity / ETF ─────────────────────────────────────────────────────
    if c.isalpha() or (c.replace(".", "").replace("-", "").isalnum()):
        return InstrumentIdentity(
            instrument_id=f"eq:{c}", canonical_symbol=c, display_symbol=c,
            asset_class=EQUITY,
            product=product or EQUITY_SPOT, quantity_unit=SHARES,
            base_asset=c, quote_asset="USD", venue_family=venue or "us_equity",
            multiplier=1.0, tick_size=0.01, quantity_step=1.0,
            minimum_quantity=1.0, executable_symbol=c,
            status=SUPPORTED, provenance="equity_default")

    return InstrumentIdentity(
        instrument_id=f"unk:{c}", canonical_symbol=c, display_symbol=raw,
        asset_class=EQUITY, product=PRODUCT_UNKNOWN,
        quantity_unit=UNIT_UNKNOWN, status=STATUS_UNKNOWN,
        reason="symbol matched no known instrument shape",
        provenance="unresolved")


def is_futures(symbol: str) -> bool:
    return str(symbol or "").upper() in FUTURES_SPECS


def get_spec(symbol: str) -> ContractSpec:
    """Spec for any symbol. Equities and crypto fall back to unit specs so
    callers can use one code path for every asset class."""
    s = str(symbol or "").upper().strip()
    if s in FUTURES_SPECS:
        return FUTURES_SPECS[s]
    # An unrecognised FUTURE must not borrow an equity multiplier of 1. A
    # contract sized that way is wrong by whatever the real multiplier is —
    # 50x on ES, 100x on gold — and nothing downstream could tell.
    if s.endswith("=F"):
        raise UnsupportedInstrument(
            f"{s}: MISSING_CONTRACT_SPEC — futures cannot be sized without a "
            f"verified multiplier and tick. Research may continue; execution "
            f"may not.")
    # FX is not crypto. `"/" in s` sent EUR/USD down the crypto path, where
    # a 0.0001 pip instrument was priced with crypto fee and spread
    # assumptions.
    if fx_canonical(s):
        return DEFAULT_FX_SPEC
    if "/" in s or s.endswith("USD"):
        return DEFAULT_CRYPTO_SPEC
    return DEFAULT_EQUITY_SPEC


def contract_notional(symbol: str, price: float, qty: float = 1.0) -> float:
    """True dollar exposure. This is the number that was wrong everywhere."""
    return float(price) * float(qty) * get_spec(symbol).multiplier


def snap_to_tick(symbol: str, price: float, direction: str = "nearest") -> float:
    """Round a price to a valid increment for the instrument.

    A stop at 7766.83 on ES cannot exist — the contract trades in 0.25
    increments. Simulating a fill at an impossible price makes backtest and
    paper results unreachable in live trading, so levels are snapped here.
    `direction` may be "nearest", "up", or "down" so a stop can always be
    moved to the SAFER side rather than the closer one.
    """
    import math
    spec = get_spec(symbol)
    tick = spec.tick_size
    if not tick or tick <= 0 or not price:
        return float(price)
    ratio = float(price) / tick
    if direction == "up":
        stepped = math.ceil(ratio)
    elif direction == "down":
        stepped = math.floor(ratio)
    else:
        stepped = round(ratio)
    # Re-round to kill binary float dust (0.1+0.2 problems at tick scale).
    decimals = max(0, -int(math.floor(math.log10(tick)))) + 2
    return round(stepped * tick, decimals)


def whole_contracts(symbol: str, qty: float) -> float:
    """Futures trade in whole contracts; a 0.37-contract position cannot be
    filled anywhere. Equities round down to whole shares, crypto stays
    fractional."""
    spec = get_spec(symbol)
    if is_futures(symbol):
        return float(int(abs(qty))) * (1 if qty >= 0 else -1)
    if spec is DEFAULT_CRYPTO_SPEC:
        return float(qty)
    return float(int(abs(qty))) * (1 if qty >= 0 else -1)


def commission_for(symbol: str, qty: float, notional: float = 0.0,
                   pct_fee: float = 0.0) -> float:
    """Round-trip commission in dollars.

    Futures charge PER CONTRACT — a flat $2.25 whether the contract is worth
    $30k or $600k — so applying a percentage-of-notional fee (as the generic
    cost model does) overstates futures costs by orders of magnitude.
    """
    spec = get_spec(symbol)
    if is_futures(symbol):
        return abs(float(qty)) * spec.commission * 2.0     # in and out
    return abs(float(notional)) * float(pct_fee) * 2.0


def margin_required(symbol: str, qty: float, price: float = 0.0,
                    leverage: float = 1.0) -> float:
    """Capital tied up by the position.

    Futures margin is a fixed dollar amount per contract set by the
    exchange — it is NOT notional/leverage, which is how everything else in
    this system computes it.
    """
    spec = get_spec(symbol)
    if is_futures(symbol):
        return abs(float(qty)) * spec.initial_margin
    return abs(contract_notional(symbol, price, qty)) / max(1.0, float(leverage))


def max_affordable_contracts(symbol: str, free_capital: float,
                             max_pct_of_capital: float = 100.0) -> int:
    """How many contracts the account can actually margin."""
    spec = get_spec(symbol)
    if not is_futures(symbol) or spec.initial_margin <= 0:
        return 0
    budget = float(free_capital) * (float(max_pct_of_capital) / 100.0)
    return int(budget // spec.initial_margin)


def suggest_micro(symbol: str) -> str | None:
    """The micro equivalent of a full-size contract, when one exists."""
    s = str(symbol or "").upper()
    for sym, spec in FUTURES_SPECS.items():
        if spec.micro_of == s:
            return sym
    return None


# ── EXACT EXECUTION IDENTITY ─────────────────────────────────────────────
#
# `resolve()` answers "what instrument does this symbol usually mean?" and for
# a bare crypto pair that answer is SPOT in COINS. That is the right default
# for a watchlist and the wrong one for an execution whose product was already
# frozen at T0.
#
# MEASURED CONSEQUENCE. A canonical plan frozen as
# BTC/USD / CRYPTO_PERP / kraken_derivatives_us / PBTCUCZ50 reached
# `virtual_orders.execute_market()` with no instrument, so execution
# re-resolved it to crypto:BTC/USD, CRYPTO_SPOT, COINS, multiplier 1.0 — while
# the position's own provenance recorded PBTCUCZ50. One order, two
# incompatible descriptions of what a unit of quantity means.
#
# It is easy to miss because BTC's generic multiplier is 1.0 and the fill
# price still looks sane. It is not harmless: PBTCUCZ50 has a contract size of
# 0.01 BTC, so "1 unit" means 1 BTC under one reading and 0.01 BTC under the
# other — a hundredfold difference in exposure, fees and P&L.
#
# This function is the exact-execution answer. It is NOT RoutingIdentity:
# routing says WHICH product was chosen, this says what ONE executable unit
# of that product actually is.


class ExecutionIdentityRefused(ValueError):
    """The frozen identity and the venue spec do not describe one instrument."""


def resolve_for_execution(symbol: str, *, product: str,
                          venue: str | None = None,
                          instrument_id: str | None = None
                          ) -> InstrumentIdentity:
    """The instrument an EXECUTION will actually transact, or a refusal.

    Refuses rather than guesses when the caller's frozen contract disagrees
    with what the venue spec resolves — silently preferring either side is how
    a position ends up settled in units nobody authorized.
    """
    from lib import product_router as PR

    if product == PR.CRYPTO_PERP:
        from lib import bitnomial_products as BP
        spec = BP.resolve(symbol)
        if not spec.ok:
            raise ExecutionIdentityRefused(
                f"{symbol} has no executable perpetual identity: "
                f"{spec.reason}: {spec.detail}")
        if instrument_id and instrument_id != spec.symbol:
            raise ExecutionIdentityRefused(
                f"frozen contract {instrument_id!r} but the venue spec "
                f"resolves {symbol} to {spec.symbol!r}; refusing rather than "
                f"choosing one")
        return InstrumentIdentity(
            instrument_id=spec.symbol,
            canonical_symbol=symbol.upper().strip(),
            display_symbol=symbol.upper().strip(),
            asset_class="crypto",
            product=PR.CRYPTO_PERP,
            # CONTRACTS, not COINS. The whole point of this function.
            quantity_unit="CONTRACTS",
            base_asset=spec.contract_size_unit,
            quote_asset="USD",
            venue_family=spec.venue or venue,
            multiplier=float(spec.contract_size),
            contract_size=float(spec.contract_size),
            tick_size=float(spec.price_increment),
            # Perpetual contracts are indivisible.
            quantity_step=1.0,
            minimum_quantity=1.0,
        )

    # Every other product keeps the existing generic behaviour, so no legacy
    # caller changes shape because perpetuals became exact.
    ident = resolve(symbol)
    if instrument_id and ident.instrument_id != instrument_id:
        raise ExecutionIdentityRefused(
            f"frozen instrument {instrument_id!r} but {symbol} resolves to "
            f"{ident.instrument_id!r}")
    return ident
