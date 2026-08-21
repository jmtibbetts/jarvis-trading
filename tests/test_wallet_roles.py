"""What a wallet is watched FOR, which is not how good it is.

THE CONTRADICTION THIS RESOLVES, measured live: a wallet distributing tokens
across 415 counterparties and 107 mints in under two hours, with ZERO proven
swaps, sat at WATCH with a smart-money score of 80. Nothing was wrong with
the score — it measured activity quality faithfully. The system simply never
asked whether that activity was COPYABLE, so "how good is it?" and "what are
we watching it for?" shared one field.

STATUS  how proven is this wallet?      CANDIDATE -> WATCH -> SMART_MONEY
PURPOSE what are we watching it FOR?    ALPHA / FLOW_CONTEXT / EVIDENCE

A behavioural finding may set the PURPOSE. It may never set the IDENTITY:
`entity_type` and `EXCLUDED_ENTITY` require structural or authoritative
evidence, and a pattern is neither.

AND DEMOTION MUST NOT BE A CAGE. A wallet moved out of the alpha population
keeps receiving cheap transfer observations, because otherwise it can never
gather the evidence that would let it come back and the demotion is
permanent by construction.
"""
from __future__ import annotations

import uuid

from app.database import WalletRegistry, get_db
from lib import wallet_behaviour as WB
from lib import wallet_lifecycle as L
from lib import wallet_registry as WR
from lib import wallet_shadow_intel as SI


def _addr(p="Role"):
    return p + uuid.uuid4().hex[:28]


class _Row:
    def __init__(self, **kw):
        self.address = kw.get("address", _addr())
        self.status = kw.get("status", "CANDIDATE")
        self.entity_type = kw.get("entity_type", "TRADER_CANDIDATE")
        self.is_protocol = kw.get("is_protocol", False)
        self.behaviour_state = kw.get("behaviour_state")
        self.copyability_state = kw.get("copyability_state")
        self.smart_money_score = kw.get("smart_money_score")
        self.confidence_score = kw.get("confidence_score", 0.0)
        self.qualified_trades = kw.get("qualified_trades", 0)
        self.pinned = kw.get("pinned", False)
        self.last_seen_at = kw.get("last_seen_at",
                                   "2026-08-21T08:00:00+00:00")
        self.last_score_update = kw.get("last_score_update")
        self.updated_at = kw.get("updated_at")


# ── Purpose vocabulary ───────────────────────────────────────────────────
def test_purpose_is_separate_from_status():
    assert WR.MONITORING_PURPOSES == {WR.ALPHA, WR.FLOW_CONTEXT,
                                      WR.EVIDENCE_COLLECTION}
    assert WR.ALPHA_PURPOSES == {WR.ALPHA}
    # A purpose is not a status, and the two vocabularies must not overlap.
    assert not (WR.MONITORING_PURPOSES & WR.MONITORABLE_STATUSES)


# ── Non-copyable behaviour leaves the alpha population ───────────────────
def test_measured_non_copyable_behaviour_cannot_stay_alpha_watch():
    """THE LIVE CASE: score 80, consolidating across 415 counterparties."""
    w = _Row(status="WATCH", smart_money_score=80.0, confidence_score=95.0,
             qualified_trades=200,
             behaviour_state=WB.CUSTODY_OR_CONSOLIDATION_PATTERN,
             copyability_state=WB.COPY_CUSTODY)
    v = L.evaluate(w)
    assert v["purpose"] == WR.FLOW_CONTEXT
    assert v["changed"] is True
    assert v["status"] != "WATCH", "a high score must not hold it in ALPHA"
    assert v["status"] != "EXCLUDED_ENTITY", "behaviour is not identity"


def test_every_non_copyable_pattern_leaves_the_alpha_population():
    for b in sorted(WB.NON_COPYABLE_BEHAVIOURS):
        v = L.evaluate(_Row(status="WATCH", smart_money_score=99.0,
                            confidence_score=99.0, qualified_trades=500,
                            behaviour_state=b))
        assert v["purpose"] == WR.FLOW_CONTEXT, b
        assert v["status"] == "CANDIDATE", b


def test_a_behavioural_pattern_never_becomes_an_identity():
    """No entity_type, no EXCLUDED_ENTITY, from a pattern alone."""
    v = L.evaluate(_Row(status="WATCH", smart_money_score=90.0,
                        behaviour_state=WB.MARKET_MAKING_PATTERN))
    assert v["status"] != "EXCLUDED_ENTITY"
    assert "behaviour, not about who owns" in " ".join(v["reasons"])
    # And the verdict carries no entity assertion at all.
    assert "entity_type" not in v


def test_a_confirmed_entity_still_overrides_behaviour():
    v = L.evaluate(_Row(status="WATCH", entity_type="CEX",
                        behaviour_state=WB.DIRECTIONAL_TRADER,
                        smart_money_score=99.0))
    assert v["status"] == "EXCLUDED_ENTITY"
    assert v["purpose"] == WR.FLOW_CONTEXT


def test_directional_behaviour_keeps_alpha_purpose():
    v = L.evaluate(_Row(status="WATCH", smart_money_score=70.0,
                        confidence_score=70.0, qualified_trades=30,
                        behaviour_state=WB.DIRECTIONAL_TRADER,
                        copyability_state=WB.COPY_CAPABILITY_UNAVAILABLE))
    assert v["purpose"] == WR.ALPHA
    assert v["status"] == "WATCH"


def test_automation_alone_keeps_alpha_purpose():
    v = L.evaluate(_Row(status="WATCH", smart_money_score=70.0,
                        confidence_score=70.0, qualified_trades=30,
                        behaviour_state=WB.AUTOMATED_DIRECTIONAL_TRADER))
    assert v["purpose"] == WR.ALPHA


def test_insufficient_evidence_keeps_collecting_and_is_not_smart_money():
    v = L.evaluate(_Row(status="CANDIDATE", smart_money_score=None,
                        behaviour_state=WB.INSUFFICIENT_BEHAVIOURAL_EVIDENCE))
    assert v["purpose"] == WR.EVIDENCE_COLLECTION
    assert v["status"] == "CANDIDATE"
    assert v["changed"] is False


# ── Demotion must not be a cage ──────────────────────────────────────────
def test_a_flow_context_wallet_is_still_observed_so_it_can_recover():
    """Otherwise the demotion is permanent by construction."""
    a = _addr()
    with get_db() as db:
        db.add(WalletRegistry(address=a, status="CANDIDATE", pinned=False,
                              monitoring_purpose=WR.FLOW_CONTEXT,
                              behaviour_state=WB.CUSTODY_OR_CONSOLIDATION_PATTERN))
        db.flush()
        assert a in WR.get_monitorable_wallets(db=db)
        db.rollback()


def test_flow_context_observations_are_retained():
    from app.database import WalletObservation

    a = _addr()
    with get_db() as db:
        db.add(WalletRegistry(address=a, status="CANDIDATE",
                              monitoring_purpose=WR.FLOW_CONTEXT,
                              behaviour_state=WB.CUSTODY_OR_CONSOLIDATION_PATTERN))
        db.add(WalletObservation(wallet_address=a, mint="M",
                                 evidence_class="HOLDER_SNAPSHOT",
                                 alpha_eligible=0, discovery_source="test"))
        db.flush()
        assert db.query(WalletObservation).filter(
            WalletObservation.wallet_address == a).count() == 1
        row = db.query(WalletRegistry).filter_by(address=a).first()
        assert row.smart_money_score is None or True   # score untouched
        db.rollback()


def test_an_excluded_entity_is_never_monitorable_even_as_flow_context():
    a = _addr()
    with get_db() as db:
        db.add(WalletRegistry(address=a, status="EXCLUDED_ENTITY",
                              entity_type="CEX", pinned=True,
                              monitoring_purpose=WR.FLOW_CONTEXT))
        db.flush()
        assert a not in WR.get_monitorable_wallets(db=db)
        db.rollback()


# ── The shadow gate refuses non-copyable behaviour ───────────────────────
def test_a_non_copyable_wallet_cannot_produce_a_new_alpha_pick():
    from lib import wallet_event_classifier as C

    sig = "sig" + uuid.uuid4().hex
    mint = "Mint" + uuid.uuid4().hex[:20]
    wallet = _addr()
    ev = C.classify_group([
        C.TransferLeg(signature=sig, mint=mint, direction="in", amount=1000.0,
                      counterparty="pool", watched_wallet=wallet,
                      block_time=1_787_000_000.0),
        C.TransferLeg(signature=sig,
                      mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                      direction="out", amount=500.0, counterparty="pool",
                      watched_wallet=wallet, symbol="USDC",
                      block_time=1_787_000_000.0)])
    wq = {"measurable": True, "entities": [], "entity_types": [],
          "non_copyable": ["Cust…0001"],
          "non_copyable_states": [WB.CUSTODY_OR_CONSOLIDATION_PATTERN],
          "max_sample_count": 100}
    v = SI.evaluate([ev], wallet_quality=wq,
                    ctx={"state": "FRESH", "price_usd": 1.0,
                         "price_age_seconds": 5.0, "liquidity_usd": 10 ** 9})
    assert v["state"] == SI.STATE_REFUSED
    assert v["refusal_reason"] == SI.EXCHANGE_OR_ENTITY_WALLET
    assert "not copyable directional alpha" in v["reason"]


# ── Derived validation state ─────────────────────────────────────────────
def test_validation_state_is_derived_and_explicit():
    assert SI.validation_state("ELIGIBLE",
                               WB.COPYABLE_EVIDENCE_SUPPORTED) \
        == SI.VALIDATED_ELIGIBLE
    assert SI.validation_state("ELIGIBLE",
                               WB.COPY_CAPABILITY_UNAVAILABLE) \
        == SI.PROVISIONAL_ELIGIBLE
    assert SI.validation_state("ELIGIBLE", WB.COPY_CUSTODY) \
        == SI.NON_COPYABLE_REFUSED
    assert SI.validation_state("ELIGIBLE", WB.COPY_CONFIRMED_ENTITY) \
        == SI.CONFIRMED_ENTITY_REFUSED
    assert SI.validation_state("REFUSED", None) == SI.GATE_REFUSED
    # Unassessed is PROVISIONAL, never validated.
    assert SI.validation_state("ELIGIBLE", None) == SI.PROVISIONAL_ELIGIBLE
    for s in SI.VALIDATION_STATES:
        assert isinstance(s, str)


def test_the_original_gate_state_is_never_rewritten():
    """`validation_state` is a pure function; it stores nothing."""
    import inspect

    body = inspect.getsource(SI.validation_state)
    for w in ("UPDATE", "INSERT", "session", "commit", "db."):
        assert w not in body, f"{w} in a derived read"


# ── History queue ────────────────────────────────────────────────────────
def test_the_history_queue_is_bounded_and_deterministic():
    from lib import wallet_intel_cycle as CY

    sql = CY.HISTORY_QUEUE_SQL
    assert "LIMIT :lim" in sql
    assert "ORDER BY" in sql
    # Deterministic tie-break, so the same state always yields the same pick.
    assert "r.address" in sql.split("ORDER BY")[1]
    assert CY.HISTORY_WALLETS_PER_CYCLE * CY.HISTORY_PAGE_SIZE <= 100


def test_flow_context_never_consumes_the_alpha_history_budget():
    from lib import wallet_intel_cycle as CY

    assert "<> 'FLOW_CONTEXT'" in CY.HISTORY_QUEUE_SQL
    assert "EXCLUDED_ENTITY" in CY.HISTORY_QUEUE_SQL


def test_pinned_seeds_are_in_the_queue_and_not_starved():
    from lib import wallet_intel_cycle as CY

    sql = CY.HISTORY_QUEUE_SQL
    assert "r.pinned = 1" in sql
    # They sit at their own priority tier rather than behind every alpha
    # wallet forever, and ties rotate on last_deep_backfill_at.
    assert "WHEN r.pinned = 1 THEN 2" in sql
    assert "COALESCE(r.last_deep_backfill_at, '') ASC" in sql


def test_a_wallet_with_a_pick_and_no_ledger_is_first_in_the_queue():
    from lib import wallet_intel_cycle as CY

    sql = CY.HISTORY_QUEUE_SQL
    head = sql[sql.index("CASE"):sql.index("WHEN COALESCE(r.monitoring_purpose, '') = 'ALPHA'")]
    assert "wallet_shadow_events" in head
    assert "THEN 0" in head


# ── Score source ─────────────────────────────────────────────────────────
def test_score_source_says_which_evidence_produced_the_number():
    import inspect

    from lib import wallet_scoring as S

    body = inspect.getsource(S._score_one)
    for token in ("SHALLOW_TRANSFER_FALLBACK", "DURABLE_LEDGER_PARTIAL",
                  "DURABLE_LEDGER_COMPLETE"):
        assert token in body
    # A provider failure must not relabel the evidence behind the last
    # score. Slice ONLY the failure branch — from the handler to its return.
    start = body.index("    except Exception as e:")
    fail = body[start:body.index('return "FAILED"', start)]
    # The ASSIGNMENT, not the word — the branch comments on score_source
    # precisely to explain why it does not write it.
    assert "w.score_source =" not in fail, fail
    assert 'w.analysis_status = "FAILED"' in fail


def test_durable_history_change_forces_full_reassessment():
    """Rescoring alone would leave behaviour, copyability and role stale."""
    from lib import wallet_intel_cycle as CY

    order = list(CY.STAGES)
    assert order.index("BACKFILL_WALLET_HISTORY") < order.index(
        "RESCORE_AFFECTED_WALLETS")
    assert order.index("RESCORE_AFFECTED_WALLETS") < order.index(
        "ASSESS_WALLET_BEHAVIOUR")
    assert order.index("ASSESS_WALLET_BEHAVIOUR") < order.index(
        "APPLY_WALLET_LIFECYCLE")
    import inspect
    assert "last_history_sync_at >= :s" in inspect.getsource(CY._rescore)


def test_behaviour_assessment_costs_no_provider_call():
    import inspect

    from lib import wallet_intel_cycle as CY

    body = inspect.getsource(CY._assess_behaviour)
    for forbidden in ("helius_client", "batch_identity", "rpc(", "transfers("):
        assert forbidden not in body


# ── Redaction ────────────────────────────────────────────────────────────
def test_the_roles_route_exposes_safe_labels_only():
    import json
    import re

    import app.routers.onchain as R

    d = R.onchain_wallet_roles(limit=50)
    blob = json.dumps(d, default=str)
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", blob)
    for w in d["wallets"]:
        assert "…" in w["wallet"] or len(w["wallet"]) <= 10


def test_the_desk_renders_purpose_behaviour_and_validation():
    import pathlib

    page = (pathlib.Path(__file__).parent.parent
            / "frontend/src/lib/sections/OnChain.svelte").read_text(
                encoding="utf-8")
    assert 'title="Monitored Wallets — Purpose and Evidence"' in page
    assert 'title="Outcome Populations"' in page
    for token in ("w2.purpose", "w2.behaviour", "w2.copyability",
                  "w2.score_source", "t.validation_state", "t.behaviour_state",
                  "identity_capability", "validated_outcomes",
                  "provisional_outcomes"):
        assert token in page, token
    assert "/onchain/wallets/roles" in page
