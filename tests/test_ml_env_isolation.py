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

    def test_the_research_requirements_are_a_separate_file(self):
        self.assertTrue((ROOT / "requirements-research.txt").exists())

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
        the training file and now sits in the core file,
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

    def test_numpy_is_pinned_exactly_once(self):
        """Installing scipy unpinned upgraded numpy 1.26.4 -> 2.5.2, silently
        replacing the numerical foundation under fee and P&L arithmetic.

        Pinned ONCE, in the core file. A second pin site is somewhere for
        the two to drift apart, and the drift is invisible until the day
        two environments disagree about scalar promotion."""
        sites = [f for f in ("requirements.txt", "requirements-inference.txt",
                             "requirements-research.txt", "requirements-cuda.txt")
                 if "numpy==" in (ROOT / f).read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(sites, ["requirements.txt"], f"numpy pinned in {sites}")

    def test_every_install_path_still_reaches_the_pin(self):
        """One pin site only helps if every entry point includes it.
        openvino alone constrains numpy to <2.6.0, so an inference install
        that missed the pin would resolve numpy 2.x quite happily."""
        for f in ("requirements.txt", "requirements-inference.txt",
                  "requirements-research.txt", "requirements-cuda.txt"):
            with self.subTest(requirements=f):
                self.assertIn("numpy==1.26.4", self.reachable_from(f))

    def test_no_normal_install_path_can_pull_cuda(self):
        """THE POINT OF THE SPLIT. JARVIS does not need CUDA: the RTX 5090
        is reached through LM Studio's HTTP API, not through this process.
        Only the file whose name says CUDA may mention a CUDA wheel."""
        for f in ("requirements.txt", "requirements-inference.txt",
                  "requirements-research.txt"):
            with self.subTest(requirements=f):
                reachable = self.reachable_from(f)
                self.assertNotIn("+cu", reachable, f"{f} reaches a CUDA wheel")
                self.assertNotIn("nvidia", reachable.lower())

    def test_the_runtime_never_needs_torch_at_all(self):
        """Neither CPU nor CUDA. Established by tracing imports: nothing
        under app/, jobs/ or lib/ imports torch, so it belongs to research
        and must not appear in the runtime or inference sets."""
        for f in ("requirements.txt", "requirements-inference.txt"):
            with self.subTest(requirements=f):
                self.assertNotIn("torch", self.reachable_from(f))

    def test_cpu_learning_needs_nothing_beyond_the_core_runtime(self):
        """The claim this whole split rests on: JARVIS learns from its
        trades with neither research nor CUDA installed. Asserted against
        the modules themselves rather than against a requirements file."""
        learning = ["lib/calibration.py", "lib/expectancy.py",
                    "lib/learning_engine.py", "lib/strategy_lifecycle.py",
                    "lib/venue_expectancy.py", "lib/wallet_lifecycle.py"]
        offenders = []
        for rel in learning:
            p = ROOT / rel
            if not p.exists():
                continue
            bad = top_level_imports(p) & ML_ONLY
            if bad:
                offenders.append(f"{rel} imports {sorted(bad)}")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_the_installed_numpy_matches_the_pin(self):
        import numpy
        self.assertEqual(numpy.__version__, "1.26.4")


if __name__ == "__main__":
    unittest.main()
