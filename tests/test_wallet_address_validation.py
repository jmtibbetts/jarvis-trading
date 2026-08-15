"""Structural validation vs. behavioural validation — kept apart on purpose.

Three distinctions this file exists to defend, each of which has a wrong
answer that looks reasonable:

    VALID SOLANA ADDRESS  != HUMAN / TRADER WALLET
    OFF-CURVE             != INVALID
    NO ON-CHAIN ACTIVITY  != MALFORMED

The tempting "fix" for the truncation limitation is an ed25519 on-curve
check. It would be wrong: Program Derived Addresses are off-curve BY
DESIGN and are entirely valid Solana addresses, so that check would reject
every PDA, program-owned account and protocol address on the chain.
"""
import unittest
from unittest.mock import patch

from lib.wallet_classify import (NON_TRADER_ENTITIES, classify,
                                 trader_eligibility)
from lib.wallet_registry import (b58decode, b58encode, is_valid_address,
                                 structural_check)

WALLET = "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"   # normal, on-curve
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"    # program
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"   # CEX
SYSTEM = "11111111111111111111111111111111"                # 32 zero bytes
BURN = "1nc1nerator11111111111111111111111111111111"
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class StructuralValidationTests(unittest.TestCase):
    def test_accepts_a_normal_wallet(self):
        self.assertTrue(is_valid_address(WALLET))

    def test_accepts_off_curve_program_and_pda_style_addresses(self):
        """THE requirement. An off-curve address is valid; a validator that
        rejects these has thrown away every PDA on Solana."""
        for addr in (JUPITER, TOKEN_PROG, BURN):
            self.assertTrue(is_valid_address(addr),
                            f"{addr} is a real Solana address")

    def test_accepts_the_all_zero_system_program(self):
        """32 zero bytes encode as 32 '1' characters. An encoder that
        appends a fallback '1' for the zero value produces 33 characters
        and rejects the System Program as malformed — which it is not."""
        self.assertTrue(is_valid_address(SYSTEM))
        self.assertEqual(b58encode(b"\0" * 32), "1" * 32)

    def test_rejects_malformed_base58(self):
        for bad in ("", "   ", "not-an-address!", "0x1234567890abcdef",
                    "IOl0" * 11):          # I, O, l, 0 are not in the alphabet
            self.assertFalse(is_valid_address(bad), repr(bad))

    def test_rejects_wrong_decoded_length(self):
        short = b58encode(b"\x01" * 31)
        long_ = b58encode(b"\x01" * 33)
        self.assertEqual(len(b58decode(short)), 31)
        self.assertEqual(len(b58decode(long_)), 33)
        self.assertFalse(is_valid_address(short))
        self.assertFalse(is_valid_address(long_))

    def test_rejects_a_non_canonical_encoding_of_valid_bytes(self):
        """A stray leading '1' decodes to 33 bytes and re-encodes
        differently. This is what the round-trip is actually for — it does
        NOT catch truncation, and this test does not pretend it does."""
        non_canonical = "1" + WALLET
        self.assertFalse(is_valid_address(non_canonical))
        self.assertFalse(structural_check(non_canonical)["valid"])

    def test_round_trip_is_canonical_for_every_real_address(self):
        for addr in (WALLET, JUPITER, BINANCE, SYSTEM, BURN, TOKEN_PROG):
            self.assertEqual(b58encode(b58decode(addr)), addr, addr)

    def test_a_truncated_address_remains_indistinguishable(self):
        """Kept deliberately, per spec: do not weaken a test because it
        documents a real boundary.

        Dropping a base58 character divides the value by ~58, which often
        still lands on 32 bytes and still round-trips cleanly. No structural
        test can infer "this was meant to be a different address". The
        answer comes from LAYER 2 — on-chain evidence — not the encoding.
        """
        truncated = WALLET[:-1]
        self.assertNotEqual(truncated, WALLET)
        self.assertTrue(is_valid_address(truncated),
                        "structurally valid; only the chain can say it is "
                        "not the intended address")

    def test_structural_check_explains_why_it_refused(self):
        """The UI must never print INVALID ADDRESS for an address that is
        merely off-curve or merely inactive, so it needs the reason."""
        self.assertIn("base58", structural_check("not-an-address!")["reason"])
        self.assertIn("32", structural_check(b58encode(b"\x01" * 31))["reason"])
        self.assertTrue(structural_check(WALLET)["valid"])


def _ev(**kw):
    base = {"checked": True, "exists": True, "executable": False,
            "owner_program": "11111111111111111111111111111111",
            "lamports": 10_000_000, "signature_count": 40, "error": None}
    return {**base, **kw}


class EntityClassificationTests(unittest.TestCase):
    def test_known_infrastructure_is_never_a_trader(self):
        for addr, expected in ((BINANCE, "CEX"), (JUPITER, "PROGRAM"),
                               (BURN, "BURN")):
            c = classify(addr, _ev())
            self.assertEqual(c["entity_type"], expected, addr)
            self.assertFalse(c["is_trader"])
            self.assertTrue(c["is_protocol"])

    def test_an_executable_account_is_a_program_not_a_trader(self):
        c = classify(WALLET, _ev(executable=True))
        self.assertEqual(c["entity_type"], "PROGRAM")
        self.assertFalse(c["is_trader"])

    def test_a_token_account_is_excluded_and_its_owner_resolved(self):
        """Discovery reaches token accounts constantly —
        getTokenLargestAccounts returns accounts, not wallets. The account
        must not be scored; the OWNER is the candidate."""
        owner = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
        with patch("lib.wallet_classify.resolve_token_account_owner",
                   return_value=owner):
            c = classify(WALLET, _ev(owner_program=TOKEN_PROG))
        self.assertEqual(c["entity_type"], "TOKEN_ACCOUNT")
        self.assertFalse(c["is_trader"])
        self.assertEqual(c["owner_wallet"], owner)

    def test_an_account_owned_by_another_program_is_a_PDA_not_a_trader(self):
        """Found by spot-checking the first live discovery pass, not by
        reasoning: among five sampled "trader candidates" were an account
        owned by Pump.fun's AMM and another owned by Meteora DLMM. Both are
        liquidity vaults. Neither is executable and neither is an SPL token
        account, so every other check passed them — and they would have
        scored as traders with huge volume and constant activity, ranking
        protocol plumbing above every human on the chain.

        A user wallet is owned by the SYSTEM program. Nothing else is."""
        for program in ("pAMMBay6oceH9fJKBRHGwLPKnCACo9AJ7YXFPz3mQzY",   # Pump.fun AMM
                        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"):  # Meteora DLMM
            c = classify(WALLET, _ev(owner_program=program))
            self.assertEqual(c["entity_type"], "PDA", program)
            self.assertFalse(c["is_trader"])
            self.assertTrue(c["is_protocol"])

    def test_a_system_owned_account_is_still_a_trader_candidate(self):
        c = classify(WALLET, _ev(owner_program="11111111111111111111111111111111"))
        self.assertEqual(c["entity_type"], "TRADER_CANDIDATE")
        self.assertTrue(c["is_trader"])

    def test_a_valid_address_with_no_account_is_not_malformed(self):
        c = classify(WALLET, _ev(exists=False))
        self.assertNotEqual(c["entity_type"], "INVALID")
        self.assertIn("no account on chain", c["reason"])

    def test_an_ordinary_system_owned_account_is_a_trader_candidate(self):
        c = classify(WALLET, _ev())
        self.assertEqual(c["entity_type"], "TRADER_CANDIDATE")
        self.assertTrue(c["is_trader"])

    def test_a_failed_lookup_does_not_become_a_verdict(self):
        c = classify(WALLET, {"checked": False, "error": "Timeout"})
        self.assertEqual(c["entity_type"], "UNKNOWN")
        self.assertIn("could not classify", c["reason"])


class TraderEligibilityTests(unittest.TestCase):
    def test_infrastructure_is_ineligible_however_valid(self):
        for addr in (BINANCE, JUPITER):
            r = trader_eligibility(addr, _ev())
            self.assertFalse(r["eligible"])
            self.assertIn(r["entity_type"], NON_TRADER_ENTITIES)

    def test_too_little_history_is_ineligible_but_not_invalid(self):
        r = trader_eligibility(WALLET, _ev(signature_count=2))
        self.assertFalse(r["eligible"])
        self.assertIn("2 recent signature", r["eligibility_reason"])
        self.assertNotEqual(r["entity_type"], "INVALID")

    def test_an_active_ordinary_wallet_is_eligible(self):
        r = trader_eligibility(WALLET, _ev(signature_count=40))
        self.assertTrue(r["eligible"])

    def test_a_structurally_invalid_address_is_ineligible(self):
        r = trader_eligibility("not-an-address!", _ev())
        self.assertFalse(r["eligible"])
        self.assertEqual(r["entity_type"], "INVALID")


if __name__ == "__main__":
    unittest.main()
