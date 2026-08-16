"""The protocol registry, and the two errors that verification caught.

Every program ID here was checked with getAccountInfo before being written
down. Two were wrong on the first pass, and both failures are pinned as
tests because both are the kind that survive review:

  Jito4APy... is not a program. It exists, it is NOT executable, and it is
  owned by the SPL Stake Pool program — it is Jito's stake POOL ACCOUNT.

  pAMMBay6oceH9fJKBRHGwLPKnCACo9AJ7YXFPz3mQzY does not exist. It was
  reconstructed from a 20-character truncated log line with the remaining
  23 characters invented, and it reached a committed test before anyone
  asked the chain.
"""
import unittest

from lib.solana_protocols import (LST_MINTS, PROGRAMS, STAKE_POOLS,
                                  categorize_programs, is_lst, is_stable,
                                  lst_info, protocol_of, retains_sol_exposure)

FABRICATED_PUMP_AMM = "pAMMBay6oceH9fJKBRHGwLPKnCACo9AJ7YXFPz3mQzY"
REAL_PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
JITO_POOL_ACCOUNT = "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb"
SPL_STAKE_POOL = "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy"
JITOSOL = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FabricatedAddressTests(unittest.TestCase):
    def test_the_invented_pump_amm_id_is_not_in_the_registry(self):
        """It shares its first 20 characters with the real one, which is
        exactly why it read as plausible."""
        self.assertIsNone(protocol_of(FABRICATED_PUMP_AMM))
        self.assertNotIn(FABRICATED_PUMP_AMM, PROGRAMS)
        self.assertEqual(FABRICATED_PUMP_AMM[:20], REAL_PUMP_AMM[:20])
        self.assertNotEqual(FABRICATED_PUMP_AMM, REAL_PUMP_AMM)

    def test_the_real_pump_amm_is_registered(self):
        p = protocol_of(REAL_PUMP_AMM)
        self.assertIsNotNone(p)
        self.assertEqual(p["category"], "amm")
        self.assertTrue(p["verified_on_chain"])

    def test_jito_is_a_pool_account_not_a_program(self):
        """Not executable, owned by SPL Stake Pool. Registering it as a
        program would mean parsing Jito with an ID that never appears as
        one in any transaction."""
        self.assertNotIn(JITO_POOL_ACCOUNT, PROGRAMS)
        self.assertIn(JITO_POOL_ACCOUNT, STAKE_POOLS)
        self.assertIn(SPL_STAKE_POOL, PROGRAMS)
        self.assertEqual(PROGRAMS[SPL_STAKE_POOL][0], "liquid_staking")

    def test_every_registered_entry_claims_verification(self):
        for addr, (_cat, name, verified) in PROGRAMS.items():
            self.assertTrue(verified, f"{name} ({addr}) is unverified")


class LiquidStakingTests(unittest.TestCase):
    def test_an_lst_still_means_long_sol(self):
        """The live misread this exists to prevent: converting SOL into
        JitoSOL lowers the raw SOL balance and changes nothing about the
        wallet's exposure. Reading it as a sale inverts the signal."""
        self.assertTrue(is_lst(JITOSOL))
        self.assertTrue(retains_sol_exposure(JITOSOL))
        info = lst_info(JITOSOL)
        self.assertEqual(info["symbol"], "JitoSOL")
        self.assertEqual(info["underlying"], "SOL")

    def test_a_stablecoin_does_not_retain_sol_exposure(self):
        self.assertFalse(retains_sol_exposure(USDC))
        self.assertTrue(is_stable(USDC))
        self.assertFalse(is_lst(USDC))

    def test_wrapped_sol_retains_exposure_too(self):
        self.assertTrue(retains_sol_exposure(
            "So11111111111111111111111111111111111111112"))

    def test_every_lst_maps_to_a_named_provider(self):
        for mint, (symbol, provider) in LST_MINTS.items():
            self.assertTrue(symbol and provider, mint)
            self.assertTrue(retains_sol_exposure(mint))


class UnknownProgramTests(unittest.TestCase):
    def test_an_unknown_program_is_unknown_not_absent(self):
        """None means "not in this registry", never "not a protocol". The
        difference decides whether a transaction is UNPARSEABLE or quietly
        reduced to whatever the recognised subset suggests."""
        self.assertIsNone(protocol_of("SomeProgramNobodyRegistered1111111111111111"))

    def test_a_transaction_with_any_unknown_program_is_not_fully_understood(self):
        c = categorize_programs([
            "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD",
            "UnregisteredProgram11111111111111111111111111",
        ])
        self.assertFalse(c["fully_understood"])
        self.assertEqual(len(c["unknown_programs"]), 1)
        self.assertIn("lending", c["categories"])

    def test_all_known_programs_reports_fully_understood(self):
        c = categorize_programs([
            "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD",
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        ])
        self.assertTrue(c["fully_understood"])
        self.assertEqual(c["unknown_programs"], [])

    def test_empty_input_is_handled(self):
        c = categorize_programs([])
        self.assertTrue(c["fully_understood"])
        self.assertEqual(c["protocols"], [])


if __name__ == "__main__":
    unittest.main()
