"""A launcher declares its posture; it never inherits one.

TWO VARIABLES THAT FAIL IN OPPOSITE DIRECTIONS.

    lib.platform_mode   unset -> VIRTUAL_ONLY   (fails CLOSED, safe)
    lib.runtime_mode    unset -> FULL_VIRTUAL   (fails OPEN, permits mutation)

The library defaults are kept exactly as they are and are NOT what these
tests cover. A fail-closed default protects against silence. It does not
protect against a conflicting value that something actually set, because
the default never runs when a value is present -- an exported
JARVIS_PLATFORM_MODE=LIVE_ENABLED in a shell, a unit file or a stale
profile sails straight past it.

So the launchers form a second, independent boundary in front of the
library, and these tests pin that boundary. The failure they prevent is the
one already observed on this machine: a collector running EVIDENCE_ONLY was
restarted with a different script and silently came back FULL_VIRTUAL, with
nothing in the environment or the log contradicting it.
"""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "scripts" / "_common.sh"
START = REPO / "scripts" / "start_jarvis.sh"
EVIDENCE = REPO / "scripts" / "run_evidence_collector.sh"


def _helper(platform_env, runtime_env, want_platform, want_runtime):
    """Run jarvis_establish_modes in a clean shell with a chosen inherited
    environment. Returns (returncode, combined output)."""
    script = f"""
      export JARVIS_ROOT={REPO}
      export PYTHON={REPO}/.venv/bin/python
      cd {REPO}
      source {COMMON}
      jarvis_establish_modes {want_platform} {want_runtime}
      echo "RESULT platform=$JARVIS_PLATFORM_MODE runtime=$JARVIS_RUNTIME_MODE"
    """
    # `env` requires every -u BEFORE any NAME=VALUE, so unsets are
    # collected first; interleaving them makes env treat -u as a command.
    unsets, assigns = [], []
    for name, value in (("JARVIS_PLATFORM_MODE", platform_env),
                        ("JARVIS_RUNTIME_MODE", runtime_env)):
        (assigns.append(f"{name}={value}") if value is not None
         else unsets.extend(["-u", name]))
    proc = subprocess.run(["env", *unsets, *assigns, "bash", "-c", script],
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


class TheSupportedPosturesAreEstablishedTests(unittest.TestCase):

    def test_the_normal_launcher_runs_virtual_only_and_full_virtual(self):
        rc, out = _helper(None, None, "VIRTUAL_ONLY", "FULL_VIRTUAL")
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT platform=VIRTUAL_ONLY runtime=FULL_VIRTUAL", out)

    def test_the_evidence_launcher_runs_virtual_only_and_evidence_only(self):
        rc, out = _helper(None, None, "VIRTUAL_ONLY", "EVIDENCE_ONLY")
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT platform=VIRTUAL_ONLY runtime=EVIDENCE_ONLY", out)

    def test_an_agreeing_explicit_value_is_accepted(self):
        rc, out = _helper("VIRTUAL_ONLY", "FULL_VIRTUAL",
                          "VIRTUAL_ONLY", "FULL_VIRTUAL")
        self.assertEqual(rc, 0, out)

    def test_the_library_confirms_the_declaration(self):
        """The check reads the mode back from the LIBRARY, not from the
        variable it just set -- otherwise it proves only that bash can
        assign a variable."""
        rc, out = _helper(None, None, "VIRTUAL_ONLY", "FULL_VIRTUAL")
        self.assertIn("confirmed by the library", out, out)


class AConflictingEnvironmentIsFatalTests(unittest.TestCase):
    """Each case is a posture someone could plausibly have exported."""

    def test_evidence_only_cannot_leak_into_the_normal_launcher(self):
        rc, out = _helper(None, "EVIDENCE_ONLY", "VIRTUAL_ONLY", "FULL_VIRTUAL")
        self.assertNotEqual(rc, 0, "an inherited EVIDENCE_ONLY was accepted")
        self.assertIn("Refusing to start", out)

    def test_full_virtual_cannot_leak_into_the_evidence_launcher(self):
        rc, out = _helper(None, "FULL_VIRTUAL", "VIRTUAL_ONLY", "EVIDENCE_ONLY")
        self.assertNotEqual(rc, 0, "an inherited FULL_VIRTUAL was accepted")
        self.assertIn("Refusing to start", out)

    def test_a_live_platform_mode_cannot_be_inherited(self):
        """THE ONE THAT MATTERS. Neither supported launcher may become a
        real-money platform through an environment variable."""
        for live in ("LIVE_ENABLED", "LIVE_LIMITED", "LIVE_SHADOW"):
            with self.subTest(mode=live):
                rc, out = _helper(live, None, "VIRTUAL_ONLY", "FULL_VIRTUAL")
                self.assertNotEqual(rc, 0, f"{live} was inherited silently")
                self.assertIn("Refusing to start", out)

    def test_an_invalid_platform_string_is_rejected(self):
        rc, out = _helper("NONSENSE", None, "VIRTUAL_ONLY", "FULL_VIRTUAL")
        self.assertNotEqual(rc, 0, "an unrecognised platform mode was accepted")

    def test_an_invalid_runtime_string_is_rejected(self):
        rc, out = _helper(None, "NONSENSE", "VIRTUAL_ONLY", "FULL_VIRTUAL")
        self.assertNotEqual(rc, 0, "an unrecognised runtime mode was accepted")


class BothLaunchersActuallyCallTheHelperTests(unittest.TestCase):
    """The helper is only a boundary if the entry points go through it."""

    def test_start_jarvis_declares_virtual_only_full_virtual(self):
        body = START.read_text()
        self.assertIn("jarvis_establish_modes VIRTUAL_ONLY FULL_VIRTUAL", body)

    def test_the_evidence_collector_declares_virtual_only_evidence_only(self):
        body = EVIDENCE.read_text()
        self.assertIn("jarvis_establish_modes VIRTUAL_ONLY EVIDENCE_ONLY", body)

    def test_neither_launcher_hard_codes_a_live_platform(self):
        for script in (START, EVIDENCE):
            body = script.read_text()
            for live in ("LIVE_ENABLED", "LIVE_LIMITED"):
                self.assertNotIn(f"JARVIS_PLATFORM_MODE={live}", body,
                                 f"{script.name} hard-codes {live}")


class EndToEndThroughTheRealLauncherTests(unittest.TestCase):
    """--dry-run exercises the real script, not a reconstruction of it."""

    def _run(self, env_pairs):
        unsets, assigns = [], []
        for name, value in env_pairs:
            (assigns.append(f"{name}={value}") if value is not None
             else unsets.extend(["-u", name]))
        return subprocess.run(["env", *unsets, *assigns,
                               "bash", str(START), "--dry-run"],
                              capture_output=True, text=True, cwd=str(REPO),
                              timeout=180)

    def test_a_clean_environment_starts_in_the_supported_posture(self):
        p = self._run([("JARVIS_PLATFORM_MODE", None),
                       ("JARVIS_RUNTIME_MODE", None)])
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("platform mode: VIRTUAL_ONLY", out)
        self.assertIn("runtime mode:  FULL_VIRTUAL", out)

    def test_a_conflicting_environment_refuses_to_start(self):
        p = self._run([("JARVIS_RUNTIME_MODE", "EVIDENCE_ONLY")])
        self.assertNotEqual(p.returncode, 0,
                            "start_jarvis.sh started as EVIDENCE_ONLY")


if __name__ == "__main__":
    unittest.main()
