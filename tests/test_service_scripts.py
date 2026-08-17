"""`pkill python` would have taken down the operator's whole desk.

This machine runs Claude Code, a hermes agent, LM Studio's runtime and any
number of pytest workers — all Python, several with "main" somewhere in
their command line. A stop script that pattern-matches the process table is
one bad day away from killing the tools you were using to debug it, and the
blast radius is only visible afterwards.

So a JARVIS process is identified by a conjunction that nothing else on the
box can satisfy at once: argv[0] resolves to THIS repository's venv
interpreter, argv[1] is main.py, and the process cwd is THIS repository.
The third clause is what distinguishes two checkouts, which a command-line
match never could.

These tests spawn deliberate near-misses — every decoy satisfies two of the
three conditions — and prove each one is left alone.

The other property pinned here is the scheduler default. main.py starts
APScheduler unless JARVIS_DISABLE_SCHEDULER=1, and APScheduler begins
EXECUTING SIGNALS at T+3m. A start script that inherited that default turns
"let me look at the dashboard" into live activity. It is asserted through
the script's own --dry-run rather than by grepping the source, because
searching source for a guard reliably matches the comment describing the
guard that used to be there — that mistake has cost this project time three
separate times.
"""
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
COMMON = SCRIPTS / "_common.sh"
VENV_PY = REPO / ".venv" / "bin" / "python"

LINUX_ONLY = unittest.skipUnless(
    os.path.isdir("/proc") and shutil.which("bash"),
    "process identity is read from /proc, which needs Linux and bash")


def matched_pids():
    """Whatever scripts/_common.sh currently considers a JARVIS server."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(f'source "{COMMON}"\njarvis_pids\n')
        path = fh.name
    try:
        r = subprocess.run(["bash", path], capture_output=True, text=True,
                           cwd=str(REPO), timeout=60)
        return {int(x) for x in r.stdout.split()}
    finally:
        os.unlink(path)


class Decoy:
    """A process that satisfies some, but never all, of the identity rules."""

    def __init__(self, argv, cwd):
        self.proc = subprocess.Popen(argv, cwd=cwd,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        # /proc/<pid>/cmdline is populated by the time exec has happened;
        # give it a moment so the assertion is not racing the fork.
        time.sleep(0.4)

    @property
    def pid(self):
        return self.proc.pid

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@LINUX_ONLY
class NearMissesAreLeftAloneTests(unittest.TestCase):
    """Each decoy is two-thirds of a JARVIS process. None may be matched."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="jarvis-decoy-")
        # A file genuinely named main.py, so the argv[1] clause is satisfied
        # honestly rather than by a near-name.
        cls.fake_main = Path(cls.tmp) / "main.py"
        cls.fake_main.write_text(textwrap.dedent("""
            import time
            time.sleep(600)
        """))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def assert_not_matched(self, decoy, why):
        self.addCleanup(decoy.stop)
        self.assertNotIn(decoy.pid, matched_pids(), why)

    def test_the_right_interpreter_running_the_wrong_script(self):
        """Our venv python, our cwd — but it is not the server."""
        d = Decoy([str(VENV_PY), "-c", "import time; time.sleep(600)"], cwd=str(REPO))
        self.assert_not_matched(d, "a -c invocation is not the JARVIS server")

    def test_the_wrong_interpreter_running_a_file_called_main_py(self):
        """The closest decoy there is: system python, cwd inside the repo,
        argv[1] literally main.py. Only the interpreter differs."""
        d = Decoy(["/usr/bin/python3", str(self.fake_main)], cwd=str(REPO))
        self.assert_not_matched(d, "a non-venv interpreter is not our server")

    def test_the_right_interpreter_and_main_py_but_the_wrong_directory(self):
        """This is what separates two checkouts of JARVIS from each other."""
        d = Decoy([str(VENV_PY), str(self.fake_main)], cwd=self.tmp)
        self.assert_not_matched(d, "a process in another directory is another instance")

    def test_a_plain_sleeping_python(self):
        d = Decoy(["/usr/bin/python3", "-c", "import time; time.sleep(600)"],
                  cwd=str(REPO))
        self.assert_not_matched(d, "an unrelated python must never be a target")


@LINUX_ONLY
class AGenuineMatchIsFoundTests(unittest.TestCase):
    """A predicate that never matches anything would pass every test above
    and be useless. This is the other half."""

    def test_all_three_conditions_together_are_matched(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-match-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        main = Path(tmp) / "main.py"
        main.write_text("import time\ntime.sleep(600)\n")
        # Venv interpreter + a file named main.py + cwd of this repository.
        # Deliberately NOT the real main.py: proving the predicate must not
        # require starting a second server against the operator's database.
        d = Decoy([str(VENV_PY), str(main)], cwd=str(REPO))
        self.addCleanup(d.stop)
        self.assertIn(d.pid, matched_pids())


@LINUX_ONLY
class TheStopScriptCannotBeTalkedIntoAPatternMatchTests(unittest.TestCase):

    def test_it_never_invokes_pkill_or_killall(self):
        """Not a comment check — these commands appear nowhere as code, and
        the prose that mentions them is stripped before searching."""
        src = (SCRIPTS / "stop_jarvis.sh").read_text().splitlines()
        code = [ln.split("#", 1)[0] for ln in src]
        for forbidden in ("pkill", "killall", "pgrep"):
            hits = [ln for ln in code if forbidden in ln]
            self.assertEqual(hits, [], f"{forbidden} must not appear in executable code")

    def test_it_reverifies_identity_immediately_before_signalling(self):
        """A process that exits between the check and the kill frees its PID
        for somebody else, so the check cannot be hoisted out of the loop."""
        src = (SCRIPTS / "stop_jarvis.sh").read_text()
        self.assertIn("signal_if_ours", src)
        code = [ln.split("#", 1)[0] for ln in src.splitlines()]
        kills = [ln for ln in code if "kill " in ln or "kill -" in ln]
        for ln in kills:
            self.assertIn("signal_if_ours", src.split(ln)[0][-400:] + ln,
                          f"a kill outside signal_if_ours: {ln.strip()}")


@LINUX_ONLY
class TheSchedulerIsNeverImplicitTests(unittest.TestCase):
    """Asserted through the script's own dry run, not by reading its source."""

    def dry_run(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPTS / "start_jarvis.sh"), "--dry-run", *args],
            capture_output=True, text=True, cwd=str(REPO), timeout=180)

    def test_the_default_start_disables_it(self):
        r = self.dry_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scheduler DISABLED", r.stdout)

    def test_enabling_it_requires_the_explicit_flag(self):
        r = self.dry_run("--with-scheduler")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scheduler ENABLED", r.stdout + r.stderr)

    def test_enabling_it_says_what_that_actually_means(self):
        """"Scheduler on" understates it: signals begin EXECUTING at T+3m."""
        r = self.dry_run("--with-scheduler")
        self.assertIn("EXECUTION", r.stdout + r.stderr)

    def test_an_unknown_flag_is_refused_rather_than_ignored(self):
        """A typo'd --with-scheduler must not silently start without it."""
        r = subprocess.run(
            ["bash", str(SCRIPTS / "start_jarvis.sh"), "--dry-run", "--with-schedular"],
            capture_output=True, text=True, cwd=str(REPO), timeout=180)
        self.assertNotEqual(r.returncode, 0)

    def test_a_dry_run_creates_nothing(self):
        run_dir = REPO / "run"
        existed = run_dir.exists()
        self.dry_run()
        if not existed:
            self.assertFalse(run_dir.exists(), "a dry run must not create run/")


@LINUX_ONLY
class TheDoctorOnlyLooksTests(unittest.TestCase):

    def test_it_reports_without_failing(self):
        r = subprocess.run(["bash", str(SCRIPTS / "doctor.sh")],
                           capture_output=True, text=True, cwd=str(REPO), timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        for section in ("Machine", "Repository", "Python", "Databases", "LLM endpoint"):
            self.assertIn(section, r.stdout)

    def test_a_missing_npu_is_a_fact_not_a_warning(self):
        """The supported runtime enumerates ['CPU']. Warning about the NPU
        every single run teaches the reader to skip warnings."""
        r = subprocess.run(["bash", str(SCRIPTS / "doctor.sh")],
                           capture_output=True, text=True, cwd=str(REPO), timeout=300)
        npu_lines = [ln for ln in r.stdout.splitlines() if "npu" in ln.lower()]
        self.assertTrue(npu_lines, "the doctor should still mention the NPU")
        for ln in npu_lines:
            self.assertNotIn("!!", ln, f"NPU absence reported as a warning: {ln}")

    def test_it_opens_operator_databases_read_only(self):
        """A probe that opened the operator DB read-write destroyed
        dex_portfolios once. Every open here carries mode=ro."""
        import re
        code = [ln.split("#", 1)[0]
                for ln in (SCRIPTS / "doctor.sh").read_text().splitlines()]
        # An INVOCATION against a database, not a `command -v sqlite3` probe.
        opens = [ln for ln in code
                 if re.search(r"""sqlite3\s+["']?(file:|\$|/|\w+\.db)""", ln)]
        self.assertTrue(opens, "expected the doctor to inspect the databases")
        for ln in opens:
            self.assertIn("mode=ro", ln, f"a read-write DB open in the doctor: {ln.strip()}")


if __name__ == "__main__":
    unittest.main()
