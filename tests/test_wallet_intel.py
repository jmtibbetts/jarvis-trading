"""Wallet intelligence — the scores, and what they refuse to claim.

Half these tests assert a NEGATIVE: that a shared exchange funder is not
a conspiracy, that a big balance is not skill, that close timing is not
a relationship. Those are the properties worth pinning, because every one
of them is a plausible-sounding inference this desk has an existing scar
from making. See lib/wallet_intel's module docstring.

Identity fixtures are verbatim shapes from the live Helius Wallet API
(captured 2026-08-17), including the bare {"address", "type": "unknown"}
that unclassified wallets actually return.
"""
import unittest

from lib.wallet_intel import (
    MIN_HISTORY_FOR_BASELINE,
    accumulation_score,
    classify_counterparty,
    cluster_by_funder,
    coordination_score,
    copy_trade_candidate,
    exchange_flows,
    smart_money_score,
    wallet_baseline,
    whale_score,
)

BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
PLAIN = "Du1TJhM5x4k5a98Nc6H4GpcAL5dvRsuX4d64wkV7FHS8"

EXCHANGE_IDENTITY = {          # verbatim from the live API
    "address": BINANCE, "type": "exchange", "name": "Binance Hot Wallet 2",
    "category": "Centralized Exchange", "tags": [], "icon": "binance.png",
}
UNKNOWN_IDENTITY = {"address": PLAIN, "type": "unknown"}


class ClassificationTests(unittest.TestCase):
    def test_unknown_is_an_answer_not_a_gap(self):
        c = classify_counterparty(UNKNOWN_IDENTITY)
        self.assertEqual(c["type"], "unknown")
        self.assertFalse(c["known"])
        self.assertFalse(c["is_infrastructure"])

    def test_exchange_is_recognized_as_infrastructure(self):
        c = classify_counterparty(EXCHANGE_IDENTITY)
        self.assertTrue(c["is_infrastructure"])
        self.assertEqual(c["name"], "Binance Hot Wallet 2")

    def test_missing_identity_does_not_raise(self):
        self.assertEqual(classify_counterparty(None)["type"], "unknown")


class ExchangeFlowTests(unittest.TestCase):
    IDS = {BINANCE: EXCHANGE_IDENTITY, PLAIN: UNKNOWN_IDENTITY}

    def _t(self, direction, cp=BINANCE):
        return {"wallet": PLAIN, "counterparty": cp, "direction": direction,
                "symbol": "SOL", "amount": 1000.0, "signature": "sig",
                "timestamp": 1786789506}

    def test_out_to_an_exchange_is_an_inflow(self):
        """Direction is relative to the WATCHED wallet: it sending out is
        the exchange receiving in. Getting this backwards would invert
        every flow signal and look entirely plausible on a dashboard."""
        f = exchange_flows([self._t("out")], self.IDS)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["flow"], "exchange_inflow")

    def test_in_from_an_exchange_is_an_outflow(self):
        f = exchange_flows([self._t("in")], self.IDS)
        self.assertEqual(f[0]["flow"], "exchange_outflow")

    def test_a_deposit_is_not_reported_as_selling(self):
        f = exchange_flows([self._t("out")], self.IDS)
        text = f[0]["implication"].lower()
        self.assertIn("not proof", text)

    def test_transfers_to_unknown_wallets_are_not_flows(self):
        self.assertEqual(exchange_flows([self._t("out", cp=PLAIN)], self.IDS), [])


class WhaleTests(unittest.TestCase):
    def test_a_baseline_needs_enough_history(self):
        thin = wallet_baseline([{"amount": 5} for _ in range(3)])
        self.assertFalse(thin["established"])
        thick = wallet_baseline([{"amount": 5}] * MIN_HISTORY_FOR_BASELINE)
        self.assertTrue(thick["established"])

    def test_median_resists_a_single_outlier(self):
        b = wallet_baseline([{"amount": 100}] * 10 + [{"amount": 10_000_000}])
        self.assertEqual(b["median"], 100)

    def test_absolute_size_alone_can_flag_a_whale(self):
        v = whale_score({"usd_value": 500_000})
        self.assertTrue(v["is_whale"])

    def test_a_small_move_that_is_huge_for_this_wallet_scores(self):
        """The case a fixed dollar threshold misses entirely: $40k is
        nothing absolutely and everything for a wallet whose median is
        $300."""
        baseline = wallet_baseline([{"usd_value": 300}] * 20)
        v = whale_score({"usd_value": 40_000}, baseline)
        self.assertGreater(v["score"], 0)
        self.assertTrue(any("median" in r for r in v["reasons"]))

    def test_a_big_move_for_a_wallet_that_always_moves_big_is_not_relative(self):
        baseline = wallet_baseline([{"usd_value": 2_000_000}] * 20)
        v = whale_score({"usd_value": 2_000_000}, baseline)
        self.assertFalse(any("x this wallet's median" in r for r in v["reasons"]))

    def test_a_missing_baseline_is_explained_not_scored_as_zero(self):
        v = whale_score({"usd_value": 50}, wallet_baseline([{"amount": 1}]))
        self.assertTrue(any("no relative read" in r for r in v["reasons"]))


class AccumulationTests(unittest.TestCase):
    def test_growth_reads_as_accumulation(self):
        a = accumulation_score([(0, 100.0), (86400 * 14, 440.0)])
        self.assertEqual(a["direction"], "accumulating")
        self.assertEqual(a["change_pct"], 340.0)

    def test_a_sale_into_strength_reads_as_distribution(self):
        a = accumulation_score([(0, 100.0), (86400 * 7, 28.0)])
        self.assertEqual(a["direction"], "distributing")
        self.assertLess(a["score"], 0)

    def test_a_small_wobble_is_holding_not_a_signal(self):
        a = accumulation_score([(0, 100.0), (86400 * 7, 102.0)])
        self.assertEqual(a["direction"], "flat")

    def test_an_enormous_percentage_is_capped(self):
        """4,000% on a dust position is not forty times the conviction of
        100% on a real one."""
        a = accumulation_score([(0, 0.01), (86400, 100.0)])
        self.assertLessEqual(a["score"], 100.0)

    def test_one_observation_cannot_establish_a_trend(self):
        self.assertEqual(accumulation_score([(0, 5.0)])["direction"], "unknown")


class ClusterTests(unittest.TestCase):
    def test_a_shared_exchange_funder_is_near_worthless_evidence(self):
        """The central refusal. Binance funds millions of wallets;
        grouping on that would put half of Solana in one cluster."""
        funding = {f"w{i}": {"funder": BINANCE, "funderName": "Binance Hot Wallet 2",
                             "funderType": "Centralized Exchange"}
                   for i in range(6)}
        c = cluster_by_funder(funding, {BINANCE: EXCHANGE_IDENTITY})[0]
        self.assertTrue(c["is_infrastructure_funder"])
        self.assertLessEqual(c["confidence"], 0.1)
        self.assertTrue(any("meaningless" in r for r in c["reasons"]))

    def test_the_weak_cluster_is_still_returned_not_hidden(self):
        """Suppressing it would hide WHY a wallet has no cluster."""
        funding = {"w1": {"funder": BINANCE, "funderType": "Centralized Exchange"}}
        self.assertEqual(len(cluster_by_funder(funding, {BINANCE: EXCHANGE_IDENTITY})), 1)

    def test_an_unknown_funder_earns_more_confidence_but_never_certainty(self):
        funding = {f"w{i}": {"funder": "FRESHFUNDER", "funderType": "unknown"}
                   for i in range(5)}
        c = cluster_by_funder(funding, {"FRESHFUNDER": {"type": "unknown"}})[0]
        self.assertFalse(c["is_infrastructure_funder"])
        self.assertGreater(c["confidence"], 0.1)
        self.assertLessEqual(c["confidence"], 0.5, "never asserted as fact")

    def test_a_lone_wallet_is_not_a_cluster(self):
        c = cluster_by_funder({"w1": {"funder": "X", "funderType": "unknown"}},
                              {"X": {"type": "unknown"}})[0]
        self.assertEqual(c["confidence"], 0.0)

    def test_no_output_ever_calls_anyone_an_insider(self):
        funding = {f"w{i}": {"funder": "F", "funderType": "unknown"} for i in range(9)}
        blob = repr(cluster_by_funder(funding, {"F": {"type": "unknown"}}))
        for word in ("insider", "conspiracy", "manipulat"):
            self.assertNotIn(word, blob.lower())


class CoordinationTests(unittest.TestCase):
    def _ev(self, wallet, ts, sym="BONK", direction="in"):
        return {"wallet": wallet, "symbol": sym, "direction": direction,
                "timestamp": ts}

    def test_three_wallets_in_a_tight_window_scores(self):
        evs = [self._ev("a", 1000), self._ev("b", 1030), self._ev("c", 1060)]
        c = coordination_score(evs)
        self.assertGreater(c["score"], 0)
        self.assertEqual(c["groups"][0]["wallet_count"], 3)

    def test_two_wallets_is_not_coordination(self):
        c = coordination_score([self._ev("a", 1000), self._ev("b", 1010)])
        self.assertEqual(c["score"], 0.0)

    def test_the_same_wallet_repeating_is_not_three_wallets(self):
        evs = [self._ev("a", 1000), self._ev("a", 1010), self._ev("a", 1020)]
        self.assertEqual(coordination_score(evs)["score"], 0.0)

    def test_spread_out_activity_does_not_score(self):
        evs = [self._ev("a", 0), self._ev("b", 100_000), self._ev("c", 200_000)]
        self.assertEqual(coordination_score(evs)["score"], 0.0)

    def test_it_states_that_timing_is_not_a_relationship(self):
        evs = [self._ev("a", 1000), self._ev("b", 1030), self._ev("c", 1060)]
        joined = " ".join(coordination_score(evs)["reasons"]).lower()
        self.assertIn("not evidence the wallets are related", joined)


class SmartMoneyTests(unittest.TestCase):
    def test_a_thin_record_claims_nothing(self):
        v = smart_money_score({"realized_trades": 4, "win_rate": 100})
        self.assertEqual(v["score"], 0.0)
        self.assertEqual(v["confidence"], "insufficient")

    def test_balance_is_not_an_input_at_all(self):
        """§28's headline refusal, and this desk's own scar: the composite
        score was once measured INVERTED because impressive stood in for
        correct."""
        base = {"realized_trades": 40, "win_rate": 60, "avg_return_pct": 5}
        self.assertEqual(smart_money_score(base)["score"],
                         smart_money_score({**base, "portfolio_usd": 90_000_000})["score"])

    def test_negative_expectancy_is_penalised_despite_a_good_win_rate(self):
        """Many small wins and rare huge losses is the classic shape."""
        good = smart_money_score({"realized_trades": 60, "win_rate": 85,
                                  "avg_return_pct": 4})
        bad = smart_money_score({"realized_trades": 60, "win_rate": 85,
                                 "avg_return_pct": -9})
        self.assertGreater(good["score"], bad["score"])

    def test_a_narrow_record_is_discounted(self):
        wide = smart_money_score({"realized_trades": 40, "win_rate": 70,
                                  "avg_return_pct": 8, "distinct_tokens": 25})
        narrow = smart_money_score({"realized_trades": 40, "win_rate": 70,
                                    "avg_return_pct": 8, "distinct_tokens": 1})
        self.assertLess(narrow["score"], wide["score"])

    def test_confidence_tracks_sample_size(self):
        for trades, expected in ((12, "low"), (30, "medium"), (80, "high")):
            v = smart_money_score({"realized_trades": trades, "win_rate": 60,
                                   "avg_return_pct": 5})
            self.assertEqual(v["confidence"], expected)

    def test_absent_inputs_do_not_count_against_a_wallet(self):
        sparse = smart_money_score({"realized_trades": 30, "avg_return_pct": 10})
        self.assertGreater(sparse["score"], 0)


class CopyTradeTests(unittest.TestCase):
    TRADE = {"wallet": PLAIN, "symbol": "BONK", "direction": "in",
             "amount": 5000, "signature": "sig"}

    def test_a_strong_wallet_produces_a_candidate_never_an_execution(self):
        r = copy_trade_candidate(self.TRADE, {"score": 82, "confidence": "high",
                                              "reasons": []})
        self.assertEqual(r["stage"], "COPY_CANDIDATE")
        self.assertNotIn("size", r)
        self.assertIn("not an approved", r["note"])

    def test_a_weak_wallet_is_rejected(self):
        r = copy_trade_candidate(self.TRADE, {"score": 20, "confidence": "low",
                                              "reasons": []})
        self.assertEqual(r["stage"], "REJECTED")

    def test_a_wallet_with_no_record_is_rejected(self):
        r = copy_trade_candidate(self.TRADE, {"score": 0,
                                              "confidence": "insufficient",
                                              "reasons": []})
        self.assertEqual(r["stage"], "REJECTED")

    def test_concentrated_token_supply_rejects_a_good_wallet(self):
        r = copy_trade_candidate(self.TRADE,
                                 {"score": 90, "confidence": "high", "reasons": []},
                                 {"holder_concentration_pct": 78})
        self.assertEqual(r["stage"], "REJECTED")
        self.assertTrue(any("supply" in x for x in r["reasons"]))


if __name__ == "__main__":
    unittest.main()
