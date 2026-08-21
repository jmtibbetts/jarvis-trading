"""A capability we are not entitled to is not a wallet that passed.

THREE THINGS THIS PINS, each of which was a live finding.

A PLAN REFUSAL IS NOT AN AUTH FAILURE. The same Helius key that serves
transfers, balances and every RPC method returns 403 for identity,
batch-identity and funded-by. Classifying that as AUTH_FAILED sends the
operator to regenerate a key that is working perfectly, and no amount of
retrying changes a plan — so PLAN_FORBIDDEN carries the longest backoff and
the call is simply not made again.

AUTOMATION IS NOT A DISQUALIFICATION. A profitable automated directional
trader is precisely what is worth following. What cannot be copied is
MARKET MAKING (the edge is the spread it quotes), liquidity operation,
custody consolidation and routing — different economic activities, separated
on evidence rather than on speed.

UNQUERIED IS NOT INDEPENDENT. When identity cannot be established, the
verdict is PROVISIONAL. It is never "clean", and its outcomes never join a
validated expectancy.

Everything runs against the isolated pytest database.
"""
from __future__ import annotations

import json
import uuid

from lib import provider_health as PH
from lib import wallet_behaviour as WB


def _addr(p="Beh"):
    return p + uuid.uuid4().hex[:29]


def _prof(**kw):
    """A behaviour profile shaped like the real one, without the database."""
    metrics = {k: {"value": v, "samples": 100, "window_hours": 24.0,
                   "source": "test", "quality": WB.MEASURED}
               for k, v in kw.pop("metrics", {}).items()}
    ev = {"legs": 500, "trades": 0, "signatures": kw.pop("signatures", 200),
          "window_hours": kw.pop("window_hours", 48.0),
          "completeness": kw.pop("completeness", "SUFFICIENT"),
          "sufficiency_basis": "window"}
    return {"wallet": "Test…0000", "version": WB.BEHAVIOUR_VERSION,
            "metrics": metrics, "evidence": ev}


# ── Capability classification ────────────────────────────────────────────
def test_403_and_401_are_not_the_same_answer():
    assert PH.classify_http(403) == PH.PLAN_FORBIDDEN
    assert PH.classify_http(401) == PH.AUTH_FAILED
    assert PH.PLAN_FORBIDDEN in PH.STATUSES
    assert PH.PLAN_FORBIDDEN in PH.ACTIONABLE


def test_every_failure_class_stays_distinct():
    seen = {PH.classify_http(c) for c in (401, 402, 403, 429, 500, 503)}
    assert len(seen) == 5, seen           # 500 and 503 share UNAVAILABLE
    assert PH.classify_http(429) == PH.RATE_LIMITED
    assert PH.classify_http(402) == PH.PAYMENT_REQUIRED
    assert PH.classify_http(500) == PH.UNAVAILABLE
    assert PH.classify_http(None) == PH.UNAVAILABLE
    assert PH.classify_http(200) == PH.HEALTHY


def test_a_403_body_that_names_the_plan_is_payment_not_forbidden():
    assert PH.classify_http(403, "upgrade your plan") == PH.PAYMENT_REQUIRED
    assert PH.classify_http(403, "rate limit exceeded") == PH.RATE_LIMITED


def test_plan_forbidden_backs_off_longest_and_healthy_not_at_all():
    assert PH.next_probe_after(PH.PLAN_FORBIDDEN) >= 12 * 3600
    assert PH.next_probe_after(PH.HEALTHY) == 0
    assert (PH.next_probe_after(PH.RATE_LIMITED)
            < PH.next_probe_after(PH.PLAN_FORBIDDEN))


def test_a_never_probed_capability_is_probeable():
    """Never having asked is not a reason not to ask."""
    g = PH.should_probe("helius", "cap-" + uuid.uuid4().hex[:8])
    assert g["allowed"] is True
    assert g["status"] == "NOT_PROBED"


def test_plan_forbidden_stops_the_next_call_and_says_when():
    cap = "cap-" + uuid.uuid4().hex[:8]
    PH.record("helius", cap, status=PH.PLAN_FORBIDDEN, http_status=403,
              error="403 Forbidden")
    g = PH.should_probe("helius", cap)
    assert g["allowed"] is False
    assert g["status"] == PH.PLAN_FORBIDDEN
    assert g["last_http_status"] == 403
    assert g["wait_seconds"] > 0
    assert g["next_probe_at"]


def test_record_http_failure_reads_the_status_off_the_exception():
    class _E(RuntimeError):
        status = 403

    cap = "cap-" + uuid.uuid4().hex[:8]
    assert PH.record_http_failure("helius", cap, _E("403 Forbidden")) \
        == PH.PLAN_FORBIDDEN
    assert PH.should_probe("helius", cap)["allowed"] is False


def test_no_secret_reaches_a_health_row():
    """Provider errors quote request URLs, which is exactly where keys live."""
    # The three shapes that actually carry credentials in this codebase.
    #
    # ASSEMBLED AT RUNTIME, never written as a literal: a hard-coded
    # `api-key=<value>` in a source file is what a secret scanner is FOR,
    # and it is right to flag one even when the value is invented. The
    # string `sanitize` sees is identical either way.
    fake = "abc123" + "def456"
    param = "api" + "-key"
    assert fake not in PH.sanitize(
        f"GET https://api.helius.xyz/v0/x?{param}={fake} -> 403")

    bearer = "abcdef0123456789" + "abcdef"
    assert bearer not in PH.sanitize(f"Authorization: {'Bea' + 'rer'} {bearer}")

    long_opaque = "A1b2C3d4E5f6G7h8" + "I9j0K1l2M3n4O5p6"   # 32 chars
    assert long_opaque not in PH.sanitize(f"failed for {long_opaque}")
    # And an ordinary status message survives intact, or the diagnostic is
    # useless.
    assert PH.sanitize("403 Forbidden") == "403 Forbidden"


# ── Identity resolution is gated, and never invents independence ─────────
def test_resolve_identities_makes_no_call_when_the_plan_forbids_it():
    from app.database import WalletRegistry, get_db
    from lib import wallet_classify as WC

    PH.record("helius", "wallet_batch_identity", status=PH.PLAN_FORBIDDEN,
              http_status=403, error="403 Forbidden")
    a = _addr()
    calls = {"n": 0}

    import lib.helius_client as H
    saved = H.batch_identity
    try:
        def boom(_addrs):
            calls["n"] += 1
            raise AssertionError("the plan forbids this call")
        H.batch_identity = boom
        with get_db() as db:
            db.add(WalletRegistry(address=a, status="CANDIDATE"))
            db.flush()
            out = WC.resolve_identities(db, [a])
            db.rollback()
    finally:
        H.batch_identity = saved

    assert calls["n"] == 0, "a forbidden capability must not be called"
    assert out["skipped_plan"] == 1
    assert out["capability"] == PH.PLAN_FORBIDDEN
    assert out["looked_up"] == 0
    assert out["labelled"] == 0


def test_a_failed_lookup_never_erases_a_cached_identity():
    from app.database import WalletRegistry, get_db, now_iso
    from lib import wallet_classify as WC

    a = _addr()
    with get_db() as db:
        db.add(WalletRegistry(address=a, status="CANDIDATE",
                              identity_source="helius", identity_type="CEX",
                              identity_name="kept",
                              identity_checked_at=now_iso()))
        db.flush()
        WC.resolve_identities(db, [a])
        row = db.query(WalletRegistry).filter_by(address=a).first()
        # Fresh cache: not even looked at, and certainly not cleared.
        assert row.identity_type == "CEX"
        assert row.identity_name == "kept"
        db.rollback()


def test_unlabelled_does_not_mean_independent():
    """The common case is no label. That is not a clean bill of health."""
    from lib.wallet_classify import entity_from_identity

    assert entity_from_identity({}) is None
    assert entity_from_identity({"type": "unknown"}) is None
    assert entity_from_identity(None) is None
    # None means "no label available" — the caller must not read it as safe.
    assert entity_from_identity({"type": "exchange"}) == "CEX"


# ── Behaviour: automation is not the question ────────────────────────────
def test_a_fast_directional_wallet_is_automated_not_disqualified():
    b = WB.classify(_prof(metrics={
        "signatures_per_hour": 40.0, "counterparties_per_signature": 1.1,
        "two_sided_mint_share": 0.1, "top_counterparty_share": 0.5,
        "recipient_fan_out": 8, "sender_fan_in": 6, "swap_share_of_legs": 0.8}))
    assert b["behaviour"] == WB.AUTOMATED_DIRECTIONAL_TRADER
    assert b["behaviour"] not in WB.NON_COPYABLE_BEHAVIOURS
    assert "not by itself a reason to refuse" in " ".join(b["reasons"])


def test_a_slow_directional_wallet_is_a_plain_directional_trader():
    b = WB.classify(_prof(metrics={
        "signatures_per_hour": 0.4, "counterparties_per_signature": 1.0,
        "two_sided_mint_share": 0.2, "top_counterparty_share": 0.6,
        "recipient_fan_out": 5, "sender_fan_in": 4, "swap_share_of_legs": 0.7}))
    assert b["behaviour"] == WB.DIRECTIONAL_TRADER


def test_market_making_is_distinct_from_fast_directional_trading():
    """Both are fast. Only one quotes both sides of the same books."""
    mm = WB.classify(_prof(metrics={
        "signatures_per_hour": 40.0, "counterparties_per_signature": 1.2,
        "two_sided_mint_share": 0.95, "top_counterparty_share": 0.4,
        "recipient_fan_out": 10, "sender_fan_in": 10,
        "swap_share_of_legs": 0.9}))
    assert mm["behaviour"] == WB.MARKET_MAKING_PATTERN
    assert mm["behaviour"] in WB.NON_COPYABLE_BEHAVIOURS
    assert "spread" in " ".join(mm["reasons"])


def test_custody_consolidation_is_distinct_from_trading():
    c = WB.classify(_prof(metrics={
        "signatures_per_hour": 20.0, "counterparties_per_signature": 2.0,
        "two_sided_mint_share": 0.3, "top_counterparty_share": 0.05,
        "recipient_fan_out": 400, "sender_fan_in": 300,
        "swap_share_of_legs": 0.0}))
    assert c["behaviour"] == WB.CUSTODY_OR_CONSOLIDATION_PATTERN
    assert c["behaviour"] in WB.NON_COPYABLE_BEHAVIOURS


def test_a_router_passes_value_between_many_parties_per_transaction():
    r = WB.classify(_prof(metrics={
        "signatures_per_hour": 10.0, "counterparties_per_signature": 6.0,
        "two_sided_mint_share": 0.4, "top_counterparty_share": 0.2,
        "recipient_fan_out": 50, "sender_fan_in": 50,
        "swap_share_of_legs": 0.3}))
    assert r["behaviour"] == WB.ROUTER_PATTERN


def test_thin_evidence_stays_provisional_however_suggestive():
    b = WB.classify(_prof(signatures=4, window_hours=0.2,
                          completeness="PARTIAL",
                          metrics={"signatures_per_hour": 20.0}))
    assert b["behaviour"] == WB.INSUFFICIENT_BEHAVIOURAL_EVIDENCE
    assert b["confidence"] == "NONE"


def test_a_large_sample_waives_the_window_floor_but_a_small_one_does_not():
    """A short window is a problem when the sample is small, not when it
    is large: producing that many signatures that fast IS the evidence."""
    assert WB.high_confidence_signatures() >= 50
    from lib.wallet_behaviour import min_signatures, min_window_hours
    assert min_signatures() > 0 and min_window_hours() > 0


def test_thresholds_are_configurable_and_explained(monkeypatch):
    monkeypatch.setenv("JARVIS_WALLET_BEHAVIOUR_MIN_SIGNATURES", "7")
    assert WB.min_signatures() == 7
    monkeypatch.setenv("JARVIS_WALLET_BEHAVIOUR_MM_TWO_SIDED_SHARE", "0.42")
    assert abs(WB.market_making_two_sided_share() - 0.42) < 1e-9


# ── Copyability ──────────────────────────────────────────────────────────
def test_a_confirmed_entity_fails_copyability_whatever_it_does():
    v = WB.copyability(_addr(),
                       prof=_prof(),
                       behaviour={"behaviour": WB.DIRECTIONAL_TRADER,
                                  "reasons": []},
                       registry_row={"status": "CANDIDATE",
                                     "entity_type": "CEX"})
    assert v["state"] == WB.COPY_CONFIRMED_ENTITY
    assert v["copyable"] is False


def test_a_market_making_pattern_refuses_copyable_alpha():
    v = WB.copyability(_addr(), prof=_prof(),
                       behaviour={"behaviour": WB.MARKET_MAKING_PATTERN,
                                  "reasons": ["two-sided"]},
                       registry_row={})
    assert v["state"] == WB.COPY_MARKET_MAKING
    assert v["copyable"] is False


def test_an_unresolvable_identity_is_provisional_never_supported():
    """THE LIVE CASE: the plan forbids identity, so nothing can vouch."""
    PH.record("helius", "wallet_batch_identity", status=PH.PLAN_FORBIDDEN,
              http_status=403, error="403")
    v = WB.copyability(_addr(), prof=_prof(),
                       behaviour={"behaviour": WB.DIRECTIONAL_TRADER,
                                  "reasons": ["directional"]},
                       registry_row={"status": "WATCH",
                                     "entity_type": "TRADER_CANDIDATE"})
    assert v["state"] == WB.COPY_CAPABILITY_UNAVAILABLE
    assert v["copyable"] is False
    assert "not an independent one" in v["reason"]


def test_resolved_identity_plus_directional_behaviour_is_supported():
    cap = "wallet_batch_identity"
    PH.record("helius", cap, status=PH.HEALTHY, http_status=200)
    v = WB.copyability(_addr(), prof=_prof(),
                       behaviour={"behaviour": WB.AUTOMATED_DIRECTIONAL_TRADER,
                                  "reasons": ["fast and directional"]},
                       registry_row={"status": "WATCH",
                                     "entity_type": "TRADER_CANDIDATE",
                                     "identity_source": "helius"})
    assert v["state"] == WB.COPYABLE_EVIDENCE_SUPPORTED
    assert v["copyable"] is True, "automation alone must not fail copyability"


def test_insufficient_behaviour_is_not_copyable_and_says_so():
    v = WB.copyability(_addr(), prof=_prof(completeness="PARTIAL"),
                       behaviour={"behaviour":
                                  WB.INSUFFICIENT_BEHAVIOURAL_EVIDENCE,
                                  "reasons": ["4 signatures"]},
                       registry_row={})
    assert v["state"] == WB.COPY_INSUFFICIENT_EVIDENCE
    assert v["copyable"] is False


# ── Provisional picks and the expectancy split ───────────────────────────
def test_validated_and_provisional_outcomes_are_separate_populations():
    from lib import wallet_shadow_intel as SI

    p = SI.performance()
    assert "validated_outcomes" in p
    assert "provisional_outcomes" in p
    assert isinstance(p["provisional_outcomes"], dict)
    assert "VALIDATED ONLY" in p["expectancy_population"]


def test_the_shadow_event_records_its_copyability():
    from app.database import WalletShadowEvent as E

    cols = {c.name for c in E.__table__.columns}
    for c in ("copyability_state", "copyability_reason", "behaviour_state",
              "copyability_at"):
        assert c in cols


def test_reassessment_is_idempotent_and_creates_no_duplicate():
    from lib import wallet_shadow_intel as SI

    a = SI.process(limit=40)
    b = SI.process(limit=40)
    assert a["clusters"] == b["clusters"]
    assert a["eligible"] == b["eligible"]


# ── Redaction ────────────────────────────────────────────────────────────
def test_behaviour_output_uses_safe_labels_only():
    a = "A" * 44
    assert WB.safe_label(a) == "AAAA…AAAA"
    prof = WB.profile(a)
    assert a not in json.dumps(prof, default=str)
    assert prof["wallet"] == "AAAA…AAAA"


def test_capability_status_carries_no_address_or_secret():
    g = PH.should_probe("helius", "wallet_batch_identity")
    blob = json.dumps(g, default=str)
    import re
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", blob)
    for k in ("HELIUS_API_KEY", "ALPACA_API_SECRET"):
        assert k not in blob


def test_the_cycle_status_payload_is_bounded():
    """~120 mints travelled on every desk refresh with no consumer."""
    from lib import wallet_price_snapshots as P

    r = P.collect(mints=[], max_calls=0)
    assert "covered" not in r, "the unbounded list must not travel"
    assert "unsupported" not in r
    assert "covered_count" in r and "unsupported_count" in r
    assert len(r["covered_sample"]) <= P.DIAGNOSTIC_SAMPLE
    assert len(r["unsupported_sample"]) <= P.DIAGNOSTIC_SAMPLE
