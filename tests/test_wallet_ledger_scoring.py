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
