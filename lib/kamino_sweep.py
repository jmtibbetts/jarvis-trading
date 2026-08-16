"""The whole Kamino book, ranked by which positions actually matter.

Size alone is the wrong sort. A $20M position at health 3.5 is irrelevant
to a cascade; a $3.16M position at health 1.17 is the one that moves a
market. Significance is size AND fragility together, weighted by how
volatile the collateral is and who owns it.

Measured over the live protocol:

    61,246 obligations carry debt      (of 139,756 total)
     8,002 carry debt above $1,000     $915M of debt
       170 hold collateral above $1M
        30 hold collateral above $10M  $769M of collateral

The sweep is affordable because the filtering happens server-side —
`has_debt` is a single byte at a known offset, so memcmp removes 56% of the
accounts before anything is transferred, and a dataSlice sends only the
prefix through the value fields rather than all 3,344 bytes. 4 seconds for
the fetch, 0.2 for the decode.
"""
from __future__ import annotations

import base64
import logging
import math
import os

logger = logging.getLogger(__name__)

# Size bands for reporting. Configurable because "large" depends on what
# the operator can act on, not on a constant in a file.
DEFAULT_BANDS = (100_000, 500_000, 1_000_000, 5_000_000, 10_000_000)

# Collateral volatility multipliers for the significance score. Stables
# barely move, so a stable-collateralised position near its threshold is
# far less likely to actually cross it.
VOLATILITY_BY_FAMILY = {"SOL_FAMILY": 1.0, "STABLE": 0.25, "unknown": 0.7}


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def min_debt_usd() -> float:
    return _cfg_float("KAMINO_SWEEP_MIN_DEBT_USD", 1_000.0)


def sweep_obligations(min_debt: float | None = None,
                      slice_only: bool = True) -> dict:
    """Every leveraged Kamino position, decoded to its value fields.

    `slice_only` keeps the transfer to the prefix that carries owner,
    collateral, debt and threshold. Full accounts are only needed to name
    assets, which is done later for the shortlist rather than for 61,000
    positions nobody will look at.
    """
    from lib.capital_lending import (KAMINO_LEND_PROGRAM, OBLIGATION_DISCRIMINATOR,
                                     OBLIGATION_SIZE, OFFSETS, SCALE_SF)
    from lib.helius_client import rpc
    from lib.wallet_registry import b58encode

    floor = min_debt_usd() if min_debt is None else min_debt
    slice_len = OFFSETS["has_debt"] + 1
    out = {"scanned": 0, "with_debt": 0, "positions": [], "error": None,
           "total_debt_usd": 0.0, "total_collateral_usd": 0.0}
    try:
        rows = rpc("getProgramAccounts", [KAMINO_LEND_PROGRAM, {
            "encoding": "base64",
            "filters": [{"dataSize": OBLIGATION_SIZE},
                        # "2" is base58 for a single 0x01 byte — has_debt set.
                        {"memcmp": {"offset": OFFSETS["has_debt"], "bytes": "2"}}],
            "dataSlice": {"offset": 0, "length": slice_len} if slice_only else None,
        }]) or []
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        return out

    u128 = lambda b, o: int.from_bytes(b[o:o + 16], "little")  # noqa: E731
    for row in rows:
        out["scanned"] += 1
        try:
            raw = base64.b64decode(row["account"]["data"][0])
        except (KeyError, IndexError, ValueError):
            continue
        # The discriminator still governs, exactly as in the full decoder —
        # a slice is a smaller buffer, not a weaker guarantee.
        if len(raw) < slice_len or raw[:8] != OBLIGATION_DISCRIMINATOR:
            continue
        debt = u128(raw, OFFSETS["debt_value_sf"]) / SCALE_SF
        if debt < floor:
            continue
        collateral = u128(raw, OFFSETS["deposited_value_sf"]) / SCALE_SF
        allowed = u128(raw, OFFSETS["allowed_borrow_value_sf"]) / SCALE_SF
        unhealthy = u128(raw, OFFSETS["unhealthy_borrow_value_sf"]) / SCALE_SF
        if max(collateral, debt, allowed, unhealthy) > 1e12:
            continue
        if not (allowed <= unhealthy + 1e-6 <= collateral + 1e-6):
            continue

        out["with_debt"] += 1
        out["total_debt_usd"] += debt
        out["total_collateral_usd"] += collateral
        hf = unhealthy / debt if debt else None
        out["positions"].append({
            "obligation": row.get("pubkey"),
            "owner": b58encode(raw[OFFSETS["owner"]:OFFSETS["owner"] + 32]),
            "collateral_value_usd": round(collateral, 2),
            "debt_value_usd": round(debt, 2),
            "liquidation_threshold_usd": round(unhealthy, 2),
            "health_factor": round(hf, 4) if hf else None,
            "net_equity_usd": round(collateral - debt, 2),
            "leverage": round(collateral / max(collateral - debt, 1e-9), 3),
            "distance_to_liquidation_pct": round(
                max((unhealthy - debt) / unhealthy * 100.0, 0.0), 3) if unhealthy > 0 else 0.0,
        })

    out["total_debt_usd"] = round(out["total_debt_usd"], 2)
    out["total_collateral_usd"] = round(out["total_collateral_usd"], 2)
    return out


def significance(position: dict, *, collateral_family: str = "unknown",
                 wallet_score: float | None = None) -> dict:
    """How much this position matters if the market moves against it.

    Deliberately NOT a size ranking. Size sets the ceiling; proximity
    decides whether the ceiling is ever reached. A $20M position 60% from
    liquidation contributes nothing to a cascade, while a $3M position 3%
    away is the one that starts it.

    Components, each reported so the score can be argued with:

      size      log-scaled forced-sale exposure, so a $10M position
                outranks a $1M one without swamping every other term
      proximity how close to the threshold, steeply weighted
      volatility whether the collateral can actually move that far
      wallet    a known high-alpha owner raises the stakes; an unknown
                one is NEUTRAL rather than penalised
    """
    debt = float(position.get("debt_value_usd") or 0)
    distance = float(position.get("distance_to_liquidation_pct") or 0)

    # $1k -> 0, $10M -> 1. Below the floor the position cannot move a market.
    size = min(1.0, max(0.0, math.log10(max(debt, 1.0) / 1_000.0) / 4.0))
    # 0% away -> 1.0, 20% away -> ~0.2, beyond 50% -> negligible.
    proximity = 1.0 / (1.0 + (distance / 5.0))
    vol = VOLATILITY_BY_FAMILY.get(collateral_family, VOLATILITY_BY_FAMILY["unknown"])
    # An unscored wallet must not be treated as a bad one.
    wallet = 1.0 + (float(wallet_score) / 100.0 if wallet_score else 0.0)

    score = 100.0 * size * proximity * vol * min(wallet, 2.0)
    return {
        "significance_score": round(min(score, 100.0), 2),
        "components": {
            "size": round(size, 4),
            "proximity": round(proximity, 4),
            "collateral_volatility": vol,
            "wallet_multiplier": round(min(wallet, 2.0), 3),
        },
        "potential_forced_sale_usd": round(debt, 2),
        "basis": ("CALCULATED from verified position values; volatility and "
                  "wallet weighting are ASSUMED inputs"),
    }


def rank_by_significance(positions: list[dict], limit: int = 50,
                         families: dict | None = None,
                         wallet_scores: dict | None = None) -> list[dict]:
    """Positions ordered by how much they matter, not by how big they are."""
    families = families or {}
    wallet_scores = wallet_scores or {}
    scored = []
    for p in positions:
        s = significance(
            p,
            collateral_family=families.get(p.get("obligation"), "unknown"),
            wallet_score=wallet_scores.get(p.get("owner")),
        )
        scored.append({**p, **s})
    scored.sort(key=lambda x: x["significance_score"], reverse=True)
    return scored[:limit]


def join_wallet_registry(positions: list[dict], db=None) -> dict:
    """Attach what the wallet registry knows about each position's owner.

    A $42M position 2% from liquidation owned by a measured high-alpha
    wallet is a different event from the same position owned by an unknown
    one: the first is a signal that someone skilled is about to be forced
    out, the second is just leverage unwinding.

    The overlap is reported because it is currently ZERO and that fact is
    informative, not a failure. Discovery finds wallets by TOKEN ACTIVITY
    while this finds them by BORROWING, and those are different
    populations — a profitable spot trader need never touch a lending
    market. The join is built so it fills in as both sides grow; claiming
    coverage it does not have would be worse than reporting none.
    """
    from app.database import WalletRegistry, get_db

    def _run(session):
        owners = {p.get("owner") for p in positions if p.get("owner")}
        if not owners:
            return {}
        found = {}
        # Chunked: SQLite refuses very large IN clauses.
        owner_list = list(owners)
        for i in range(0, len(owner_list), 500):
            for w in session.query(WalletRegistry).filter(
                    WalletRegistry.address.in_(owner_list[i:i + 500])).all():
                found[w.address] = {
                    "status": w.status, "source": w.source,
                    "pinned": bool(w.pinned), "is_trader": w.is_trader,
                    "entity_type": w.entity_type, "entity_name": w.entity_name,
                    # None means NOT MEASURED — never zero, which would rank
                    # an unanalysed wallet as a bad one.
                    "smart_money_score": w.smart_money_score,
                    "alpha_score": w.alpha_score,
                    "copy_score": w.copy_score,
                }
        return found

    if db is not None:
        known = _run(db)
    else:
        with get_db() as _db:
            known = _run(_db)

    enriched = []
    for p in positions:
        w = known.get(p.get("owner"))
        enriched.append({**p, "wallet": w,
                         "wallet_known": bool(w),
                         "wallet_score": (w or {}).get("smart_money_score")})

    matched = sum(1 for e in enriched if e["wallet_known"])
    return {
        "positions": enriched,
        "owners_seen": len({p.get("owner") for p in positions if p.get("owner")}),
        "owners_known": matched,
        "note": ("Discovery finds wallets by token activity; this finds them "
                 "by borrowing. Zero overlap means those populations have "
                 "not intersected yet, not that the join failed."),
    }


def band_summary(positions: list[dict], bands=DEFAULT_BANDS) -> list[dict]:
    """Position counts and totals by collateral size."""
    out = []
    for lo in bands:
        rows = [p for p in positions if float(p.get("collateral_value_usd") or 0) >= lo]
        near = [p for p in rows if float(p.get("distance_to_liquidation_pct") or 100) <= 5.0]
        out.append({
            "min_collateral_usd": lo,
            "positions": len(rows),
            "collateral_usd": round(sum(float(p["collateral_value_usd"]) for p in rows), 2),
            "debt_usd": round(sum(float(p["debt_value_usd"]) for p in rows), 2),
            "within_5pct_of_liquidation": len(near),
            "debt_within_5pct_usd": round(sum(float(p["debt_value_usd"]) for p in near), 2),
        })
    return out
