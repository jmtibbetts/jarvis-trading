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


def is_futures(symbol: str) -> bool:
    return str(symbol or "").upper() in FUTURES_SPECS


def get_spec(symbol: str) -> ContractSpec:
    """Spec for any symbol. Equities and crypto fall back to unit specs so
    callers can use one code path for every asset class."""
    s = str(symbol or "").upper().strip()
    if s in FUTURES_SPECS:
        return FUTURES_SPECS[s]
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
