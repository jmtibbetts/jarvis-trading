"""New-pool discovery across DEXes — GeckoTerminal + DEX Screener, free
and keyless.

This is the SECOND desk's discovery layer, and it is deliberately not
wired to the first one. The majors book trades BTC/ETH/SOL on 4H with
3,901 measured samples behind its expectancy; a three-hour-old Solana
pool shares none of that physics — different horizon, different
liquidity, different failure modes (rugs, honeypots, MEV). Nothing here
produces a signal, sizes a position, or enters the gate experiment's
candidate ledger. It answers one question: *which new tokens are worth
starting to collect history on?*

**The feed is mostly noise and the filter is the product.** A live sample
from GeckoTerminal's Solana new-pools endpoint: $1,680 of liquidity,
$7.46 of 24h volume, one buy and zero sells. Handing that to anything
downstream as an "opportunity" would be malpractice dressed as coverage.
So candidates must clear floors on liquidity, real two-sided trading,
and age — and every rejection reason is reported, because "we looked at
4,000 pools and 6 survived" is the honest headline, not a hidden one.

What survives here becomes a WATCH candidate: the desk starts caching
its bars so that, in a few weeks, the incubator has enough history for
the same honest analysis every other instrument gets. Discovery is not
endorsement — it is the decision to start paying attention.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com/latest/dex"

# Networks worth scanning. Solana and Base carry most new-listing volume;
# ethereum is slower but higher quality per listing.
NETWORKS = ("solana", "base", "eth")

# ── Quality floors ───────────────────────────────────────────────────────────
# Calibrated against what the live feed actually returns, not aspiration.
# A pool below these is not "early" — it is untraded.
MIN_LIQUIDITY_USD = 50_000.0     # thin books make every fill a lottery
MIN_VOLUME_24H_USD = 25_000.0    # real turnover, not a single wallet
MIN_TXNS_24H = 100               # participants, not one bot
MIN_BUYERS_24H = 25              # distinct demand — rugs show 1-3 buyers
MIN_AGE_HOURS = 24               # survived a full day, incl. one US session
MAX_AGE_DAYS = 30                # after this it is not a NEW listing
# Buy/sell balance: a pool with only buys is either brand-new or a
# honeypot that cannot be sold out of. Both are reasons to wait.
MIN_SELL_RATIO = 0.15


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Keyless GeckoTerminal allows ~30 calls/min. Self-inflicted 429s are the
# easy failure here, so passes are paced and failures are REPORTED — an
# empty result that cannot say whether it looked is worse than no result.
_GT_SPACING_S = 2.5
_last_gt_call = 0.0

# A 429 used to end that call permanently: return [], record the error,
# move on. With six calls per pass sharing one spacing clock, a single
# throttling episode therefore wiped the WHOLE discovery pass — observed
# live as `scanned: 0` with all six paths reporting "rate limited (429)"
# while the API answered every request normally seconds later.
#
# 429 is the one status that explicitly means "ask again shortly", so it is
# the one worth retrying. Bounded at three attempts: this runs behind an
# explicit button, and a few seconds is a fair price for the panel having
# anything in it at all. Every other status still fails on the first try.
_GT_RETRIES = 3
_GT_BACKOFF_S = (4.0, 9.0)


def _gt_get(path: str, errors: list) -> list[dict]:
    global _last_gt_call
    import time

    for attempt in range(_GT_RETRIES):
        wait = _GT_SPACING_S - (time.time() - _last_gt_call)
        if wait > 0:
            time.sleep(wait)
        _last_gt_call = time.time()
        try:
            r = httpx.get(f"{GT_BASE}/{path}", timeout=30.0)
            if r.status_code == 429:
                if attempt < _GT_RETRIES - 1:
                    # Honour Retry-After when the server sends one; it knows
                    # its own window better than a guessed constant.
                    try:
                        hinted = float(r.headers.get("Retry-After", "") or 0)
                    except ValueError:
                        hinted = 0.0
                    time.sleep(max(hinted, _GT_BACKOFF_S[attempt]))
                    continue
                errors.append(f"{path}: rate limited (429) after "
                              f"{_GT_RETRIES} attempts")
                return []
            r.raise_for_status()
            return r.json().get("data") or []
        except Exception as e:
            errors.append(f"{path}: {type(e).__name__}")
            return []
    return []


def fetch_new_pools(network: str, limit: int = 20,
                    errors: list | None = None) -> list[dict]:
    """Raw new-pool listings for one network. Keyless; ~30s refresh."""
    return _gt_get(f"networks/{network}/new_pools",
                   errors if errors is not None else [])[:limit]


def screen_pool(pool: dict, network: str, now: datetime | None = None) -> dict:
    """Judge one pool against the floors. Returns the verdict WITH its
    reasons — a rejection that cannot say why is not a filter, it is a
    black box."""
    now = now or datetime.now(timezone.utc)
    a = pool.get("attributes") or {}
    name = a.get("name") or "?"

    liq = _f(a.get("reserve_in_usd"))
    vol24 = _f((a.get("volume_usd") or {}).get("h24"))
    tx24 = (a.get("transactions") or {}).get("h24") or {}
    buys, sells = int(tx24.get("buys") or 0), int(tx24.get("sells") or 0)
    buyers = int(tx24.get("buyers") or 0)
    total_tx = buys + sells

    created = a.get("pool_created_at")
    age_h = None
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age_h = (now - dt).total_seconds() / 3600.0
        except Exception:
            age_h = None

    # (category, human detail) — the category is what the tally counts.
    # Parsing categories back out of prose produced a histogram keyed by
    # transaction counts, which described nothing.
    fails = []
    if liq < MIN_LIQUIDITY_USD:
        fails.append(("liquidity", f"${liq:,.0f} < ${MIN_LIQUIDITY_USD:,.0f}"))
    if vol24 < MIN_VOLUME_24H_USD:
        fails.append(("volume", f"${vol24:,.0f} < ${MIN_VOLUME_24H_USD:,.0f}"))
    if total_tx < MIN_TXNS_24H:
        fails.append(("txns", f"{total_tx} < {MIN_TXNS_24H}"))
    if buyers < MIN_BUYERS_24H:
        fails.append(("buyers", f"{buyers} < {MIN_BUYERS_24H}"))
    if age_h is None:
        fails.append(("no_creation_time", "unusable"))
    elif age_h < MIN_AGE_HOURS:
        fails.append(("too_young", f"{age_h:.1f}h < {MIN_AGE_HOURS}h"))
    elif age_h > MAX_AGE_DAYS * 24:
        fails.append(("too_old", f"{age_h / 24:.0f}d — not a new listing"))
    if total_tx > 0 and (sells / total_tx) < MIN_SELL_RATIO:
        # Cannot distinguish "everyone is holding" from "nobody CAN sell"
        # without a trade simulation, and the honest response to that
        # ambiguity is to wait rather than to guess.
        fails.append(("one_way_flow",
                      f"sells {sells}/{total_tx} — possible honeypot"))

    return {
        "network": network,
        "name": name,
        "pool_address": a.get("address"),
        "liquidity_usd": round(liq, 0),
        "volume_24h_usd": round(vol24, 0),
        "txns_24h": total_tx,
        "buyers_24h": buyers,
        "sell_ratio": round(sells / total_tx, 3) if total_tx else None,
        "age_hours": round(age_h, 1) if age_h is not None else None,
        "fdv_usd": _f(a.get("fdv_usd")) or None,
        "passes": not fails,
        "reasons": [f"{c}: {d}" for c, d in fails],
        "reason_tags": [c for c, _ in fails],
    }


# GeckoTerminal network slug -> DEX Screener chain id. Different vendors,
# different names for the same chain; mapping them explicitly beats hoping
# the strings happen to match.
DS_CHAIN = {"solana": "solana", "base": "base", "eth": "ethereum"}


def confirm_on_dexscreener(pool_address: str, network: str) -> dict | None:
    """Second-source confirmation, matched on POOL ADDRESS.

    Searching by token symbol looked reasonable and was badly wrong: the
    first live run "confirmed" CATE/SOL's $791k pool against a $198M one,
    because ticker search returns every unrelated token sharing those
    letters and this took the largest. That is the LINK incident in a new
    costume — an identity assumption doing damage where an identifier
    belonged. A pool address is unambiguous; a ticker never was.
    """
    chain = DS_CHAIN.get(network)
    if not chain or not pool_address:
        return None
    try:
        r = httpx.get(f"{DS_BASE}/pairs/{chain}/{pool_address}", timeout=30.0)
        r.raise_for_status()
        payload = r.json()
        pairs = payload.get("pairs") or payload.get("pair") or []
        if isinstance(pairs, dict):
            pairs = [pairs]
    except Exception as e:
        logger.debug(f"[DexDiscovery] dexscreener {pool_address}: {e}")
        return None
    if not pairs:
        return None
    p = pairs[0]
    return {
        "ds_liquidity_usd": round(_f((p.get("liquidity") or {}).get("usd")), 0),
        "ds_volume_24h_usd": round(_f((p.get("volume") or {}).get("h24")), 0),
        "ds_chain": p.get("chainId"),
        "ds_dex": p.get("dexId"),
        "ds_url": p.get("url"),
    }


def fetch_trending_pools(network: str, limit: int = 20,
                         errors: list | None = None) -> list[dict]:
    """Pools that have actually caught on. new_pools alone returns
    minutes-old listings that can never clear a 24h age floor, so the
    survivor set would be permanently empty — a filter that always says
    no is indistinguishable from a broken one."""
    return _gt_get(f"networks/{network}/trending_pools",
                   errors if errors is not None else [])[:limit]


def discover(networks: tuple = NETWORKS, confirm: bool = True) -> dict:
    """One discovery pass. Reports what SURVIVED and what was rejected and
    why — the rejection tally is the honest measure of the feed's noise."""
    screened, survivors = [], []
    fetch_errors: list = []
    seen: set = set()
    for net in networks:
        for pool in (fetch_new_pools(net, errors=fetch_errors)
                     + fetch_trending_pools(net, errors=fetch_errors)):
            addr = (pool.get("attributes") or {}).get("address")
            if addr and addr in seen:
                continue
            if addr:
                seen.add(addr)
            v = screen_pool(pool, net)
            screened.append(v)
            if v["passes"]:
                survivors.append(v)

    if confirm:
        for s in survivors:
            extra = confirm_on_dexscreener(s["pool_address"], s["network"])
            if extra:
                s.update(extra)
                # Disagreement worth surfacing rather than averaging away.
                gt, ds = s["liquidity_usd"], s["ds_liquidity_usd"]
                if gt and ds and (max(gt, ds) / max(1.0, min(gt, ds))) > 3.0:
                    s["source_disagreement"] = (
                        f"GT ${gt:,.0f} vs DS ${ds:,.0f} — treat with care")

    # Why things failed, most common first: the shape of the noise.
    reason_counts: dict[str, int] = {}
    for v in screened:
        for tag in v["reason_tags"]:
            reason_counts[tag] = reason_counts.get(tag, 0) + 1

    survivors.sort(key=lambda s: -s["liquidity_usd"])
    return {
        "scanned": len(screened),
        # A pass that could not fetch must SAY so: 'scanned 0, survivors 0'
        # reads as 'nothing qualified' when it may mean 'we never looked'.
        "fetch_errors": fetch_errors,
        "degraded": bool(fetch_errors),
        "survivors": survivors,
        "rejected": len(screened) - len(survivors),
        "rejection_reasons": dict(sorted(reason_counts.items(),
                                         key=lambda kv: -kv[1])),
        "floors": {
            "min_liquidity_usd": MIN_LIQUIDITY_USD,
            "min_volume_24h_usd": MIN_VOLUME_24H_USD,
            "min_txns_24h": MIN_TXNS_24H,
            "min_buyers_24h": MIN_BUYERS_24H,
            "age_window_hours": [MIN_AGE_HOURS, MAX_AGE_DAYS * 24],
            "min_sell_ratio": MIN_SELL_RATIO,
        },
        "note": ("discovery is not endorsement — surviving here means the "
                 "desk should START COLLECTING history, nothing more. No "
                 "signal, no sizing, no entry into the majors book's gate."),
    }
