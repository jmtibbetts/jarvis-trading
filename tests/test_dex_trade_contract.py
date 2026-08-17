"""P0 — the DEX closed-trade contract, end to end.

`/onchain/dex/trades` read `t.pnl_pct`, `t.total_fees_usd` and
`t.exit_reason`. `DexTrade` stores `net_pnl_pct`, `total_costs_usd` and
`reason`, so the route raised

    AttributeError: 'DexTrade' object has no attribute 'pnl_pct'

for EVERY real closed row. It had never fired only because the virtual DEX
book had no closed swaps yet — the first one would have taken the endpoint
down, and it would have looked like a DEX outage rather than a typo.

Three layers each invented their own names, and no test caught it because
every existing test built its payload from a hand-made dict, which cannot
disagree with the ORM. These tests go through the REAL model and the REAL
route, which is the only arrangement that could have failed.
"""
import unittest

from app.database import DexTrade, get_db
from lib.dex_contracts import (DEX_TRADE_CONTRACT_VERSION, ClosedDexTrade,
                               closed_trades_payload)

# Every field the frontend's DexTrade type reads. Kept as a literal list
# rather than derived from the dataclass on purpose: deriving it would make
# the test agree with whatever the backend happens to expose, which is the
# failure being guarded against.
FRONTEND_FIELDS = {
    "id", "mint", "symbol", "dex", "position_id",
    "qty_tokens", "notional_usd", "entry_price_usd", "exit_price_usd",
    "gross_pnl_usd", "net_pnl_usd", "net_pnl_pct",
    "pool_fees_usd", "network_fees_usd", "total_costs_usd",
    "entry_impact_pct", "exit_impact_pct", "impact_cost_usd",
    "reason", "opened_at", "closed_at", "hold_minutes",
}


def _row(**over):
    base = dict(
        id="ctr-1", position_id="pos-1", mint="MintAAA", symbol="CTR",
        dex="raydium", qty_tokens=1000.0, notional_usd=500.0,
        entry_price_usd=0.5, exit_price_usd=0.62,
        gross_pnl_usd=120.0, total_costs_usd=18.0, net_pnl_usd=102.0,
        net_pnl_pct=20.4, entry_impact_pct=0.8, exit_impact_pct=1.1,
        pool_fees_usd=5.0, network_fees_usd=0.4,
        reason="TARGET", opened_at="2026-08-01T00:00:00+00:00",
        closed_at="2026-08-02T00:00:00+00:00", hold_minutes=1440.0,
    )
    base.update(over)
    return DexTrade(**base)


class TheRouteSerializesARealRow(unittest.TestCase):
    """The regression proper: a real ORM instance through the real route."""

    def setUp(self):
        with get_db() as db:
            db.query(DexTrade).filter(DexTrade.id.like("ctr-%")).delete(
                synchronize_session=False)
            db.add(_row())
            db.commit()

    def tearDown(self):
        with get_db() as db:
            db.query(DexTrade).filter(DexTrade.id.like("ctr-%")).delete(
                synchronize_session=False)
            db.commit()

    def test_the_route_does_not_raise_on_a_real_closed_row(self):
        from app.routers.onchain import dex_trades
        body = dex_trades(limit=10)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["trades"][0]["id"], "ctr-1")

    def test_the_payload_carries_every_field_the_frontend_reads(self):
        from app.routers.onchain import dex_trades
        keys = set(dex_trades(limit=10)["trades"][0])
        missing = FRONTEND_FIELDS - keys
        self.assertEqual(missing, set(),
                         f"frontend reads fields the API does not send: {missing}")

    def test_the_drifted_names_are_gone_from_the_wire(self):
        """If these come back, some layer has reintroduced its own
        vocabulary and the contract has drifted again."""
        from app.routers.onchain import dex_trades
        keys = set(dex_trades(limit=10)["trades"][0])
        for dead in ("pnl_pct", "total_fees_usd", "exit_reason"):
            self.assertNotIn(dead, keys)

    def test_the_response_declares_its_contract_version(self):
        from app.routers.onchain import dex_trades
        self.assertEqual(dex_trades(limit=10)["contract_version"],
                         DEX_TRADE_CONTRACT_VERSION)


class CostsAreNotFees(unittest.TestCase):
    def test_impact_is_derived_as_total_less_the_explicit_charges(self):
        """Impact has no rate — nobody charges it. It is what is left of
        the total after the venue's cut and the chain's."""
        t = ClosedDexTrade.from_row(_row())
        self.assertAlmostEqual(t.impact_cost_usd, 18.0 - 5.0 - 0.4, places=6)

    def test_the_three_components_stay_separately_addressable(self):
        d = ClosedDexTrade.from_row(_row()).as_dict()
        self.assertEqual(d["pool_fees_usd"], 5.0)
        self.assertEqual(d["network_fees_usd"], 0.4)
        self.assertEqual(d["total_costs_usd"], 18.0)
        # A cheaper venue fixes the pool fee and does nothing about impact,
        # which is why they must never be summed into one column.
        self.assertNotEqual(d["impact_cost_usd"], d["pool_fees_usd"])

    def test_an_unpriced_total_yields_an_unknown_impact_not_zero(self):
        t = ClosedDexTrade.from_row(_row(total_costs_usd=None))
        self.assertIsNone(t.impact_cost_usd)


class TheContractReadsColumnsThatExist(unittest.TestCase):
    def test_every_field_maps_to_a_real_column_or_is_derived(self):
        """The original bug in one assertion: the serializer must only
        read attributes the model actually has."""
        cols = {c.name for c in DexTrade.__table__.columns}
        d = ClosedDexTrade.from_row(_row()).as_dict()
        derived = {"impact_cost_usd"}
        unknown = set(d) - cols - derived
        self.assertEqual(unknown, set(),
                         f"contract exposes fields backed by nothing: {unknown}")

    def test_an_empty_book_still_answers(self):
        self.assertEqual(closed_trades_payload([])["count"], 0)


if __name__ == "__main__":
    unittest.main()
