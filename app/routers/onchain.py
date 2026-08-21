"""On-chain desk routes — wallet intelligence, token surge, DEX book, lending.

Six modules had been built and tested with no way to reach them from the
browser: dex_paper, dex_swap_math, wallet_discovery, token_surge,
capital_stake and capital_lending. Intelligence that cannot be seen changes
no decision, which is the same complaint §4 makes about a silent failure —
the work exists and the operator cannot tell.

Every route here returns PROVENANCE alongside its numbers. On-chain values
carry very different weights: an obligation decoded by the canonical Kamino
layout is verified, a liquidation distance derived from it is calculated,
and a cascade estimate is modelled. Collapsing those into one confident
number is how a model starts being read as a measurement.
"""
from fastapi import APIRouter, Body

from app.routers.common import *  # noqa: F401,F403

router = APIRouter()


# ── Wallet intelligence polling ──────────────────────────────────────────
@router.get("/onchain/wallet-polling")
def onchain_wallet_polling():
    """Whether Helius wallet intelligence is polling, and what it last did.

    Read-only. Carries no wallet address, no key and no credential-bearing
    URL — provider error text is redacted before it reaches this payload,
    because the upstream collector prefixes its own failure messages with
    eight characters of a real wallet.

    Counts are null rather than zero when a pass did not look: "we could not
    reach the provider" and "we looked and the chain was quiet" are
    different facts, and only one of them is about the market.
    """
    from lib import wallet_poller

    return wallet_poller.status()


# ── Wallet shadow intelligence ───────────────────────────────────────────
def _current_population(model):
    """Everything currently monitored, whatever it is monitored for."""
    from sqlalchemy import or_
    return or_(model.status.in_(("WATCH", "SMART_MONEY", "HIGH_CONVICTION")),
               model.pinned.is_(True),
               model.monitoring_purpose == "FLOW_CONTEXT")


def _current(model):
    """Only observations that still stand.

    A SUPERSEDED row holds the same signatures under a reading that full
    transaction evidence has since corrected. Counting it beside its
    replacement would be the double vote the cluster rules exist to
    prevent, so every read path filters on this one predicate.
    """
    from sqlalchemy import or_
    return or_(model.revision_state == "CURRENT",
               model.revision_state.is_(None))


def _abbrev_mint(mint):
    """A mint the operator can recognise; the full value ships separately so
    a copy control can offer it deliberately rather than the table leaking
    it into every screenshot."""
    if not mint:
        return None
    m = str(mint)
    return f"{m[:5]}…{m[-4:]}" if len(m) > 12 else m


def _validation(r) -> dict:
    """The DERIVED reading of a stored decision. Nothing is rewritten."""
    from lib import wallet_shadow_intel as SI

    v = SI.validation_state(getattr(r, "state", None),
                            getattr(r, "copyability_state", None))
    return {
        "validation_state": v,
        "validated": v == SI.VALIDATED_ELIGIBLE,
        "provisional": v == SI.PROVISIONAL_ELIGIBLE,
        "gate_state": getattr(r, "state", None),
        "copyability_state": getattr(r, "copyability_state", None),
        "copyability_reason": getattr(r, "copyability_reason", None),
        "behaviour_state": getattr(r, "behaviour_state", None),
    }


def _event_row(r, *, full_mint: bool = False) -> dict:
    import json as _json

    def _load(v, default=None):
        try:
            return _json.loads(v) if v else default
        except (TypeError, ValueError):
            return default

    return {
        "id": r.id,
        "cluster_id": r.cluster_id,
        "source": r.source,
        "execution_mode": r.execution_mode,
        "event_type": r.event_type,
        "classification": r.classification,
        "evidence_quality": r.evidence_quality,
        "classification_reason": r.classification_reason,
        "schema_compatibility": r.schema_compatibility,
        "direction": r.direction,
        # DISPLAY vs IDENTITY. The abbreviation is for the table; the full
        # mint is offered explicitly, and a ticker is never either.
        "mint_abbrev": _abbrev_mint(r.subject_mint),
        "mint": r.subject_mint if full_mint else None,
        # A MINT IS NOT A SYMBOL. `parse_transfers` falls back to the mint
        # when the feed reports no symbol — which is most SPL tokens — so
        # passing it through as `symbol` put the FULL 44-character address
        # in a display column that was meant to be abbreviated. Null it, and
        # the client falls back to `mint_abbrev` as intended.
        "symbol": (r.subject_symbol
                   if r.subject_symbol and r.subject_symbol != r.subject_mint
                   else None),
        "chain": r.chain,
        "subject_amount": r.subject_amount,
        "quote_symbol": r.quote_symbol,
        "quote_amount": r.quote_amount,
        "notional_usd": r.notional_usd,
        # Safe labels only — never a full wallet address.
        "wallets": _load(r.wallets_json, []),
        "wallet_count": r.wallet_count,
        "signature_count": r.signature_count,
        "leg_count": r.leg_count,
        "event_time": r.event_time,
        "observed_at": r.observed_at,
        "state": r.state,
        "refusal_reason": r.refusal_reason,
        "eligibility_reason": r.eligibility_reason,
        "reference_price_usd": r.reference_price_usd,
        "reference_price_source": r.reference_price_source,
        "reference_price_at": r.reference_price_at,
        "wallet_quality": _load(r.wallet_quality_json),
        "market_context": _load(r.market_context_json),
        "expected_cost": _load(r.expected_cost_json),
        "thesis_id": r.thesis_id,
        "horizons": _load(r.horizons_json, []),
        "model_version": r.model_version,
        "classifier_version": r.classifier_version,
    }


@router.get("/onchain/shadow/summary")
def onchain_shadow_summary():
    """Source-isolated Helius wallet intelligence, and the polling behind it.

    SHADOW ONLY — no order was ever submitted. Kept apart from JARVIS
    execution, manual operator results and both virtual books.
    """
    from lib import wallet_poller, wallet_shadow_intel as SI

    perf = SI.performance()
    return {
        **perf,
        "polling": wallet_poller.status(),
        "policy": {
            "cluster_window_seconds": SI.CLUSTER_WINDOW_SECONDS,
            "price_max_age_seconds": SI.PRICE_MAX_AGE_SECONDS,
            "min_notional_usd": SI.MIN_NOTIONAL_USD,
            "min_liquidity_usd": SI.MIN_LIQUIDITY_USD,
            "horizons": list(SI.HORIZONS),
        },
    }


@router.get("/onchain/shadow/events")
def onchain_shadow_events(state: str = None, event_type: str = None,
                          limit: int = 50):
    """Recent classified economic events — eligible AND refused.

    Refused events are first-class here: they are how the operator sees
    which evidence is missing rather than watching rows disappear.
    """
    from app.database import WalletShadowEvent, get_db

    with get_db() as db:
        q = db.query(WalletShadowEvent).filter(_current(WalletShadowEvent))
        if state:
            q = q.filter(WalletShadowEvent.state == state.upper())
        if event_type:
            q = q.filter(WalletShadowEvent.event_type == event_type.upper())
        rows = q.order_by(WalletShadowEvent.event_time.desc()).limit(
            max(1, min(int(limit), 200))).all()
        out = [_event_row(r) for r in rows]
    return {"events": out, "count": len(out),
            "source": "HELIUS_WALLET_INTELLIGENCE",
            "execution_mode": "SHADOW",
            "disclaimer": "SHADOW INTELLIGENCE — NO ORDER SUBMITTED"}


@router.get("/onchain/shadow/theses")
def onchain_shadow_theses(limit: int = 50):
    """Eligible shadow theses, with their forward checkpoints.

    An empty list is a real answer — it means every event was refused, and
    `/onchain/shadow/summary` says exactly why.
    """
    from app.database import (WalletShadowEvent, WalletShadowOutcome, get_db)

    with get_db() as db:
        rows = db.query(WalletShadowEvent).filter(
            _current(WalletShadowEvent),
            WalletShadowEvent.state == "ELIGIBLE").order_by(
            WalletShadowEvent.event_time.desc()).limit(
            max(1, min(int(limit), 200))).all()
        out = []
        for r in rows:
            item = _event_row(r, full_mint=True)
            # DERIVED, never stored: the gate's own decision stays
            # exactly as it was recorded.
            item.update(_validation(r))
            item["outcomes"] = [{
                "horizon": o.horizon, "due_at": o.due_at,
                "status": o.status,
                "checkpoint_at": o.checkpoint_at,
                "checkpoint_price_usd": o.checkpoint_price_usd,
                "gross_return_pct": o.gross_return_pct,
                "estimated_cost_pct": o.estimated_cost_pct,
                "net_return_pct": o.net_return_pct,
                "unresolved_reason": o.unresolved_reason,
            } for o in db.query(WalletShadowOutcome).filter(
                WalletShadowOutcome.event_id == r.id).all()]
            out.append(item)
    return {"theses": out, "count": len(out),
            "execution_mode": "SHADOW",
            "disclaimer": "SHADOW INTELLIGENCE — NO ORDER SUBMITTED"}


@router.get("/onchain/shadow/refusals")
def onchain_shadow_refusals(limit: int = 12):
    """Why events did not become theses, most common first."""
    from sqlalchemy import text

    from app.database import engine
    from lib import wallet_shadow_intel as SI

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT refusal_reason, COUNT(*) c, MAX(event_time) latest "
            "FROM wallet_shadow_events WHERE state='REFUSED' "
            "  AND (revision_state = 'CURRENT' OR revision_state IS NULL) "
            "GROUP BY refusal_reason ORDER BY c DESC LIMIT :lim"),
            {"lim": max(1, min(int(limit), 50))}).fetchall()
    return {"refusals": [{"reason": r[0] or "UNKNOWN", "count": r[1],
                          "latest_event_time": r[2]} for r in rows],
            "vocabulary": list(SI.REFUSAL_REASONS)}


@router.post("/onchain/shadow/process")
def onchain_shadow_process(limit: int = 20000, max_clusters: int = 2000):
    """Re-classify stored observations. IDEMPOTENT — keyed on cluster id.

    Read-only against every provider: it re-reads observations already
    collected and writes only shadow rows. No order, no position, no cash.
    """
    from lib import wallet_shadow_intel as SI

    stats = SI.process(limit=limit, max_clusters=max_clusters)
    stats["outcomes"] = SI.resolve_outcomes(limit=2000)
    return stats


# ── Wallet registry ──────────────────────────────────────────────────────
@router.get("/onchain/wallets/roles")
def onchain_wallet_roles(limit: int = 60):
    """The monitored population, with what each wallet is watched FOR.

    Safe labels only — the address never leaves the database.
    """
    from app.database import WalletRegistry, get_db
    from lib import wallet_registry as WR
    from lib.wallet_shadow_intel import safe_label

    with get_db() as db:
        rows = (db.query(WalletRegistry)
                .filter(_current_population(WalletRegistry))
                .order_by(WalletRegistry.status.desc(),
                          WalletRegistry.smart_money_score.desc())
                .limit(max(1, min(limit, 200))).all())
        out = [{
            "wallet": safe_label(w.address),
            "status": w.status,
            "purpose": w.monitoring_purpose or "EVIDENCE_COLLECTION",
            "purpose_reason": w.monitoring_reason,
            "entity_type": w.entity_type,
            "behaviour": w.behaviour_state,
            "copyability": w.copyability_state,
            "score": w.smart_money_score,
            "score_source": w.score_source,
            "sample_count": w.sample_count,
            "history_records": w.history_records_loaded,
            "history_complete": bool(w.history_backfill_complete),
            "pinned": bool(w.pinned),
            "behaviour_at": w.behaviour_at,
            "last_score_update": w.last_score_update,
        } for w in rows]
    counts: dict = {}
    for r in out:
        counts[r["purpose"]] = counts.get(r["purpose"], 0) + 1
    from lib import provider_health as PH
    cap = PH.should_probe("helius", "wallet_batch_identity")
    return {
        "wallets": out, "counts": counts,
        "purposes": sorted(WR.MONITORING_PURPOSES),
        "identity_capability": {
            "status": cap.get("status"),
            "http_status": cap.get("last_http_status"),
            "last_probe_at": cap.get("last_attempt_at"),
            "next_probe_at": cap.get("next_probe_at"),
            "message": ("Helius Wallet Identity is unavailable on the current "
                        "plan. Wallet identity remains unresolved; behavioural "
                        "evidence is used for provisional copyability only."
                        if cap.get("status") == PH.PLAN_FORBIDDEN else
                        cap.get("reason")),
        },
        "note": ("ALPHA wallets may source a shadow pick. FLOW_CONTEXT "
                 "wallets are real activity that is not copyable alpha — "
                 "still observed, never a pick. EVIDENCE_COLLECTION is not "
                 "yet characterised. A behavioural finding is never an "
                 "identity claim"),
    }


@router.get("/onchain/intel/cycle")
def onchain_intel_cycle():
    """Everything the completed cycle produced, in one read.

    Four sections because they fail INDEPENDENTLY and the desk has to say
    which one is holding everything up: the cycle itself, the swap
    evidence, wallet-score coverage, and price coverage. A section that
    cannot be measured reports UNAVAILABLE rather than zeroes.
    """
    from lib import wallet_intel_cycle, wallet_price_snapshots
    from lib import wallet_scoring, wallet_swap_enrichment

    def _safe(fn, label):
        try:
            return fn()
        except Exception as e:                               # noqa: BLE001
            return {"state": "UNAVAILABLE", "detail": f"{label}: {e}"[:200]}

    out = {
        "cycle": _safe(wallet_intel_cycle.status, "cycle"),
        "swap_evidence": _safe(wallet_swap_enrichment.coverage, "enrichment"),
        "wallet_scoring": _safe(wallet_scoring.coverage, "scoring"),
        "price_coverage": _safe(wallet_price_snapshots.coverage, "prices"),
        "quote_series": _safe(wallet_price_snapshots.quote_series_state,
                              "quote series"),
    }
    out["note"] = ("the cycle runs at the end of each wallet poll. It reads "
                   "chain and market data and writes shadow intelligence "
                   "only — no order, no signing, no cash movement. A null "
                   "count means that stage did not look")
    return out


@router.post("/onchain/intel/cycle/run")
def onchain_intel_cycle_run(full: bool = False):
    """Run one cycle NOW. DIAGNOSTIC ONLY — normal operation is automatic.

    The desk does not need this: the poller runs the same cycle on its own
    timer. It exists so an operator can force a pass after a code change
    without waiting out the interval.
    """
    from lib import wallet_intel_cycle

    return wallet_intel_cycle.run_once(full=bool(full))


@router.get("/onchain/wallets")
def onchain_wallets(status: str = None, limit: int = 100):
    """The wallet universe, with its lifecycle state.

    Seeds are NOT presented as smart money — they are unproven candidates
    that happen to have been supplied by hand, and the registry keeps that
    distinction because the whole point is to MEASURE which wallets earn
    the label rather than assume it.
    """
    from app.database import WalletRegistry, get_db
    from lib.wallet_registry import counts

    with get_db() as db:
        q = db.query(WalletRegistry)
        if status:
            q = q.filter(WalletRegistry.status == status.upper())
        q = q.order_by(WalletRegistry.smart_money_score.desc().nullslast(),
                       WalletRegistry.first_discovered_at.desc())
        rows = q.limit(max(1, min(limit, 500))).all()
        out = [{
            "address": w.address,
            "status": w.status,
            "source": w.source,
            "pinned": bool(w.pinned),
            "entity_type": w.entity_type,
            "entity_name": w.entity_name,
            "is_trader": w.is_trader,
            "discovery_reason": w.discovery_reason,
            "first_discovered_at": w.first_discovered_at,
            # None means NOT YET MEASURED, never zero. A wallet with no
            # score has not been analysed; showing 0 would rank it as bad.
            "whale_score": w.whale_score,
            "smart_money_score": w.smart_money_score,
            "alpha_score": w.alpha_score,
            "copy_score": w.copy_score,
            "confidence_score": w.confidence_score,
        } for w in rows]
    return {"wallets": out, "counts": counts(),
            "note": ("Scores are null until a wallet has been analysed. Null "
                     "is 'not yet measured', not zero — an unscored wallet "
                     "is unknown, not bad.")}


@router.get("/onchain/discovery/status")
def discovery_status():
    """Is autonomous discovery actually running, and what has it found?"""
    from app.database import WalletRegistry, get_db
    from lib.wallet_registry import counts, discovery_enabled

    with get_db() as db:
        by_source = {}
        for w in db.query(WalletRegistry).all():
            by_source[w.source or "unknown"] = by_source.get(w.source or "unknown", 0) + 1
    try:
        from app.scheduler import job_status
        job = job_status.get("wallet_discovery") or {}
    except Exception:
        job = {}
    return {"enabled": discovery_enabled(), "counts": counts(),
            "by_source": by_source,
            "last_run": job.get("last"), "job_status": job.get("status"),
            "job_error": job.get("error")}


@router.post("/onchain/discovery/run")
def discovery_run(max_tokens: int = 5):
    """Run one discovery pass now. Bounded — each token costs 2 RPC calls."""
    from lib.wallet_discovery import discover_from_tokens
    return discover_from_tokens(max_tokens=max(1, min(max_tokens, 20)))


# ── Token surge ──────────────────────────────────────────────────────────
@router.get("/onchain/surge")
def token_surge(limit: int = 20):
    """Tokens becoming unusually active FOR THEMSELVES.

    Ranked by acceleration against each token's own baseline, not by size —
    a pair doing $2M every day is not news, and one that went from $5k to
    $500k in an hour is.
    """
    from lib.token_surge import enabled, scan_and_score

    # ONE pipeline. This route used to call `score_snapshot(snap, [])` — an
    # empty history literal — so every token was permanently scored by the
    # new-token model, and the rigorous baseline machinery it was calling
    # never had anything to be rigorous about. scan_and_score persists each
    # observation and scores against the token's own stored history, and
    # wallet discovery reads the same result.
    # READ-ONLY. This route used to run the pass with persistence ON, so a
    # dashboard refresh wrote a TokenActivitySnapshot, advanced the baseline
    # and updated surge state. The operator's refresh rate then became an
    # input to the model: leaving the tab open on a 10-second poll would
    # manufacture a dense baseline, and closing it would starve one. How
    # often someone LOOKS at a detector must never change what it measures.
    #
    # Ingestion is owned by the scheduled `token_surge` sampler, on a fixed
    # cadence. Scoring here is identical — only the storage is skipped — and
    # baseline_quality still reports honestly because history is read either
    # way.
    result = scan_and_score(limit=max(1, min(limit, 100)), persist=False)
    return {"enabled": enabled(), "read_only": True,
            "ingestion": "scheduled sampler owns snapshots and surge state",
            **result}


# ── Virtual DEX book ─────────────────────────────────────────────────────
@router.get("/onchain/dex/book")
def dex_book():
    """The simulated on-chain book — separate from paper and Auto Sim."""
    from app.database import DexPosition, get_db
    from lib.dex_paper import summary

    # ONE SESSION FOR THE WHOLE SNAPSHOT.
    #
    # This read positions in one session and then called summary(), which
    # opened a SECOND — so the position rows and the equity that supposedly
    # described them came from two different instants and were returned as
    # one atomic book. Harmless while nothing else wrote; wrong as soon as
    # autonomous DEX trading writes concurrently, and wrong in exactly the
    # way that is hardest to reproduce.
    with get_db() as db:
        rows = db.query(DexPosition).filter(DexPosition.status == "Open").all()
        positions = [{
            "id": p.id, "mint": p.mint, "symbol": p.symbol, "dex": p.dex,
            "qty_tokens": p.qty_tokens,
            "entry_price_usd": p.entry_price_usd,
            "quoted_price_usd": p.quoted_price_usd,
            "current_price_usd": p.current_price_usd,
            "notional_usd": p.notional_usd,
            # Kept separate on purpose: the pool's price, your own size, and
            # the chain each call for a different remedy.
            "entry_impact_pct": p.entry_impact_pct,
            "entry_pool_fee_usd": p.entry_pool_fee_usd,
            "entry_network_fee_usd": p.entry_network_fee_usd,
            "pool_reserve_usd_at_entry": p.pool_reserve_usd_at_entry,
            "exit_state": p.exit_state,
            "exit_blocked_reason": p.exit_blocked_reason,
            "opened_at": p.opened_at, "notes": p.notes,
        } for p in rows]
        book = summary(db)

    # Each position carries its own exit economics inline, so the UI never
    # has to join two lists to answer "what is this actually worth".
    by_id = {v["position_id"]: v for v in book.get("positions_valuation", [])}
    for pos in positions:
        val = by_id.get(pos["id"])
        if val:
            pos.update({k: v for k, v in val.items()
                        if k not in ("position_id", "symbol", "mint",
                                     "qty_tokens", "notional_usd",
                                     "exit_state", "exit_blocked_reason")})
    return {**book, "positions": positions}


@router.get("/onchain/dex/quote")
def dex_quote(amount_usd: float, reserve_usd: float, dex: str = None,
              sol_price_usd: float = 0.0, concentrated: bool = False):
    """Price a swap before taking it, with every cost itemised."""
    from lib.dex_swap_math import (max_size_for_impact, quote_swap,
                                   round_trip_cost)
    q = quote_swap(amount_usd, reserve_usd, dex=dex,
                   sol_price_usd=sol_price_usd, concentrated=concentrated)
    if not q.get("ok"):
        return q
    rt = round_trip_cost(amount_usd, reserve_usd, dex=dex,
                         sol_price_usd=sol_price_usd, concentrated=concentrated)
    return {**q,
            "round_trip_cost_pct": rt.get("round_trip_cost_pct"),
            "breakeven_move_pct": rt.get("breakeven_move_pct"),
            "max_size_1pct_impact": max_size_for_impact(reserve_usd, 1.0, dex=dex),
            "max_size_2pct_impact": max_size_for_impact(reserve_usd, 2.0, dex=dex)}


@router.get("/onchain/dex/trades")
def dex_trades(limit: int = 50):
    """Closed swaps, newest first — the book's actual record.

    Costs are itemised rather than netted into one P&L figure, because on
    a DEX they fail differently: price impact is a function of YOUR size
    against pool depth, the pool fee is the venue's cut, and the network
    fee is the chain's. Collapsing them hides which one made a trade
    unprofitable.
    """
    from app.database import DexTrade, get_db
    from lib.dex_contracts import closed_trades_payload

    with get_db() as db:
        rows = (db.query(DexTrade)
                .order_by(DexTrade.closed_at.desc())
                .limit(max(1, min(limit, 500))).all())
        # Serialized inside the session: these are ORM instances, and
        # reading an attribute after the context manager closes raises
        # DetachedInstanceError rather than returning None.
        return closed_trades_payload(rows)


@router.post("/onchain/dex/open")
def dex_open(payload: dict = Body(...)):
    """Simulate a buy against real pool depth.

    Every refusal names itself — "impact 4.2% exceeds the 3.0% ceiling" is
    actionable, an empty response is not.
    """
    from lib.dex_paper import open_dex_position

    required = ("mint", "reserve_usd", "price_usd")
    missing = [k for k in required if payload.get(k) in (None, "")]
    if missing:
        return {"error": f"missing required field(s): {', '.join(missing)}"}

    return open_dex_position(
        mint=str(payload["mint"]),
        symbol=payload.get("symbol"),
        pool_address=payload.get("pool_address"),
        dex=payload.get("dex"),
        reserve_usd=float(payload["reserve_usd"]),
        price_usd=float(payload["price_usd"]),
        size_usd=(float(payload["size_usd"])
                  if payload.get("size_usd") not in (None, "") else None),
        stop_price_usd=(float(payload["stop_price_usd"])
                        if payload.get("stop_price_usd") not in (None, "") else None),
        target_price_usd=(float(payload["target_price_usd"])
                          if payload.get("target_price_usd") not in (None, "") else None),
        sol_price_usd=float(payload.get("sol_price_usd") or 0.0),
        concentrated=bool(payload.get("concentrated")),
    )


@router.post("/onchain/dex/close")
def dex_close(payload: dict = Body(...)):
    """Simulate the sell, priced against pool depth on the way OUT too.

    Getting in cheaply and being unable to get out is the characteristic
    on-chain failure, so exit impact is quoted rather than assumed.
    """
    from lib.dex_paper import close_dex_position

    if not payload.get("position_id"):
        return {"error": "position_id is required"}
    if payload.get("price_usd") in (None, ""):
        return {"error": "price_usd is required — the exit needs a price"}

    # THE ECONOMIC ACTION IS DECLARED, NOT INFERRED. An urgent exit may
    # rationally outbid a normal one, so the caller names which this is and
    # the fee ceiling follows from that — never from how aggressively the
    # bid happens to land. An unrecognised action is refused by
    # close_dex_position rather than quietly widening a ceiling.
    return close_dex_position(
        str(payload["position_id"]),
        float(payload["price_usd"]),
        reserve_usd=(float(payload["reserve_usd"])
                     if payload.get("reserve_usd") not in (None, "") else None),
        reason=str(payload.get("reason") or "manual"),
        sol_price_usd=float(payload.get("sol_price_usd") or 0.0),
        concentrated=bool(payload.get("concentrated")),
        exit_action=str(payload.get("exit_action") or "NORMAL_EXIT"),
    )


@router.get("/onchain/dex/sizing")
def dex_sizing(reserve_usd: float, cash_usd: float | None = None):
    """The size this pool can absorb, and why it is bounded there.

    On-chain the binding constraint is POOL DEPTH, not account equity — a
    book that sizes off cash alone will happily propose a trade the pool
    cannot fill without moving the price against itself.
    """
    from lib.dex_paper import (max_impact_pct, min_pool_reserve_usd,
                               size_for_pool, summary)

    cash = cash_usd if cash_usd is not None else summary().get("cash_usd", 0.0)
    out = size_for_pool(float(reserve_usd), float(cash))
    return {**out,
            "reserve_usd": reserve_usd, "cash_usd": cash,
            "limits": {"max_impact_pct": max_impact_pct(),
                       "min_pool_reserve_usd": min_pool_reserve_usd()},
            "provenance": "CALCULATED from pool reserve and constant-product math"}


# ── Kamino: the book, and what it would take to break a position ─────────
@router.get("/onchain/sweep")
def kamino_book_sweep(limit: int = 40, min_debt: float | None = None):
    """The whole Kamino book, ranked by what MATTERS rather than by size.

    `lib/kamino_sweep` had no route. A $42M position 2% from liquidation
    owned by a measured high-alpha wallet is a different event from the
    same position owned by an unknown one, and ranking by collateral alone
    cannot express that.

    The registry overlap is reported even though it is currently zero: that
    fact is informative, not a failure. Discovery finds wallets by TOKEN
    ACTIVITY and this finds them by BORROWING, which are different
    populations — a profitable spot trader need never touch a lending
    market. Claiming coverage it does not have would be worse.
    """
    from lib.kamino_sweep import (band_summary, join_wallet_registry,
                                  min_debt_usd, rank_by_significance,
                                  sweep_obligations)

    swept = sweep_obligations(min_debt=min_debt)
    positions = swept.get("positions") or []
    if not positions:
        return {**swept, "ranked": [], "bands": [], "registry": {},
                "detail": swept.get("error") or
                          "no obligations above the debt floor"}

    joined = join_wallet_registry(positions)
    ranked = rank_by_significance(
        positions, limit=max(1, min(limit, 200)),
        wallet_scores=joined.get("scores") or {})
    return {
        "scanned": swept.get("scanned"),
        "positions": len(positions),
        "min_debt_usd": min_debt if min_debt is not None else min_debt_usd(),
        "ranked": ranked,
        "bands": band_summary(positions),
        "registry": {k: v for k, v in joined.items() if k != "scores"},
        "provenance": {
            "position_values": "VERIFIED — canonical Kamino decode",
            "significance": "CALCULATED — size, proximity and wallet quality",
        },
    }


@router.get("/onchain/stress/{obligation}")
def obligation_stress(obligation: str, days: int = 0,
                      stable_depeg_pct: float = 0.0,
                      sol_price_usd: float = 0.0):
    """The stress matrix for ONE obligation, on three independent axes.

    `lib/liquidation_matrix` had no route either. A single "distance to
    liquidation" number answers one question badly, because the boundary
    MOVES — with SOL, with LST basis, with the borrowed stable's own price,
    and with time and carry.

    `stable_depeg_pct` is a SELECTOR rather than a fourth grid dimension:
    the principal matrix stays SOL x LST-depeg (which is readable), and the
    stable axis picks which slice of it you are looking at. Positive means
    the stable trades ABOVE par, which is adverse for a borrower and
    favourable for a holder, and it is applied to BOTH sides so a
    stable-collateral/stable-debt position nets out instead of being
    shocked in one direction only.
    """
    from lib.capital_lending import obligation_by_address
    from lib.liquidation_matrix import (DEFAULT_STABLE_DEPEG_SHOCKS,
                                        liquidation_boundary,
                                        position_risk_report,
                                        stable_depeg_sensitivity,
                                        stress_matrix)

    try:
        position = obligation_by_address(obligation, sol_price_usd=sol_price_usd)
    except Exception as e:
        return {"available": False,
                "reason": f"could not decode {obligation[:8]}…: {type(e).__name__}"}
    if not position:
        return {"available": False,
                "reason": f"no obligation found at {obligation[:8]}…"}

    report = position_risk_report(position, carry_by_scenario={})
    if not report.get("available"):
        return report

    return {
        **report,
        "obligation": obligation,
        "matrix": stress_matrix(position, days=days,
                                stable_depeg_pct=stable_depeg_pct),
        "stable_axis": stable_depeg_sensitivity(position, days=days),
        "stable_shocks_available": list(DEFAULT_STABLE_DEPEG_SHOCKS),
        "boundary_at_selected_depeg": liquidation_boundary(
            position, days=days, stable_depeg_pct=stable_depeg_pct),
        "selected_stable_depeg_pct": stable_depeg_pct,
    }


# ── Capital: staking and lending ─────────────────────────────────────────
@router.get("/onchain/stake/{wallet}")
def wallet_stake(wallet: str, sol_price_usd: float = 0.0):
    """Native staking, and whether capital is becoming liquid."""
    from lib.capital_stake import capital_liquidity_signal, stake_accounts_for
    stake = stake_accounts_for(wallet)
    return {**stake, "signal": capital_liquidity_signal(stake, sol_price_usd),
            "provenance": "VERIFIED on-chain (RPC-parsed stake accounts)"}


@router.get("/onchain/lending/{wallet}")
def wallet_lending(wallet: str):
    """Kamino positions for one wallet, decoded canonically."""
    from lib.capital_lending import (DECODER_SOURCE, DECODER_VERSION,
                                     obligations_for)
    positions = obligations_for(wallet)
    # Assets are now NAMED: reserve decoding resolves each deposit and
    # borrow to its mint, decimals, oracle price and liquidation threshold.
    for p in positions:
        raw = p.pop("_raw", None)
        if raw:
            from lib.capital_reserves import name_positions
            p["assets"] = name_positions(raw)
    return {"wallet": wallet, "positions": positions,
            "count": len(positions),
            "decoder": {"source": DECODER_SOURCE, "version": DECODER_VERSION},
            "provenance": {
                "position_values": "VERIFIED — canonical Kamino layout",
                "health_factor": "CALCULATED from Kamino's own rule",
                "asset_identity": "VERIFIED — canonical Kamino reserve layout",
                "prices": ("VERIFIED — Kamino's reserve oracle, not exchange "
                           "spot: liquidation is decided by the protocol's "
                           "own price"),
                "collateral_amounts": ("deposits are cTOKENS; the underlying "
                                       "figure is derived from Kamino's own "
                                       "position value, not amount x price"),
                "liquidation_price": ("UNAVAILABLE for multi-collateral "
                                      "positions — no single honest price "
                                      "exists when several assets move health"),
            }}


@router.get("/onchain/lending/risk/scan")
def lending_risk_scan(limit_scanned: int = 5000, min_debt_usd: float = 10_000.0):
    """Kamino positions closest to forced selling.

    Scans the protocol rather than a watchlist: a cascade is driven by
    aggregate leverage, so counting only known wallets would measure the
    watchlist instead of the market.
    """
    from lib.capital_lending import scan_positions_at_risk
    res = scan_positions_at_risk(limit_scanned=max(100, min(limit_scanned, 50_000)),
                                 min_debt_usd=min_debt_usd)

    # Name the assets, then model the shock ladder over them. Both steps
    # need reserve data, so a position whose reserve will not decode stays
    # UNRESOLVED and is counted rather than quietly dropped.
    from lib.capital_reserves import load_reserves, name_positions, position_reserves
    from lib.liquidation_stress import aggregate_by_asset, stress_ladder

    positions = res.get("positions", [])[:50]
    wanted = []
    for p in positions:
        raw = p.get("_raw")
        if raw:
            slots = position_reserves(raw)
            wanted += [d["reserve"] for d in slots["deposits"]]
            wanted += [b["reserve"] for b in slots["borrows"]]
    reserves = load_reserves(wanted) if wanted else {}
    for p in positions:
        raw = p.pop("_raw", None)
        if raw:
            p["assets"] = name_positions(raw, reserves)

    res["positions"] = positions
    res["by_asset"] = aggregate_by_asset(positions)
    res["stress"] = {
        fam: stress_ladder(positions, family=fam)
        for fam in ("SOL_FAMILY", "STABLE")
    }
    res["provenance"] = {
        "values": "VERIFIED — canonical Kamino decode",
        "asset_identity": "VERIFIED — canonical Kamino reserve layout",
        "prices": "VERIFIED — Kamino reserve oracle, not exchange spot",
        "risk_state": "CALCULATED from Kamino's own health rule",
        "stress_ladder": ("MODELLED — hypothetical prices over verified "
                          "positions. Not a forecast."),
    }
    return res


# ── Protocol registry ────────────────────────────────────────────────────
@router.get("/onchain/protocols")
def protocols():
    """Which programs JARVIS can recognise, and which it cannot.

    Exposed because an unrecognised program is a KNOWN GAP rather than an
    absence of activity, and the operator should be able to see the edge of
    what the parser understands.
    """
    from lib.solana_protocols import LST_MINTS, PROGRAMS, STAKE_POOLS
    return {
        "programs": [{"program_id": pid, "category": cat, "name": name,
                      "verified_on_chain": ok}
                     for pid, (cat, name, ok) in sorted(
                         PROGRAMS.items(), key=lambda kv: kv[1][0])],
        "stake_pools": [{"account": a, "provider": p}
                        for a, p in STAKE_POOLS.items()],
        "lst_mints": [{"mint": m, "symbol": s, "provider": p,
                       "underlying": "SOL"}
                      for m, (s, p) in LST_MINTS.items()],
        "note": ("Every program ID was checked with getAccountInfo before "
                 "being registered. An unlisted program is UNKNOWN, which is "
                 "not the same as 'not a protocol'."),
    }
