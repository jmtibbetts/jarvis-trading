"""Kamino lending positions, decoded from the OFFICIAL account layout.

Offsets here are computed from `@kamino-finance/klend-sdk` v10.1.0
(`dist/@codegen/klend/accounts/Obligation.js` and its nested types), not
reverse-engineered. An earlier attempt to find the owner field by scanning
for plausible 32-byte windows failed and would, if pushed, have produced
invented lending positions — health factors and liquidation prices are
exactly the wrong thing to guess at.

Sizes derived from the SDK's borsh structs:

    BigFractionBytes              u64*4 + u64*2                 =  48
    FixedTermBorrowRolloverConfig u8*4 + u32 + u64              =  16
    ObligationCollateral          32 + 8 + 16 + 8 + u64*9       = 136
    ObligationLiquidity           32+48+8+16+16+16+8+16+8+32    = 200

Giving the top-level offsets below. They are self-validating on live data:
`collateral > liquidation_threshold > debt` holds on every decoded row,
which wrong offsets would not produce.

Kamino stores value fields as scaled fractions ("Sf" suffix) with a 2^60
scaling factor.

HEALTH: a position becomes liquidatable when the borrow-factor-adjusted
debt reaches `unhealthyBorrowValue`. Health factor is therefore
`unhealthy / debt` — above 1 is safe, at or below 1 is liquidatable. This
is Kamino's own rule; other protocols define health differently and must
get their own adapter rather than borrowing this formula.
"""
from __future__ import annotations

import base64
import logging

logger = logging.getLogger(__name__)

KAMINO_LEND_PROGRAM = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
OBLIGATION_SIZE = 3344
SCALE_SF = 2 ** 60

# Provenance, so a future Kamino layout change is a visible failure rather
# than silently decoded nonsense. If the discriminator or size stops
# matching, decoding returns None and the integration reports itself
# unavailable instead of guessing.
DECODER_SOURCE = "@kamino-finance/klend-sdk"
DECODER_VERSION = "10.1.0"
# Canonical Anchor discriminator, read from the SDK's Obligation.decode,
# which throws "invalid account discriminator" on mismatch.
OBLIGATION_DISCRIMINATOR = bytes([168, 206, 141, 106, 88, 76, 172, 167])

# No real Kamino position approaches a trillion dollars; a random u128
# scaled by 2^60 routinely exceeds 1e20.
MAX_PLAUSIBLE_USD = 1e12

_COLLATERAL_SIZE = 136
_LIQUIDITY_SIZE = 200
_DEPOSITS_AT = 96
_AFTER_DEPOSITS = _DEPOSITS_AT + _COLLATERAL_SIZE * 8      # 1184

OFFSETS = {
    "lending_market": 32,
    "owner": 64,
    "deposited_value_sf": _AFTER_DEPOSITS + 8,             # 1192
    "debt_value_sf": _AFTER_DEPOSITS + 8 + 16 + _LIQUIDITY_SIZE * 5,   # 2208
}
OFFSETS["allowed_borrow_value_sf"] = OFFSETS["debt_value_sf"] + 32       # 2240
OFFSETS["unhealthy_borrow_value_sf"] = OFFSETS["allowed_borrow_value_sf"] + 16  # 2256
OFFSETS["has_debt"] = OFFSETS["unhealthy_borrow_value_sf"] + 16 + 13 + 1 + 1    # 2287

# Risk bands by health factor. CRITICAL starts above 1.0 deliberately: the
# point is to warn BEFORE forced selling, not to report it afterwards.
RISK_BANDS = (
    (1.00, "LIQUIDATION_IN_PROGRESS"),
    (1.05, "CRITICAL"),
    (1.15, "HIGH"),
    (1.35, "ELEVATED"),
)
# Dust positions are real but not market-moving; they should not crowd a
# risk dashboard meant for positions that can move a price.
MIN_TRACKED_DEBT_USD = 10_000.0


def _u128(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 16], "little")


def _b58(raw: bytes) -> str:
    from lib.wallet_registry import b58encode
    return b58encode(raw)


def decode_obligation(raw: bytes) -> dict | None:
    """One obligation account -> position figures. None if not decodable.

    THREE gates, because size alone is not identity. Measured: 3,344 bytes
    of os.urandom passed a size-only check and produced the plausible
    wallet Ef8QvULRFjLEG7usD4HDpokKH9otsWE3781KPaZPXQm5 holding
    $2.5e20 of collateral. Any 32-byte window base58-encodes into something
    that looks exactly like a Solana address — the same lesson already
    learned in address validation, reappearing one layer down.

    1. SIZE      — 3,344 bytes
    2. DISCRIMINATOR — the canonical 8-byte Anchor tag from the official
       SDK. This is what makes the buffer an Obligation rather than
       something else of the same length.
    3. STRUCTURAL SANITY — Kamino's own invariant, allowed <= unhealthy <=
       collateral, plus a magnitude ceiling. Random u128s are astronomical
       and do not honour the ordering; a buffer that carries the right
       discriminator but garbage values still fails here.

    Returning None is always preferable to a plausible number. There is no
    acceptable case where this function invents lending exposure.
    """
    if not raw or len(raw) != OBLIGATION_SIZE:
        return None
    if raw[:8] != OBLIGATION_DISCRIMINATOR:
        return None
    try:
        collateral = _u128(raw, OFFSETS["deposited_value_sf"]) / SCALE_SF
        debt = _u128(raw, OFFSETS["debt_value_sf"]) / SCALE_SF
        allowed = _u128(raw, OFFSETS["allowed_borrow_value_sf"]) / SCALE_SF
        unhealthy = _u128(raw, OFFSETS["unhealthy_borrow_value_sf"]) / SCALE_SF

        # Gate 3. Kamino derives both borrow ceilings from the SAME
        # collateral, using the LTV and the liquidation threshold, so
        # allowed <= unhealthy <= collateral holds by construction.
        # Random data satisfies neither the ordering nor the magnitude.
        if max(collateral, debt, allowed, unhealthy) > MAX_PLAUSIBLE_USD:
            return None
        if not (allowed <= unhealthy + 1e-6 <= collateral + 1e-6):
            return None

        return {
            "decoder_version": DECODER_VERSION,
            "decoder_source": DECODER_SOURCE,
            "program_id": KAMINO_LEND_PROGRAM,
            "owner": _b58(raw[OFFSETS["owner"]:OFFSETS["owner"] + 32]),
            "lending_market": _b58(raw[OFFSETS["lending_market"]:
                                       OFFSETS["lending_market"] + 32]),
            "collateral_value_usd": round(collateral, 2),
            "debt_value_usd": round(debt, 2),
            "allowed_borrow_value_usd": round(allowed, 2),
            "liquidation_threshold_usd": round(unhealthy, 2),
            "has_debt": bool(raw[OFFSETS["has_debt"]]),
        }
    except (IndexError, ValueError) as e:
        logger.debug(f"[CapitalLending] decode failed: {e}")
        return None


def health_of(position: dict) -> dict:
    """Health factor, distance to liquidation, and the risk band.

    `distance_to_liquidation_pct` is how far collateral can fall before the
    position is liquidatable — the number that answers "how much does the
    market have to move against them", which is what makes this a leading
    signal rather than a post-mortem.
    """
    debt = float(position.get("debt_value_usd") or 0)
    unhealthy = float(position.get("liquidation_threshold_usd") or 0)
    collateral = float(position.get("collateral_value_usd") or 0)

    if debt <= 0:
        return {"health_factor": None, "risk_state": "SAFE",
                "distance_to_liquidation_pct": None,
                "reason": "no debt — nothing to liquidate"}

    hf = unhealthy / debt if debt else None
    # Collateral may fall until the threshold meets the debt. The threshold
    # scales with collateral, so the drop that closes the gap is the gap's
    # share of the threshold.
    distance = ((unhealthy - debt) / unhealthy * 100.0) if unhealthy > 0 else 0.0

    state = "SAFE"
    for ceiling, band in RISK_BANDS:
        if hf is not None and hf <= ceiling:
            state = band
            break

    return {
        "health_factor": round(hf, 4) if hf is not None else None,
        "risk_state": state,
        "distance_to_liquidation_pct": round(max(distance, 0.0), 3),
        "collateral_value_usd": round(collateral, 2),
        "debt_value_usd": round(debt, 2),
        # What would hit the market if this position is forced out. The
        # cascade model prices this against pool depth with
        # dex_swap_math.quote_swap rather than assuming it fills at mid.
        "potential_forced_sale_usd": round(debt, 2),
        "reason": (f"health {hf:.3f} — collateral can fall "
                   f"{max(distance, 0.0):.1f}% before liquidation"
                   if hf is not None else "no debt"),
    }


def obligations_for(wallet: str) -> list[dict]:
    """Every Kamino obligation owned by `wallet`, with health attached."""
    from lib.helius_client import rpc
    try:
        rows = rpc("getProgramAccounts", [KAMINO_LEND_PROGRAM, {
            "encoding": "base64",
            "filters": [{"dataSize": OBLIGATION_SIZE},
                        {"memcmp": {"offset": OFFSETS["owner"], "bytes": wallet}}],
        }]) or []
    except Exception as e:
        logger.debug(f"[CapitalLending] {wallet[:8]}…: {e}")
        return []

    out = []
    for row in rows:
        try:
            raw = base64.b64decode(row["account"]["data"][0])
        except (KeyError, IndexError, ValueError):
            continue
        pos = decode_obligation(raw)
        if not pos:
            continue
        pos["obligation"] = row.get("pubkey")
        pos["protocol"] = "Kamino Lend"
        pos.update(health_of(pos))
        # Carried so the caller can resolve assets without re-fetching.
        pos["_raw"] = raw
        out.append(pos)
    return out


def obligation_by_address(obligation: str, *, with_assets: bool = True) -> dict | None:
    """ONE obligation, by its own account address.

    The stress matrix works on a single position, and every existing reader
    here fetches by OWNER (`obligations_for`) or sweeps the whole program.
    Both are the wrong shape for "show me this position's boundary": one
    needs a wallet the caller may not have, the other decodes 61,000 rows
    to reach one.

    Assets are named by default because the matrix identifies collateral by
    MINT — the BSOL ticker collides with a US-listed ETF, and a stress
    profile chosen by ticker would eventually apply liquid-staking depeg
    assumptions to an equity.
    """
    from lib.helius_client import rpc

    try:
        acc = rpc("getAccountInfo", [obligation, {"encoding": "base64"}])
    except Exception as e:
        logger.debug(f"[CapitalLending] obligation {obligation[:8]}…: {e}")
        return None

    data = ((acc or {}).get("value") or {}).get("data")
    if not data:
        return None
    try:
        raw = base64.b64decode(data[0])
    except (IndexError, ValueError, TypeError):
        return None

    pos = decode_obligation(raw)
    if not pos:
        return None
    pos["obligation"] = obligation
    pos["protocol"] = "Kamino Lend"
    pos.update(health_of(pos))
    pos["_raw"] = raw
    if with_assets:
        try:
            from lib.capital_reserves import name_positions
            pos["assets"] = name_positions(raw)
        except Exception as e:
            # A position without named assets still has a health factor;
            # say the names are missing rather than failing the whole read.
            logger.debug(f"[CapitalLending] assets for {obligation[:8]}…: {e}")
            pos["assets_error"] = f"{type(e).__name__}"
    return pos


def scan_positions_at_risk(limit_scanned: int = 20_000,
                           min_debt_usd: float | None = None) -> dict:
    """Sweep every obligation and rank the ones close to forced selling.

    Scans the protocol rather than a watchlist on purpose: a cascade is
    driven by aggregate leverage, and only counting positions belonging to
    already-known wallets would measure the watchlist instead of the
    market.
    """
    from lib.helius_client import rpc
    floor = MIN_TRACKED_DEBT_USD if min_debt_usd is None else min_debt_usd
    out = {"scanned": 0, "with_debt": 0, "tracked": 0, "positions": [],
           "total_debt_usd": 0.0, "at_risk_usd": 0.0, "error": None}
    try:
        rows = rpc("getProgramAccounts", [KAMINO_LEND_PROGRAM, {
            "encoding": "base64", "filters": [{"dataSize": OBLIGATION_SIZE}]}]) or []
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        return out

    for row in rows[:limit_scanned]:
        out["scanned"] += 1
        try:
            raw = base64.b64decode(row["account"]["data"][0])
        except (KeyError, IndexError, ValueError):
            continue
        pos = decode_obligation(raw)
        if not pos or not pos["has_debt"]:
            continue
        out["with_debt"] += 1
        if pos["debt_value_usd"] < floor:
            continue
        pos["obligation"] = row.get("pubkey")
        pos["protocol"] = "Kamino Lend"
        pos.update(health_of(pos))
        pos["_raw"] = raw          # so the caller can resolve assets
        out["tracked"] += 1
        out["total_debt_usd"] += pos["debt_value_usd"]
        if pos["risk_state"] in ("ELEVATED", "HIGH", "CRITICAL",
                                 "LIQUIDATION_IN_PROGRESS"):
            out["at_risk_usd"] += pos["debt_value_usd"]
        out["positions"].append(pos)

    out["positions"].sort(key=lambda p: (p.get("health_factor") or 999))
    out["total_debt_usd"] = round(out["total_debt_usd"], 2)
    out["at_risk_usd"] = round(out["at_risk_usd"], 2)
    return out
