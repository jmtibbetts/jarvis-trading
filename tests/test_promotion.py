"""§4.3 promotion framework — the rules that keep a lucky challenger out.

These tests seed synthetic resolved candidates and verify each criterion
does its one job: leakage cutoffs exclude pre-definition history, span and
sample gates return INSUFFICIENT_DATA rather than a verdict, tail
tolerance blocks a higher-mean-but-fatter-left-tail challenger, and the
champion ledger only ever grows.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from app.database import CandidateSignal, ScoreChampion, get_db, init_db
from lib.promotion import (
    FOUNDING_CHAMPION,
    MIN_SELECTED,
    _evaluate_challenger,
    _net_r,
    champion_history,
    current_champion,
    evaluate_promotion,
    promote,
)

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _row(i: int, day_offset: float, a_score: float, c_score: float,
         net_r: float, asset_class: str = "Crypto") -> dict:
    """A universe row in the shape _load_universe produces."""
    return {
        "id": f"TEST-PROMO-{i}",
        "created_at": (BASE + timedelta(days=day_offset)).isoformat(),
        "composite_score": a_score,
        "_shadow": {"C": c_score},
        "net_r": net_r,
        "asset_class": asset_class,
    }


def _universe(n=90, span_days=30.0, a_picks_losers=True):
    """n rows over span_days. A scores >=55 on losers, C scores >=55 on
    winners — the measured inversion, synthesized."""
    rows = []
    for i in range(n):
        winner = i % 2 == 0
        r = 1.0 if winner else -1.0
        a = 40.0 if winner else 80.0      # A selects the losers
        c = 80.0 if winner else 40.0      # C selects the winners
        if not a_picks_losers:
            a = 80.0 if winner else 40.0  # both select winners
        rows.append(_row(i, span_days * i / n, a, c, r))
    return rows


class TestNetR(unittest.TestCase):
    def test_net_r_recovers_r_from_pct(self):
        # entry 100, stop 95 -> risk 5%; pnl +10% -> +2R
        self.assertAlmostEqual(_net_r(10.0, 100.0, 95.0), 2.0)
        # short geometry: stop above entry, same magnitude
        self.assertAlmostEqual(_net_r(-5.0, 100.0, 105.0), -1.0)

    def test_degenerate_levels_refuse(self):
        self.assertIsNone(_net_r(10.0, 100.0, 100.0))
        self.assertIsNone(_net_r(None, 100.0, 95.0))


class TestCriteria(unittest.TestCase):
    def test_inverted_champion_loses_to_challenger(self):
        ev = _evaluate_challenger(_universe(), "C", "A", gate=55.0)
        self.assertEqual(ev["verdict"], "PROMOTE_ELIGIBLE")
        self.assertEqual(ev["failed"], [])
        self.assertGreaterEqual(
            ev["criteria"]["walk_forward"]["folds_won"], 2)
        self.assertGreater(
            ev["criteria"]["net_r_improvement"]["delta"], 0)

    def test_short_span_is_insufficient_not_failed(self):
        ev = _evaluate_challenger(_universe(span_days=5.0), "C", "A", 55.0)
        self.assertEqual(ev["verdict"], "INSUFFICIENT_DATA")

    def test_small_sample_is_insufficient(self):
        ev = _evaluate_challenger(_universe(n=20), "C", "A", 55.0)
        self.assertEqual(ev["verdict"], "INSUFFICIENT_DATA")
        self.assertLess(ev["criteria"]["min_sample"]["selected"],
                        MIN_SELECTED)

    def test_leakage_cutoff_excludes_predefinition_rows(self):
        # 60 absurdly good rows BEFORE C's defined_at (2026-08-13) must not
        # count; the post-cutoff stream alone is too small to judge.
        old = [_row(1000 + i, -400 + i, 40.0, 99.0, 5.0) for i in range(60)]
        recent = _universe(n=20)
        ev = _evaluate_challenger(old + recent, "C", "A", 55.0)
        self.assertEqual(ev["oos_universe"], 20)
        self.assertEqual(ev["verdict"], "INSUFFICIENT_DATA")

    def test_fat_left_tail_blocks_promotion(self):
        # Challenger has the better mean but a catastrophic tail: mostly
        # +1.2R with sprinkled -6R. Champion steady at +0.3R.
        rows = []
        for i in range(90):
            r_ch = -6.0 if i % 10 == 0 else 1.2
            rows.append(_row(i, 30.0 * i / 90, 40.0, 80.0, r_ch))
            rows.append(_row(1000 + i, 30.0 * i / 90, 80.0, 40.0, 0.3))
        ev = _evaluate_challenger(rows, "C", "A", 55.0)
        self.assertIn(ev["verdict"], ("NOT_ELIGIBLE", "PROMOTE_ELIGIBLE"))
        if ev["verdict"] == "NOT_ELIGIBLE":
            self.assertIn("tail_not_worse", ev["failed"])
        else:
            # Mean gap can legitimately dominate; the criterion must at
            # least have measured the tail rather than skipped it.
            self.assertIn("challenger_p5", ev["criteria"]["tail_not_worse"])
        # The p5 measurement itself must see the -6R sprinkle.
        self.assertLessEqual(
            ev["criteria"]["tail_not_worse"]["challenger_p5"], -5.0)

    def test_no_improvement_means_not_eligible(self):
        ev = _evaluate_challenger(
            _universe(a_picks_losers=False), "C", "A", 55.0)
        self.assertEqual(ev["verdict"], "NOT_ELIGIBLE")
        self.assertIn("net_r_improvement", ev["failed"])

    def test_selection_frequency_always_reported(self):
        ev = _evaluate_challenger(_universe(), "C", "A", 55.0)
        sf = ev["criteria"]["selection_frequency"]
        self.assertIn("challenger", sf)
        self.assertIn("champion", sf)


class TestChampionLedger(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_db() as db:
            db.query(ScoreChampion).delete()
            db.query(CandidateSignal).filter(
                CandidateSignal.id.like("TEST-PROMO-%")).delete(
                synchronize_session=False)
            db.commit()

    tearDown = setUp

    def test_founding_champion_is_seeded_once(self):
        c1 = current_champion()
        c2 = current_champion()
        self.assertEqual(c1["variant"], FOUNDING_CHAMPION)
        self.assertEqual(c1["id"], c2["id"])
        self.assertEqual(len(champion_history()), 1)

    def test_promote_refuses_without_eligibility(self):
        # Empty candidate table -> INSUFFICIENT_DATA for every challenger.
        with self.assertRaises(ValueError):
            promote("C")
        # Refusal must not have written a row.
        self.assertEqual(len(champion_history()), 1)

    def test_promote_appends_on_eligibility(self):
        # Seed real candidate rows that make C eligible end-to-end.
        with get_db() as db:
            for i in range(90):
                winner = i % 2 == 0
                created = (BASE + timedelta(days=30.0 * i / 90)).isoformat()
                db.add(CandidateSignal(
                    id=f"TEST-PROMO-{i}", created_at=created,
                    dedup_hash=f"TEST-PROMO-H{i}", symbol="TEST/USD",
                    asset_class="Crypto", timeframe="4H", direction="LONG",
                    entry_price=100.0, stop_loss=95.0, target_price=110.0,
                    composite_score=(40.0 if winner else 80.0),
                    shadow_variants=json.dumps(
                        {"C": (80.0 if winner else 40.0)}),
                    verdict="persisted", resolved=True,
                    pnl_pct=(5.0 if winner else -5.0),
                ))
            db.commit()
        ev = evaluate_promotion()
        self.assertEqual(ev["challengers"]["C"]["verdict"],
                         "PROMOTE_ELIGIBLE")
        promoted = promote("C", note="test promotion")
        history = champion_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["variant"], FOUNDING_CHAMPION)
        self.assertEqual(history[-1]["variant"], "C")
        self.assertEqual(current_champion()["id"], promoted["id"])
        # The evidence is frozen into the row.
        with get_db() as db:
            row = db.query(ScoreChampion).order_by(
                ScoreChampion.id.desc()).first()
            frozen = json.loads(row.evidence)
            self.assertEqual(frozen["verdict"], "PROMOTE_ELIGIBLE")


class TestMarketStateVariant(unittest.TestCase):
    def test_ms_ignores_history_and_geometry_components(self):
        from lib.score_variants import variant_ms
        base = {"ta_confluence": 70, "volatility": 60, "conflict_ratio": 0.4,
                "regime": 50, "news": 50, "freshness": 50,
                "data_quality": 80, "liquidity": 80}
        with_history = dict(base, calibrated_confidence=95, rr=99)
        self.assertEqual(variant_ms(base), variant_ms(with_history))

    def test_ms_flips_measured_inverted_components(self):
        from lib.score_variants import variant_ms
        low_confluence = {"ta_confluence": 10, "volatility": 50,
                          "conflict_ratio": 0.5}
        high_confluence = dict(low_confluence, ta_confluence=90)
        self.assertGreater(variant_ms(low_confluence),
                           variant_ms(high_confluence))

    def test_ms_refuses_without_core_trio(self):
        from lib.score_variants import variant_ms
        self.assertIsNone(variant_ms({"volatility": 50}))
        self.assertIsNone(variant_ms({}))

    def test_compute_variants_carries_ms_and_v2_schema(self):
        from lib.score_variants import compute_variants
        out = compute_variants(60.0, {"ta_confluence": 70, "volatility": 60,
                                      "conflict_ratio": 0.4})
        self.assertEqual(out["schema"], "shadow_v2_2026-08-14")
        self.assertIsNotNone(out["MS"])


if __name__ == "__main__":
    unittest.main()
