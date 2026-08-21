"""Entity flow is not copyable trader alpha, however well it scores.

THE DEFECT THIS PINS. `EXCHANGE_OR_ENTITY_WALLET` was listed in
`REFUSAL_REASONS` and **no code path could ever emit it**. The desk
therefore showed zero entity refusals — which reads as "we checked, and
every contributing wallet was an independent trader" when nothing had ever
been checked. Measured on the live registry: 0 events had ever carried that
reason, and not one of 1,086 wallets had a resolved identity.

The lifecycle had the same hole from the other side: `evaluate()` asked only
"how well did it do?", never "what is it?", so a router, pool, treasury or
exchange whose flow scored well was promotable into WATCH — and WATCH is the
population that produces wallet-alpha picks.

IDENTITY OVERRIDES SCORE. That is the whole rule. A custodial wallet's
"profit" belongs to its customers; a router's belongs to its flow. Both look
exactly like skill to an arithmetic that never asks what it is measuring.

Everything here runs against the isolated pytest database.
"""
from __future__ import annotations

import json
import uuid

from app.database import WalletRegistry, get_db
from lib import wallet_event_classifier as C
from lib import wallet_lifecycle as L
from lib import wallet_shadow_intel as SI
from lib.wallet_classify import NON_TRADER_ENTITIES

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T0 = 1_787_000_000.0


def _addr(p="Ent"):
    return p + uuid.uuid4().hex[:29]


def _leg(mint, direction, amount, *, sig, wallet, symbol=None):
    return C.TransferLeg(signature=sig, mint=mint, direction=direction,
                         amount=amount, counterparty="pool",
                         watched_wallet=wallet, symbol=symbol,
                         block_time=T0, observed_ts=T0,
                         parser_version="helius_v1_transfers_v1")


def _swap(wallet, mint):
    sig = "sig" + uuid.uuid4().hex
    return C.classify_group([
        _leg(mint, "in", 1000.0, sig=sig, wallet=wallet),
        _leg(USDC, "out", 500.0, sig=sig, wallet=wallet, symbol="USDC"),
    ])


class _Row:
    """A registry row stand-in, so the pure verdict needs no database."""

    def __init__(self, **kw):
        self.address = kw.get("address", _addr())
        self.status = kw.get("status", "CANDIDATE")
        self.entity_type = kw.get("entity_type")
        self.is_protocol = kw.get("is_protocol", False)
        self.smart_money_score = kw.get("smart_money_score")
        self.confidence_score = kw.get("confidence_score", 0.0)
        self.qualified_trades = kw.get("qualified_trades", 0)
        self.pinned = kw.get("pinned", False)
        self.last_seen_at = kw.get("last_seen_at")
        self.last_score_update = kw.get("last_score_update")
        self.updated_at = kw.get("updated_at")


# ── The predicate ────────────────────────────────────────────────────────
def test_the_entity_vocabulary_is_the_existing_canonical_one():
    """One definition of "entity", so gate and lifecycle cannot disagree."""
    for t in ("CEX", "BRIDGE", "TREASURY", "MARKET_MAKER", "CUSTODY",
              "DEX_ROUTER", "LIQUIDITY_POOL", "VAULT", "PDA", "PROGRAM",
              "TOKEN_ACCOUNT", "BURN"):
        assert t in NON_TRADER_ENTITIES
        assert SI._is_non_trader_entity(t) is True
    # A candidate trader is NOT an entity, and neither is an unclassified row.
    assert SI._is_non_trader_entity("TRADER_CANDIDATE") is False
    assert SI._is_non_trader_entity(None) is False
    assert SI._is_non_trader_entity("") is False
    # Terminal statuses count regardless of entity_type.
    assert SI._is_non_trader_entity(None, "EXCLUDED_ENTITY") is True
    assert SI._is_non_trader_entity(None, "ARCHIVED") is True


# ── The shadow gate ──────────────────────────────────────────────────────
def test_the_shadow_gate_refuses_an_entity_wallet_by_name():
    """THE REGRESSION: this reason could never fire before."""
    ev = _swap(_addr(), "Mint" + uuid.uuid4().hex[:20])
    wq = {"measurable": True, "entities": ["CEX1…aaaa"],
          "entity_types": ["CEX"], "max_sample_count": 500}
    v = SI.evaluate([ev], wallet_quality=wq,
                    ctx={"state": "FRESH", "price_usd": 1.0,
                         "price_age_seconds": 5.0, "liquidity_usd": 10 ** 9})
    assert v["state"] == SI.STATE_REFUSED
    assert v["refusal_reason"] == SI.EXCHANGE_OR_ENTITY_WALLET
    assert v["refusal_reason"] in SI.REFUSAL_REASONS
    assert "CEX" in v["reason"]


def test_entity_identity_is_checked_before_wallet_quality():
    """An exchange scores WELL. "Measurable" is what busy custody looks like."""
    ev = _swap(_addr(), "Mint" + uuid.uuid4().hex[:20])
    wq = {"measurable": True, "best_score": 99.0, "max_sample_count": 5000,
          "entities": ["MM01…bbbb"], "entity_types": ["MARKET_MAKER"]}
    v = SI.evaluate([ev], wallet_quality=wq,
                    ctx={"state": "FRESH", "price_usd": 1.0,
                         "price_age_seconds": 5.0, "liquidity_usd": 10 ** 9})
    assert v["refusal_reason"] == SI.EXCHANGE_OR_ENTITY_WALLET, \
        "a high score must not outrank identity"


def test_an_independent_wallet_is_not_affected_by_the_entity_gate():
    """Exact classification only — no collateral refusals."""
    ev = _swap(_addr(), "Mint" + uuid.uuid4().hex[:20])
    wq = {"measurable": True, "entities": [], "entity_types": [],
          "max_sample_count": 40}
    v = SI.evaluate([ev], wallet_quality=wq,
                    ctx={"state": "FRESH", "price_usd": 1.0,
                         "price_age_seconds": 5.0, "liquidity_usd": 10 ** 9})
    assert v["state"] == SI.STATE_ELIGIBLE, v["reason"]


def _committed(*rows):
    """`wallet_quality_snapshot` opens its OWN session, so a flushed-but-
    uncommitted row in a different session is invisible to it. The pytest
    database is a temp file, so committing here is safe — and the rows are
    removed again so no other test sees them."""
    import contextlib

    # Read the addresses BEFORE the commit. Committing expires every
    # instance and closing detaches it, so `r.address` afterwards raises
    # DetachedInstanceError — the same lifecycle trap this repo just fixed
    # in market_context.
    addrs = [r.address for r in rows]

    @contextlib.contextmanager
    def _ctx():
        with get_db() as db:
            for r in rows:
                db.add(r)
        try:
            yield
        finally:
            with get_db() as db:
                db.query(WalletRegistry).filter(
                    WalletRegistry.address.in_(addrs)).delete(
                        synchronize_session=False)
    return _ctx()


def test_quality_snapshot_reports_entities_from_the_registry():
    a_entity, a_trader = _addr("Cex"), _addr("Trd")
    rows = (WalletRegistry(address=a_entity, status="CANDIDATE",
                           entity_type="CEX", measurable=True,
                           smart_money_score=95.0, sample_count=900),
            WalletRegistry(address=a_trader, status="WATCH",
                           entity_type="TRADER_CANDIDATE", measurable=True,
                           smart_money_score=70.0, sample_count=30))
    with _committed(*rows):
        wq = SI.wallet_quality_snapshot([a_entity, a_trader])
        assert len(wq["entities"]) == 1
        assert "CEX" in wq["entity_types"]
        # SAFE LABELS ONLY — never the address.
        assert a_entity not in json.dumps(wq)
        assert wq["entities"][0] == SI.safe_label(a_entity)
        # The independent trader is untouched and still usable.
        assert wq["measurable"] is True


def test_an_excluded_entity_status_alone_trips_the_gate():
    a = _addr()
    with _committed(WalletRegistry(address=a, status="EXCLUDED_ENTITY",
                                   measurable=True, smart_money_score=88.0)):
        wq = SI.wallet_quality_snapshot([a])
        assert wq["entities"] == [SI.safe_label(a)]


# ── The lifecycle ────────────────────────────────────────────────────────
def test_lifecycle_cannot_promote_a_classified_entity():
    for etype in ("CEX", "BRIDGE", "TREASURY", "MARKET_MAKER", "CUSTODY",
                  "DEX_ROUTER", "LIQUIDITY_POOL"):
        w = _Row(status="CANDIDATE", entity_type=etype,
                 smart_money_score=99.0, confidence_score=99.0,
                 qualified_trades=500)
        v = L.evaluate(w)
        assert v["status"] == "EXCLUDED_ENTITY", etype
        assert "not copyable trader alpha" in " ".join(v["reasons"])


def test_a_protocol_flag_alone_blocks_promotion():
    w = _Row(status="CANDIDATE", is_protocol=True, smart_money_score=99.0,
             confidence_score=99.0, qualified_trades=500)
    assert L.evaluate(w)["status"] == "EXCLUDED_ENTITY"


def test_pinned_does_not_override_entity_exclusion():
    """An operator pinning an exchange must not buy it into the pipeline."""
    w = _Row(status="CANDIDATE", entity_type="CEX", pinned=True,
             smart_money_score=99.0, confidence_score=99.0,
             qualified_trades=500)
    assert L.evaluate(w)["status"] == "EXCLUDED_ENTITY"


def test_an_existing_watch_entity_is_reconciled_downward():
    """Already promoted is not a defence."""
    w = _Row(status="WATCH", entity_type="CEX", smart_money_score=99.0,
             confidence_score=99.0, qualified_trades=500,
             last_seen_at="2026-08-21T00:00:00+00:00")
    v = L.evaluate(w)
    assert v["status"] == "EXCLUDED_ENTITY"
    assert v["changed"] is True


def test_an_independent_candidate_still_promotes_normally():
    """The repair must not freeze legitimate promotion."""
    w = _Row(status="CANDIDATE", entity_type="TRADER_CANDIDATE",
             smart_money_score=L.WATCH_MIN_SMART_MONEY + 5,
             confidence_score=L.WATCH_MIN_CONFIDENCE + 5,
             qualified_trades=30)
    assert L.evaluate(w)["status"] == "WATCH"


def test_exclusion_is_idempotent():
    w = _Row(status="EXCLUDED_ENTITY", entity_type="CEX")
    v = L.evaluate(w)
    assert v["status"] == "EXCLUDED_ENTITY"
    assert v["changed"] is False


# ── Population and persistence ───────────────────────────────────────────
def test_monitorable_selection_excludes_entities_even_when_pinned():
    from lib.wallet_registry import (MONITORABLE_STATUSES,
                                     NEVER_MONITOR_STATUSES,
                                     get_monitorable_wallets)

    assert "EXCLUDED_ENTITY" in NEVER_MONITOR_STATUSES
    assert not (MONITORABLE_STATUSES & NEVER_MONITOR_STATUSES)

    a = _addr()
    with get_db() as db:
        db.add(WalletRegistry(address=a, status="EXCLUDED_ENTITY",
                              entity_type="CEX", pinned=True))
        db.flush()
        assert a not in get_monitorable_wallets(db=db)
        db.rollback()


def test_scoring_skips_confirmed_entities():
    from lib import wallet_scoring

    a = _addr()

    def forbidden(*_a, **_k):
        raise AssertionError("an excluded entity must not be scored")

    with get_db() as db:
        db.add(WalletRegistry(address=a, status="EXCLUDED_ENTITY",
                              entity_type="CEX"))
        db.flush()
        out = wallet_scoring.score_wallets([a], db=db,
                                           transfers_fn=forbidden)
        assert out["attempted"] == 0
        assert out["skipped_not_in_registry"] == 1
        db.rollback()


def test_entity_observations_are_retained_for_audit():
    """Excluded is not deleted. The evidence stays."""
    from app.database import WalletObservation

    a = _addr()
    with get_db() as db:
        db.add(WalletRegistry(address=a, status="EXCLUDED_ENTITY",
                              entity_type="CEX"))
        db.add(WalletObservation(wallet_address=a, mint="M",
                                 evidence_class="HOLDER_SNAPSHOT",
                                 alpha_eligible=0, discovery_source="test"))
        db.flush()
        kept = (db.query(WalletObservation)
                .filter(WalletObservation.wallet_address == a).count())
        assert kept == 1, "audit evidence must survive exclusion"
        row = db.query(WalletRegistry).filter_by(address=a).first()
        assert row is not None, "the row is excluded, never deleted"
        assert row.entity_type == "CEX", "the category is retained"
        db.rollback()


# ── Redaction ────────────────────────────────────────────────────────────
def test_no_env_value_or_full_address_reaches_the_status_surface():
    import re

    from lib import wallet_intel_cycle as CY

    blob = json.dumps(CY.status(), default=str)
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", blob)
    for key in ("HELIUS_API_KEY", "HELIUS_WATCH_WALLETS", "ALPACA_API_SECRET"):
        assert key not in blob


def test_the_refusal_reason_is_declared_and_reachable():
    """A declared gate that cannot fire is worse than no gate."""
    import inspect

    body = inspect.getsource(SI.evaluate)
    assert "EXCHANGE_OR_ENTITY_WALLET" in body, \
        "the reason must be emitted by the gate, not merely declared"
    assert SI.EXCHANGE_OR_ENTITY_WALLET in SI.REFUSAL_REASONS
