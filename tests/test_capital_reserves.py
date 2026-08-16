"""Reserves — turning "2 deposits, 1 borrow" into named assets.

Offsets come from the official klend-sdk Reserve layout and are validated
field-for-field against SDK-decoded output on four live reserves. The
sharpest of those checks is USDC's oracle price returning $0.9999: a wrong
offset does not produce exactly a dollar for a stablecoin.

The cTOKEN correction is the other reason this file exists. Deposits are
denominated in collateral tokens, not the underlying, so amount x oracle
price understates a position — measured live at 1.1023x and 1.0992x on two
collateral legs while the borrow leg came back at 1.0001x. Reporting
"0.316 bSOL" when the wallet holds 0.348 bSOL worth of cTokens is a real
error that reconciles to nothing.
"""
import base64
import json
import os
import pathlib
import unittest

from lib.capital_reserves import (OFFSETS, RESERVE_DISCRIMINATOR, SCALE_SF,
                                  decode_reserve, name_positions,
                                  position_reserves)
from lib.capital_lending import decode_obligation

FIX = pathlib.Path(__file__).parent / "fixtures"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def reserves_fixture():
    return json.loads((FIX / "kamino_reserves.json").read_text(encoding="utf-8"))


def obligations_fixture():
    return json.loads((FIX / "kamino_obligations.json").read_text(encoding="utf-8"))


def decoded_reserves():
    out = {}
    for addr, r in reserves_fixture().items():
        d = decode_reserve(base64.b64decode(r["rawBase64"]))
        if d:
            out[addr] = {**d, "reserve": addr}
    return out


class ReserveDecodeMatchesSdkTests(unittest.TestCase):
    def test_every_fixture_reserve_decodes(self):
        self.assertEqual(len(decoded_reserves()), len(reserves_fixture()))

    def test_liquidity_mint_matches_the_official_decoder(self):
        for addr, r in reserves_fixture().items():
            got = decode_reserve(base64.b64decode(r["rawBase64"]))
            self.assertEqual(got["liquidity_mint"], r["liquidityMint"], addr)

    def test_decimals_ltv_and_liquidation_threshold_match(self):
        for addr, r in reserves_fixture().items():
            got = decode_reserve(base64.b64decode(r["rawBase64"]))
            self.assertEqual(got["mint_decimals"], r["mintDecimals"], addr)
            self.assertEqual(got["loan_to_value_pct"], r["loanToValuePct"], addr)
            self.assertEqual(got["liquidation_threshold_pct"],
                             r["liquidationThresholdPct"], addr)

    def test_the_stablecoin_oracle_price_is_a_dollar(self):
        """The offset check that cannot be luck."""
        for addr, r in reserves_fixture().items():
            got = decode_reserve(base64.b64decode(r["rawBase64"]))
            if got["liquidity_mint"] == USDC:
                self.assertAlmostEqual(got["market_price_usd"], 1.0, places=2)
                return
        self.skipTest("no USDC reserve in fixture")

    def test_prices_come_from_kaminos_oracle_not_an_exchange(self):
        """Liquidation is decided by the protocol's own price; substituting
        exchange spot would model a different protocol."""
        for r in decoded_reserves().values():
            self.assertEqual(r["price_source"], "kamino_reserve_oracle")


class RefusesFabricationTests(unittest.TestCase):
    def test_random_bytes_are_refused(self):
        for _ in range(20):
            self.assertIsNone(decode_reserve(os.urandom(9000)))

    def test_random_bytes_with_a_valid_discriminator_are_refused(self):
        for _ in range(20):
            raw = RESERVE_DISCRIMINATOR + os.urandom(9000)
            self.assertIsNone(decode_reserve(raw))

    def test_a_wrong_discriminator_is_refused(self):
        raw = bytearray(base64.b64decode(next(iter(reserves_fixture().values()))["rawBase64"]))
        raw[0] ^= 0xFF
        self.assertIsNone(decode_reserve(bytes(raw)))

    def test_short_buffers_are_refused(self):
        self.assertIsNone(decode_reserve(b""))
        self.assertIsNone(decode_reserve(None))
        self.assertIsNone(decode_reserve(RESERVE_DISCRIMINATOR + b"\x00" * 100))

    def test_a_threshold_below_the_ltv_is_refused_as_impossible(self):
        """Kamino never configures a position that is liquidatable the
        moment it opens; seeing that means the offset is wrong."""
        raw = bytearray(base64.b64decode(next(iter(reserves_fixture().values()))["rawBase64"]))
        raw[OFFSETS["loan_to_value_pct"]] = 90
        raw[OFFSETS["liquidation_threshold_pct"]] = 50
        self.assertIsNone(decode_reserve(bytes(raw)))


class CTokenCorrectionTests(unittest.TestCase):
    """Deposits are cTokens. Borrows are underlying. They are not the same."""

    def test_collateral_reconciles_with_the_obligation_total(self):
        res = decoded_reserves()
        for o in obligations_fixture():
            raw = base64.b64decode(o["rawBase64"])
            pos = decode_obligation(raw)
            named = name_positions(raw, res)
            total = sum(d["value_usd"] for d in named["deposits"])
            self.assertAlmostEqual(total, pos["collateral_value_usd"], places=1,
                                   msg=f"{o['obligation']} collateral must "
                                       f"reconcile with the obligation")

    def test_a_deposit_reports_both_ctokens_and_underlying(self):
        res = decoded_reserves()
        raw = base64.b64decode(obligations_fixture()[0]["rawBase64"])
        for d in name_positions(raw, res)["deposits"]:
            self.assertIsNotNone(d["ctoken_amount"])
            # cTokens accrue, so the underlying is always the larger number.
            self.assertGreater(d["amount"], d["ctoken_amount"])
            self.assertIn("cTokens", d["amount_basis"])

    def test_a_borrow_is_underlying_with_no_ctoken_figure(self):
        res = decoded_reserves()
        raw = base64.b64decode(obligations_fixture()[0]["rawBase64"])
        for b in name_positions(raw, res)["borrows"]:
            self.assertIsNone(b["ctoken_amount"])
            self.assertIn("as borrowed", b["amount_basis"])

    def test_multiplying_amount_by_price_would_have_understated_collateral(self):
        """Pins the bug: the naive product is ~10% low."""
        res = decoded_reserves()
        raw = base64.b64decode(obligations_fixture()[0]["rawBase64"])
        named = name_positions(raw, res)
        naive = sum(d["ctoken_amount"] * d["price_usd"] for d in named["deposits"])
        real = sum(d["value_usd"] for d in named["deposits"])
        self.assertGreater(real, naive * 1.05)


class AssetIdentityTests(unittest.TestCase):
    def test_positions_resolve_to_named_assets(self):
        res = decoded_reserves()
        for o in obligations_fixture():
            raw = base64.b64decode(o["rawBase64"])
            named = name_positions(raw, res)
            self.assertEqual(named["unresolved_deposits"], 0)
            self.assertEqual(named["unresolved_borrows"], 0)
            for p in named["deposits"] + named["borrows"]:
                self.assertTrue(p["symbol"], "every position must be named")

    def test_lst_collateral_is_flagged_as_still_sol_exposure(self):
        res = decoded_reserves()
        raw = base64.b64decode(obligations_fixture()[0]["rawBase64"])
        for d in name_positions(raw, res)["deposits"]:
            if d["symbol"] in ("bSOL", "mSOL", "JitoSOL", "SOL"):
                self.assertTrue(d["retains_sol_exposure"], d["symbol"])

    def test_an_undecodable_reserve_leaves_the_position_unresolved_not_guessed(self):
        raw = base64.b64decode(obligations_fixture()[0]["rawBase64"])
        named = name_positions(raw, {})          # nothing decodable
        self.assertGreater(named["unresolved_deposits"], 0)
        for d in named["deposits"]:
            self.assertIsNone(d["symbol"])
            self.assertFalse(d["resolved"])
            self.assertIn("unknown", d["reason"])

    def test_position_slots_skip_empty_entries(self):
        raw = base64.b64decode(obligations_fixture()[0]["rawBase64"])
        slots = position_reserves(raw)
        self.assertGreater(len(slots["deposits"]), 0)
        self.assertLessEqual(len(slots["deposits"]), 8)
        self.assertLessEqual(len(slots["borrows"]), 5)
        for d in slots["deposits"]:
            self.assertGreater(d["amount_native"], 0)


if __name__ == "__main__":
    unittest.main()
