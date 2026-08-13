"""Two places construct a TradingSignal. They must persist the same fields.

The scanner and the generator both call score_signal, so both have a
classified strategy in hand. Only the generator wrote it. The scanner is
the larger producer by far — 1,302 of one day's 1,657 signals — so
strategy attribution sat at roughly zero and calibration's by_strategy
table could never fill, while the classification ran on every scan and was
discarded at the last step.

That defect is invisible from either file alone: each looks like a complete,
reasonable constructor. It is only visible by comparing them, which is what
this test does.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Fields legitimately unique to one producer, with the reason. Anything not
# listed here must appear in both.
ALLOWED_ASYMMETRY = {
    # the generator records which track proposed the signal; the scanner
    # stamps its mode into trigger_event instead
    "signal_source",
    # scanner-only provenance
    "paper_direction", "signal_version", "market_data_at", "trigger_event",
    "id", "generated_at", "asset_name", "paper_mode", "updated_date",
}


def _constructor_kwargs(path: pathlib.Path, call_name: str) -> list[set]:
    """Every keyword passed to `call_name(...)` in a file, one set per call."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name == call_name and node.keywords:
            found.append({k.arg for k in node.keywords if k.arg})
    return found


class BothWritersPersistTheSameEvidenceTests(unittest.TestCase):

    GENERATOR = ROOT / "jobs" / "generate_signals.py"
    SCANNER = ROOT / "jobs" / "scan_opportunities.py"

    def _biggest(self, path):
        calls = _constructor_kwargs(path, "TradingSignal")
        self.assertTrue(calls, f"no TradingSignal(...) found in {path.name}")
        return max(calls, key=len)

    def test_the_scanner_records_the_strategy_it_classified(self):
        """The specific regression: computed on every scan, then dropped."""
        scanner = self._biggest(self.SCANNER)
        self.assertIn("strategy", scanner)
        self.assertIn("strategy_score", scanner)

    def test_neither_writer_silently_omits_what_the_other_records(self):
        gen, scan = self._biggest(self.GENERATOR), self._biggest(self.SCANNER)
        missing_from_scanner = (gen - scan) - ALLOWED_ASYMMETRY
        missing_from_generator = (scan - gen) - ALLOWED_ASYMMETRY
        self.assertEqual(
            missing_from_scanner, set(),
            "scan_opportunities.py drops fields generate_signals.py records")
        self.assertEqual(
            missing_from_generator, set(),
            "generate_signals.py drops fields scan_opportunities.py records")

    def test_the_learning_critical_fields_are_in_both(self):
        """Whatever else drifts, these are what the learning loop reads.
        A signal without them produces an outcome that teaches nothing."""
        for path in (self.GENERATOR, self.SCANNER):
            got = self._biggest(path)
            for field in ("strategy", "strategy_score", "composite_score",
                          "calibrated_confidence", "timeframe", "score_breakdown"):
                self.assertIn(field, got, f"{path.name} is missing {field}")

    def test_there_are_still_only_two_writers(self):
        """A third producer would need the same audit. This fails loudly
        rather than letting one appear with a partial field list."""
        writers = [p.relative_to(ROOT).as_posix()
                   for p in (ROOT / "jobs").rglob("*.py")
                   if _constructor_kwargs(p, "TradingSignal")]
        self.assertEqual(sorted(writers),
                         ["jobs/generate_signals.py", "jobs/scan_opportunities.py"])


if __name__ == "__main__":
    unittest.main()
