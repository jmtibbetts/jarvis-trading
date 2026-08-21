"""Regression tests for durable wallet-history scoring.

The wallet cycle used to claim it had drained history while scoring only the
newest 100 transfer legs.  These tests pin the stronger contract: once the
balance-delta ledger exists, scoring consumes it and never substitutes a
shallow transfer page.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.database import WalletRegistry, WalletTrade, get_db
from lib import wallet_scoring


def _address() -> str:
    return "LedgerScore" + uuid.uuid4().hex[:24]


def _trade(address: str, *, mint: str, direction: str, qty: float,
           value: float, when: datetime) -> WalletTrade:
    return WalletTrade(
        address=address, signature=uuid.uuid4().hex, mint=mint,
        counterparty="", direction=direction, quantity=qty,
        value_usd=value, price=value / qty, price_source="test",
        price_quality="MEASURED", ledger_version="swap_v1_balance_delta",
        opened_at=when.isoformat(), population="WALLET_ALPHA")


def test_ledger_fifo_reconstructs_closed_round_trips():
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(8):
        mint = f"Mint{i}"
        rows.append(_trade("wallet", mint=mint, direction="buy", qty=10,
                           value=100, when=now + timedelta(minutes=i * 2)))
        rows.append(_trade("wallet", mint=mint, direction="sell", qty=10,
                           value=110, when=now + timedelta(minutes=i * 2 + 1)))

    reconstructed = wallet_scoring.reconstruct_ledger_trades(rows)
    assert reconstructed["closed"] == 8
    assert reconstructed["still_open"] == 0
    assert reconstructed["source"] == "WALLET_TRADE_BALANCE_DELTA_LEDGER"
    assert all(t["pnl_usd"] == 10 for t in reconstructed["trades"])


def test_named_scoring_prefers_ledger_and_never_fetches_shallow_page():
    address = _address()
    now = datetime.now(timezone.utc)

    def forbidden_transfer_fetch(*_args, **_kwargs):
        raise AssertionError("the durable ledger must make this unnecessary")

    with get_db() as db:
        wallet = WalletRegistry(address=address, status="CANDIDATE", pinned=1)
        db.add(wallet)
        for i in range(8):
            mint = f"Mint{i}"
            db.add(_trade(address, mint=mint, direction="buy", qty=10,
                          value=100, when=now + timedelta(minutes=i * 2)))
            db.add(_trade(address, mint=mint, direction="sell", qty=10,
                          value=110, when=now + timedelta(minutes=i * 2 + 1)))
        db.flush()

        result = wallet_scoring.score_wallets(
            [address], db=db, transfers_fn=forbidden_transfer_fetch)

        assert result["attempted"] == 1
        assert result["scored"] == 1
        assert wallet.measurable is True
        assert wallet.sample_count == 8
        assert wallet.smart_money_score is not None
        db.rollback()


def test_history_advance_forces_rescore_even_after_old_refusal():
    import inspect

    source = inspect.getsource(__import__(
        "lib.wallet_intel_cycle", fromlist=["_rescore"])._rescore)
    assert "last_history_sync_at >= :s" in source
    assert source.index("last_history_sync_at >= :s") < source.index(
        "analysis_status NOT IN")


def test_cycle_has_bounded_deep_history_stage():
    from lib import wallet_intel_cycle

    assert "BACKFILL_WALLET_HISTORY" in wallet_intel_cycle.STAGES
    assert wallet_intel_cycle.HISTORY_WALLETS_PER_CYCLE == 1
    assert 1 <= wallet_intel_cycle.HISTORY_PAGE_SIZE <= 100
    assert "APPLY_WALLET_LIFECYCLE" in wallet_intel_cycle.STAGES


def test_cycle_uses_existing_lifecycle_after_scoring():
    from lib import wallet_intel_cycle

    assert wallet_intel_cycle.STAGES.index(
        "RESCORE_AFFECTED_WALLETS") < wallet_intel_cycle.STAGES.index(
            "APPLY_WALLET_LIFECYCLE")
    assert wallet_intel_cycle.STAGES.index(
        "APPLY_WALLET_LIFECYCLE") < wallet_intel_cycle.STAGES.index(
            "PROCESS_SHADOW_EVENTS")
    assert wallet_intel_cycle.STAGES.index(
        "COLLECT_PRICE_SNAPSHOTS") < wallet_intel_cycle.STAGES.index(
            "RESCORE_AFFECTED_WALLETS")


# ─────────────────────────────────────────────────────────────────────────
# FIFO arithmetic. Each of these produced a WRONG NUMBER rather than an
# error, which is the only reason they are worth pinning: a double-counted
# proceed and a fabricated cost basis both look exactly like a profitable
# wallet.
# ─────────────────────────────────────────────────────────────────────────
def test_several_buy_lots_close_against_one_sell_at_blended_cost():
    """Two lots at different prices, one sale. Cost basis is BOTH lots."""
    now = datetime.now(timezone.utc)
    rows = [
        _trade("w", mint="M", direction="buy", qty=10, value=100,
               when=now),                                    # $10/unit
        _trade("w", mint="M", direction="buy", qty=10, value=300,
               when=now + timedelta(minutes=1)),             # $30/unit
        _trade("w", mint="M", direction="sell", qty=20, value=500,
               when=now + timedelta(minutes=2)),
    ]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["closed"] == 1
    t = r["trades"][0]
    assert t["cost_basis_usd"] == 400.0        # 100 + 300, not 100 and not 300
    assert t["proceeds_usd"] == 500.0
    assert t["pnl_usd"] == 100.0
    assert r["still_open"] == 0


def test_one_buy_closed_by_several_partial_sells_never_double_counts():
    """The lot must be consumed once. Total cost basis cannot exceed it."""
    now = datetime.now(timezone.utc)
    rows = [
        _trade("w", mint="M", direction="buy", qty=100, value=1000,
               when=now),
        _trade("w", mint="M", direction="sell", qty=40, value=500,
               when=now + timedelta(minutes=1)),
        _trade("w", mint="M", direction="sell", qty=60, value=700,
               when=now + timedelta(minutes=2)),
    ]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["closed"] == 2
    costs = [t["cost_basis_usd"] for t in r["trades"]]
    assert costs == [400.0, 600.0]
    assert sum(costs) == 1000.0                # exactly the lot, never twice
    assert sum(t["proceeds_usd"] for t in r["trades"]) == 1200.0
    assert r["still_open"] == 0


def test_a_sell_larger_than_the_lot_only_books_the_matched_fraction():
    """Selling 100 against a 40-unit lot is not a 100-unit round trip.

    Scaling the proceeds is the whole guard: booking all $1,000 against a
    $400 basis would invent a 150% return out of a partial match.
    """
    now = datetime.now(timezone.utc)
    rows = [
        _trade("w", mint="M", direction="buy", qty=40, value=400, when=now),
        _trade("w", mint="M", direction="sell", qty=100, value=1000,
               when=now + timedelta(minutes=1)),
    ]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    t = r["trades"][0]
    assert t["cost_basis_usd"] == 400.0
    assert t["proceeds_usd"] == 400.0          # 1000 * (40/100)
    assert t["pnl_usd"] == 0.0
    assert t["return_pct"] == 0.0


def test_a_sell_with_no_prior_buy_is_unmeasurable_not_profit():
    """The acquisition predates the bounded history. It is NOT free money."""
    now = datetime.now(timezone.utc)
    rows = [_trade("w", mint="M", direction="sell", qty=10, value=900,
                   when=now)]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["closed"] == 0
    assert r["trades"] == []
    assert r["unpriced_legs"] == 1
    assert any("no earlier priced buy" in x for x in r["unpriced_reasons"])
    # And it must not reach a score.
    assert wallet_scoring.score_wallet(r)["measurable"] is False


def test_an_unvalued_ledger_row_stays_unpriced_and_never_becomes_zero():
    """MISSING PRICE IS NOT ZERO — a $0 basis is an infinite return."""
    now = datetime.now(timezone.utc)
    buy = _trade("w", mint="M", direction="buy", qty=10, value=100, when=now)
    buy.value_usd = None
    rows = [buy,
            _trade("w", mint="M", direction="sell", qty=10, value=110,
                   when=now + timedelta(minutes=1))]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["closed"] == 0                    # the buy never opened a lot
    assert r["unpriced_legs"] == 2             # unvalued buy + unmatched sell
    assert all(t["cost_basis_usd"] > 0 for t in r["trades"])


def test_a_zero_valued_row_is_treated_as_unpriced_too():
    now = datetime.now(timezone.utc)
    buy = _trade("w", mint="M", direction="buy", qty=10, value=100, when=now)
    buy.value_usd = 0.0
    r = wallet_scoring.reconstruct_ledger_trades([buy])
    assert r["closed"] == 0
    assert r["unpriced_legs"] == 1


def test_one_estimated_leg_demotes_the_whole_round_trip():
    """A MEASURED exit against an ESTIMATED entry is an ESTIMATED trip."""
    now = datetime.now(timezone.utc)
    buy = _trade("w", mint="M", direction="buy", qty=10, value=100, when=now)
    buy.price_quality = "ESTIMATED"
    sell = _trade("w", mint="M", direction="sell", qty=10, value=110,
                  when=now + timedelta(minutes=1))
    sell.price_quality = "MEASURED"
    r = wallet_scoring.reconstruct_ledger_trades([buy, sell])
    assert r["trades"][0]["price_quality"] == wallet_scoring.ESTIMATED


def test_all_measured_stays_measured():
    now = datetime.now(timezone.utc)
    rows = [_trade("w", mint="M", direction="buy", qty=10, value=100,
                   when=now),
            _trade("w", mint="M", direction="sell", qty=10, value=110,
                   when=now + timedelta(minutes=1))]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["trades"][0]["price_quality"] == wallet_scoring.MEASURED


def test_the_real_ledger_writes_uppercase_directions():
    """`wallet_swaps` stores BUY/SELL, not buy/sell. Both must reconstruct."""
    from lib import wallet_swaps

    assert (wallet_swaps.BUY, wallet_swaps.SELL) == ("BUY", "SELL")
    now = datetime.now(timezone.utc)
    rows = [_trade("w", mint="M", direction=wallet_swaps.BUY, qty=10,
                   value=100, when=now),
            _trade("w", mint="M", direction=wallet_swaps.SELL, qty=10,
                   value=130, when=now + timedelta(minutes=1))]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["closed"] == 1
    assert r["trades"][0]["pnl_usd"] == 30.0


def test_out_of_order_rows_are_sorted_before_matching():
    """A sell must never close a lot that did not exist yet."""
    now = datetime.now(timezone.utc)
    sell = _trade("w", mint="M", direction="sell", qty=10, value=110,
                  when=now + timedelta(minutes=1))
    buy = _trade("w", mint="M", direction="buy", qty=10, value=100, when=now)
    r = wallet_scoring.reconstruct_ledger_trades([sell, buy])
    assert r["closed"] == 1
    assert r["trades"][0]["opened_ts"] < r["trades"][0]["closed_ts"]


def test_positions_in_different_mints_do_not_cross_match():
    now = datetime.now(timezone.utc)
    rows = [_trade("w", mint="A", direction="buy", qty=10, value=100,
                   when=now),
            _trade("w", mint="B", direction="sell", qty=10, value=900,
                   when=now + timedelta(minutes=1))]
    r = wallet_scoring.reconstruct_ledger_trades(rows)
    assert r["closed"] == 0                    # B's sale cannot use A's lot
    assert r["still_open"] == 1


# ─────────────────────────────────────────────────────────────────────────
# Which evidence scoring consumes, and what happens when it cannot.
# ─────────────────────────────────────────────────────────────────────────
def test_shallow_transfers_are_used_only_when_no_ledger_exists():
    address = _address()
    called = {"n": 0}

    def transfers(_addr, limit=100):
        called["n"] += 1
        return []

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE"))
        db.flush()
        wallet_scoring.score_wallets([address], db=db, transfers_fn=transfers)
        assert called["n"] == 1, "no ledger -> the shallow page is the fallback"
        db.rollback()


def test_a_failed_read_preserves_the_existing_measurement():
    """A provider failure is NOT a measurement of zero."""
    address = _address()

    def boom(*_a, **_k):
        raise RuntimeError("helius 502")

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE",
                              qualified_trades=12, sample_count=12,
                              win_rate=0.75, measurable=True,
                              smart_money_score=61.0,
                              last_score_update="2026-01-01T00:00:00+00:00"))
        db.flush()
        w = db.query(WalletRegistry).filter_by(address=address).first()
        out = wallet_scoring.score_wallets([address], db=db, transfers_fn=boom)

        assert out["errors"] == 1
        assert w.analysis_status == "FAILED"
        # Every prior count survives, and the score keeps its real age.
        assert w.qualified_trades == 12
        assert w.sample_count == 12
        assert w.smart_money_score == 61.0
        assert w.last_score_update == "2026-01-01T00:00:00+00:00"
        db.rollback()


def test_an_unreadable_ledger_falls_back_instead_of_reporting_failure():
    """A database problem is not a measurement of the wallet.

    The ledger read was written in the same handler as the provider call,
    so ANY failure there — including a session that cannot serve the query —
    was recorded as PROVIDER_FAILURE. That collapsed the four evidence
    states this repo keeps apart (ZERO / INSUFFICIENT / NO_VERIFIED /
    PROVIDER_FAILURE) into a single FAILED, and it broke every existing
    test that drives the scorer with a lightweight session double.
    """
    class _Blind:
        """A session that cannot serve the ledger query."""

        def query(self, *_a, **_k):
            raise AttributeError("no order_by on this session")

    assert wallet_scoring._ledger_rows_for(_Blind(), "addr") == []

    address = _address()
    used = {"transfers": 0}

    def transfers(_addr, limit=100):
        used["transfers"] += 1
        return []

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE"))
        db.flush()
        w = db.query(WalletRegistry).filter_by(address=address).first()

        import lib.wallet_scoring as ws
        saved = ws._ledger_rows_for
        try:
            ws._ledger_rows_for = lambda *_a, **_k: []
            out = ws.score_wallets([address], db=db, transfers_fn=transfers)
        finally:
            ws._ledger_rows_for = saved

        # It fell back to the transfer page and produced a MEASUREMENT of
        # zero, not a provider failure.
        assert used["transfers"] == 1
        assert out["errors"] == 0
        assert w.analysis_status == "NO_VERIFIED_TRADES"
        db.rollback()


def test_unpriced_trades_column_records_the_real_field():
    """`rec.get("unpriced")` was always None — the key is `unpriced_legs`."""
    address = _address()
    now = datetime.now(timezone.utc)
    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE"))
        sell = _trade(address, mint="M", direction="sell", qty=10, value=900,
                      when=now)
        db.add(sell)
        db.flush()
        wallet_scoring.score_wallets([address], db=db,
                                     transfers_fn=lambda *a, **k: [])
        w = db.query(WalletRegistry).filter_by(address=address).first()
        assert w.unpriced_trades == 1
        db.rollback()


# ─────────────────────────────────────────────────────────────────────────
# Deep history: the cursor, its exhaustion, and its failure mode.
# ─────────────────────────────────────────────────────────────────────────
def _fake_rpc(pages, *, fail_on=None, calls=None):
    """A Helius stand-in that serves signature pages then transactions."""
    def rpc(method, params=None):
        if calls is not None:
            calls.append((method, params))
        if method == "getSignaturesForAddress":
            opts = (params or [None, {}])[1] or {}
            if fail_on is not None and opts.get("before") == fail_on:
                raise RuntimeError("helius 500")
            key = opts.get("before")
            return pages.get(key, [])
        if method == "getTransaction":
            # A transaction with no net balance change: NOT a trade. Keeps
            # the test about the CURSOR rather than about decoding.
            return {"transaction": {"signatures": [(params or [""])[0]],
                                    "message": {"accountKeys": []}},
                    "meta": {"err": None, "fee": 5000,
                             "preTokenBalances": [], "postTokenBalances": [],
                             "preBalances": [], "postBalances": []},
                    "blockTime": 1787000000, "slot": 1}
        return None
    return rpc


def test_first_pass_reads_the_newest_page_then_walks_backward(monkeypatch):
    from lib import helius_client, wallet_swaps

    address = _address()
    pages = {None: [{"signature": f"s{i}"} for i in range(25)],
             "s24": [{"signature": f"t{i}"} for i in range(25)],
             "t24": []}
    calls = []
    monkeypatch.setattr(helius_client, "rpc",
                        _fake_rpc(pages, calls=calls))

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE", pinned=1))
        db.flush()
        w = db.query(WalletRegistry).filter_by(address=address).first()

        # Pass 1 — no cursor yet, so it must NOT use `before`.
        wallet_swaps.sync_wallet_history(address, session=db, max_pages=1,
                                         page_size=25, deep=False)
        first = [c for c in calls if c[0] == "getSignaturesForAddress"][0]
        assert "before" not in (first[1][1] or {})
        assert w.history_newest_signature == "s0"
        assert w.history_oldest_signature == "s24"
        assert not w.history_backfill_complete

        # Pass 2 — deep, continuing backward from the durable cursor.
        calls.clear()
        wallet_swaps.sync_wallet_history(address, session=db, max_pages=1,
                                         page_size=25, deep=True)
        second = [c for c in calls if c[0] == "getSignaturesForAddress"][0]
        assert (second[1][1] or {}).get("before") == "s24"
        assert w.history_oldest_signature == "t24"
        # Newest must NOT move backward on a deep pass.
        assert w.history_newest_signature == "s0"
        db.rollback()


def test_backfill_is_marked_complete_only_when_history_is_exhausted(monkeypatch):
    from lib import helius_client, wallet_swaps

    address = _address()
    pages = {None: [{"signature": f"s{i}"} for i in range(25)], "s24": []}
    monkeypatch.setattr(helius_client, "rpc", _fake_rpc(pages))

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE", pinned=1))
        db.flush()
        w = db.query(WalletRegistry).filter_by(address=address).first()

        wallet_swaps.sync_wallet_history(address, session=db, max_pages=1,
                                         page_size=25, deep=False)
        assert not w.history_backfill_complete, "a full page is not the end"

        wallet_swaps.sync_wallet_history(address, session=db, max_pages=1,
                                         page_size=25, deep=True)
        assert w.history_backfill_complete == 1
        db.rollback()


def test_a_failed_history_call_never_marks_the_backfill_complete(monkeypatch):
    """"We could not look" must not be recorded as "there is nothing left"."""
    from lib import helius_client, wallet_swaps

    address = _address()
    pages = {None: [{"signature": f"s{i}"} for i in range(25)]}
    monkeypatch.setattr(helius_client, "rpc",
                        _fake_rpc(pages, fail_on="s24"))

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE", pinned=1))
        db.flush()
        w = db.query(WalletRegistry).filter_by(address=address).first()

        wallet_swaps.sync_wallet_history(address, session=db, max_pages=1,
                                         page_size=25, deep=False)
        cursor_before = w.history_oldest_signature

        out = wallet_swaps.sync_wallet_history(address, session=db,
                                               max_pages=1, page_size=25,
                                               deep=True)
        assert out.get("error")
        assert not w.history_backfill_complete
        assert w.history_status == "FAILED"
        # The cursor must not advance past a page that was never read.
        assert w.history_oldest_signature == cursor_before
        db.rollback()


def test_replaying_the_same_page_stores_no_duplicate_ledger_rows(monkeypatch):
    """Restart safety: `_persist` is idempotent on (address, signature)."""
    from lib import wallet_swaps

    address = _address()
    row = {"wallet_address": address, "signature": "sig-repeat",
           "kind": wallet_swaps.BUY, "base_mint": "M", "base_amount": 10.0,
           "quote_mint": "SOL", "quote_amount": 1.0, "notional_usd": 100.0,
           "entry_price_usd": 10.0, "timestamp": 1787000000,
           "ledger_version": wallet_swaps.LEDGER_VERSION}

    with get_db() as db:
        db.add(WalletRegistry(address=address, status="CANDIDATE"))
        db.flush()
        assert wallet_swaps._persist(db, row) is True
        db.flush()
        assert wallet_swaps._persist(db, row) is False
        db.flush()
        n = (db.query(WalletTrade)
             .filter(WalletTrade.address == address,
                     WalletTrade.signature == "sig-repeat").count())
        assert n == 1
        db.rollback()


# ─────────────────────────────────────────────────────────────────────────
# The cycle: ordering, boundedness, and what it may publish.
# ─────────────────────────────────────────────────────────────────────────
def test_the_stage_order_is_the_one_the_evidence_requires():
    from lib import wallet_intel_cycle as C

    order = list(C.STAGES)
    assert order == [
        "ENRICH_SWAP_EVIDENCE", "BACKFILL_WALLET_HISTORY",
        "COLLECT_PRICE_SNAPSHOTS", "RESOLVE_WALLET_ALPHA",
        "RESCORE_AFFECTED_WALLETS",
        # Behaviour is recomputed from the refreshed evidence BEFORE the
        # lifecycle decides what the wallet is watched for.
        "ASSESS_WALLET_BEHAVIOUR", "APPLY_WALLET_LIFECYCLE",
        "PROCESS_SHADOW_EVENTS", "RESOLVE_OUTCOMES", "REFRESH_SUMMARIES",
    ]
    assert order.index("RESCORE_AFFECTED_WALLETS") < order.index(
        "ASSESS_WALLET_BEHAVIOUR")
    assert order.index("ASSESS_WALLET_BEHAVIOUR") < order.index(
        "APPLY_WALLET_LIFECYCLE")
    # Prices must be current BEFORE anything values a SOL-quoted trade.
    assert order.index("COLLECT_PRICE_SNAPSHOTS") < order.index(
        "RESOLVE_WALLET_ALPHA")
    assert order.index("COLLECT_PRICE_SNAPSHOTS") < order.index(
        "RESCORE_AFFECTED_WALLETS")


def test_history_backfill_stays_bounded():
    from lib import wallet_intel_cycle as C

    assert C.HISTORY_WALLETS_PER_CYCLE >= 1
    assert C.HISTORY_WALLETS_PER_CYCLE <= 5
    assert 1 <= C.HISTORY_PAGE_SIZE <= 100
    # One signature costs one getTransaction, so the per-cycle ceiling is
    # small on purpose.
    assert C.HISTORY_WALLETS_PER_CYCLE * C.HISTORY_PAGE_SIZE <= 100


def test_the_lifecycle_stage_never_publishes_a_full_wallet_address():
    """Every stage result is served verbatim by the status API.

    `wallet_lifecycle.run` reports `{"address": w.address}` — the full
    address — so the leak was dormant only while nothing transitioned, which
    is exactly the condition this stage exists to end.
    """
    import lib.wallet_intel_cycle as C

    real = C.__dict__["_apply_lifecycle"]
    import lib.wallet_lifecycle as L

    saved = L.run
    try:
        L.run = lambda limit=200: {
            "examined": 1, "promoted": 1, "demoted": 0, "unchanged": 0,
            "transitions": [{"address": "A" * 44, "from": "CANDIDATE",
                             "to": "WATCH", "reasons": ["test"]}]}
        out = real()
    finally:
        L.run = saved

    assert out["promoted"] == 1
    published = out["transitions"][0]
    assert "address" not in published
    assert published["wallet"] == "AAAA\u2026AAAA"
    assert "A" * 44 not in str(out)


def test_the_cycle_status_exposes_the_new_history_counters():
    from lib import wallet_intel_cycle as C

    s = C.status()
    for key in ("history_wallets_attempted", "history_records_loaded",
                "history_swaps_stored", "history_backfills_completed",
                "wallets_promoted", "wallets_demoted"):
        assert key in s, f"{key} must reach the desk"
    assert s["history_wallets_per_cycle"] == C.HISTORY_WALLETS_PER_CYCLE
    assert s["history_page_size"] == C.HISTORY_PAGE_SIZE


def test_the_desk_renders_the_new_counters_and_shows_missing_as_a_dash():
    import pathlib

    page = (pathlib.Path(__file__).parent.parent
            / "frontend/src/lib/sections/OnChain.svelte").read_text(
                encoding="utf-8")
    for field in ("c.history_wallets_attempted", "c.history_records_loaded",
                  "c.history_swaps_stored", "c.history_backfills_completed",
                  "c.wallets_promoted", "c.wallets_demoted"):
        assert f'{{{field} ?? "\u2014"}}' in page, f"{field} must render"


def test_the_backfill_stage_reports_counts_and_no_identities():
    """The desk gets numbers; the addresses stay in the database."""
    import inspect

    from lib import wallet_intel_cycle as C

    body = inspect.getsource(C._backfill_history)
    assert '"wallets_attempted"' in body
    assert '"records_loaded"' in body
    assert "address" not in body.split("return out")[0].split(
        'out = {')[1].split("}")[0]


def test_no_execution_or_scheduler_was_introduced():
    import pathlib

    root = pathlib.Path(__file__).parent.parent
    for rel in ("lib/wallet_intel_cycle.py", "lib/wallet_scoring.py"):
        body = (root / rel).read_text(encoding="utf-8")
        for forbidden in ("sendTransaction", "signTransaction", "Keypair",
                          "submit_order", "place_order", "BackgroundScheduler",
                          "add_job"):
            assert forbidden not in body, f"{forbidden} in {rel}"
