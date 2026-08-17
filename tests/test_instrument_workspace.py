"""P26 — the per-instrument workspace.

The behaviour worth pinning is not the happy path. It is that an
instrument this system REFUSES to trade still produces a full, honest
answer: the refusal is the headline, it carries the reason and the count
of what is blocked behind it, and nothing downstream quietly substitutes
a default multiplier to make the panel look complete.
"""
import unittest

from lib.instrument_workspace import workspace


class RefusalIsFirstClass(unittest.TestCase):
    def test_an_unspecced_future_returns_a_workspace_not_an_error(self):
        w = workspace("6J=F")
        self.assertEqual(w["canonical_symbol"], "6J=F")
        self.assertFalse(w["executable"])
        self.assertIsNotNone(w["refusal"])

    def test_the_refusal_names_the_reason(self):
        """MISSING_CONTRACT_SPEC, verbatim from the instrument authority.
        A workspace that said only "unavailable" would send the operator
        looking for a broken feed."""
        r = workspace("6J=F")["refusal"]
        self.assertEqual(r["status"], "UNSUPPORTED")
        self.assertIn("MISSING_CONTRACT_SPEC", str(r["reason"]))

    def test_recorded_and_active_signal_counts_stay_separate(self):
        """A historical total presented as a live backlog overstates the
        urgency. Both numbers are reported, neither substitutes."""
        r = workspace("6J=F")["refusal"]
        self.assertIn("signals_recorded", r)
        self.assertIn("signals_active", r)

    def test_no_multiplier_is_invented_for_an_unknown_contract(self):
        """The fail-open this whole layer exists to prevent: an unknown
        future taking an equity spec and being sized as though one point
        were one dollar."""
        ident = workspace("6J=F")["identity"]
        self.assertEqual(ident["product"], "UNKNOWN")
        self.assertEqual(ident["quantity_unit"], "CONTRACTS")

    def test_a_known_contract_is_executable_and_carries_its_units(self):
        w = workspace("MES=F")
        self.assertTrue(w["executable"])
        self.assertIsNone(w["refusal"])
        self.assertEqual(w["identity"]["quantity_unit"], "CONTRACTS")
        self.assertGreater(w["identity"]["multiplier"], 1.0)


class IdentityIsOneAnswer(unittest.TestCase):
    def test_fx_resolves_as_fx_and_not_as_crypto(self):
        """`EUR/USD` has a slash, which used to make it crypto — and
        `EURUSD=X` is the SAME instrument, so both spellings must land on
        one canonical answer rather than three."""
        w = workspace("EURUSD=X")
        self.assertEqual(w["canonical_symbol"], "EUR/USD")
        self.assertEqual(workspace("EUR/USD")["canonical_symbol"], "EUR/USD")
        self.assertEqual(w["identity"]["asset_class"], "FOREX")
        self.assertEqual(w["identity"]["product"], "FX_SPOT")

    def test_every_spelling_the_book_might_use_is_listed(self):
        """The book records BTCUSD and the signal records BTC/USD. A
        workspace matching one of them reports a held instrument as
        untouched."""
        w = workspace("BTC/USD")
        self.assertIn("BTC/USD", w["spellings"])
        self.assertIn("BTCUSD", w["spellings"])


class VenueStance(unittest.TestCase):
    def test_equity_spot_shows_kraken_as_ui_only_rather_than_absent(self):
        """UI availability is not API availability, and the difference
        belongs on screen — an absent row would read as "not offered"."""
        rows = {v["venue"]: v for v in workspace("AAPL")["venues"]}
        self.assertIn("kraken", rows)
        self.assertEqual(rows["kraken"]["status"], "UI_ONLY")
        self.assertFalse(rows["kraken"]["executable"])

    def test_executable_venues_sort_first(self):
        venues = workspace("AAPL")["venues"]
        self.assertTrue(venues)
        self.assertTrue(venues[0]["executable"])

    def test_a_product_no_venue_carries_returns_empty_not_a_guess(self):
        self.assertEqual(workspace("6J=F")["venues"], [])


class CostFloor(unittest.TestCase):
    def test_the_floor_is_reported_without_a_price(self):
        """The floor is a fraction of entry, so it stands alone. The
        round trip in R does not, and says so rather than assuming one."""
        c = workspace("AAPL")["cost"]
        self.assertTrue(c["available"])
        self.assertGreater(c["min_viable_stop_pct"], 0.0)
        self.assertIsNone(c["reference"])
        self.assertIn("no price", c["reference_reason"])

    def test_a_supplied_price_prices_the_round_trip_in_r(self):
        c = workspace("AAPL", entry=200.0)["cost"]
        self.assertIsNotNone(c["reference"])
        self.assertTrue(c["reference"]["ok"])
        # Priced AT the floor, so the round trip costs the floor's own
        # ceiling — that is the definition of min_viable_stop_pct.
        self.assertAlmostEqual(c["reference"]["total_r"], 0.50, places=1)

    def test_a_tighter_stop_costs_more_r_than_the_floor(self):
        """Cost in R scales as 1/stop distance. This is the arithmetic
        behind "the desk can only trade wide-stop setups"."""
        from lib.transaction_costs import estimate_costs
        floor = workspace("AAPL", entry=200.0)["cost"]
        tight = estimate_costs("AAPL", 200.0, 199.8)
        self.assertGreater(tight["total_r"], floor["reference"]["total_r"])


if __name__ == "__main__":
    unittest.main()
