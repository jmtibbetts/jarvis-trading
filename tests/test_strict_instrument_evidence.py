"""Exact-contract isolation across all eight evidence seams.

A perpetual's economics belong to a LISTED CONTRACT. Two contracts can share
a symbol, a venue and a product and still be different markets, so evidence
keyed only on symbol/product/venue can let one answer for the other.

The live 15m control case is why these exist: it resolved COMPLETE with
genuinely correct PBTCUCZ50-only evidence, and the resolver never filtered by
contract — it was right by accident, because nothing else happened to be in
that window. Accidentally correct is indistinguishable from enforced-correct
in the output, and only one of them survives a second contract appearing.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.database import (DecisionObservation, DecisionOutcome,
                          InstrumentQuoteSample, get_db)
from lib import decision_outcome as DO
from lib import range_collector as RC

PERP, VENUE = "CRYPTO_PERP", "kraken_derivatives_us"
SYM, RIGHT, WRONG = "BTC/USD", "PBTCUCZ50", "WRONG_CONTRACT"


def _clear():
    RC.reset_stream_state()
    with get_db() as db:
        db.query(InstrumentQuoteSample).delete()
        db.query(DecisionOutcome).delete()
        db.query(DecisionObservation).delete()


def _samples(instrument, t0, prices, *, step_s=60):
    with get_db() as db:
        for i, (b, a) in enumerate(prices):
            db.add(InstrumentQuoteSample(
                product=PERP, venue=VENUE, symbol=SYM,
                instrument_id=instrument,
                market_data_source="bitnomial_public_book",
                observed_at=(t0 + timedelta(seconds=i * step_s)).isoformat(),
                bid=b, ask=a, mid=(b + a) / 2.0, sample_reason="CHANGE"))


class PolicyTests(unittest.TestCase):
    def test_perps_require_an_exact_contract(self):
        self.assertTrue(RC.requires_exact_instrument("CRYPTO_PERP"))

    def test_the_rule_lives_in_one_authority(self):
        """Not re-implemented per seam, where one could be forgotten."""
        import inspect
        src = inspect.getsource(RC) + inspect.getsource(DO)
        self.assertGreaterEqual(src.count("requires_exact_instrument"), 6)


class WrongContractContributesZeroTests(unittest.TestCase):
    """LOAD-BEARING. Must fail against symbol/product/venue-only lookup."""

    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.due = self.t0 + timedelta(minutes=15)
        # the real contract: a modest, honest range
        _samples(RIGHT, self.t0, [(99.5 + i * 0.1, 100.5 + i * 0.1)
                                  for i in range(16)])
        # a louder, later, far more favourable impostor
        _samples(WRONG, self.t0 + timedelta(seconds=5),
                 [(150.0 + i, 151.0 + i) for i in range(16)], step_s=55)

    def test_range_uses_only_the_requested_contract(self):
        ev = RC.range_over(symbol=SYM, product=PERP, venue=VENUE,
                           instrument_id=RIGHT, start=self.t0, end=self.due)
        self.assertEqual(ev.sample_count, 16)
        self.assertEqual(ev.instrument_id, RIGHT)
        self.assertTrue(all(s.instrument_id == RIGHT for s in ev.samples))
        # the impostor's 150+ prices must not appear anywhere
        self.assertLess(ev.high_bid, 120.0)

    def test_checkpoint_is_the_requested_contract(self):
        cp = RC.checkpoint_at(symbol=SYM, product=PERP, venue=VENUE,
                              instrument_id=RIGHT, at=self.due)
        self.assertTrue(cp.ok)
        self.assertEqual(cp.instrument_id, RIGHT)
        self.assertLess(cp.bid, 120.0)   # not the impostor's later, higher quote

    def test_the_impostor_is_visible_but_never_consumed(self):
        """Proves the isolation is enforced, not merely absent data."""
        with get_db() as db:
            total = db.query(InstrumentQuoteSample).filter(
                InstrumentQuoteSample.symbol == SYM).count()
        self.assertEqual(total, 32)      # both contracts really are stored
        ev = RC.range_over(symbol=SYM, product=PERP, venue=VENUE,
                           instrument_id=RIGHT, start=self.t0, end=self.due)
        self.assertEqual(ev.sample_count, 16)


class NullNeverWildcardsTests(unittest.TestCase):
    """LOAD-BEARING. Anonymous evidence is not exact-contract evidence."""

    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.due = self.t0 + timedelta(minutes=15)
        # abundant, perfect, and anonymous — exactly the 153,946 pre-stamp rows
        _samples(None, self.t0, [(99.5 + i * 0.1, 100.5 + i * 0.1)
                                 for i in range(16)])

    def test_anonymous_samples_are_not_exact_evidence(self):
        ev = RC.range_over(symbol=SYM, product=PERP, venue=VENUE,
                           instrument_id=RIGHT, start=self.t0, end=self.due)
        self.assertEqual(ev.sample_count, 0)
        self.assertIsNone(ev.high_bid)

    def test_no_exact_checkpoint_from_anonymous_rows(self):
        cp = RC.checkpoint_at(symbol=SYM, product=PERP, venue=VENUE,
                              instrument_id=RIGHT, at=self.due)
        self.assertFalse(cp.ok)

    def test_a_perp_without_a_contract_refuses_rather_than_wildcarding(self):
        self.assertEqual(
            RC.samples_between(symbol=SYM, product=PERP, venue=VENUE,
                               instrument_id=None, start=self.t0,
                               end=self.due), [])
        cp = RC.checkpoint_at(symbol=SYM, product=PERP, venue=VENUE,
                              instrument_id=None, at=self.due)
        self.assertFalse(cp.ok)
        self.assertIn("exact contract", (cp.reason or "").lower())

    def test_resolution_says_exactly_why(self):
        with get_db() as db:
            db.add(DecisionOutcome(
                observation_id="anon-1", horizon="15m", horizon_min=15,
                symbol=SYM, product=PERP, venue=VENUE, instrument_id=None,
                side="long", reference_price=100.0, status="PENDING",
                decision_at=self.t0.isoformat(), due_at=self.due.isoformat()))
        with get_db() as db:
            row = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "anon-1").first()
            out = DO.resolve_outcome(row)
        self.assertEqual(out["status"], DO.INSUFFICIENT_DATA)
        self.assertIn("exact contract", out["status_reason"])


class DedupeIsContractSpecificTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc)

    def tearDown(self):
        RC.reset_stream_state()

    def test_one_contract_cannot_suppress_another(self):
        a = RC.note_quote(symbol=SYM, product=PERP, venue=VENUE,
                          bid=100.0, ask=101.0, at=self.t0,
                          instrument_id=RIGHT)
        # identical numbers, different contract: still NEW evidence
        b = RC.note_quote(symbol=SYM, product=PERP, venue=VENUE,
                          bid=100.0, ask=101.0,
                          at=self.t0 + timedelta(milliseconds=10),
                          instrument_id=WRONG)
        self.assertEqual(a, RC.CHANGE)
        self.assertEqual(b, RC.CHANGE)
        self.assertEqual(RC.buffered_count(), 2)


class PendingTargetsAreContractSpecificTests(unittest.TestCase):
    def setUp(self):
        _clear()
        now = datetime.now(timezone.utc)
        rows = [(RIGHT, "p1"), (RIGHT, "p2"), (WRONG, "p3"), (None, "p4")]
        with get_db() as db:
            for instr, oid in rows:
                db.add(DecisionOutcome(
                    observation_id=oid, horizon="15m", horizon_min=15,
                    symbol=SYM, product=PERP, venue=VENUE,
                    instrument_id=instr, side="long", status="PENDING",
                    decision_at=now.isoformat(), due_at=now.isoformat()))

    def test_two_contracts_are_two_targets_and_null_is_none(self):
        targets = RC.instruments_pending()
        instruments = sorted(t["instrument_id"] for t in targets)
        self.assertEqual(instruments, [RIGHT, WRONG])   # p1+p2 collapse; p4 gone


class SnapshotMismatchIsRefusedTests(unittest.TestCase):
    def setUp(self):
        _clear()

    class _Snap:
        def __init__(self, instrument):
            self.bid, self.ask = 100.0, 101.0
            self.fillable, self.status, self.reason = True, "AVAILABLE", None
            self.instrument_id = instrument
            self.source, self.venue_event_at = "test", None

    def _collect(self, returned):
        import lib.execution_snapshot as ES
        real = ES.execution_market_snapshot
        ES.execution_market_snapshot = lambda *a, **k: self._Snap(returned)
        try:
            return RC.collect_once([{"symbol": SYM, "product": PERP,
                                     "venue": VENUE,
                                     "instrument_id": RIGHT}])
        finally:
            ES.execution_market_snapshot = real

    def test_a_different_contract_is_refused(self):
        out = self._collect(WRONG)
        self.assertEqual(out["recorded"], 0)
        self.assertTrue(any("MISMATCH" in k for k in out["refusals"]))

    def test_an_anonymous_snapshot_is_refused_for_an_exact_product(self):
        self.assertEqual(self._collect(None)["recorded"], 0)

    def test_the_matching_contract_is_accepted(self):
        self.assertEqual(self._collect(RIGHT)["recorded"], 1)


class VersionDescribesTerminalizationTests(unittest.TestCase):
    """A row scheduled by v1 and resolved by v2 must credit v2."""

    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.due = self.t0 + timedelta(minutes=15)
        _samples(RIGHT, self.t0, [(99.5 + i * 0.1, 100.5 + i * 0.1)
                                  for i in range(16)])

    def _seed(self, oid, status="PENDING"):
        """Rows are created and USED inside a session — a detached ORM row
        raises on the first attribute read, which is the very failure mode
        range_collector detaches its Samples to avoid."""
        with get_db() as db:
            db.add(DecisionOutcome(
                observation_id=oid, horizon="15m", horizon_min=15,
                symbol=SYM, product=PERP, venue=VENUE, instrument_id=RIGHT,
                side="long", reference_price=100.0, status=status,
                observer_version="decision_outcome_observer_v1",
                decision_at=self.t0.isoformat(), due_at=self.due.isoformat()))

    def test_the_terminal_claim_records_the_resolver_that_made_it(self):
        self._seed("ver-1")
        with get_db() as db:
            row = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "ver-1").first()
            self.assertEqual(row.observer_version,
                             "decision_outcome_observer_v1")   # scheduled by v1
            fields = DO.resolve_outcome(row)
            self.assertTrue(DO.finalize(row, fields))
            stored = row.observer_version
        self.assertEqual(stored, DO.OUTCOME_OBSERVER_VERSION)
        self.assertIn("instrument_key", stored)
        self.assertIn("instrument_key", fields["range_source"])

    def test_an_already_terminal_v1_row_is_never_rewritten(self):
        self._seed("ver-2", status=DO.COMPLETE)
        with get_db() as db:
            row = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "ver-2").first()
            self.assertFalse(DO.finalize(row, {"status": DO.COMPLETE,
                                               "observer_version": "v2"}))
            self.assertEqual(row.observer_version,
                             "decision_outcome_observer_v1")


class StructuralSeamGuardTests(unittest.TestCase):
    """Defence in depth: this project has repeatedly shipped a field that
    was wired everywhere and consumed nowhere."""

    def test_every_evidence_seam_accepts_the_contract(self):
        import inspect
        for fn in (RC.samples_between, RC.range_over, RC.checkpoint_at):
            self.assertIn("instrument_id",
                          inspect.signature(fn).parameters, fn.__name__)

    def test_the_resolver_passes_the_contract_into_both_readers(self):
        import inspect
        src = inspect.getsource(DO.resolve_outcome)
        self.assertIn('key["instrument_id"] = row.instrument_id', src)
        self.assertIn("RC.checkpoint_at(**key", src)
        self.assertIn("RC.range_over(**key", src)

    def test_typed_evidence_carries_its_contract(self):
        self.assertIn("instrument_id", RC.Sample.__dataclass_fields__)
        self.assertIn("instrument_id", RC.RangeEvidence.__dataclass_fields__)
        self.assertIn("instrument_id", RC.Checkpoint.__dataclass_fields__)


if __name__ == "__main__":
    unittest.main()
