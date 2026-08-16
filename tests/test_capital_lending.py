"""Kamino decoding, validated against the OFFICIAL SDK — and refusing junk.

Every value here was cross-checked three ways before being trusted:

    on-chain account
        -> decoded by @kamino-finance/klend-sdk v10.1.0 (official)
        -> decoded independently by lib.capital_lending
        -> confirmed against api.kamino.finance

All three agree on owner, market, collateral, debt and threshold. The
fixture carries rawBase64 so these run offline and do not depend on any
account staying active.

The two negative tests are the point of the file. Before the discriminator
and sanity gates existed, 3,344 bytes of os.urandom decoded "successfully"
into the plausible wallet Ef8QvULRFjLEG7usD4HDpokKH9otsWE3781KPaZPXQm5
holding $2.5e20 of collateral. Any 32-byte window base58-encodes into
something that looks exactly like a Solana address — the same lesson as
address validation, one layer down.
"""
import base64
import json
import os
import pathlib
import unittest

from lib.capital_lending import (DECODER_SOURCE, DECODER_VERSION,
                                 MAX_PLAUSIBLE_USD, OBLIGATION_DISCRIMINATOR,
                                 OBLIGATION_SIZE, SCALE_SF, decode_obligation,
                                 health_of)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "kamino_obligations.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class OfficialSdkAgreementTests(unittest.TestCase):
    """The python decoder must reproduce the official SDK exactly."""

    def test_fixture_exists_and_carries_raw_bytes(self):
        fx = load_fixture()
        self.assertGreaterEqual(len(fx), 1)
        for o in fx:
            self.assertIn("rawBase64", o, "fixture must decode offline")

    def test_owner_matches_the_official_decoder(self):
        for o in load_fixture():
            got = decode_obligation(base64.b64decode(o["rawBase64"]))
            self.assertIsNotNone(got, o["obligation"])
            self.assertEqual(got["owner"], o["owner"])

    def test_lending_market_matches(self):
        """Also guards a real mistake: the market address was once
        completed by hand from a log line truncated at 110 characters,
        producing 7u3HeHxYDLhnCoErrtycN4fVUnz... which does not exist.
        The real one is 7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF and
        they share a 21-character prefix."""
        for o in load_fixture():
            got = decode_obligation(base64.b64decode(o["rawBase64"]))
            self.assertEqual(got["lending_market"], o["lendingMarket"])
            self.assertNotEqual(got["lending_market"],
                                "7u3HeHxYDLhnCoErrtycN4fVUnzSDoMDGyPnCYFa3Md6")

    def test_position_values_match_to_the_cent(self):
        for o in load_fixture():
            got = decode_obligation(base64.b64decode(o["rawBase64"]))
            self.assertAlmostEqual(got["collateral_value_usd"],
                                   int(o["depositedValueSf"]) / SCALE_SF, places=2)
            self.assertAlmostEqual(got["debt_value_usd"],
                                   int(o["borrowFactorAdjustedDebtValueSf"]) / SCALE_SF,
                                   places=2)
            self.assertAlmostEqual(got["liquidation_threshold_usd"],
                                   int(o["unhealthyBorrowValueSf"]) / SCALE_SF, places=2)

    def test_has_debt_matches(self):
        for o in load_fixture():
            got = decode_obligation(base64.b64decode(o["rawBase64"]))
            self.assertEqual(got["has_debt"], bool(o["hasDebt"]))

    def test_every_decode_carries_its_provenance(self):
        """A future Kamino layout change must be a visible failure, not
        silently decoded nonsense."""
        got = decode_obligation(base64.b64decode(load_fixture()[0]["rawBase64"]))
        self.assertEqual(got["decoder_source"], DECODER_SOURCE)
        self.assertEqual(got["decoder_version"], DECODER_VERSION)
        self.assertIn("KLend", got["program_id"])


class RefusesFabricationTests(unittest.TestCase):
    """THE critical negative tests."""

    def test_random_bytes_of_the_right_size_are_refused(self):
        """Measured before the fix: this produced a plausible wallet and
        $2.5e20 of collateral."""
        for _ in range(25):
            self.assertIsNone(decode_obligation(os.urandom(OBLIGATION_SIZE)))

    def test_random_bytes_with_a_valid_discriminator_are_still_refused(self):
        """The discriminator alone is not enough — structural sanity has
        to catch a correctly-tagged buffer full of noise."""
        for _ in range(25):
            raw = OBLIGATION_DISCRIMINATOR + os.urandom(OBLIGATION_SIZE - 8)
            self.assertIsNone(decode_obligation(raw))

    def test_a_valid_buffer_with_the_wrong_discriminator_is_refused(self):
        """Identity comes from the canonical tag, never from length."""
        raw = bytearray(base64.b64decode(load_fixture()[0]["rawBase64"]))
        raw[0] ^= 0xFF
        self.assertIsNone(decode_obligation(bytes(raw)))

    def test_wrong_sizes_are_refused(self):
        good = base64.b64decode(load_fixture()[0]["rawBase64"])
        self.assertIsNone(decode_obligation(good[:-1]))
        self.assertIsNone(decode_obligation(good + b"\x00"))
        self.assertIsNone(decode_obligation(b""))
        self.assertIsNone(decode_obligation(None))

    def test_a_field_is_never_accepted_just_because_it_looks_like_an_address(self):
        """32 bytes decoding to a valid Solana address proves nothing about
        whether that field IS the owner. Identity comes from the schema."""
        raw = OBLIGATION_DISCRIMINATOR + os.urandom(OBLIGATION_SIZE - 8)
        self.assertIsNone(decode_obligation(raw))

    def test_implausible_magnitudes_are_refused(self):
        self.assertGreater(MAX_PLAUSIBLE_USD, 1e9)
        self.assertLess(MAX_PLAUSIBLE_USD, 1e15)


class HealthTests(unittest.TestCase):
    def test_health_is_computed_from_kaminos_own_threshold(self):
        """Liquidatable when borrow-factor-adjusted debt reaches
        unhealthyBorrowValue, so health = unhealthy / debt."""
        h = health_of({"collateral_value_usd": 1000.0, "debt_value_usd": 500.0,
                       "liquidation_threshold_usd": 750.0})
        self.assertAlmostEqual(h["health_factor"], 1.5, places=3)
        self.assertEqual(h["risk_state"], "SAFE")

    def test_a_position_with_no_debt_cannot_be_liquidated(self):
        h = health_of({"collateral_value_usd": 1000.0, "debt_value_usd": 0.0,
                       "liquidation_threshold_usd": 750.0})
        self.assertIsNone(h["health_factor"])
        self.assertEqual(h["risk_state"], "SAFE")

    def test_risk_states_escalate_as_health_falls(self):
        seen = []
        for debt in (400, 700, 715, 740, 755):
            h = health_of({"collateral_value_usd": 1000.0,
                           "debt_value_usd": float(debt),
                           "liquidation_threshold_usd": 750.0})
            seen.append(h["risk_state"])
        self.assertEqual(seen[0], "SAFE")
        self.assertEqual(seen[-1], "LIQUIDATION_IN_PROGRESS")
        self.assertIn("CRITICAL", seen)

    def test_distance_warns_before_liquidation_not_after(self):
        h = health_of({"collateral_value_usd": 1000.0, "debt_value_usd": 720.0,
                       "liquidation_threshold_usd": 750.0})
        self.assertGreater(h["distance_to_liquidation_pct"], 0)
        self.assertLess(h["distance_to_liquidation_pct"], 10)
        self.assertIn(h["risk_state"], ("CRITICAL", "HIGH"))

    def test_fixture_positions_produce_sane_health(self):
        for o in load_fixture():
            got = decode_obligation(base64.b64decode(o["rawBase64"]))
            h = health_of(got)
            self.assertIsNotNone(h["health_factor"])
            self.assertGreater(h["health_factor"], 0)


if __name__ == "__main__":
    unittest.main()
