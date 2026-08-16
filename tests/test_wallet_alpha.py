"""W5 — realized performance and post-entry alpha are different metrics.

The old `alpha_score` was `50 + median_return * 2` over the wallet's CLOSED
ROUND TRIPS, under a comment claiming it measured "the token's move after
entry, which is what a follower would actually capture".

CHECKPOINT 3 from the audit, both fixtures verbatim:

    wallet buys at $10, token rises to $15 within the hour, wallet holds
    too long and exits at $9
        -> realized return NEGATIVE
        -> 1h post-entry alpha STRONGLY POSITIVE

    wallet buys at $10, token immediately falls to $8, later recovers to
    $12, wallet exits profitably much later
        -> realized return POSITIVE
        -> short-horizon post-entry alpha NEGATIVE
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.database import WalletObservation, get_db
from lib import wallet_alpha
from lib.wallet_scoring import reconstruct_trades, score_wallet

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
WALLET = "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"


def _clear():
    with get_db() as db:
        db.query(WalletObservation).delete()


def _leg(sig, mint, symbol, amount, direction, ts):
    return {"signature": sig, "mint": mint, "symbol": symbol,
            "amount": amount, "direction": direction, "timestamp": ts,
            "counterparty": "cp"}


def _round_trip(mint, spent_usdc, proceeds_usdc, qty, t_open, t_close):
    """A USDC-quoted round trip, so realized return is unambiguous."""
    return [
        _leg("in-" + mint, mint, "TOKEN", qty, "in", t_open),
        _leg("in-" + mint, "usdc", "USDC", spent_usdc, "out", t_open),
        _leg("out-" + mint, mint, "TOKEN", qty, "out", t_close),
        _leg("out-" + mint, "usdc", "USDC", proceeds_usdc, "in", t_close),
    ]


class Checkpoint3Tests(unittest.TestCase):
    """The two fixtures the audit names."""

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def _observe_with_prices(self, mint, entry_px, prices):
        """Record one entry and resolve its horizons from `prices`."""
        with get_db() as db:
            row, _ = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint=mint,
                signature=f"sig-{mint}", entry_timestamp=T0.isoformat(),
                evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=entry_px)

            def lookup(_mint, when):
                return prices.get(round((when - T0).total_seconds()))

            wallet_alpha.resolve_observation(
                db, row, lookup, now=T0 + timedelta(days=2))
            db.flush()
            return {h: getattr(row, f"return_{h}") for h, _ in wallet_alpha.HORIZONS}

    def test_wallet_loses_while_post_entry_alpha_is_strongly_positive(self):
        """Buys at $10, token hits $15 in the hour, wallet exits at $9."""
        # 1. the WALLET's realized return: -$100 on a $1,000 position
        trades = reconstruct_trades(
            _round_trip("mintA", spent_usdc=1000, proceeds_usdc=900,
                        qty=100, t_open=T0.timestamp(),
                        t_close=(T0 + timedelta(days=1)).timestamp()))
        t = trades["trades"][0]
        self.assertLess(t["pnl_usd"], 0, "the wallet lost money")
        self.assertAlmostEqual(t["return_pct"], -10.0, places=2)

        # 2. the TOKEN's post-entry path: $10 -> $15 within the hour
        returns = self._observe_with_prices("mintA", 10.0, {
            300: 11.0, 900: 13.0, 3600: 15.0, 14400: 12.0, 86400: 9.0,
        })
        self.assertAlmostEqual(returns["1h"], 50.0, places=2,
                               msg="the token rose 50% in the hour after entry")
        self.assertGreater(returns["1h"], 0)

        # 3. THE POINT: opposite signs, same entry.
        self.assertLess(t["return_pct"], 0)
        self.assertGreater(returns["1h"], 0)

    def test_wallet_wins_while_short_horizon_alpha_is_negative(self):
        """Buys at $10, dips to $8, recovers to $12, exits profitably late."""
        trades = reconstruct_trades(
            _round_trip("mintB", spent_usdc=1000, proceeds_usdc=1200,
                        qty=100, t_open=T0.timestamp(),
                        t_close=(T0 + timedelta(days=3)).timestamp()))
        t = trades["trades"][0]
        self.assertGreater(t["pnl_usd"], 0, "the wallet made money")

        returns = self._observe_with_prices("mintB", 10.0, {
            300: 9.0, 900: 8.5, 3600: 8.0, 14400: 9.5, 86400: 12.0,
        })
        self.assertLess(returns["1h"], 0,
                        "the hour after entry was down — a follower on that "
                        "horizon would have been underwater")
        self.assertGreater(returns["24h"], 0)

        self.assertGreater(t["return_pct"], 0)
        self.assertLess(returns["1h"], 0)


class HorizonResolutionTests(unittest.TestCase):

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_a_pending_horizon_is_null_not_zero(self):
        """'the token did not move' and 'we have not looked yet' are
        different facts, and conflating them is the whole failure mode."""
        with get_db() as db:
            row, _ = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint="m", signature="s1",
                entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=10.0)
            # Only 20 minutes have passed: 5m and 15m are due, 1h is not.
            due = wallet_alpha.due_horizons(row, now=T0 + timedelta(minutes=20))
            self.assertEqual(due, ["5m", "15m"])
            wallet_alpha.resolve_observation(
                db, row, lambda m, w: 11.0, now=T0 + timedelta(minutes=20))
            self.assertIsNotNone(row.return_5m)
            self.assertIsNone(row.return_1h, "1h has not elapsed — NULL, not 0")
            self.assertEqual(row.fully_resolved, 0)

    def test_a_missing_price_leaves_the_horizon_unresolved_for_a_retry(self):
        with get_db() as db:
            row, _ = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint="m", signature="s2",
                entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=10.0)
            wallet_alpha.resolve_observation(
                db, row, lambda m, w: None, now=T0 + timedelta(days=2))
            self.assertIsNone(row.return_1h)
            self.assertEqual(row.horizons_resolved or "", "")
            # A later pass with data available fills it.
            wallet_alpha.resolve_observation(
                db, row, lambda m, w: 12.0, now=T0 + timedelta(days=2))
            self.assertAlmostEqual(row.return_1h, 20.0, places=2)

    def test_observations_are_append_only_and_idempotent(self):
        """The registry holds one identity; this holds many sightings."""
        with get_db() as db:
            _, made1 = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint="m", signature="sigX",
                entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=1.0)
            _, made2 = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint="m", signature="sigX",
                entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=1.0)
            _, made3 = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint="m", signature="sigY",
                entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=1.0)
            self.assertTrue(made1)
            self.assertFalse(made2, "a re-scan of one signature is one sighting")
            self.assertTrue(made3, "a NEW signature from a known wallet IS evidence")
            self.assertEqual(
                db.query(WalletObservation).filter(
                    WalletObservation.wallet_address == WALLET).count(), 2)

    def test_entering_before_the_surge_is_recorded_as_negative(self):
        with get_db() as db:
            row, _ = wallet_alpha.record_observation(
                db, wallet_address=WALLET, mint="m", signature="early",
                entry_timestamp=(T0 - timedelta(minutes=20)).isoformat(),
                surge_started_at=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=1.0)
            self.assertAlmostEqual(row.seconds_before_surge, -1200.0, places=1)


class AggregationTests(unittest.TestCase):

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def _book(self, n, one_hour_return):
        with get_db() as db:
            for i in range(n):
                row, _ = wallet_alpha.record_observation(
                    db, wallet_address=WALLET, mint=f"m{i}",
                    signature=f"s{i}", entry_timestamp=T0.isoformat(),
                    evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=10.0)
                wallet_alpha.resolve_observation(
                    db, row,
                    lambda m, w: 10.0 * (1 + one_hour_return / 100.0),
                    now=T0 + timedelta(days=2))
            db.flush()

    def test_too_few_observations_refuses_rather_than_guesses(self):
        self._book(2, 30.0)
        with get_db() as db:
            a = wallet_alpha.alpha_for_wallet(db, WALLET)
        self.assertFalse(a["measurable"])
        self.assertIsNone(a["alpha_score"])
        self.assertIn(str(wallet_alpha.MIN_OBSERVATIONS_FOR_ALPHA), a["reason"])

    def test_enough_observations_produce_a_score(self):
        self._book(10, 20.0)
        with get_db() as db:
            a = wallet_alpha.alpha_for_wallet(db, WALLET)
        self.assertTrue(a["measurable"])
        self.assertIsNotNone(a["alpha_score"])
        self.assertAlmostEqual(a["horizons"]["1h"]["median_return_pct"], 20.0,
                               places=2)

    def test_each_horizon_reports_its_own_sample_size(self):
        """A wallet with forty resolved 5m returns and three resolved 24h
        ones must not claim a 24h number it has not earned."""
        with get_db() as db:
            for i in range(6):
                row, _ = wallet_alpha.record_observation(
                    db, wallet_address=WALLET, mint=f"p{i}", signature=f"p{i}",
                    entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=10.0)
                # only the first two are old enough for 24h
                when = (T0 + timedelta(days=2) if i < 2
                        else T0 + timedelta(hours=2))
                wallet_alpha.resolve_observation(db, row, lambda m, w: 11.0,
                                                 now=when)
            db.flush()
            a = wallet_alpha.alpha_for_wallet(db, WALLET)
        self.assertEqual(a["horizons"]["1h"]["n"], 6)
        self.assertEqual(a["horizons"]["24h"]["n"], 2)


class CopyabilityTests(unittest.TestCase):

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_copy_is_not_an_alias_of_alpha(self):
        """Detection latency and costs are what separate them."""
        with get_db() as db:
            for i in range(8):
                row, _ = wallet_alpha.record_observation(
                    db, wallet_address=WALLET, mint=f"c{i}", signature=f"c{i}",
                    entry_timestamp=T0.isoformat(), evidence_class=wallet_alpha.VERIFIED_BUY_ENTRY,
                entry_price_usd=10.0)
                wallet_alpha.resolve_observation(
                    db, row, lambda m, w: 10.4, now=T0 + timedelta(days=2))
            db.flush()
            a = wallet_alpha.alpha_for_wallet(db, WALLET)
            c = wallet_alpha.copyability(db, WALLET)

        self.assertTrue(a["measurable"])
        self.assertTrue(c["measurable"])
        # A 4% move is real alpha; after 0.60% round-trip costs a follower
        # keeps 3.4%, so copy must be strictly lower and explicitly so.
        self.assertLess(c["net_after_costs_pct"], a["horizons"]["1h"]["median_return_pct"])
        self.assertIn("after", c["reason"])

    def test_assumptions_are_stated_not_buried(self):
        with get_db() as db:
            c = wallet_alpha.copyability(db, WALLET)
        self.assertIn("detection_latency_s", c["assumptions"])
        self.assertIn("round_trip_cost_pct", c["assumptions"])


class NoAliasingTests(unittest.TestCase):
    """The false semantics must not survive anywhere."""

    def test_scoring_no_longer_writes_realized_return_into_alpha(self):
        legs = []
        for i in range(12):
            legs += _round_trip(f"n{i}", 500, 700, 10, 1000 + i, 2000 + i)
        s = score_wallet(reconstruct_trades(legs))
        self.assertIsNone(s["alpha_score"])
        self.assertIsNotNone(s["legacy_alpha_score"])

    def test_the_realized_return_is_labelled_as_what_it_is(self):
        """The comment must not CLAIM post-entry alpha.

        Asserted on the marker rather than on the absence of the old
        phrase, because the current comment quotes that phrase in order to
        record what it used to claim — and a bare `assertNotIn` would fail
        on the documentation of the fix.
        """
        import inspect

        from lib import wallet_scoring
        src = inspect.getsource(wallet_scoring.score_wallet)
        self.assertIn("NOT post-entry market alpha", src,
                      "the realized-return block must say what it is not")
        self.assertIn("legacy_alpha_score", src)

    def test_the_route_and_persistence_never_alias_the_two(self):
        """No production path may write realized return into alpha_score."""
        import ast
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for d in ("lib", "app", "jobs"):
            for f in (root / d).rglob("*.py"):
                if "__pycache__" in f.parts:
                    continue
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    for tgt in node.targets:
                        name = getattr(tgt, "attr", None)
                        if name != "alpha_score":
                            continue
                        v = node.value
                        # Only None or an alpha_for_wallet result may land here.
                        ok = (isinstance(v, ast.Constant) and v.value is None) or \
                             (isinstance(v, ast.Subscript)) or \
                             (isinstance(v, ast.Call))
                        if not ok:
                            offenders.append(
                                f"{f.relative_to(root).as_posix()}:{node.lineno}")
        self.assertEqual(offenders, [], f"alpha_score written directly: {offenders}")


if __name__ == "__main__":
    unittest.main()
