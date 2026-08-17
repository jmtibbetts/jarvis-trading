"""JARVIS must start and trade with none of the ML stack installed.

Training is a batch activity on the RTX 5090. If torch, scikit-learn or
openvino ever become import-time requirements of the trading path, a
broken ML install stops the desk — which inverts the entire point of
keeping deterministic control separate from prediction.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ML_ONLY = {"torch", "sklearn", "openvino", "nncf"}

# Modules the trading system loads to place and manage orders.
CRITICAL = [
    "lib/expectancy.py", "lib/transaction_costs.py", "lib/strategy_lifecycle.py",
    "lib/calibration.py", "lib/strategies.py", "lib/regime_axes.py",
    "lib/signal_scorer.py", "lib/paper_engine.py", "lib/venues.py",
    "jobs/execute_signals.py", "jobs/manage_positions.py", "main.py",
]


def top_level_imports(path: pathlib.Path) -> set[str]:
    """Only imports at module scope. A guarded import inside a function is
    exactly the pattern that keeps an optional dependency optional."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


class TradingPathDoesNotImportTheMlStackTests(unittest.TestCase):

    def test_no_critical_module_imports_ml_at_top_level(self):
        offenders = []
        for rel in CRITICAL:
            p = ROOT / rel
            if not p.exists():
                continue
            bad = top_level_imports(p) & ML_ONLY
            if bad:
                offenders.append(f"{rel} imports {sorted(bad)}")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_the_ml_requirements_are_a_separate_file(self):
        self.assertTrue((ROOT / "requirements-ml.txt").exists())

    def test_the_runtime_requirements_do_not_pull_the_ml_stack(self):
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8", errors="replace").lower()
        for pkg in ("torch", "scikit-learn", "nncf"):
            self.assertNotIn(pkg, req, f"requirements.txt pulls {pkg}")

    def reachable_from(self, name: str, seen=None) -> str:
        """The REQUIREMENT LINES of a file and everything it `-r` includes.

        Two things this has to get right, both learned the hard way.

        Comments are stripped. These files explain themselves at length,
        and requirements-inference.txt's header names torch precisely to
        say it is NOT here — a search over raw text reads that as the
        opposite of what it says. Searching source for a thing and matching
        the prose about it has cost this project time four separate times.

        Includes are followed. The numpy pin used to sit in
        requirements-ml.txt and now sits in requirements-inference.txt,
        which that file includes. Reading one file would call the pin
        missing while it is still enforced — and would go green again the
        day somebody moved it without the include."""
        seen = seen if seen is not None else set()
        if name in seen:
            return ""
        seen.add(name)
        lines = []
        for raw in (ROOT / name).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("-r "):
                lines.append(self.reachable_from(line[3:].strip(), seen))
            else:
                lines.append(line)
        return "\n".join(lines)

    def test_numpy_is_pinned_in_the_ml_requirements(self):
        """Installing scipy unpinned upgraded numpy 1.26.4 -> 2.5.2, silently
        replacing the numerical foundation under fee and P&L arithmetic."""
        self.assertIn("numpy==1.26.4", self.reachable_from("requirements-ml.txt"))

    def test_the_pin_holds_for_an_inference_only_install_too(self):
        """CI installs the inference half alone. A pin that lived only in
        the training file would leave the gate running on a different numpy
        from the desk — the exact substitution this pin exists to stop."""
        self.assertIn("numpy==1.26.4", self.reachable_from("requirements-inference.txt"))

    def test_the_inference_half_needs_no_gpu_wheel_index(self):
        """torch==2.11.0+cu128 resolves only from the PyTorch CUDA index, so
        anything reachable from the inference file has to come from PyPI.
        When it did not, the ML extras silently failed to install in CI and
        11 predictive tests skipped inside a green check."""
        reachable = self.reachable_from("requirements-inference.txt")
        self.assertNotIn("torch", reachable)
        self.assertNotIn("+cu", reachable)

    def test_the_installed_numpy_matches_the_pin(self):
        import numpy
        self.assertEqual(numpy.__version__, "1.26.4")


if __name__ == "__main__":
    unittest.main()
