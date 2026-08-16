"""What happens to Kamino positions when a collateral asset falls.

Answers the question a health factor cannot: not "is this position safe
now" but "how far does the market have to move before a wave of forced
selling starts, and how big is that wave".

Everything here is a MODEL and says so. The positions are verified, the
health rule is Kamino's own, and the shock ladder is a scenario — labelled
separately at every layer so a projection is never read as a measurement.

Two things this refuses to do:

SHOCK ASSETS INDEPENDENTLY. bSOL, mSOL, JitoSOL and SOL do not move
separately in a SOL decline. Stressing one while holding the others flat
would understate exposure for exactly the wallets most at risk — the ones
whose collateral is entirely SOL-derived. Correlated families move
together, and individual assets are still reported so the merge is visible
rather than assumed.

CLAIM A PRICE AFTER THE CASCADE. Forced selling has a price impact, and
that impact is priced against real pool depth with the same constant-product
math the DEX book uses — but only where depth is actually known. Where it
is not, the result says UNAVAILABLE instead of inventing a number.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Shock levels. Small steps near the top because that is where positions
# actually sit — most leverage is unwound long before -20%.
DEFAULT_SHOCKS = (1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0)

# Assets that fall together in a SOL decline. Kept as a FAMILY rather than
# merged into one asset: an LST can depeg from SOL independently, so the
# individual figures stay visible.
SOL_FAMILY = {"SOL", "wSOL", "JitoSOL", "mSOL", "bSOL"}
STABLE_FAMILY = {"USDC", "USDT"}


def family_of(symbol: str | None) -> str:
    if not symbol:
        return "unknown"
    if symbol in SOL_FAMILY:
        return "SOL_FAMILY"
    if symbol in STABLE_FAMILY:
        return "STABLE"
    return symbol


def _health_after_shock(position: dict, shock_pct: float,
                        family: str) -> dict | None:
    """Recompute health with one collateral family marked down.

    Only the collateral in the shocked family moves. Debt is held flat,
    which is the conservative direction for a stablecoin borrow and the
    honest one when the debt asset is unrelated to the shock.
    """
    assets = position.get("assets") or {}
    deposits = assets.get("deposits") or []
    if not deposits:
        return None

    shocked = 0.0
    unshocked = 0.0
    for d in deposits:
        val = float(d.get("value_usd") or 0)
        if family_of(d.get("symbol")) == family:
            shocked += val
        else:
            unshocked += val
    if shocked <= 0:
        return None                       # this family is not this position's risk

    factor = 1.0 - (shock_pct / 100.0)
    new_collateral = unshocked + shocked * factor

    # The liquidation threshold is a fraction OF collateral, so it falls
    # with it. Using the original threshold against reduced collateral
    # would overstate how quickly positions become liquidatable.
    old_collateral = float(position.get("collateral_value_usd") or 0)
    old_threshold = float(position.get("liquidation_threshold_usd") or 0)
    if old_collateral <= 0:
        return None
    new_threshold = old_threshold * (new_collateral / old_collateral)

    debt = float(position.get("debt_value_usd") or 0)
    if debt <= 0:
        return None
    return {
        "health_factor": round(new_threshold / debt, 4),
        "collateral_value_usd": round(new_collateral, 2),
        "liquidation_threshold_usd": round(new_threshold, 2),
        "debt_value_usd": round(debt, 2),
        "shocked_collateral_usd": round(shocked, 2),
    }


def stress_ladder(positions: list[dict], family: str = "SOL_FAMILY",
                  shocks: tuple = DEFAULT_SHOCKS) -> dict:
    """Newly liquidatable positions and forced-sale exposure per shock.

    `newly` counts each position ONCE, at the first shock that breaks it —
    otherwise every deeper rung re-counts everything above and the ladder
    reads as though exposure multiplies with depth.
    """
    exposed = [p for p in positions
               if _health_after_shock(p, 0.0, family) is not None]
    rungs, already_broken = [], set()

    # Positions already liquidatable before any shock.
    for p in exposed:
        h = p.get("health_factor")
        if h is not None and h <= 1.0:
            already_broken.add(p.get("obligation") or id(p))

    for shock in shocks:
        newly, newly_debt, at_risk_debt, collateral_hit = [], 0.0, 0.0, 0.0
        for p in exposed:
            key = p.get("obligation") or id(p)
            after = _health_after_shock(p, shock, family)
            if not after:
                continue
            if after["health_factor"] <= 1.0:
                at_risk_debt += after["debt_value_usd"]
                collateral_hit += after["shocked_collateral_usd"]
                if key not in already_broken:
                    newly.append(p)
                    newly_debt += after["debt_value_usd"]
                    already_broken.add(key)
        rungs.append({
            "shock_pct": shock,
            "newly_liquidatable": len(newly),
            "newly_liquidatable_debt_usd": round(newly_debt, 2),
            "cumulative_liquidatable_debt_usd": round(at_risk_debt, 2),
            "collateral_at_risk_usd": round(collateral_hit, 2),
        })

    return {
        "family": family,
        "positions_considered": len(exposed),
        "already_liquidatable": len(already_broken) - sum(
            r["newly_liquidatable"] for r in rungs),
        "ladder": rungs,
        "basis": ("MODELLED — verified positions and Kamino's own health "
                  "rule, projected against hypothetical prices. Not a "
                  "forecast and not a claim that these prices will occur."),
    }


def forced_sale_impact(forced_usd: float, pool_reserve_usd: float | None,
                       dex: str | None = None) -> dict:
    """What dumping `forced_usd` would do to a pool of known depth.

    Uses the same constant-product math as the DEX book, because a
    liquidation does not fill at mid — it walks the curve exactly like any
    other market order, and on a thin pool the walk is most of the loss.
    """
    if not pool_reserve_usd or pool_reserve_usd <= 0:
        return {"available": False,
                "reason": ("UNAVAILABLE — pool depth unknown, so price "
                           "impact cannot be computed rather than guessed")}
    from lib.dex_swap_math import quote_swap
    q = quote_swap(forced_usd, pool_reserve_usd, dex=dex)
    if not q.get("ok"):
        return {"available": False, "reason": q.get("reason")}
    return {
        "available": True,
        "forced_sale_usd": round(forced_usd, 2),
        "pool_reserve_usd": round(pool_reserve_usd, 2),
        "price_impact_pct": q["price_impact_pct"],
        "proceeds_usd": q["received_usd"],
        "basis": "MODELLED — constant-product impact against known depth",
    }


def aggregate_by_asset(positions: list[dict]) -> dict:
    """Collateral and debt grouped by the asset actually held.

    Reports BOTH the individual asset and its correlated family, because
    merging them hides an LST depeg and separating them hides a broad SOL
    decline. Neither view alone is sufficient.
    """
    by_asset: dict[str, dict] = {}
    by_family: dict[str, dict] = {}

    def _bucket(store, key):
        return store.setdefault(key, {
            "collateral_usd": 0.0, "debt_usd": 0.0,
            "positions": 0, "unresolved": 0})

    for p in positions:
        assets = p.get("assets") or {}
        for d in assets.get("deposits") or []:
            sym = d.get("symbol")
            if not sym:
                _bucket(by_asset, "UNRESOLVED")["unresolved"] += 1
                continue
            b = _bucket(by_asset, sym)
            b["collateral_usd"] += float(d.get("value_usd") or 0)
            b["positions"] += 1
            f = _bucket(by_family, family_of(sym))
            f["collateral_usd"] += float(d.get("value_usd") or 0)
            f["positions"] += 1
        for br in assets.get("borrows") or []:
            sym = br.get("symbol")
            if not sym:
                continue
            _bucket(by_asset, sym)["debt_usd"] += float(br.get("value_usd") or 0)
            _bucket(by_family, family_of(sym))["debt_usd"] += float(br.get("value_usd") or 0)

    for store in (by_asset, by_family):
        for v in store.values():
            v["collateral_usd"] = round(v["collateral_usd"], 2)
            v["debt_usd"] = round(v["debt_usd"], 2)

    return {"by_asset": by_asset, "by_family": by_family,
            "note": ("Family totals overlap individual assets by design — a "
                     "broad SOL decline hits the family, an LST depeg hits "
                     "one member, and only showing one view hides the other.")}
