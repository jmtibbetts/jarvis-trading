"""B0.2 — the PRODUCTION chain carries the exact instrument, not just the API.

WHY THIS FILE EXISTS SEPARATELY FROM THE TRIPWIRE. The tripwire proves
`solve_position` sizes a contract correctly when handed one. That is the
arithmetic. It says nothing about whether anything ever hands it one, and
this project has now shipped four defects of precisely that shape — a guard
reading a field nothing populated, a signature missing the parameter its body
used, `snapshot.instrument_id` assigned by no reader across 153,946 rows, and
`canonical_entry` never calling the resolver it was built for. Every one of
them had a correct implementation nobody reached.

So these tests watch the REAL seams:

    canonical_entry -> prepare_entry -> size_position -> solve_position

and the technique is the one that has worked every time here: make the OLD
answer WRONG and prove the result does not move. `get_spec("BTC/USD")`
answers 1.0 coin, which is only 100x away from PBTC's 0.01 — close enough to
a plausible number that a wrong answer looks like a right one. Poisoning it
to 999.0 removes that cover: if any stage still consults it, the quantity,
the notional and the loss all change, and if none does, they are bit-for-bit
identical to the unpoisoned run.

Asserting that a parameter appears in a signature would prove none of this.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import instruments as INST

PERP, VENUE, CONTRACT = "CRYPTO_PERP", "kraken_derivatives_us", "PBTCUCZ50"
ENTRY, STOP = 64000.0, 63900.0


def _pbtc():
    return INST.resolve_for_execution("BTC/USD", product=PERP, venue=VENUE,
                                      instrument_id=CONTRACT)


PERP_SIGNAL = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
               "paper_direction": "Long", "entry_price": ENTRY,
               "stop_loss": STOP, "target_price": 65000.0,
               "timeframe": "4H", "id": "sig-b02", "product": PERP}


class _Recorder:
    """Wrap a real callable, remember every call, and CALL THROUGH.

    A double that returns a canned value would test the double. These record
    and delegate, so the production arithmetic still runs underneath.
    """

    def __init__(self, real):
        self.real, self.calls = real, []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return self.real(*a, **kw)

    @property
    def instruments(self):
        return [kw.get("execution_instrument") for _, kw in self.calls]


class TheExactInstrumentReachesTheSizingAuthorityTests(unittest.TestCase):
    """prepare_entry -> size_position -> solve_position, watched at each seam."""

    def setUp(self):
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    def _prepare(self, instrument):
        """Run the real chain, returning (authorization, size_rec, solve_rec)."""
        from lib import paper_engine as PE
        from lib import risk_engine as RE
        size_rec = _Recorder(PE.size_position)
        solve_rec = _Recorder(RE.solve_position)
        with patch.object(PE, "size_position", size_rec), \
             patch.object(RE, "solve_position", solve_rec):
            prep = PE.prepare_entry(PERP_SIGNAL, reference_price=ENTRY,
                                    execution_instrument=instrument)
        self.assertIn("authorization", prep, prep)
        return prep["authorization"], size_rec, solve_rec

    def test_size_position_receives_the_exact_instrument(self):
        inst = _pbtc()
        _, size_rec, _ = self._prepare(inst)
        self.assertEqual(len(size_rec.calls), 1)
        self.assertIs(size_rec.instruments[0], inst,
                      "prepare_entry sized without the exact instrument")

    def test_solve_position_receives_the_exact_instrument(self):
        """The authority itself, not merely the wrapper above it."""
        inst = _pbtc()
        _, _, solve_rec = self._prepare(inst)
        self.assertEqual(len(solve_rec.calls), 1)
        self.assertIs(solve_rec.instruments[0], inst,
                      "size_position solved without the exact instrument")

    def test_the_authorized_quantity_is_whole_contracts(self):
        auth, _, _ = self._prepare(_pbtc())
        self.assertGreater(auth.qty, 0)
        self.assertEqual(auth.qty, float(int(auth.qty)),
                         f"authorized {auth.qty} — a venue cannot fill part "
                         f"of a perpetual contract")

    def test_the_authorized_loss_uses_the_contract_multiplier(self):
        """0.01, not the 1.0 the bare symbol answers."""
        auth, _, _ = self._prepare(_pbtc())
        self.assertAlmostEqual(auth.sizing["multiplier"], 0.01)
        self.assertAlmostEqual(auth.loss_at_stop,
                               auth.qty * abs(ENTRY - STOP) * 0.01, places=6)

    def test_the_authorized_notional_is_contracts_times_contract_value(self):
        auth, _, _ = self._prepare(_pbtc())
        self.assertAlmostEqual(auth.notional, auth.qty * ENTRY * 0.01,
                               places=4)

    def test_poisoning_the_generic_spec_does_not_move_the_answer(self):
        """THE BEHAVIOURAL PROOF. If any stage still asks the bare symbol
        what a unit is, a multiplier of 999 makes that visible; if none
        does, the two runs are identical."""
        clean, _, _ = self._prepare(_pbtc())

        real = INST.get_spec

        def poisoned(symbol):
            spec = real(symbol)
            return type(spec)(**{**spec.__dict__, "multiplier": 999.0}) \
                if hasattr(spec, "__dict__") else spec
        with patch.object(INST, "get_spec", poisoned):
            dirty, _, _ = self._prepare(_pbtc())

        self.assertEqual(dirty.qty, clean.qty,
                         "quantity moved when the GENERIC multiplier was "
                         "poisoned — the bare symbol is still being consulted")
        self.assertAlmostEqual(dirty.notional, clean.notional, places=6)
        self.assertAlmostEqual(dirty.loss_at_stop, clean.loss_at_stop, places=6)
        self.assertAlmostEqual(dirty.sizing["multiplier"], 0.01)

    def test_the_poison_is_detectable_at_all(self):
        """THE CONTROL, without which the test above proves nothing.

        A poison that changes no answer anywhere would make
        `test_poisoning_the_generic_spec_does_not_move_the_answer` pass
        vacuously — green because the probe is inert, not because the seam is
        closed. So the SAME poison is applied to the SAME chain with no exact
        instrument, and the answer must move. That is what makes the silence
        in the exact case evidence of something.
        """
        legacy_clean, _, _ = self._prepare(None)

        real = INST.get_spec

        def poisoned(symbol):
            spec = real(symbol)
            return type(spec)(**{**spec.__dict__, "multiplier": 999.0}) \
                if hasattr(spec, "__dict__") else spec
        with patch.object(INST, "get_spec", poisoned):
            legacy_dirty, _, _ = self._prepare(None)

        self.assertNotAlmostEqual(
            legacy_dirty.loss_at_stop, legacy_clean.loss_at_stop, places=6,
            msg="the generic multiplier poison changes nothing even on the "
                "legacy path — the probe is inert and proves nothing")


class BothCanonicalSizingPassesShareOneInstrumentTests(unittest.TestCase):
    """canonical_entry sizes TWICE — once on the venue mid, once on the
    actual fill — and the second pass is the one that decides the size that
    settles. Two resolutions could disagree; one object cannot."""

    def setUp(self):
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    @staticmethod
    def _kraken(bid=99.90, ask=100.10):
        def _at(s=0.2):
            return datetime.now(timezone.utc) - timedelta(seconds=s)
        return patch.multiple(
            "lib.kraken_stream",
            latest_quote=lambda symbol: {"bid": bid, "ask": ask, "at": _at()},
            trade_flow=lambda symbol, window=200: None)

    @staticmethod
    def _settlement():
        def settle(auth, *, fill_price, execution_provenance=None,
                   canonical_entry_fee_usd=None, observation_id=None,
                   execution_id=None):
            return {"ok": True, "position": {"id": "pos-b02"}}
        return settle

    def test_both_passes_receive_the_same_instrument_object(self):
        """The spot expression is used because it is the one with a wired
        executable feed; the invariant under test — one object, both passes
        — is a property of the call graph, not of the product."""
        from lib import canonical_entry as CE
        from lib import paper_engine as PE

        signal = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
                  "paper_direction": "Long", "entry_price": 100.0,
                  "stop_loss": 95.0, "target_price": 115.0,
                  "timeframe": "4H", "id": "sig-b02-ce",
                  "product": "CRYPTO_SPOT"}

        prep_rec = _Recorder(PE.prepare_entry)
        with self._kraken(), \
             patch.object(PE, "prepare_entry", prep_rec), \
             patch.object(PE, "settle_position_entry", self._settlement()):
            res = CE.open_canonical_position(signal, decision_price=100.0)

        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(prep_rec.calls), 2,
                         "canonical_entry no longer sizes twice — the "
                         "post-fill revalidation is what keeps risk honest")
        first, second = prep_rec.instruments
        self.assertIsNotNone(first, "the quote-mid pass sized on the bare "
                                    "symbol, not the resolved instrument")
        self.assertIs(first, second,
                      "the post-fill pass re-resolved instead of reusing the "
                      "instrument the entry was already committed to")

    def test_the_instrument_both_passes_share_is_the_resolved_one(self):
        """Not merely the same object — the RIGHT object."""
        from lib import canonical_entry as CE
        from lib import paper_engine as PE

        signal = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
                  "paper_direction": "Long", "entry_price": 100.0,
                  "stop_loss": 95.0, "target_price": 115.0,
                  "timeframe": "4H", "id": "sig-b02-ce2",
                  "product": "CRYPTO_SPOT"}

        prep_rec = _Recorder(PE.prepare_entry)
        with self._kraken(), \
             patch.object(PE, "prepare_entry", prep_rec), \
             patch.object(PE, "settle_position_entry", self._settlement()):
            CE.open_canonical_position(signal, decision_price=100.0)

        shared = prep_rec.instruments[0]
        self.assertEqual(shared.canonical_symbol, "BTC/USD")
        self.assertEqual(shared.product, "CRYPTO_SPOT")
        self.assertTrue(shared.executable)


if __name__ == "__main__":
    unittest.main()
