"""Wallet observations -> shadow theses, refusals, forward outcomes, performance.

SHADOW MEANS SHADOW. Nothing here places an order, opens a position, moves
cash or touches any book. The output is evidence about whether watching these
wallets would have been worth anything — which is a question that has to be
answered before, not after, anyone acts on it.

WHAT ONE OBSERVATION IS. Three grains collapse, in order:

    20,778 transfer legs
      -> 3,882 signatures        one signature is one economic event
      -> N clusters              one token, one direction, one time window

The cluster is the market observation. Several watched wallets buying the
same token inside the same window are ONE piece of evidence about that token,
not five — they may be copying each other, sharing a funder, or following the
same caller. Every contributing wallet and signature is kept as evidence; the
VOTE is counted once. Treating them as independent is how five wallets turn a
coin flip into apparent conviction.

REFUSALS ARE THE PRODUCT TOO. Most events refuse, and the reasons are
persisted rather than dropped: they measure classification quality, gate
selectivity and exactly which evidence is missing. A subsystem that only
stored its successes could not tell a strict gate from a broken one.

THE GATE READS EVIDENCE, IN ORDER, AND STOPS AT THE FIRST FAILURE.
Wallet quality is POINT-IN-TIME: a wallet's score today may not be used to
justify a trade it made last week, and an unproven wallet is UNKNOWN rather
than neutral. Prices must be near the event, not near now.

UNRESOLVED IS NOT A LOSS. A horizon with no qualifying price stays
UNRESOLVED, counts in the denominator, and never becomes a zero return.

OBSERVED PROFIT BEFORE COSTS IS NOT EDGE. Every performance figure carries an
estimated cost and a sample count, and the desk refuses to state an
expectancy under the minimum sample rather than printing a number that looks
like one.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SHADOW_INTEL_VERSION = "wallet_shadow_intel_v1"
OUTCOME_VERSION = "wallet_shadow_outcome_v1"

SOURCE = "HELIUS_WALLET_INTELLIGENCE"
EXECUTION_MODE = "SHADOW"

STATE_ELIGIBLE = "ELIGIBLE"
CURRENT = "CURRENT"
SUPERSEDED = "SUPERSEDED"

#: THE canonical "this observation still stands" predicate. A superseded row
#: is kept for provenance and must never be counted again — it is the SAME
#: signatures under an older reading. `IS NULL` covers rows written before
#: the column existed, which nothing has superseded.
CURRENT_ONLY = "(revision_state = 'CURRENT' OR revision_state IS NULL)"
CURRENT_ONLY_E = "(e.revision_state = 'CURRENT' OR e.revision_state IS NULL)"
STATE_REFUSED = "REFUSED"

# ── Refusal vocabulary ───────────────────────────────────────────────────
UNKNOWN_EVENT_TYPE = "UNKNOWN_EVENT_TYPE"
NON_TRADING_TRANSFER = "NON_TRADING_TRANSFER"
PARTIAL_TRANSACTION_EVIDENCE = "PARTIAL_TRANSACTION_EVIDENCE"
UNKNOWN_TOKEN_IDENTITY = "UNKNOWN_TOKEN_IDENTITY"
UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
EXCHANGE_OR_ENTITY_WALLET = "EXCHANGE_OR_ENTITY_WALLET"
SPAM_OR_DUST = "SPAM_OR_DUST"
UNKNOWN_WALLET_QUALITY = "UNKNOWN_WALLET_QUALITY"
INSUFFICIENT_WALLET_HISTORY = "INSUFFICIENT_WALLET_HISTORY"
NO_PRICE = "NO_PRICE"
STALE_PRICE = "STALE_PRICE"
LOW_LIQUIDITY = "LOW_LIQUIDITY"
BELOW_ECONOMIC_SIZE = "BELOW_ECONOMIC_SIZE"
EXPECTED_COST_EXCEEDS_EDGE = "EXPECTED_COST_EXCEEDS_EDGE"
DUPLICATE_TRANSACTION = "DUPLICATE_TRANSACTION"
COPY_CHAIN_DUPLICATE = "COPY_CHAIN_DUPLICATE"
UNRESOLVED_OUTCOME = "UNRESOLVED_OUTCOME"

REFUSAL_REASONS = (
    UNKNOWN_EVENT_TYPE, NON_TRADING_TRANSFER, PARTIAL_TRANSACTION_EVIDENCE,
    UNKNOWN_TOKEN_IDENTITY, UNSUPPORTED_ASSET, EXCHANGE_OR_ENTITY_WALLET,
    SPAM_OR_DUST, UNKNOWN_WALLET_QUALITY, INSUFFICIENT_WALLET_HISTORY,
    NO_PRICE, STALE_PRICE, LOW_LIQUIDITY, BELOW_ECONOMIC_SIZE,
    EXPECTED_COST_EXCEEDS_EDGE, DUPLICATE_TRANSACTION, COPY_CHAIN_DUPLICATE,
    UNRESOLVED_OUTCOME,
)

# ── Policy. Stated, versioned, and the operator's to change ──────────────
#: Copy-chain window. Wallets acting on the same token within this are one
#: observation. Wide enough to catch a copy, narrow enough that two genuinely
#: separate decisions a day apart stay separate.
CLUSTER_WINDOW_SECONDS = 900
#: A price must be THIS close to the event to describe it. Beyond it the
#: price is evidence about a different moment.
PRICE_MAX_AGE_SECONDS = 3600
#: Below this a position is not economically meaningful evidence.
MIN_NOTIONAL_USD = 50.0
#: Pool depth below which a fill is not reproducible at any size.
MIN_LIQUIDITY_USD = 10_000.0
#: Round-trip cost assumption for an on-chain memecoin swap: pool fee, two
#: legs of slippage and gas, as a fraction of notional. DELIBERATELY BLUNT —
#: it is an assumption and is labelled one, never a measurement.
ESTIMATED_ROUND_TRIP_COST_PCT = 3.0
#: Below this many resolved observations the desk states no expectancy.
MIN_SAMPLE_FOR_EXPECTANCY = 20

HORIZONS = {"15m": 15, "1h": 60, "4h": 240, "24h": 1440, "7d": 10_080}
#: How far from `due_at` a price may sit and still describe the checkpoint.
HORIZON_TOLERANCE_SECONDS = {"15m": 300, "1h": 900, "4h": 3600,
                             "24h": 7200, "7d": 21_600}

RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED"
EXPIRED = "EXPIRED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _parse(ts):
    if ts in (None, ""):
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Loading the existing observations ────────────────────────────────────
def load_legs(*, limit: int | None = None, since_ts: float | None = None):
    """Read stored Helius transfers as classifier legs. READ-ONLY.

    Reads the EXISTING event store. No second observation table is created
    and nothing is written here.
    """
    import os
    import sqlite3

    from lib.event_store import _db_path
    from lib.wallet_event_classifier import TransferLeg

    path = _db_path()
    # A store that does not exist yet holds no observations. `mode=ro`
    # REFUSES to create one, which is the behaviour we want — but it raises,
    # and a context feed must not be able to take the desk down over an
    # empty deployment.
    if not os.path.exists(path):
        logger.info("[ShadowIntel] no event store at %s yet", path)
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    sql = ("SELECT payload FROM events WHERE dedup_key LIKE 'helius:%'")
    params: list = []
    if since_ts:
        sql += " AND exchange_ts >= ?"
        params.append(float(since_ts))
    sql += " ORDER BY exchange_ts DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    legs = []
    for (payload,) in con.execute(sql, params):
        try:
            d = json.loads(payload)
        except (TypeError, ValueError):
            continue
        dk = str(d.get("dedup_key") or "").split(":")
        if len(dk) < 5:
            continue
        metric = str(d.get("metric") or "")
        direction = ("in" if metric.endswith("_in")
                     else "out" if metric.endswith("_out") else None)
        if not direction:
            continue
        legs.append(TransferLeg(
            signature=dk[1],
            # The dedup key is the identity of record; the added payload
            # fields are newer and only present on rows written since.
            mint=d.get("mint") or dk[2],
            direction=direction,
            amount=float(d.get("value") or 0.0),
            counterparty=d.get("counterparty") or dk[3],
            watched_wallet=d.get("watched_wallet"),
            symbol=d.get("symbol"),
            block_time=d.get("exchange_ts"),
            observed_ts=d.get("ingest_ts"),
            parser_version=d.get("source_schema_version")))
    con.close()
    return legs


def entity_lookup_factory():
    """Registry entity classification, loaded once. READ-ONLY."""
    from app.database import WalletRegistry, get_db

    table: dict = {}
    try:
        with get_db() as db:
            for r in db.query(
                    WalletRegistry.address, WalletRegistry.entity_type,
                    WalletRegistry.entity_name, WalletRegistry.is_protocol,
                    WalletRegistry.is_trader).all():
                table[r[0]] = {"entity_type": r[1], "entity_name": r[2],
                               "is_protocol": bool(r[3]),
                               "is_trader": bool(r[4])}
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[ShadowIntel] registry unavailable: %s", e)
    return lambda addr: table.get(addr)


# ── Copy-chain / multi-leg suppression ───────────────────────────────────
def cluster_key(event) -> str:
    """One token, one direction, one window — ONE market observation.

    Deliberately EXCLUDES the wallet. Five wallets buying the same token in
    the same fifteen minutes are one piece of evidence about that token; the
    wallets are recorded as evidence and counted once as a vote.
    """
    bucket = 0
    t = _parse(event.block_time)
    if t:
        bucket = int(t.timestamp() // CLUSTER_WINDOW_SECONDS)
    raw = f"{event.subject_mint}|{event.direction}|{bucket}|{event.event_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cluster(events: list) -> list:
    """Group classified events into market observations."""
    groups: dict = {}
    for e in events:
        groups.setdefault(cluster_key(e), []).append(e)
    return [(k, v) for k, v in groups.items()]


# ── Point-in-time wallet quality ─────────────────────────────────────────
def _NON_COPYABLE_BEHAVIOURS():
    from lib.wallet_behaviour import NON_COPYABLE_BEHAVIOURS
    return NON_COPYABLE_BEHAVIOURS


def _is_non_trader_entity(entity_type, status=None) -> bool:
    """Whether this row is a known NON-TRADER, by the canonical vocabulary.

    `lib/wallet_classify.NON_TRADER_ENTITIES` already names them — CEX,
    BRIDGE, TREASURY, MARKET_MAKER, CUSTODY, DEX_ROUTER, LIQUIDITY_POOL,
    VAULT, PDA, PROGRAM, TOKEN_ACCOUNT, BURN. This is the one place that
    asks the question, so the gate and the lifecycle cannot disagree about
    what an entity is.
    """
    from lib.wallet_classify import NON_TRADER_ENTITIES

    if str(status or "").upper() in ("EXCLUDED_ENTITY", "ARCHIVED"):
        return True
    return str(entity_type or "").upper() in NON_TRADER_ENTITIES


def wallet_quality_snapshot(addresses: list) -> dict:
    """Wallet evidence AS RECORDED, never invented.

    An unscored wallet is UNKNOWN. It is NOT given a neutral score to get an
    event through the gate — that would let an unproven wallet manufacture a
    thesis, which is the opposite of what measuring wallet quality is for.
    """
    from app.database import WalletRegistry, get_db

    out = {"wallets": [], "known": 0, "unknown": 0, "best_score": None,
           "max_sample_count": 0, "measurable": False,
           "score_version": None, "score_at": None,
           "entities": [], "entity_types": [],
           "non_copyable": [], "non_copyable_states": [],
           "as_of": _iso(_now()),
           "note": ("point-in-time as recorded on the registry row; an "
                    "unscored wallet is UNKNOWN, never neutral")}
    if not addresses:
        out["unknown"] = 0
        return out
    # READ EVERY VALUE INSIDE THE SESSION. Holding the ORM instances past
    # the `with` block worked only while nothing had written to the registry
    # first; once the cycle rescores a wallet in an earlier stage, the
    # instances come back EXPIRED and every attribute read raises
    # DetachedInstanceError — which failed the whole classification pass for
    # a reason that had nothing to do with classification.
    cols = (WalletRegistry.address, WalletRegistry.smart_money_score,
            WalletRegistry.alpha_score, WalletRegistry.sample_count,
            WalletRegistry.required_sample_count, WalletRegistry.status,
            WalletRegistry.entity_type, WalletRegistry.confidence_score,
            WalletRegistry.win_rate, WalletRegistry.average_holding_period,
            WalletRegistry.wallet_score_version,
            WalletRegistry.last_score_update, WalletRegistry.measurable,
            WalletRegistry.measurability_reason,
            WalletRegistry.behaviour_state, WalletRegistry.copyability_state,
            WalletRegistry.monitoring_purpose, WalletRegistry.score_source)
    keys = ("address", "smart_money_score", "alpha_score", "sample_count",
            "required_sample_count", "status", "entity_type",
            "confidence_score", "win_rate", "average_holding_period",
            "wallet_score_version", "last_score_update", "measurable",
            "measurability_reason", "behaviour_state", "copyability_state",
            "monitoring_purpose", "score_source")
    try:
        with get_db() as db:
            found = {r[0]: dict(zip(keys, r)) for r in
                     db.query(*cols).filter(
                         WalletRegistry.address.in_(list(addresses))).all()}
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[ShadowIntel] wallet quality unavailable: %s", e)
        found = {}

    for addr in addresses:
        r = found.get(addr)
        if r is None:
            out["wallets"].append({"label": safe_label(addr),
                                   "quality": "UNKNOWN",
                                   "reason": "not in the registry"})
            out["unknown"] += 1
            continue
        score = (r["smart_money_score"] if r["smart_money_score"] is not None
                 else r["alpha_score"])
        sample = int(r["sample_count"] or 0)
        required = int(r["required_sample_count"] or 0)
        measurable = bool(r["measurable"]) and score is not None
        entry = {
            "label": safe_label(addr),
            "tier": r["status"],
            "entity_type": r["entity_type"],
            "score": score,
            "confidence": r["confidence_score"],
            "win_rate": r["win_rate"],
            "sample_count": sample,
            "required_sample_count": required,
            "average_holding_period": r["average_holding_period"],
            "score_version": r["wallet_score_version"],
            "score_at": r["last_score_update"],
            "measurable": measurable,
            "measurability_reason": r["measurability_reason"],
            "quality": "KNOWN" if measurable else "UNKNOWN",
            "behaviour": r.get("behaviour_state"),
            "copyability": r.get("copyability_state"),
            "purpose": r.get("monitoring_purpose"),
            "score_source": r.get("score_source"),
        }
        out["wallets"].append(entry)
        # ENTITY IDENTITY IS NOT A SCORE. An exchange, router, pool or
        # treasury can post an enormous, perfectly real "profit" that
        # belongs to its customers or its routing flow, not to skill a
        # follower could copy.
        if _is_non_trader_entity(r["entity_type"], r["status"]):
            out["entities"].append(entry["label"])
            out["entity_types"].append(r["entity_type"] or r["status"])
        # A BEHAVIOURAL finding, read from the registry the lifecycle wrote.
        # Kept separate from `entities` because it is a weaker claim: it
        # says what the wallet does, not what it is.
        if r.get("behaviour_state") in _NON_COPYABLE_BEHAVIOURS():
            out["non_copyable"].append(entry["label"])
            out["non_copyable_states"].append(r["behaviour_state"])
        if measurable:
            out["known"] += 1
            if out["best_score"] is None or (score or 0) > out["best_score"]:
                out["best_score"] = score
                out["score_version"] = r["wallet_score_version"]
                out["score_at"] = r["last_score_update"]
        else:
            out["unknown"] += 1
        out["max_sample_count"] = max(out["max_sample_count"], sample)
    out["measurable"] = out["known"] > 0
    return out


def safe_label(address: str | None) -> str:
    """A wallet the operator can recognise WITHOUT publishing the address."""
    if not address:
        return "UNKNOWN"
    a = str(address)
    return f"{a[:4]}…{a[-4:]}" if len(a) > 10 else a


# ── Point-in-time market context ─────────────────────────────────────────
def market_context(mint: str | None, at) -> dict:
    """Token context NEAR THE EVENT, from the existing snapshot store.

    Today's price is NOT a substitute for the price then, so a snapshot
    outside `PRICE_MAX_AGE_SECONDS` is reported as stale rather than used.
    Missing stays missing.
    """
    from app.database import TokenActivitySnapshot, get_db

    ctx = {"mint": mint, "price_usd": None, "price_source": None,
           "price_at": None, "price_age_seconds": None,
           "liquidity_usd": None, "volume_h1": None, "volume_h24": None,
           "symbol": None, "network": None, "state": NO_PRICE,
           "max_age_seconds": PRICE_MAX_AGE_SECONDS}
    if not mint:
        ctx["state"] = UNKNOWN_TOKEN_IDENTITY
        return ctx
    when = _parse(at) or _now()
    # SELECT THE COLUMNS, NOT THE ENTITY. `get_db()` commits on the way out,
    # and a commit EXPIRES every instance the session loaded; closing it then
    # detaches them. So reading `row.captured_at` after this block asks a
    # detached instance to refresh itself, which raises DetachedInstanceError
    # and failed the whole classification stage.
    #
    # It only ever fired when a mint actually HAD snapshots, so it stayed
    # invisible while prices were collected AFTER processing and most subject
    # mints had no rows to iterate. Collecting prices first — which is
    # correct, because a SOL-quoted event cannot be valued otherwise — made
    # `rows` non-empty and the latent defect immediate.
    #
    # A column tuple owns its values outright and cannot expire, which is the
    # same idiom `lib/token_price_history` already uses against this table.
    try:
        with get_db() as db:
            rows = (db.query(TokenActivitySnapshot.captured_at,
                             TokenActivitySnapshot.price_usd,
                             TokenActivitySnapshot.liquidity_usd,
                             TokenActivitySnapshot.volume_h1,
                             TokenActivitySnapshot.volume_h24,
                             TokenActivitySnapshot.symbol,
                             TokenActivitySnapshot.network)
                    .filter(TokenActivitySnapshot.mint == mint,
                            TokenActivitySnapshot.price_usd.isnot(None))
                    .all())
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[ShadowIntel] snapshot lookup failed: %s", e)
        rows = []
    best, best_gap = None, None
    for row in rows:
        t = _parse(row[0])
        if t is None or row[1] is None:
            continue
        gap = abs((t - when).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    if best is None:
        return ctx
    ctx.update({
        "price_usd": best[1], "price_source": "token_activity_snapshot",
        "price_at": best[0], "price_age_seconds": best_gap,
        "liquidity_usd": best[2], "volume_h1": best[3],
        "volume_h24": best[4], "symbol": best[5],
        "network": best[6],
        "state": "FRESH" if best_gap <= PRICE_MAX_AGE_SECONDS else STALE_PRICE,
    })
    return ctx


def expected_costs(ctx: dict) -> dict:
    """The round trip this thesis would have to overcome. AN ASSUMPTION."""
    return {
        "round_trip_pct": ESTIMATED_ROUND_TRIP_COST_PCT,
        "basis": "POOL_FEE_PLUS_SLIPPAGE_PLUS_GAS",
        "quality": "ASSUMPTION",
        "note": ("a blunt on-chain round-trip assumption, not a measurement. "
                 "It exists so no return is called edge before costs"),
    }


# ── Derived validation state ─────────────────────────────────────────────
#
# `state` records what the GATE decided at the time, and it must stay
# exactly as it was — it is the audit record of a decision made on the
# evidence then available. But `ELIGIBLE` beside
# `copyability_state=PROVIDER_CAPABILITY_UNAVAILABLE` reads as "validated",
# and it is not: it means the gate passed and nothing could vouch for the
# wallet. The validation state is DERIVED from both, so the original stays
# untouched and the reader is not left to infer.
VALIDATED_ELIGIBLE = "VALIDATED_ELIGIBLE"
PROVISIONAL_ELIGIBLE = "PROVISIONAL_ELIGIBLE"
NON_COPYABLE_REFUSED = "NON_COPYABLE_REFUSED"
CONFIRMED_ENTITY_REFUSED = "CONFIRMED_ENTITY_REFUSED"
GATE_REFUSED = "GATE_REFUSED"

VALIDATION_STATES = (VALIDATED_ELIGIBLE, PROVISIONAL_ELIGIBLE,
                     NON_COPYABLE_REFUSED, CONFIRMED_ENTITY_REFUSED,
                     GATE_REFUSED)


def validation_state(state, copyability_state) -> str:
    """What this observation actually amounts to, right now.

    PURE and DERIVED — it stores nothing and rewrites nothing, so a pick
    reassessed after new evidence simply reads differently without its
    decision-time record changing.
    """
    from lib import wallet_behaviour as WB

    if state != STATE_ELIGIBLE:
        return GATE_REFUSED
    c = copyability_state
    if c == WB.COPY_CONFIRMED_ENTITY:
        return CONFIRMED_ENTITY_REFUSED
    if c in (WB.COPY_MARKET_MAKING, WB.COPY_LIQUIDITY_OPERATION,
             WB.COPY_CUSTODY, WB.COPY_COORDINATED):
        return NON_COPYABLE_REFUSED
    if c == WB.COPYABLE_EVIDENCE_SUPPORTED:
        return VALIDATED_ELIGIBLE
    # Unassessed, unresolved identity, or too little behaviour to say.
    return PROVISIONAL_ELIGIBLE


# ── The gate ─────────────────────────────────────────────────────────────
def evaluate(events: list, *, wallet_quality=None, ctx=None) -> dict:
    """Deterministic eligibility for ONE cluster. Pure — no writes.

    Stops at the FIRST failure, so the reported reason is the binding one
    rather than the last one checked.
    """
    from lib import wallet_event_classifier as C

    primary = max(events, key=lambda e: (e.subject_amount or 0.0))
    wallets = sorted({e.watched_wallet for e in events if e.watched_wallet})
    sigs = sorted({e.signature for e in events})

    def refuse(reason, detail):
        return {"state": STATE_REFUSED, "refusal_reason": reason,
                "reason": detail, "primary": primary,
                "wallets": wallets, "signatures": sigs}

    if primary.event_type in C.NON_TRADING_EVENT_TYPES:
        return refuse(NON_TRADING_TRANSFER,
                      f"{primary.event_type}: {primary.reason}")
    if primary.classification == C.PARTIAL_EVIDENCE:
        return refuse(PARTIAL_TRANSACTION_EVIDENCE, primary.reason)
    if not primary.is_trading_event:
        return refuse(UNKNOWN_EVENT_TYPE, primary.reason)
    if not primary.subject_mint:
        return refuse(UNKNOWN_TOKEN_IDENTITY,
                      "no mint on the subject leg; a ticker is not identity")
    if C.is_quote_asset(primary.subject_mint):
        return refuse(UNSUPPORTED_ASSET,
                      "the subject is a quote asset, not a position")

    wq = wallet_quality if wallet_quality is not None else \
        wallet_quality_snapshot(wallets)

    # EXCHANGE_OR_ENTITY_WALLET WAS A REFUSAL REASON THAT COULD NEVER FIRE.
    # It was declared in REFUSAL_REASONS and no code path emitted it, so the
    # desk showed zero entity refusals — which reads as "we checked and they
    # were all independent traders" when nothing had ever been checked.
    #
    # It is checked BEFORE wallet quality on purpose: an exchange's flow can
    # score extremely well, and "measurable" is exactly what a busy custodial
    # wallet looks like. Skill is the wrong question to ask about it.
    # A measured non-copyable pattern refuses a NEW pick outright. This is
    # narrower than the entity gate above and says something different: the
    # wallet may be entirely legitimate, and its economics still cannot be
    # reproduced by a follower.
    if wq.get("non_copyable"):
        return refuse(EXCHANGE_OR_ENTITY_WALLET,
                      f"contributing wallet(s) {', '.join(wq['non_copyable'])} "
                      f"show {', '.join(sorted(set(wq['non_copyable_states'])))}"
                      f" — real activity, but not copyable directional alpha")

    if wq.get("entities"):
        return refuse(EXCHANGE_OR_ENTITY_WALLET,
                      f"contributing wallet(s) {', '.join(wq['entities'])} "
                      f"are classified "
                      f"{', '.join(sorted(set(wq['entity_types'])))} — "
                      f"entity flow is not copyable trader alpha, however "
                      f"well it scores")

    if not wq.get("measurable"):
        reason = (INSUFFICIENT_WALLET_HISTORY
                  if wq.get("max_sample_count") else UNKNOWN_WALLET_QUALITY)
        return refuse(reason,
                      f"{wq.get('unknown', 0)} of {len(wallets) or 1} "
                      f"contributing wallet(s) have no usable score; an "
                      f"unproven wallet is not a neutral one")

    m = ctx if ctx is not None else market_context(primary.subject_mint,
                                                  primary.block_time)
    if m["state"] == NO_PRICE:
        return refuse(NO_PRICE,
                      "no price snapshot exists for this mint near the event")
    if m["state"] == STALE_PRICE:
        return refuse(STALE_PRICE,
                      f"nearest price is {m['price_age_seconds']:.0f}s from "
                      f"the event, past the {PRICE_MAX_AGE_SECONDS}s policy")
    if (m.get("liquidity_usd") or 0) < MIN_LIQUIDITY_USD:
        return refuse(LOW_LIQUIDITY,
                      f"pool depth {m.get('liquidity_usd')} below "
                      f"${MIN_LIQUIDITY_USD:,.0f}")

    notional = None
    if primary.subject_amount is not None and m.get("price_usd"):
        notional = float(primary.subject_amount) * float(m["price_usd"])
    if notional is None:
        return refuse(NO_PRICE, "cannot value the position at event time")
    if notional < MIN_NOTIONAL_USD:
        return refuse(BELOW_ECONOMIC_SIZE,
                      f"${notional:,.2f} is below the ${MIN_NOTIONAL_USD:,.0f} "
                      f"economic floor")

    return {"state": STATE_ELIGIBLE, "refusal_reason": None,
            "reason": (f"{primary.event_type} on paired swap legs, "
                       f"{len(wallets) or 'unknown'} scored wallet(s), "
                       f"price {m['price_age_seconds']:.0f}s from the event, "
                       f"${notional:,.0f} notional"),
            "primary": primary, "wallets": wallets, "signatures": sigs,
            "notional_usd": notional, "market_context": m,
            "wallet_quality": wq}


# ── Persistence. Idempotent by cluster_id ────────────────────────────────
def _json(v):
    return None if v is None else json.dumps(v, default=str)


def process(*, limit: int | None = None, since_ts: float | None = None,
            max_clusters: int | None = None, enrichment_lookup=None,
            use_enrichment: bool = True) -> dict:
    """Classify, cluster, gate and persist. REPEATABLE WITHOUT DOUBLE-VOTING.

    `cluster_id` is a UNIQUE column, so re-running updates the same row
    rather than casting a second observation — proven by running it twice
    and counting.
    """
    from sqlalchemy import text

    from app.database import engine, new_id, now_iso
    from lib import wallet_event_classifier as C
    from lib.engine_epoch import ENGINE_EPOCH

    def _copyability(addresses):
        """The source wallet's copyability, cached per pass.

        Behaviour is measured from evidence already stored, so this adds no
        provider call and cannot be blocked by a plan entitlement.
        """
        if not addresses:
            return None
        try:
            from lib import wallet_behaviour as WB
            best = None
            for a in addresses:
                v = WB.copyability(a)
                # The WEAKEST contributing wallet decides. One unverifiable
                # wallet in a cluster makes the whole observation
                # unverifiable.
                if best is None or v["state"] != WB.COPYABLE_EVIDENCE_SUPPORTED:
                    best = v
                if v["state"] != WB.COPYABLE_EVIDENCE_SUPPORTED:
                    break
            return best
        except Exception as e:                               # noqa: BLE001
            logger.warning("[ShadowIntel] copyability unavailable: %s", e)
            return None

    if enrichment_lookup is None and use_enrichment:
        try:
            from lib.wallet_swap_enrichment import lookup_factory
            enrichment_lookup = lookup_factory()
        except Exception as e:                               # noqa: BLE001
            # Enrichment is an UPGRADE, never a dependency. Without it the
            # transfer reading stands exactly as it did before.
            logger.warning("[ShadowIntel] enrichment unavailable: %s", e)
            enrichment_lookup = None

    legs = load_legs(limit=limit, since_ts=since_ts)
    events = C.classify_all(legs, entity_lookup=entity_lookup_factory(),
                            enrichment_lookup=enrichment_lookup)
    clusters = cluster(events)
    if max_clusters:
        clusters = clusters[:int(max_clusters)]

    stats = {"legs": len(legs), "events": len(events),
             "clusters": len(clusters), "eligible": 0, "refused": 0,
             "inserted": 0, "updated": 0, "reclassified": 0,
             "superseded": 0, "reference_price_preserved": 0,
             "by_refusal": {}, "by_event_type": {}, "by_classification": {},
             "enrichment_applied": enrichment_lookup is not None}
    touched: dict = {}          # signature -> the cluster now holding it

    now = now_iso()
    with engine.begin() as conn:
        for cid, group in clusters:
            verdict = evaluate(group)
            p = verdict["primary"]
            stats["by_event_type"][p.event_type] = \
                stats["by_event_type"].get(p.event_type, 0) + 1
            stats["by_classification"][p.classification] = \
                stats["by_classification"].get(p.classification, 0) + 1

            eligible = verdict["state"] == STATE_ELIGIBLE
            if eligible:
                stats["eligible"] += 1
            else:
                stats["refused"] += 1
                r = verdict["refusal_reason"]
                stats["by_refusal"][r] = stats["by_refusal"].get(r, 0) + 1

            m = verdict.get("market_context")
            wq = verdict.get("wallet_quality")
            row = {
                "cluster_id": cid, "source": SOURCE,
                "execution_mode": EXECUTION_MODE,
                "subject_mint": p.subject_mint,
                "subject_symbol": p.subject_symbol, "chain": p.chain,
                "direction": p.direction, "event_type": p.event_type,
                "classification": p.classification,
                "evidence_quality": p.evidence_quality,
                "classification_reason": p.reason,
                "schema_compatibility": p.schema_compatibility,
                "signatures_json": _json(verdict["signatures"]),
                "wallets_json": _json([safe_label(w)
                                       for w in verdict["wallets"]]),
                "signature_count": len(verdict["signatures"]),
                "wallet_count": len(verdict["wallets"]),
                "leg_count": sum(e.leg_count for e in group),
                "subject_amount": p.subject_amount,
                "quote_mint": p.quote_mint, "quote_symbol": p.quote_symbol,
                "quote_amount": p.quote_amount,
                "notional_usd": verdict.get("notional_usd"),
                "event_time": _iso(_parse(p.block_time)),
                "observed_at": _iso(_parse(p.observed_ts)),
                "wallet_quality_json": _json(wq),
                "market_context_json": _json(m),
                "expected_cost_json": _json(expected_costs(m or {})),
                "state": verdict["state"],
                "refusal_reason": verdict["refusal_reason"],
                "eligibility_reason": verdict["reason"],
                "reference_price_usd": (m or {}).get("price_usd")
                if eligible else None,
                "reference_price_source": (m or {}).get("price_source")
                if eligible else None,
                "reference_price_at": (m or {}).get("price_at")
                if eligible else None,
                "thesis_id": cid if eligible else None,
                "horizons_json": _json(list(HORIZONS)) if eligible else None,
                "engine_epoch": ENGINE_EPOCH,
                "copyability_state": None, "copyability_reason": None,
                "behaviour_state": None, "copyability_at": None,
                "classifier_version": p.classifier_version,
                "model_version": SHADOW_INTEL_VERSION,
                "updated_at": now,
            }
            if eligible:
                # Only worth measuring for an observation that actually
                # passed the gate — the rest already have a binding reason.
                cop = _copyability(verdict.get("wallets") or [])
                if cop:
                    row["copyability_state"] = cop.get("state")
                    row["copyability_reason"] = (cop.get("reason") or "")[:500]
                    row["behaviour_state"] = cop.get("behaviour")
                    row["copyability_at"] = now
                    stats.setdefault("by_copyability", {})
                    k = cop.get("state")
                    stats["by_copyability"][k] = \
                        stats["by_copyability"].get(k, 0) + 1

            for _s in verdict["signatures"]:
                touched[_s] = cid

            existing = conn.execute(text(
                "SELECT id, event_type, classification, evidence_quality, "
                "       revision, reference_price_usd, "
                "       reference_price_source, reference_price_at "
                "FROM wallet_shadow_events WHERE cluster_id=:c"),
                {"c": cid}).fetchone()

            if existing and eligible and row["reference_price_usd"] is None \
                    and existing[5] is not None:
                # THE RECORD OF A PRICE OUTLIVES THE SNAPSHOT IT CAME FROM.
                # `token_activity_snapshots` is pruned by age, so a reprocess
                # weeks later would find nothing near the event and demote a
                # thesis that was correctly admitted on evidence that DID
                # exist at the time. Re-deriving a point-in-time fact from
                # what happens to remain in the store is the same mistake as
                # using today's price for an old event.
                row["reference_price_usd"] = existing[5]
                row["reference_price_source"] = existing[6]
                row["reference_price_at"] = existing[7]
                stats["reference_price_preserved"] += 1

            if existing:
                if (existing[1] != row["event_type"]
                        or existing[2] != row["classification"]):
                    row["prior_event_type"] = existing[1]
                    row["prior_classification"] = existing[2]
                    row["prior_evidence_quality"] = existing[3]
                    row["revision"] = int(existing[4] or 1) + 1
                    stats["reclassified"] += 1
                row["revision_state"] = CURRENT
                row["superseded_by"] = None
                row["superseded_at"] = None
                sets = ", ".join(f"{k}=:{k}" for k in row if k != "cluster_id")
                conn.execute(text(
                    f"UPDATE wallet_shadow_events SET {sets} "
                    f"WHERE cluster_id=:cluster_id"), row)
                stats["updated"] += 1
                event_id = existing[0]
            else:
                row["id"] = new_id()
                row["created_at"] = now
                row["revision_state"] = CURRENT
                row["revision"] = 1
                cols = list(row)
                conn.execute(text(
                    f"INSERT INTO wallet_shadow_events ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})"), row)
                stats["inserted"] += 1
                event_id = row["id"]

            if eligible:
                _schedule_horizons(conn, event_id, cid,
                                   _parse(p.block_time),
                                   (m or {}).get("price_usd"), now)

        stats["superseded"] = _supersede_moved(conn, touched, now)
    return stats


def _supersede_moved(conn, touched: dict, now) -> int:
    """Retire rows whose signatures now belong to a different cluster.

    MULTIPLE LEGS ARE NOT MULTIPLE VOTES, and neither is one signature
    classified twice. `cluster_key` hashes the event TYPE, so correcting a
    classification necessarily mints a new cluster id; without this the
    corrected observation would be ADDED to the uncorrected one and the desk
    would count both.

    A row is retired only when EVERY signature it holds has moved to a
    cluster written by this same pass. A cluster still holding evidence
    nobody reclassified is left exactly as it is.
    """
    import json as _json

    from sqlalchemy import text

    if not touched:
        return 0
    live = set(touched.values())
    rows = conn.execute(text(
        "SELECT id, cluster_id, signatures_json FROM wallet_shadow_events "
        "WHERE revision_state = :cur OR revision_state IS NULL"),
        {"cur": CURRENT}).fetchall()

    retired = 0
    for row_id, cid, sig_json in rows:
        if cid in live:
            continue
        try:
            sigs = _json.loads(sig_json or "[]")
        except (TypeError, ValueError):
            continue
        if not sigs:
            continue
        moved = {touched.get(s) for s in sigs}
        if None in moved or not moved:
            continue
        conn.execute(text(
            "UPDATE wallet_shadow_events SET revision_state=:sup, "
            "superseded_by=:by, superseded_at=:now, updated_at=:now "
            "WHERE id=:id"),
            {"sup": SUPERSEDED, "by": sorted(x for x in moved if x)[0],
             "now": now, "id": row_id})
        # Its checkpoints describe an observation that no longer stands.
        conn.execute(text(
            "UPDATE wallet_shadow_outcomes SET status=:ex, "
            "unresolved_reason=:r, updated_at=:now "
            "WHERE event_id=:id AND status=:un"),
            {"ex": EXPIRED, "un": UNRESOLVED, "now": now, "id": row_id,
             "r": "the observation was superseded by a reclassification"})
        retired += 1
    return retired


def _schedule_horizons(conn, event_id, cluster_id, event_time, ref_price,
                       now) -> None:
    """One UNRESOLVED checkpoint per horizon, created once."""
    from sqlalchemy import text

    from app.database import new_id

    if event_time is None:
        return
    for horizon, minutes in HORIZONS.items():
        due = event_time + timedelta(minutes=minutes)
        exists = conn.execute(text(
            "SELECT id FROM wallet_shadow_outcomes "
            "WHERE event_id=:e AND horizon=:h"),
            {"e": event_id, "h": horizon}).fetchone()
        if exists:
            continue
        conn.execute(text(
            "INSERT INTO wallet_shadow_outcomes "
            "(id, event_id, cluster_id, horizon, due_at, status, "
            " reference_price_usd, unresolved_reason, outcome_version, "
            " created_at, updated_at) VALUES "
            "(:id, :e, :c, :h, :due, :st, :ref, :ur, :ov, :now, :now)"),
            {"id": new_id(), "e": event_id, "c": cluster_id, "h": horizon,
             "due": _iso(due), "st": UNRESOLVED, "ref": ref_price,
             "ur": "awaiting the due time", "ov": OUTCOME_VERSION,
             "now": now})


def resolve_outcomes(*, limit: int = 500) -> dict:
    """Fill checkpoints whose due time has passed AND whose price exists.

    A checkpoint with no qualifying price stays UNRESOLVED. It is never
    given today's price, the wallet's later exit, or a flattering
    intraperiod extreme — each of those answers a different question.
    """
    from sqlalchemy import text

    from app.database import engine, now_iso

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT o.id, o.event_id, o.horizon, o.due_at, "
            "       o.reference_price_usd, e.subject_mint "
            "FROM wallet_shadow_outcomes o "
            "JOIN wallet_shadow_events e ON e.id = o.event_id "
            "WHERE o.status = :st ORDER BY o.due_at LIMIT :lim"),
            {"st": UNRESOLVED, "lim": int(limit)}).fetchall()

    out = {"examined": len(rows), "resolved": 0, "still_unresolved": 0,
           "not_yet_due": 0, "by_reason": {}}
    now = _now()
    resolved_updates, unresolved_updates = [], []

    for oid, _event_id, horizon, due_at, ref, mint in rows:
        due = _parse(due_at)
        if due is None or due > now:
            out["not_yet_due"] += 1
            continue
        ctx = market_context(mint, due)
        tol = HORIZON_TOLERANCE_SECONDS.get(horizon, 3600)
        age = ctx.get("price_age_seconds")

        if ctx.get("price_usd") is None:
            reason = NO_PRICE
        elif age is None or age > tol:
            # A price from outside the tolerance describes a DIFFERENT
            # moment. Using it would answer a question nobody asked.
            reason = STALE_PRICE
        elif not ref:
            reason = "NO_REFERENCE_PRICE"
        else:
            gross = (float(ctx["price_usd"]) - float(ref)) / float(ref) * 100.0
            resolved_updates.append({
                "id": oid, "st": RESOLVED, "cat": ctx.get("price_at"),
                "cp": ctx.get("price_usd"), "src": ctx.get("price_source"),
                "age": age, "g": gross,
                "cost": ESTIMATED_ROUND_TRIP_COST_PCT,
                "net": gross - ESTIMATED_ROUND_TRIP_COST_PCT,
                "now": now_iso()})
            out["resolved"] += 1
            continue

        out["still_unresolved"] += 1
        out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
        unresolved_updates.append({"id": oid, "r": reason, "now": now_iso()})

    if resolved_updates or unresolved_updates:
        with engine.begin() as conn:
            for u in resolved_updates:
                conn.execute(text(
                    "UPDATE wallet_shadow_outcomes SET status=:st, "
                    "checkpoint_at=:cat, checkpoint_price_usd=:cp, "
                    "price_source=:src, price_age_seconds=:age, "
                    "gross_return_pct=:g, estimated_cost_pct=:cost, "
                    "net_return_pct=:net, unresolved_reason=NULL, "
                    "updated_at=:now WHERE id=:id"), u)
            for u in unresolved_updates:
                conn.execute(text(
                    "UPDATE wallet_shadow_outcomes SET unresolved_reason=:r, "
                    "updated_at=:now WHERE id=:id"), u)
    return out


# ── Source-isolated performance ──────────────────────────────────────────
def performance() -> dict:
    """What watching these wallets has been worth — on its own.

    KEPT APART from JARVIS execution, manual operator results and both
    virtual books, by construction: this reads only `wallet_shadow_*` and
    stamps `source = HELIUS_WALLET_INTELLIGENCE`. Nothing here reaches
    `trade_outcomes`, so no filter has to remember to exclude it.

    NO WIN RATE WITHOUT A SAMPLE COUNT, and no expectancy at all below the
    minimum — a number computed from four observations looks exactly like
    one computed from four hundred, and only one of them means anything.
    """
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as conn:
        totals = conn.execute(text(
            f"SELECT state, COUNT(*) FROM wallet_shadow_events "
            f"WHERE {CURRENT_ONLY} GROUP BY state"
        )).fetchall()
        refusals = conn.execute(text(
            f"SELECT refusal_reason, COUNT(*) FROM wallet_shadow_events "
            f"WHERE state='REFUSED' AND {CURRENT_ONLY} "
            f"GROUP BY refusal_reason ORDER BY 2 DESC"
        )).fetchall()
        by_type = conn.execute(text(
            f"SELECT event_type, COUNT(*) FROM wallet_shadow_events "
            f"WHERE {CURRENT_ONLY} GROUP BY event_type ORDER BY 2 DESC")).fetchall()
        by_class = conn.execute(text(
            f"SELECT classification, COUNT(*) FROM wallet_shadow_events "
            f"WHERE {CURRENT_ONLY} GROUP BY classification ORDER BY 2 DESC")).fetchall()
        suppression = conn.execute(text(
            f"SELECT COALESCE(SUM(signature_count),0), "
            f"       COALESCE(SUM(leg_count),0), COUNT(*) "
            f"FROM wallet_shadow_events WHERE {CURRENT_ONLY}")).fetchone()
        horizons = conn.execute(text(
            "SELECT horizon, status, COUNT(*), AVG(gross_return_pct), "
            "       AVG(net_return_pct) "
            "FROM wallet_shadow_outcomes GROUP BY horizon, status"
        )).fetchall()
        # VALIDATED AND PROVISIONAL ARE NOT ONE POPULATION. An outcome from
        # a wallet whose identity could never be resolved is real evidence
        # about that wallet and is NOT evidence that following it would have
        # worked — pooling them would let unverifiable wallets set the
        # expectancy the desk reports.
        copy_split = conn.execute(text(
            "SELECT COALESCE(e.copyability_state,'UNASSESSED'), o.status, "
            "       COUNT(*), AVG(o.gross_return_pct), AVG(o.net_return_pct) "
            "FROM wallet_shadow_outcomes o "
            "JOIN wallet_shadow_events e ON e.id = o.event_id "
            f"WHERE {CURRENT_ONLY_E} "
            "GROUP BY 1, 2")).fetchall()
        unresolved_reasons = conn.execute(text(
            "SELECT unresolved_reason, COUNT(*) FROM wallet_shadow_outcomes "
            "WHERE status <> 'RESOLVED' GROUP BY 1 ORDER BY 2 DESC"
        )).fetchall()

    state = {s: n for s, n in totals}
    per_horizon = {}
    for h, status, n, g, net in horizons:
        cell = per_horizon.setdefault(
            h, {"resolved": 0, "unresolved": 0, "gross_return_pct": None,
                "net_return_pct": None})
        if status == RESOLVED:
            cell["resolved"] = n
            cell["gross_return_pct"] = round(g, 4) if g is not None else None
            cell["net_return_pct"] = round(net, 4) if net is not None else None
        else:
            cell["unresolved"] += n
    for h, cell in per_horizon.items():
        enough = cell["resolved"] >= MIN_SAMPLE_FOR_EXPECTANCY
        cell["sample_sufficient"] = enough
        if not enough:
            # STATED, not silently rounded away.
            cell["expectancy_note"] = (
                f"{cell['resolved']} resolved — below the "
                f"{MIN_SAMPLE_FOR_EXPECTANCY} this desk will state an "
                f"expectancy from")

    VALIDATED = "COPYABLE_EVIDENCE_SUPPORTED"
    validated = {"resolved": 0, "unresolved": 0}
    provisional: dict = {}
    for copy_state, status, n, _g, _net in copy_split:
        bucket = (validated if copy_state == VALIDATED
                  else provisional.setdefault(
                      copy_state, {"resolved": 0, "unresolved": 0}))
        bucket["resolved" if status == RESOLVED else "unresolved"] += n

    legs, sigs, clusters = (suppression[1] or 0, suppression[0] or 0,
                            suppression[2] or 0)
    return {
        "source": SOURCE,
        "execution_mode": EXECUTION_MODE,
        "validated_outcomes": validated,
        "provisional_outcomes": provisional,
        "expectancy_population": (
            "VALIDATED ONLY. A provisional outcome comes from a wallet whose "
            "identity or behaviour is not yet supported by evidence; it is "
            "kept, shown and audited, and it is never pooled into a "
            "validated expectancy"),
        "observations": {
            "transfer_legs": legs,
            "signatures": sigs,
            "market_observations": clusters,
            "legs_per_observation": (round(legs / clusters, 2)
                                     if clusters else None),
            "signatures_per_observation": (round(sigs / clusters, 2)
                                           if clusters else None),
        },
        "eligible": state.get(STATE_ELIGIBLE, 0),
        "refused": state.get(STATE_REFUSED, 0),
        "by_refusal_reason": [{"reason": r or "UNKNOWN", "count": n}
                              for r, n in refusals],
        "by_event_type": [{"event_type": t, "count": n} for t, n in by_type],
        "by_classification": [{"classification": c, "count": n}
                              for c, n in by_class],
        "horizons": per_horizon,
        "unresolved_reasons": [{"reason": r or "PENDING_DUE_TIME",
                                "count": n}
                               for r, n in unresolved_reasons],
        "min_sample_for_expectancy": MIN_SAMPLE_FOR_EXPECTANCY,
        "estimated_round_trip_cost_pct": ESTIMATED_ROUND_TRIP_COST_PCT,
        "model_version": SHADOW_INTEL_VERSION,
        "note": ("shadow intelligence only — no order was ever submitted. "
                 "Kept apart from JARVIS execution, manual operator and both "
                 "virtual books. Unresolved is not a loss; returns are shown "
                 "before AND after an assumed round-trip cost"),
    }
