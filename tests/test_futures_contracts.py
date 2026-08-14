"""Phase 4B — contract identity and the delivery-risk gate.

Dates asserted here are real calendar derivations for known contracts,
not synthetic fixtures: if a rule drifts, the test names the contract
whose calendar it broke.
"""
import unittest
from datetime import date
from unittest.mock import patch

from lib.futures_contracts import (
    contract,
    delivery_risk,
    front_contract,
    listed_contracts,
    root_of,
)


class IdentityTests(unittest.TestCase):
    def test_roots_resolve_from_continuous_and_micro(self):
        self.assertEqual(root_of("CL=F"), "CL")
        self.assertEqual(root_of("MCL=F"), "MCL")
        self.assertEqual(root_of("ES"), "ES")
        self.assertIsNone(root_of("BTC/USD"))
        self.assertIsNone(root_of("NVDA"))

    def test_cl_last_trade_convention(self):
        # CLU26 (Sep-2026 delivery): 25-Aug-2026 is a Tuesday; three
        # business days back = Thu 20-Aug-2026.
        c = contract("CL", "U", 2026)
        self.assertEqual(c.code, "CLU26")
        self.assertEqual(c.last_trade, date(2026, 8, 20))
        self.assertFalse(c.cash_settled)

    def test_es_third_friday(self):
        c = contract("ES", "U", 2026)
        self.assertEqual(c.last_trade, date(2026, 9, 18))
        self.assertTrue(c.cash_settled)
        self.assertIsNone(c.first_notice)

    def test_gold_first_notice_precedes_delivery_month(self):
        # GCQ26 (Aug delivery): FND = last business day of July 2026
        # (Fri 31-Jul). The RISK date is FND, not last trade.
        c = contract("GC", "Q", 2026)
        self.assertEqual(c.first_notice, date(2026, 7, 31))
        self.assertEqual(c.risk_date, date(2026, 7, 31))

    def test_micro_shares_parent_calendar(self):
        self.assertEqual(contract("MCL", "U", 2026).last_trade,
                         contract("CL", "U", 2026).last_trade)
        self.assertEqual(contract("MCL", "U", 2026).code, "MCLU26")


class FrontContractTests(unittest.TestCase):
    def test_front_skips_past_first_notice_contracts(self):
        # 14-Aug-2026: GCQ26 is past its 31-Jul FND — commercials may still
        # trade it into delivery, but a retail account cannot ENTER it.
        # The enterable front is GCV26 (Oct).
        f = front_contract("GC", date(2026, 8, 14))
        self.assertEqual(f.code, "GCV26")

    def test_cl_front_in_mid_august(self):
        f = front_contract("CL", date(2026, 8, 14))
        self.assertEqual(f.code, "CLU26")

    def test_listed_contracts_are_ordered_and_enterable(self):
        asof = date(2026, 8, 14)
        codes = [c.code for c in listed_contracts("ES", asof, n=3)]
        self.assertEqual(codes, ["ESU26", "ESZ26", "ESH27"])
        for c in listed_contracts("GC", asof, n=4):
            self.assertGreater(c.risk_date, asof)


class DeliveryRiskTests(unittest.TestCase):
    def test_comfortable_runway_is_ok(self):
        r = delivery_risk("ES=F", asof=date(2026, 8, 14))
        self.assertEqual(r["level"], "ok")
        self.assertEqual(r["front"], "ESU26")

    def test_roll_window_warns_but_allows(self):
        # 14-Aug vs CLU26 risk 20-Aug: 6 days — inside the 7-day window.
        r = delivery_risk("CL=F", asof=date(2026, 8, 14))
        self.assertEqual(r["level"], "roll_window")
        self.assertEqual(r["days_to_risk"], 6)

    def test_inside_margin_is_blocked(self):
        r = delivery_risk("CL=F", asof=date(2026, 8, 19))
        self.assertEqual(r["level"], "blocked")

    def test_blocked_front_rolls_to_next_contract(self):
        # One day AFTER CLU26's last trade the front is CLV26 with a
        # month of runway — blocked days are a window, not a dead zone.
        r = delivery_risk("CL=F", asof=date(2026, 8, 21))
        self.assertEqual(r["front"], "CLV26")
        self.assertEqual(r["level"], "ok")

    def test_unknown_product_fails_closed(self):
        # An unknown calendar must refuse to certify runway.
        self.assertEqual(delivery_risk("??=F")["level"], "ok")  # not futures
        with patch.dict("lib.futures_contracts.CYCLES", {"XX": ("H",)}):
            r = delivery_risk("XX=F", asof=date(2026, 8, 14))
            self.assertEqual(r["level"], "blocked")


class EntryGuardTests(unittest.TestCase):
    def test_paper_entry_refuses_blocked_futures(self):
        from lib.paper_engine import open_paper_position
        with patch("lib.futures_contracts.delivery_risk",
                   return_value={"level": "blocked",
                                 "reason": "CLU26 is 1d from last trade"}):
            r = open_paper_position({"asset_symbol": "CL=F",
                                     "direction": "Long",
                                     "entry_price": 75.0,
                                     "stop_loss": 73.0,
                                     "target_price": 80.0,
                                     "asset_class": "Futures"})
        self.assertIn("error", r)
        self.assertIn("delivery risk", r["error"])


if __name__ == "__main__":
    unittest.main()
