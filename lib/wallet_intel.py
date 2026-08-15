"""Wallet intelligence — scores with their evidence attached.

Phase 7. Every function here is PURE: normalized records in, a score and
the reasons for it out. Nothing fetches. That is deliberate — the desk's
existing scoring layers earned their trust by being testable without a
network, and an intelligence layer that can only be exercised against
live mainnet is an intelligence layer nobody re-runs.

THE DISCIPLINE THIS MODULE INHERITS, stated once:

  A shared funding source is not a conspiracy. Two wallets funded by the
  same Binance hot wallet share an exchange, not an owner — and that hot
  wallet funds millions of addresses. Calling that "insider activity"
  would be the on-chain twin of labelling FINRA dark-pool volume bullish:
  taking a nondirectional fact and dressing it as conviction. So this
  module emits `coordination_score` with the evidence that produced it,
  never a verdict, and it discounts common funders that are identifiable
  infrastructure rather than treating every shared origin alike.

  A large balance is not skill. §28 is explicit and it matches this
  desk's own history: the composite score was measured INVERTED once,
  because "looks impressive" was standing in for "has been right".
  smart_money_score refuses to read size as evidence at all.

  A whale is not a dollar amount. A $50k trade is noise for one wallet
  and a career high for another, so size is judged against the wallet's
  OWN history as well as absolutely.

Every score returns `reasons` — a decision this desk cannot audit is a
decision it should not act on.
"""
from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# Identity `type` values Helius returns for infrastructure. A funder of
# this kind explains a shared origin without implying any relationship
# between the wallets it funded.
INFRASTRUCTURE_TYPES = {"exchange", "protocol", "program", "bridge"}

# Absolute whale floor in USD, and the multiple of a wallet's own median
# transfer that counts as anomalous for THAT wallet.
WHALE_USD_FLOOR = 100_000.0
WHALE_RELATIVE_MULTIPLE = 5.0

# Below this many observations a wallet's "normal" is not established, so
# relative anomaly is not claimed.
MIN_HISTORY_FOR_BASELINE = 8


def _num(v) -> float:
    try:
        return abs(float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def _identity_type(identity: dict | None) -> str:
    return str((identity or {}).get("type") or "unknown").lower()


def is_infrastructure(identity: dict | None) -> bool:
    return _identity_type(identity) in INFRASTRUCTURE_TYPES


def classify_counterparty(identity: dict | None) -> dict:
    """What a counterparty IS, with the label Helius gave it.

    `unknown` is the overwhelmingly common answer and is reported as
    such rather than as a gap — most addresses on any chain are nobody
    in particular, and treating that as missing data invites filling it.
    """
    ident = identity or {}
    t = _identity_type(ident)
    return {
        "address": ident.get("address"),
        "type": t,
        "name": ident.get("name"),
        "category": ident.get("category"),
        "is_infrastructure": t in INFRASTRUCTURE_TYPES,
        "known": t != "unknown",
    }


# ── Exchange flow (§26) ──────────────────────────────────────────────────

def exchange_flows(transfers: list[dict],
                   identities: dict[str, dict]) -> list[dict]:
    """Transfers whose counterparty is a known exchange.

    Direction is READ from the transfer, never inferred. An inflow is a
    deposit TO an exchange; an outflow is a withdrawal FROM one.

    Deliberately not called "selling". A deposit to an exchange is a
    precondition for selling, not evidence of it — wallets deposit to
    trade, to lend, to move between venues, and to sit. §26 says present
    it as probabilistic and that is what `implication` does.
    """
    out = []
    for t in transfers or []:
        cp = t.get("counterparty")
        if not cp:
            continue
        ident = identities.get(cp)
        if _identity_type(ident) != "exchange":
            continue
        direction = str(t.get("direction") or "").lower()
        if direction not in ("in", "out"):
            continue
        # transfer direction is relative to the WATCHED wallet: "out"
        # means the wallet sent to the exchange = an exchange inflow.
        flow = "exchange_inflow" if direction == "out" else "exchange_outflow"
        out.append({
            "flow": flow,
            "wallet": t.get("wallet"),
            "exchange": (ident or {}).get("name") or cp,
            "exchange_address": cp,
            "symbol": t.get("symbol") or t.get("mint"),
            "amount": _num(t.get("amount")),
            "usd_value": t.get("usd_value"),
            "signature": t.get("signature"),
            "timestamp": t.get("timestamp"),
            "implication": (
                "deposit to an exchange — a precondition for selling, not "
                "proof of it" if flow == "exchange_inflow" else
                "withdrawal from an exchange — often custody or a move to "
                "DeFi, not proof of accumulation"),
        })
    return out


# ── Whale detection (§27) ────────────────────────────────────────────────

def wallet_baseline(transfers: list[dict]) -> dict:
    """What 'normal' looks like for one wallet. Median, not mean — a
    single outlier must not redefine the baseline it is measured against.
    """
    amounts = sorted(_num(t.get("usd_value") if t.get("usd_value") is not None
                           else t.get("amount")) for t in transfers or [])
    amounts = [a for a in amounts if a > 0]
    n = len(amounts)
    if not n:
        return {"observations": 0, "median": None, "established": False}
    mid = n // 2
    median = amounts[mid] if n % 2 else (amounts[mid - 1] + amounts[mid]) / 2
    return {"observations": n, "median": median,
            "largest": amounts[-1],
            "established": n >= MIN_HISTORY_FOR_BASELINE}


def whale_score(transfer: dict, baseline: dict | None = None,
                usd_floor: float = WHALE_USD_FLOOR) -> dict:
    """Is this transfer big? Absolutely, and relative to this wallet.

    Both matter and they disagree usefully: a $2M move by a wallet that
    moves $2M weekly is unremarkable, and a $40k move by a wallet whose
    median is $300 is the more interesting event.
    """
    usd = transfer.get("usd_value")
    usd = _num(usd) if usd is not None else None
    reasons, score = [], 0.0

    if usd is not None and usd_floor > 0:
        if usd >= usd_floor:
            score += 60.0
            reasons.append(f"${usd:,.0f} clears the ${usd_floor:,.0f} floor")
        elif usd >= usd_floor / 2:
            score += 25.0
            reasons.append(f"${usd:,.0f} is within half the absolute floor")

    b = baseline or {}
    if b.get("established") and b.get("median"):
        amount = usd if usd is not None else _num(transfer.get("amount"))
        mult = amount / b["median"] if b["median"] else 0
        if mult >= WHALE_RELATIVE_MULTIPLE:
            score += 40.0
            reasons.append(
                f"{mult:.1f}x this wallet's median over {b['observations']} "
                f"transfers")
    elif b:
        # Say why the relative half is missing rather than scoring it zero
        # silently — an absent input is not a negative reading.
        reasons.append(
            f"no relative read: {b.get('observations', 0)} observations, "
            f"under the {MIN_HISTORY_FOR_BASELINE} needed for a baseline")

    if usd is None:
        reasons.append("no USD value available — size judged on token "
                       "amount only")

    return {"score": round(min(100.0, score), 1),
            "is_whale": score >= 60.0, "reasons": reasons}


# ── Accumulation / distribution (§6 BALANCE-AT) ──────────────────────────

def accumulation_score(balance_series: list[tuple[int, float]]) -> dict:
    """Direction and conviction of a position change over time.

    balance_series: (unix_ts, balance) oldest first, from balance-at.
    Returns a signed score: positive accumulating, negative distributing.
    """
    # `t is not None`, not `if t`: a timestamp of 0 is falsy and dropping
    # it silently shortens the series into "unknown". Same shape as the
    # MACD "none" string — a legitimate value that reads as absence.
    pts = [(t, _num(b)) for t, b in (balance_series or []) if t is not None]
    pts.sort(key=lambda p: p[0])
    if len(pts) < 2:
        return {"score": 0.0, "direction": "unknown", "reasons":
                ["need at least two balance observations"]}

    first, last = pts[0][1], pts[-1][1]
    days = max(1.0, (pts[-1][0] - pts[0][0]) / 86400.0)
    reasons = []

    if first <= 0 and last <= 0:
        return {"score": 0.0, "direction": "flat",
                "reasons": ["no position at either end of the window"]}
    if first <= 0:
        return {"score": 100.0, "direction": "accumulating", "change_pct": None,
                "reasons": [f"opened a new position over {days:.0f}d "
                            f"(no prior balance to compare)"]}

    change = (last - first) / first * 100.0
    reasons.append(f"{change:+.0f}% over {days:.0f}d "
                   f"({first:,.4g} -> {last:,.4g})")
    # Magnitude is capped: a 4,000% increase is not forty times the
    # conviction of a 100% one, it is a small position getting larger.
    score = max(-100.0, min(100.0, change))
    direction = ("accumulating" if change > 5 else
                 "distributing" if change < -5 else "flat")
    if direction == "flat":
        reasons.append("inside the +/-5% band that counts as holding")
    return {"score": round(score, 1), "direction": direction,
            "change_pct": round(change, 1), "reasons": reasons}


# ── Clustering (§8) and coordination (§30) ───────────────────────────────

def cluster_by_funder(funding: dict[str, dict],
                      identities: dict[str, dict] | None = None) -> list[dict]:
    """Group wallets by common funder, with the confidence that deserves.

    A cluster funded by a centralized exchange is not a cluster in any
    meaningful sense — Binance funds millions of wallets, and grouping on
    that would put half of Solana in one bucket. Those groups are still
    RETURNED, because suppressing them silently would hide why a wallet
    has no cluster, but their confidence is floored and the reason says so.
    """
    identities = identities or {}
    groups: dict[str, list[str]] = defaultdict(list)
    for wallet, rec in (funding or {}).items():
        funder = (rec or {}).get("funder")
        if funder:
            groups[funder].append(wallet)

    out = []
    for funder, members in groups.items():
        ident = identities.get(funder) or {
            "type": (funding[members[0]] or {}).get("funderType", "").lower()
            .replace("centralized exchange", "exchange") or "unknown",
            "name": (funding[members[0]] or {}).get("funderName"),
        }
        infra = is_infrastructure(ident)
        reasons = [f"{len(members)} wallets share funder "
                   f"{ident.get('name') or funder[:8]}"]
        if infra:
            confidence = 0.05
            reasons.append(
                f"funder is {ident.get('type')} infrastructure — it funds "
                f"vast numbers of unrelated wallets, so a shared origin "
                f"here is close to meaningless")
        elif len(members) < 2:
            confidence = 0.0
            reasons.append("a single wallet is not a cluster")
        else:
            # Even a non-infrastructure funder is weak evidence alone.
            confidence = min(0.5, 0.2 + 0.05 * len(members))
            reasons.append("funder is not a known exchange or protocol — "
                           "shared origin is suggestive, not conclusive")
        out.append({
            "funder": funder,
            "funder_name": ident.get("name"),
            "funder_type": ident.get("type"),
            "members": sorted(members),
            "size": len(members),
            "confidence": round(confidence, 2),
            "is_infrastructure_funder": infra,
            "reasons": reasons,
        })
    return sorted(out, key=lambda c: (-c["confidence"], -c["size"]))


def coordination_score(events: list[dict], window_s: int = 300) -> dict:
    """Did distinct wallets act on the same token inside a tight window?

    events: {wallet, symbol, direction, timestamp}. Returns a score with
    the participating wallets and the observed spread, and explicitly
    does NOT claim the wallets are related — only that their timing was
    close. Relation is what cluster evidence is for, and even then it is
    a confidence, not a fact.
    """
    by_token: dict[tuple, list[dict]] = defaultdict(list)
    for e in events or []:
        sym, ts = e.get("symbol"), e.get("timestamp")
        if not sym or ts is None:
            continue
        by_token[(sym, str(e.get("direction") or "").lower())].append(e)

    best = {"score": 0.0, "reasons": ["no clustered activity observed"],
            "groups": []}
    groups = []
    for (sym, direction), evs in by_token.items():
        evs.sort(key=lambda e: e["timestamp"])
        for i, anchor in enumerate(evs):
            window = [e for e in evs[i:]
                      if e["timestamp"] - anchor["timestamp"] <= window_s]
            wallets = {e.get("wallet") for e in window if e.get("wallet")}
            if len(wallets) < 3:
                continue
            spread = window[-1]["timestamp"] - anchor["timestamp"]
            groups.append({
                "symbol": sym, "direction": direction,
                "wallets": sorted(wallets), "wallet_count": len(wallets),
                "window_seconds": spread,
                "started_at": anchor["timestamp"],
            })
            break

    if not groups:
        return best
    groups.sort(key=lambda g: (-g["wallet_count"], g["window_seconds"]))
    top = groups[0]
    # More wallets and a tighter window both raise it; neither alone is
    # enough to call anything.
    tightness = 1.0 - (top["window_seconds"] / window_s if window_s else 0)
    score = min(100.0, 20.0 * top["wallet_count"] * max(0.3, tightness))
    return {
        "score": round(score, 1),
        "groups": groups,
        "reasons": [
            f"{top['wallet_count']} distinct wallets moved {top['symbol']} "
            f"{top['direction']} within {top['window_seconds']}s",
            "timing proximity only — this is not evidence the wallets are "
            "related, and unrelated wallets react to the same news",
        ],
    }


# ── Smart money (§28) ────────────────────────────────────────────────────

def smart_money_score(profile: dict) -> dict:
    """Evidence-based skill score. Size is deliberately not an input.

    profile keys, all optional — an absent input contributes nothing
    rather than counting against the wallet:
      realized_trades, win_rate, avg_return_pct, early_entry_rate,
      wallet_age_days, distinct_tokens, max_drawdown_pct
    """
    p = profile or {}
    score, reasons = 0.0, []

    trades = int(_num(p.get("realized_trades")))
    if trades < 10:
        return {"score": 0.0, "confidence": "insufficient",
                "reasons": [f"{trades} resolved trades — too few to claim "
                            f"anything about skill"]}

    win = p.get("win_rate")
    if win is not None:
        w = _num(win)
        if w > 50:
            score += min(25.0, (w - 50) * 1.5)
            reasons.append(f"{w:.0f}% win rate over {trades} resolved trades")
        else:
            reasons.append(f"{w:.0f}% win rate — no credit below even")

    ret = p.get("avg_return_pct")
    if ret is not None:
        r = float(ret)
        # Return carries more weight than win rate on purpose: this desk
        # has already learned that optimizing win rate alone selects for
        # small gains and unbounded losses.
        if r > 0:
            score += min(35.0, r * 1.5)
            reasons.append(f"{r:+.1f}% average return per resolved trade")
        else:
            score -= min(20.0, abs(r))
            reasons.append(f"{r:+.1f}% average return — negative expectancy")

    early = p.get("early_entry_rate")
    if early is not None:
        e = _num(early)
        score += min(20.0, e * 0.2)
        reasons.append(f"entered early on {e:.0f}% of positions")

    age = _num(p.get("wallet_age_days"))
    if age >= 180:
        score += 10.0
        reasons.append(f"{age:.0f}-day history — survived more than one "
                       f"market condition")
    elif age:
        reasons.append(f"only {age:.0f} days old — no credit for a record "
                       f"set in one regime")

    dd = p.get("max_drawdown_pct")
    if dd is not None and _num(dd) < 40:
        score += 10.0
        reasons.append(f"max drawdown {_num(dd):.0f}% — position sizing held")

    tokens = int(_num(p.get("distinct_tokens")))
    if tokens and tokens < 3:
        score *= 0.7
        reasons.append(f"only {tokens} distinct tokens — a record this "
                       f"narrow may be one lucky position")

    score = max(0.0, min(100.0, score))
    confidence = ("high" if trades >= 50 else
                  "medium" if trades >= 25 else "low")
    return {"score": round(score, 1), "confidence": confidence,
            "resolved_trades": trades, "reasons": reasons}


def copy_trade_candidate(trade: dict, wallet_score: dict,
                         token_risk: dict | None = None) -> dict:
    """A DETECTED trade becomes a CANDIDATE — never an execution.

    §29 is explicit that these stages stay separate, and this desk has
    its own reason to insist: every execution path here is risk-first and
    goes through the existing engines. This returns a proposal for those
    engines to judge, and deliberately carries no size.
    """
    reasons = list(wallet_score.get("reasons") or [])
    ok = True
    if wallet_score.get("confidence") == "insufficient":
        ok = False
        reasons.append("wallet has no established record")
    elif _num(wallet_score.get("score")) < 60:
        ok = False
        reasons.append(f"wallet scores {_num(wallet_score.get('score')):.0f}, "
                       f"under the 60 needed to follow")

    risk = token_risk or {}
    if risk.get("holder_concentration_pct") is not None:
        conc = _num(risk["holder_concentration_pct"])
        if conc > 50:
            ok = False
            reasons.append(f"top holders control {conc:.0f}% of supply")

    return {
        "stage": "COPY_CANDIDATE" if ok else "REJECTED",
        "wallet": trade.get("wallet"),
        "symbol": trade.get("symbol") or trade.get("mint"),
        "direction": trade.get("direction"),
        "observed_amount": _num(trade.get("amount")),
        "signature": trade.get("signature"),
        "wallet_score": _num(wallet_score.get("score")),
        "reasons": reasons,
        # Stated rather than implied: nothing downstream may read this as
        # an approval, and it carries no size for anything to act on.
        "note": ("a candidate for the risk engine to judge; not an approved "
                 "or sized trade"),
    }
