"""The release gate must not be able to test a half-installed machine.

The CI install step was three bare pip lines. On `windows-latest` that
block runs under pwsh, where a failing NATIVE command does not stop the
script — only the LAST command's exit code decides the step:

    pip install -r requirements.txt   FAILED   (order_book needs the MSVC
                                                patch; vendor/patches)
    pip install pytest                SUCCEEDED
    -> the step reported SUCCESS

and pytest then ran against an environment that had never finished being
built. A gate that reports on a machine in that state is not a gate.

These tests pin the shape of the fix, not the network behaviour: the
bootstrap must be fail-closed, CI must use it, and Windows must keep
cryptofeed rather than being excused from the platform it deploys on.
"""
import ast
import pathlib
import unittest

SCRIPT = pathlib.Path("scripts/install_dependencies.py")
WORKFLOW = pathlib.Path(".github/workflows/ci.yml")
PATCH = pathlib.Path("vendor/patches/order_book-1.0.1-msvc.patch")


class TheBootstrapIsFailClosed(unittest.TestCase):
    def setUp(self):
        self.src = SCRIPT.read_text(encoding="utf-8")
        self.tree = ast.parse(self.src)

    def test_it_exists_and_parses(self):
        self.assertTrue(SCRIPT.exists())

    def test_every_command_raises_on_a_non_zero_exit(self):
        """`run()` is the single choke point. If it ever learns to warn
        and continue, the original bug is back."""
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "run")
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
        self.assertTrue(raises, "run() must raise on failure")

    def test_it_never_uses_check_false_or_swallows_returncodes(self):
        self.assertNotIn("check=False", self.src)
        self.assertNotIn("|| true", self.src)

    def test_a_failure_exits_non_zero(self):
        self.assertIn("return 1", self.src)

    def test_it_verifies_the_exact_patched_version(self):
        """A patch applied to a different version is not a fix; it is an
        unreviewed source edit."""
        self.assertIn("order-book==1.0.1", self.src)
        self.assertIn("order_book-1.0.1", self.src)

    def test_it_smoke_imports_rather_than_trusting_pip(self):
        """An install that 'succeeded' while leaving an unimportable C
        extension is exactly the failure being guarded against."""
        self.assertIn("SMOKE_IMPORTS", self.src)
        for mod in ("order_book", "cryptofeed"):
            self.assertIn(mod, self.src)

    def test_it_runs_pip_check(self):
        self.assertIn("check", self.src)


class TheReleaseGateUsesIt(unittest.TestCase):
    def setUp(self):
        self.yml = WORKFLOW.read_text(encoding="utf-8")
        # COMMENTS ARE NOT BEHAVIOUR. This file explains the old broken
        # sequence in prose, and a naive substring search matches the
        # explanation and reports the bug as still present. Assert against
        # executable YAML only.
        self.steps = "\n".join(
            line for line in self.yml.splitlines()
            if not line.lstrip().startswith("#"))

    def test_ci_calls_the_bootstrap(self):
        self.assertIn("scripts/install_dependencies.py", self.steps)

    def test_ci_no_longer_installs_requirements_by_bare_pip(self):
        """The bare sequence is what pwsh let fail silently."""
        self.assertNotIn("pip install -r requirements.txt", self.steps)

    def test_no_job_builds_its_environment_a_second_way(self):
        """Two ways to build an environment is two environments, and the
        one that only appears in CI is the one nobody debugs."""
        installs = [l for l in self.steps.splitlines()
                    if "pip install" in l and "pip-audit" not in l]
        self.assertEqual(installs, [], f"bare pip installs remain: {installs}")

    def test_windows_is_still_in_the_matrix(self):
        """Windows is the deployment platform. Dropping it to get green
        would make the gate green about a machine nobody ships on."""
        self.assertIn("windows-latest", self.yml)

    def test_cryptofeed_was_not_removed_to_get_green(self):
        reqs = pathlib.Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("cryptofeed", reqs)


class ThePatchIsStillPresent(unittest.TestCase):
    def test_the_patch_file_exists(self):
        self.assertTrue(PATCH.exists(),
                        "the Windows build depends on this patch")

    def test_the_procedure_is_documented(self):
        readme = pathlib.Path("vendor/patches/README.md")
        self.assertTrue(readme.exists())
        self.assertIn("order_book", readme.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
