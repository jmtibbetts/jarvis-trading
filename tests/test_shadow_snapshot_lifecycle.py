"""A snapshot read must outlive the session that loaded it.

THE RUNTIME FAILURE. `PROCESS_SHADOW_EVENTS` died mid-cycle with

    DetachedInstanceError: Instance <TokenActivitySnapshot ...> is not
    bound to a Session; attribute refresh operation cannot proceed

`market_context` loaded whole ORM entities inside `with get_db() as db:` and
then read `row.captured_at` AFTER the block. `get_db()` commits on the way
out, a commit EXPIRES every instance the session loaded, and closing detaches
them — so the first attribute read asks a detached instance to refresh and
raises.

WHY IT STAYED HIDDEN. The loop only touches attributes when the mint
actually HAS snapshots. While prices were collected AFTER processing, most
subject mints had no rows and the body never ran. Collecting prices FIRST —
which is correct, because a SOL-quoted event cannot be valued otherwise —
made `rows` non-empty and the latent defect immediate. Pre-existing, exposed
rather than introduced.

Every test here runs against the isolated pytest database that `conftest.py`
pins to a temp directory; none of it touches the operator database.
"""
from __future__ import annotations

import json
import uuid

from app.database import (TokenActivitySnapshot, WalletRegistry, get_db,
                          now_iso)
from lib import wallet_event_classifier as C
from lib import wallet_shadow_intel as SI

MINT = "SnapMint" + uuid.uuid4().hex[:24]
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T0 = 1_787_000_000.0


def _isolated_db_only():
    """The suite must never point at the operator database."""
    from app.database import DB_PATH

    assert "jarvis-test-db-" in str(DB_PATH), DB_PATH


def _snapshot(mint, *, price, captured_at, liq=250_000.0, symbol="TKN"):
    return TokenActivitySnapshot(
        mint=mint, pool_address="pool-" + uuid.uuid4().hex[:8], symbol=symbol,
        network="solana", captured_at=captured_at, price_usd=price,
        liquidity_usd=liq, volume_h1=1234.0, volume_h24=99_000.0)


def _leg(mint, direction, amount, *, sig, wallet, symbol=None):
    return C.TransferLeg(signature=sig, mint=mint, direction=direction,
                         amount=amount, counterparty="pool",
                         watched_wallet=wallet, symbol=symbol,
                         block_time=T0, observed_ts=T0,
                         parser_version="helius_v1_transfers_v1")


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── The defect itself ────────────────────────────────────────────────────
def test_a_snapshot_read_survives_its_session_closing():
    """THE REGRESSION. Against the old entity query this raised."""
    _isolated_db_only()
    mint = MINT + "A"
    with get_db() as db:
        db.add(_snapshot(mint, price=0.25, captured_at=_iso(T0)))

    # The session that loaded it has committed and closed by now.
    ctx = SI.market_context(mint, T0)

    assert ctx["price_usd"] == 0.25
    assert ctx["state"] == "FRESH"
    assert ctx["liquidity_usd"] == 250_000.0
    assert ctx["symbol"] == "TKN"
    assert ctx["network"] == "solana"
    assert ctx["price_age_seconds"] == 0


def test_the_nearest_snapshot_to_the_event_is_the_one_used():
    """Classification must read the RIGHT snapshot, not merely survive."""
    _isolated_db_only()
    mint = MINT + "B"
    with get_db() as db:
        db.add(_snapshot(mint, price=1.00, captured_at=_iso(T0 - 7200)))
        db.add(_snapshot(mint, price=2.00, captured_at=_iso(T0 - 60)))
        db.add(_snapshot(mint, price=3.00, captured_at=_iso(T0 + 9000)))

    ctx = SI.market_context(mint, T0)
    assert ctx["price_usd"] == 2.00, "nearest in TIME, not first or last"
    assert ctx["price_age_seconds"] == 60


def test_a_snapshot_outside_the_policy_is_stale_not_fresh():
    _isolated_db_only()
    mint = MINT + "C"
    far = SI.PRICE_MAX_AGE_SECONDS + 3600
    with get_db() as db:
        db.add(_snapshot(mint, price=5.0, captured_at=_iso(T0 - far)))

    ctx = SI.market_context(mint, T0)
    assert ctx["state"] == SI.STALE_PRICE
    assert ctx["price_usd"] == 5.0          # reported, and refused by name


def test_a_priceless_snapshot_row_never_becomes_a_price():
    """MISSING IS NOT ZERO."""
    _isolated_db_only()
    mint = MINT + "D"
    with get_db() as db:
        db.add(_snapshot(mint, price=None, captured_at=_iso(T0)))

    ctx = SI.market_context(mint, T0)
    assert ctx["state"] == SI.NO_PRICE
    assert ctx["price_usd"] is None


def test_market_context_returns_plain_values_not_orm_instances():
    """A value that can expire is a value that can fail later."""
    _isolated_db_only()
    mint = MINT + "E"
    with get_db() as db:
        db.add(_snapshot(mint, price=0.5, captured_at=_iso(T0)))

    ctx = SI.market_context(mint, T0)
    for key in ("price_usd", "price_at", "liquidity_usd", "symbol",
                "network", "volume_h1", "volume_h24"):
        assert not hasattr(ctx[key], "_sa_instance_state"), (
            f"{key} carries a live ORM identity out of its session")


# ── The whole path, end to end ───────────────────────────────────────────
def test_processing_completes_and_preserves_watched_wallet_attribution():
    """Attribution must survive classification and reach the gate."""
    _isolated_db_only()
    mint = MINT + "F"
    wallet = "WatchWallet" + uuid.uuid4().hex[:22]
    sig = "sig" + uuid.uuid4().hex

    with get_db() as db:
        db.add(_snapshot(mint, price=1.0, captured_at=_iso(T0)))
        db.add(WalletRegistry(address=wallet, status="WATCH",
                              measurable=True, smart_money_score=70.0,
                              sample_count=30, required_sample_count=8,
                              confidence_score=70.0,
                              wallet_score_version="v2_usd_normalized",
                              last_score_update=now_iso()))

    events = C.classify_all([
        _leg(mint, "in", 1000.0, sig=sig, wallet=wallet),
        _leg(USDC, "out", 500.0, sig=sig, wallet=wallet, symbol="USDC"),
    ])
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == C.TOKEN_BUY
    # ATTRIBUTION SURVIVES CLASSIFICATION.
    assert ev.watched_wallet == wallet

    # The gate resolves THAT wallet out of the registry.
    verdict = SI.evaluate([ev])
    assert verdict["wallets"] == [wallet]
    wq = verdict.get("wallet_quality") or SI.wallet_quality_snapshot([wallet])
    assert wq["measurable"] is True, "a scored WATCH wallet must be usable"
    assert verdict["state"] == SI.STATE_ELIGIBLE, verdict["reason"]
    assert verdict["refusal_reason"] is None


def test_an_unproven_wallet_is_refused_by_name_not_by_crash():
    _isolated_db_only()
    mint = MINT + "G"
    wallet = "ThinWallet" + uuid.uuid4().hex[:22]
    sig = "sig" + uuid.uuid4().hex

    with get_db() as db:
        db.add(_snapshot(mint, price=1.0, captured_at=_iso(T0)))
        db.add(WalletRegistry(address=wallet, status="CANDIDATE",
                              measurable=False, sample_count=3,
                              required_sample_count=8))

    ev = C.classify_group([
        _leg(mint, "in", 1000.0, sig=sig, wallet=wallet),
        _leg(USDC, "out", 500.0, sig=sig, wallet=wallet, symbol="USDC"),
    ])
    verdict = SI.evaluate([ev])
    assert verdict["state"] == SI.STATE_REFUSED
    assert verdict["refusal_reason"] == SI.INSUFFICIENT_WALLET_HISTORY
    assert verdict["refusal_reason"] in SI.REFUSAL_REASONS


def test_wallet_quality_passing_still_defers_to_the_next_gate():
    """A later refusal must name the gate that actually bound."""
    _isolated_db_only()
    mint = MINT + "H"
    wallet = "GoodWallet" + uuid.uuid4().hex[:22]
    sig = "sig" + uuid.uuid4().hex

    with get_db() as db:
        # Priced, but the pool is far below the liquidity floor.
        db.add(_snapshot(mint, price=1.0, captured_at=_iso(T0),
                         liq=SI.MIN_LIQUIDITY_USD / 10.0))
        db.add(WalletRegistry(address=wallet, status="WATCH",
                              measurable=True, smart_money_score=70.0,
                              sample_count=30, confidence_score=70.0))

    ev = C.classify_group([
        _leg(mint, "in", 1000.0, sig=sig, wallet=wallet),
        _leg(USDC, "out", 500.0, sig=sig, wallet=wallet, symbol="USDC"),
    ])
    verdict = SI.evaluate([ev])
    assert verdict["state"] == SI.STATE_REFUSED
    assert verdict["refusal_reason"] == SI.LOW_LIQUIDITY, verdict["reason"]


def test_one_event_is_one_cluster_and_reprocessing_does_not_duplicate():
    """Idempotency is a database fact, not a convention."""
    _isolated_db_only()
    mint = MINT + "I"
    wallet = "IdemWallet" + uuid.uuid4().hex[:22]
    sig = "sig" + uuid.uuid4().hex

    ev = C.classify_group([
        _leg(mint, "in", 1000.0, sig=sig, wallet=wallet),
        _leg(USDC, "out", 500.0, sig=sig, wallet=wallet, symbol="USDC"),
    ])
    first = SI.cluster_key(ev)
    assert first == SI.cluster_key(ev)
    assert len(SI.cluster([ev, ev])) == 1, "the same event is one observation"

    from app.database import WalletShadowEvent as M
    uniques = {tuple(sorted(c.name for c in u.columns))
               for u in M.__table__.constraints
               if u.__class__.__name__ == "UniqueConstraint"}
    assert ("cluster_id",) in uniques
    from app.database import WalletShadowOutcome as O
    o_uniques = {tuple(sorted(c.name for c in u.columns))
                 for u in O.__table__.constraints
                 if u.__class__.__name__ == "UniqueConstraint"}
    assert ("event_id", "horizon") in o_uniques


def test_process_runs_the_whole_path_without_detached_access():
    """The stage that failed in production, driven end to end."""
    _isolated_db_only()
    out = SI.process(limit=50)
    assert isinstance(out, dict)
    for key in ("legs", "events", "clusters", "eligible", "refused"):
        assert key in out
    # Running it twice must not double-count anything.
    again = SI.process(limit=50)
    assert again["clusters"] == out["clusters"]


# ── The cycle's own contract ─────────────────────────────────────────────
def test_a_real_stage_failure_still_reports_cycle_partial():
    """The repair must not hide a future failure behind a green result."""
    from lib import wallet_intel_cycle as CY

    saved = CY._process
    try:
        def boom(**_kw):
            raise RuntimeError("stage exploded")
        CY._process = boom
        CY._reset_for_tests()
        out = CY.run_once(enrich=False, score=False, prices=False)
    finally:
        CY._process = saved
        CY._reset_for_tests()

    assert out["result"] == CY.CYCLE_PARTIAL
    assert out["stages"]["PROCESS_SHADOW_EVENTS"]["state"] == "FAILED"
    assert any("stage exploded" in e for e in out["errors"])


def test_the_status_payload_never_carries_a_full_wallet_address():
    from lib import wallet_intel_cycle as CY

    blob = json.dumps(CY.status(), default=str)
    import re
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", blob), \
        "a full base58 identity reached the public status payload"
    assert SI.safe_label("A" * 44) == "AAAA…AAAA"


def test_every_token_activity_snapshot_consumer_materialises_in_session():
    """The same lifecycle defect must not exist beside the one just fixed."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent
    body = (root / "lib/wallet_shadow_intel.py").read_text(encoding="utf-8")
    ctx = body[body.index("def market_context("):body.index("def expected_costs(")]
    # Columns, not entities: a column tuple owns its values and cannot expire.
    assert "TokenActivitySnapshot.captured_at" in ctx
    assert "db.query(TokenActivitySnapshot)" not in ctx

    # token_price_history already selects columns.
    hist = (root / "lib/token_price_history.py").read_text(encoding="utf-8")
    assert "TokenActivitySnapshot.captured_at" in hist

    # token_surge receives an OPEN session and builds dicts before returning.
    surge = (root / "lib/token_surge.py").read_text(encoding="utf-8")
    lh = surge[surge.index("def load_history("):surge.index("def _update_state(")]
    assert "def load_history(session" in lh
    assert "return [{" in lh
