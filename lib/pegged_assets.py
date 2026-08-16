"""The pegged-asset registry — what a token is pegged TO, and how well.

`stablecoin = true` is the wrong shape and it fails in both directions.

Failing closed: XAUT and PAXG are pegged, so a boolean sweeps them into
"not a directional instrument" — but they track GOLD, which moves several
percent in USD in a week. This desk's paper book has been 76% XAUT. A flat
flag would have deleted a live position class.

Failing open: USDC and sUSDe both answer True to "is it a stablecoin", and
they are not the same risk. USDC is a redemption claim on cash and
T-bills; sUSDe is a yield-bearing wrapper over a delta-hedged basis trade,
which is not targeting $1.00 at all and can break for reasons a fiat
reserve cannot. Modelling them identically is how a stress engine reports
safety it has not established.

So the registry answers three questions separately:

    peg_currency    USD, EUR, XAU — what the price is supposed to track
    tier            the MECHANISM that holds the peg, and so how it breaks
    yield_bearing   whether the unit is designed to appreciate past par

Only Tier 1-6 USD-pegged assets are non-directional. Gold tokens are in the
registry so the liquidation engine can shock them, NOT so the scanner can
refuse them.

STATIC vs MEASURED. Everything here is a structural property that does not
change tick to tick — issuer, mechanism, peg target, collateral type. The
measured fields an obligation-level risk model also wants (current_price,
peg_deviation, liquidity_depth, market_cap, historical_max_depeg) are
deliberately NOT hardcoded here; a hardcoded price is a stale price. They
belong on the live snapshot, and `depeg_profile()` exposes the assumption
each tier carries until a measurement replaces it.
"""
from __future__ import annotations

# ── Tiers ────────────────────────────────────────────────────────────────
# Ordered by how the peg is held, because that is what determines how it
# fails. The tier drives the default depeg assumption in the stress engine.
TIER_FIAT_RESERVE = 1        # cash + T-bills, redeemable at par
TIER_OVERCOLLATERALIZED = 2  # crypto collateral above 100%, on-chain liquidations
TIER_SYNTHETIC = 3           # delta-neutral basis trade; breaks on funding/venue
TIER_YIELD_WRAPPER = 4       # a claim on tier 1-3 that ACCRUES; par is not the target
TIER_BRIDGED = 5             # backing lives on another chain; adds bridge risk
TIER_ALGORITHMIC = 6         # hybrid/algorithmic support
TIER_DISTRESSED = 7          # materially depegged, unwinding, or illiquid

TIER_NAMES = {
    TIER_FIAT_RESERVE: "fiat/reserve-backed",
    TIER_OVERCOLLATERALIZED: "overcollateralized DeFi",
    TIER_SYNTHETIC: "synthetic/delta-neutral",
    TIER_YIELD_WRAPPER: "yield-bearing wrapper",
    TIER_BRIDGED: "bridged",
    TIER_ALGORITHMIC: "algorithmic/hybrid",
    TIER_DISTRESSED: "deprecated/distressed",
}


class PeggedAsset:
    """One pegged asset's structural identity.

    `symbol` is the display ticker. Identity for on-chain work must still be
    the MINT — see the BSOL ticker collision that this repo already fixed
    once — so `mints` carries the Solana mints where they are known and the
    liquidation engine prefers them over the ticker.
    """

    def __init__(self, symbol: str, name: str, peg_currency: str, tier: int,
                 collateral_type: str, *, yield_bearing: bool = False,
                 yield_source: str | None = None, issuer: str | None = None,
                 native_chain: str | None = None, mints: tuple[str, ...] = (),
                 note: str = ""):
        self.symbol = symbol
        self.name = name
        self.peg_currency = peg_currency
        self.tier = tier
        self.collateral_type = collateral_type
        self.yield_bearing = yield_bearing
        self.yield_source = yield_source
        self.issuer = issuer
        self.native_chain = native_chain
        self.mints = mints
        self.note = note

    @property
    def is_usd_pegged(self) -> bool:
        return self.peg_currency == "USD"

    @property
    def targets_par(self) -> bool:
        """True when the unit is supposed to be worth exactly 1.0 of its peg
        currency. Yield wrappers are NOT — sDAI is worth more than a dollar
        by design, and treating its excess as a depeg would be backwards."""
        return not self.yield_bearing

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "name": self.name,
            "peg_currency": self.peg_currency,
            "tier": self.tier, "tier_name": TIER_NAMES.get(self.tier, "unknown"),
            "collateral_type": self.collateral_type,
            "yield_bearing": self.yield_bearing, "yield_source": self.yield_source,
            "issuer": self.issuer, "native_chain": self.native_chain,
            "mints": list(self.mints), "targets_par": self.targets_par,
            "note": self.note,
        }


def _a(*args, **kw) -> PeggedAsset:
    return PeggedAsset(*args, **kw)


# ── Tier 1 — fiat / reserve-backed ───────────────────────────────────────
_T1 = [
    _a("USDT", "Tether", "USD", TIER_FIAT_RESERVE, "cash+treasuries+other",
       issuer="Tether", native_chain="multi",
       mints=("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",),
       note="largest float; reserve composition historically less transparent than peers"),
    _a("USDC", "USD Coin", "USD", TIER_FIAT_RESERVE, "cash+treasuries",
       issuer="Circle", native_chain="multi",
       mints=("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",)),
    _a("PYUSD", "PayPal USD", "USD", TIER_FIAT_RESERVE, "cash+treasuries",
       issuer="Paxos", native_chain="multi",
       mints=("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",)),
    _a("USDG", "Global Dollar", "USD", TIER_FIAT_RESERVE, "cash+treasuries",
       issuer="Paxos/Global Dollar Network", native_chain="multi"),
    _a("RLUSD", "Ripple USD", "USD", TIER_FIAT_RESERVE, "cash+treasuries",
       issuer="Ripple", native_chain="multi"),
    _a("FDUSD", "First Digital USD", "USD", TIER_FIAT_RESERVE, "cash+treasuries",
       issuer="First Digital", native_chain="multi"),
    _a("USDP", "Pax Dollar", "USD", TIER_FIAT_RESERVE, "cash+treasuries", issuer="Paxos"),
    _a("GUSD", "Gemini Dollar", "USD", TIER_FIAT_RESERVE, "cash+treasuries", issuer="Gemini"),
    _a("TUSD", "TrueUSD", "USD", TIER_FIAT_RESERVE, "cash", issuer="Techteryx"),
    _a("USD1", "World Liberty Financial USD", "USD", TIER_FIAT_RESERVE, "cash+treasuries",
       issuer="World Liberty Financial"),
]

# ── Tier 2 — overcollateralized DeFi ─────────────────────────────────────
_T2 = [
    _a("DAI", "Dai", "USD", TIER_OVERCOLLATERALIZED, "crypto+RWA", issuer="Sky (MakerDAO)"),
    _a("USDS", "Sky Dollar", "USD", TIER_OVERCOLLATERALIZED, "crypto+RWA", issuer="Sky"),
    _a("GHO", "Aave GHO", "USD", TIER_OVERCOLLATERALIZED, "crypto", issuer="Aave"),
    _a("CRVUSD", "Curve USD", "USD", TIER_OVERCOLLATERALIZED, "crypto", issuer="Curve"),
    _a("LUSD", "Liquity USD", "USD", TIER_OVERCOLLATERALIZED, "ETH", issuer="Liquity"),
    _a("MKUSD", "Prisma mkUSD", "USD", TIER_OVERCOLLATERALIZED, "LST", issuer="Prisma"),
    _a("DOLA", "Inverse DOLA", "USD", TIER_OVERCOLLATERALIZED, "crypto", issuer="Inverse Finance"),
    _a("MIM", "Magic Internet Money", "USD", TIER_OVERCOLLATERALIZED, "crypto",
       issuer="Abracadabra"),
    _a("ALUSD", "Alchemix USD", "USD", TIER_OVERCOLLATERALIZED, "crypto", issuer="Alchemix"),
    _a("SUSD", "Synthetix USD", "USD", TIER_OVERCOLLATERALIZED, "SNX", issuer="Synthetix"),
    _a("USDA", "Angle USDA", "USD", TIER_OVERCOLLATERALIZED, "crypto+RWA", issuer="Angle"),
    _a("UXD", "UXD Protocol", "USD", TIER_OVERCOLLATERALIZED, "crypto", native_chain="solana"),
    _a("USDH", "Hubble USDH", "USD", TIER_OVERCOLLATERALIZED, "crypto", native_chain="solana"),
    _a("PAI", "Parrot USD", "USD", TIER_OVERCOLLATERALIZED, "crypto", native_chain="solana"),
    _a("USX", "dForce USX", "USD", TIER_OVERCOLLATERALIZED, "crypto"),
]

# ── Tier 3 — synthetic / delta-neutral ───────────────────────────────────
# Held by a hedged position, not a reserve. Breaks on funding inversion,
# venue failure or collateral stress — none of which look like a bank run.
_T3 = [
    _a("USDE", "Ethena USDe", "USD", TIER_SYNTHETIC, "delta-hedged-perp", issuer="Ethena",
       note="peg held by a short perp hedge; sustained negative funding is the stress case"),
    _a("USR", "Resolv USR", "USD", TIER_SYNTHETIC, "delta-hedged-perp", issuer="Resolv"),
    _a("FXUSD", "f(x) fxUSD", "USD", TIER_SYNTHETIC, "structured", issuer="f(x) Protocol"),
]

# ── Tier 4 — yield-bearing wrappers ──────────────────────────────────────
# These do NOT target par. Their price rises against the peg by design, so
# "deviation from 1.00" is meaningless for them and `targets_par` is False.
_T4 = [
    _a("SUSDE", "Staked USDe", "USD", TIER_YIELD_WRAPPER, "staked-USDe",
       yield_bearing=True, yield_source="perp funding + staking", issuer="Ethena"),
    _a("SUSDS", "Savings USDS", "USD", TIER_YIELD_WRAPPER, "staked-USDS",
       yield_bearing=True, yield_source="Sky savings rate", issuer="Sky"),
    _a("SDAI", "Savings DAI", "USD", TIER_YIELD_WRAPPER, "staked-DAI",
       yield_bearing=True, yield_source="DAI savings rate", issuer="Sky"),
    _a("USDY", "Ondo USDY", "USD", TIER_YIELD_WRAPPER, "treasuries",
       yield_bearing=True, yield_source="short-term treasuries", issuer="Ondo",
       native_chain="solana"),
    _a("OUSG", "Ondo Short-Term US Govt", "USD", TIER_YIELD_WRAPPER, "treasuries",
       yield_bearing=True, yield_source="short-term treasuries", issuer="Ondo"),
    _a("USDM", "Mountain USDM", "USD", TIER_YIELD_WRAPPER, "treasuries",
       yield_bearing=True, yield_source="short-term treasuries", issuer="Mountain Protocol"),
    _a("USYC", "Hashnote USYC", "USD", TIER_YIELD_WRAPPER, "treasuries+repo",
       yield_bearing=True, yield_source="treasuries/repo", issuer="Hashnote/Circle"),
    _a("FRXUSD", "Frax USD", "USD", TIER_YIELD_WRAPPER, "treasuries", issuer="Frax"),
    _a("SFRXUSD", "Staked frxUSD", "USD", TIER_YIELD_WRAPPER, "staked-frxUSD",
       yield_bearing=True, yield_source="Frax yield", issuer="Frax"),
    _a("USD0", "Usual USD0", "USD", TIER_YIELD_WRAPPER, "treasuries", issuer="Usual"),
    _a("USD0++", "Usual USD0++", "USD", TIER_YIELD_WRAPPER, "locked-USD0",
       yield_bearing=True, yield_source="Usual emissions", issuer="Usual",
       note="LOCKED to 2028 with a floor-price redemption; traded well below par in "
            "Jan 2025 when the floor was announced. Not a par instrument."),
    _a("WSTUSR", "Wrapped Staked USR", "USD", TIER_YIELD_WRAPPER, "staked-USR",
       yield_bearing=True, yield_source="Resolv yield", issuer="Resolv"),
]

# ── Tier 6 — algorithmic / hybrid ────────────────────────────────────────
_T6 = [
    _a("FRAX", "Frax", "USD", TIER_ALGORITHMIC, "hybrid", issuer="Frax"),
    _a("USDD", "USDD", "USD", TIER_ALGORITHMIC, "crypto+algorithmic", issuer="Tron DAO"),
]

# ── Non-USD pegs ─────────────────────────────────────────────────────────
# In the registry so the liquidation engine can shock them correctly. They
# are NOT dollar-stable and a USD-denominated book carries real FX risk on
# them — which is exactly why they must not share USD's depeg assumption.
_FX = [
    _a("EURC", "Euro Coin", "EUR", TIER_FIAT_RESERVE, "cash+treasuries", issuer="Circle"),
    _a("EURCV", "Societe Generale EUR", "EUR", TIER_FIAT_RESERVE, "cash", issuer="SG-Forge"),
    _a("EURS", "STASIS EURO", "EUR", TIER_FIAT_RESERVE, "cash", issuer="STASIS"),
    _a("EURE", "Monerium EUR", "EUR", TIER_FIAT_RESERVE, "cash", issuer="Monerium"),
    _a("EURT", "Tether EURt", "EUR", TIER_FIAT_RESERVE, "cash", issuer="Tether"),
    _a("EURA", "Angle EURA", "EUR", TIER_OVERCOLLATERALIZED, "crypto+RWA", issuer="Angle"),
    _a("AGEUR", "Angle agEUR", "EUR", TIER_OVERCOLLATERALIZED, "crypto+RWA", issuer="Angle"),
    _a("GBPT", "Poundtoken", "GBP", TIER_FIAT_RESERVE, "cash", issuer="Poundtoken"),
    _a("XSGD", "XSGD", "SGD", TIER_FIAT_RESERVE, "cash", issuer="StraitsX"),
    _a("GYEN", "GMO JPY", "JPY", TIER_FIAT_RESERVE, "cash", issuer="GMO Trust"),
    _a("BRZ", "Brazilian Digital Token", "BRL", TIER_FIAT_RESERVE, "cash", issuer="Transfero"),
    _a("TRYB", "BiLira", "TRY", TIER_FIAT_RESERVE, "cash", issuer="BiLira"),
]

# ── Commodity pegs ───────────────────────────────────────────────────────
# Pegged, and fully directional in USD. Gold moved 27% in 2024. These are
# here so the stress engine knows their price is a CLAIM ON GOLD, and they
# are deliberately excluded from `is_non_directional`.
_COMMODITY = [
    _a("PAXG", "Pax Gold", "XAU", TIER_FIAT_RESERVE, "allocated-gold", issuer="Paxos",
       note="one fine troy ounce of allocated London Good Delivery gold"),
    _a("XAUT", "Tether Gold", "XAU", TIER_FIAT_RESERVE, "allocated-gold", issuer="Tether",
       note="tracks gold, NOT the dollar — moves several percent a week and is "
            "traded directionally on this desk"),
]

REGISTRY: dict[str, PeggedAsset] = {
    a.symbol.upper(): a for a in (_T1 + _T2 + _T3 + _T4 + _T6 + _FX + _COMMODITY)
}

# Mint -> asset, for the on-chain path where the ticker cannot be trusted.
BY_MINT: dict[str, PeggedAsset] = {
    m: a for a in REGISTRY.values() for m in a.mints
}


# ── Lookup ───────────────────────────────────────────────────────────────

def _base_of(symbol: str | None) -> str:
    """The BASE ticker. `USDT/USD` is a bet on a peg; `BTC/USDT` is a bet on
    BTC. Only the base decides what the instrument is."""
    s = str(symbol or "").upper().strip()
    if not s:
        return ""
    for sep in ("/", "-", "_"):
        if sep in s:
            s = s.split(sep)[0]
            break
    # Yahoo/venue decorations, and the wrapped prefix (wUSDC is USDC).
    s = s.replace("=X", "").replace("=F", "").lstrip("^").strip()
    if len(s) > 4 and s.startswith("W") and s[1:] in REGISTRY:
        return s[1:]
    return s


def lookup(symbol: str | None = None, *, mint: str | None = None) -> PeggedAsset | None:
    """The registry entry, by mint when available and ticker otherwise."""
    if mint and mint in BY_MINT:
        return BY_MINT[mint]
    return REGISTRY.get(_base_of(symbol))


def is_pegged(symbol: str | None = None, *, mint: str | None = None) -> bool:
    """True for ANY asset tracking an external reference — including gold."""
    return lookup(symbol, mint=mint) is not None


def is_usd_pegged(symbol: str | None = None, *, mint: str | None = None) -> bool:
    a = lookup(symbol, mint=mint)
    return bool(a and a.is_usd_pegged)


def is_non_directional(symbol: str | None = None, *, mint: str | None = None) -> bool:
    """True only for assets with no USD price thesis a momentum scanner can
    legitimately express.

    USD-pegged only. PAXG and XAUT are pegged and excluded here on purpose:
    they track gold, and gold is a directional USD trade.
    """
    a = lookup(symbol, mint=mint)
    return bool(a and a.is_usd_pegged)


def peg_currency_of(symbol: str | None = None, *, mint: str | None = None) -> str | None:
    a = lookup(symbol, mint=mint)
    return a.peg_currency if a else None


# ── Stress assumptions ───────────────────────────────────────────────────
# Per TIER, not per asset, because the mechanism is what determines the
# failure mode. Every number is an ASSUMPTION and is labelled as one
# wherever it reaches output — the same discipline LSTStressProfile follows.
#
# `stress` / `severe` are deviations from par in PERCENT. Sign convention
# throughout the stress engine: POSITIVE means the asset trades ABOVE its
# peg, which is adverse for a borrower and favourable for a holder.
_TIER_DEPEG_ASSUMPTIONS = {
    TIER_FIAT_RESERVE:       (0.5, 3.0, "redemption gate or reserve-bank failure "
                                        "(USDC traded to 0.878 in the Mar 2023 SVB weekend)"),
    TIER_OVERCOLLATERALIZED: (1.0, 5.0, "collateral crash outrunning on-chain liquidations"),
    TIER_SYNTHETIC:          (2.0, 10.0, "sustained negative funding or hedge-venue failure"),
    TIER_YIELD_WRAPPER:      (2.0, 12.0, "redemption lockup or exit-queue discount; "
                                         "does not target par in the first place"),
    TIER_BRIDGED:            (3.0, 15.0, "bridge compromise severs the claim on backing"),
    TIER_ALGORITHMIC:        (5.0, 25.0, "reflexive support mechanism unwinding"),
    TIER_DISTRESSED:         (10.0, 50.0, "already impaired"),
}


def depeg_profile(symbol: str | None = None, *, mint: str | None = None) -> dict | None:
    """The stress assumption for this asset, with its basis stated.

    Returns None for an unregistered asset rather than a default, because
    silently assuming a peg for an unknown token is the failure this module
    exists to prevent.
    """
    a = lookup(symbol, mint=mint)
    if a is None:
        return None
    stress, severe, why = _TIER_DEPEG_ASSUMPTIONS.get(a.tier, (1.0, 5.0, "unclassified"))
    return {
        "symbol": a.symbol, "peg_currency": a.peg_currency,
        "tier": a.tier, "tier_name": TIER_NAMES.get(a.tier),
        "targets_par": a.targets_par,
        "stress_depeg_pct": stress, "severe_depeg_pct": severe,
        "yield_bearing": a.yield_bearing,
        "basis": "ASSUMED — tier default, not measured",
        "failure_mode": why,
    }
