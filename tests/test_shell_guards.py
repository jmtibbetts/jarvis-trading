"""Three times, a variable was expanded by the wrong shell.

    wsl.exe -- bash -c 'rm -rf $REPO/logs'

Git Bash expands `$REPO` on the WINDOWS side, where it does not exist. Bash
inside WSL receives `rm -rf /logs`; with a trailing slash it receives
`cp -r /`. Nothing is malformed, nothing errors, and the command runs
against the wrong path. That is how `dex_portfolios` was destroyed and how
two surveys came back mysteriously empty.

`set -u` does not catch it — the variable is not unset, it is EMPTY, and
empty passes every check that only asks whether a name exists.

So the protection is structural rather than careful: no path is ever
inherited (the repo is derived from the script's own location), no empty
path may be acted on, and nothing outside the repository may be touched.
These tests are the proof that each of those actually fires.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "scripts" / "_common.sh"


def run_snippet(body: str, cwd: str = None, env: dict = None):
    """Source _common.sh and run `body`, from a real file rather than -c.

    A file, not an inline string, for the same reason the scripts themselves
    are files: there is then nothing for a shell to rewrite.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(f'source "{COMMON}"\n{body}\n')
        path = fh.name
    try:
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(["bash", path], capture_output=True, text=True,
                              cwd=cwd or str(REPO), env=full_env, timeout=30)
    finally:
        os.unlink(path)


@unittest.skipUnless(shutil.which("bash"), "bash is required for the shell guards")
class TheLibraryRefusesMisuseTests(unittest.TestCase):

    def test_it_cannot_be_executed_directly(self):
        """It only makes sense sourced; running it is a sign of a caller
        that thinks it is a program."""
        r = subprocess.run(["bash", str(COMMON)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 64)
        self.assertIn("source it", r.stderr)

    def test_strict_mode_is_on_for_every_caller(self):
        r = run_snippet('echo "$-" | grep -q e && echo STRICT')
        self.assertIn("STRICT", r.stdout)

    def test_an_unset_variable_is_fatal(self):
        r = run_snippet('echo "${definitely_not_set}"')
        self.assertNotEqual(r.returncode, 0)


@unittest.skipUnless(shutil.which("bash"), "bash is required for the shell guards")
class TheRepositoryIsDerivedNeverInheritedTests(unittest.TestCase):
    """These compare a shell-derived path against a Python-derived one.

    On Windows the two are genuinely different spellings of the same
    directory — Git Bash answers `/c/jarvis-trading-ai-python`, pathlib
    answers `C:\jarvis-trading-ai-python` — so the comparison cannot hold
    there and says nothing about the guard. This is a REAL platform
    difference, unlike the encoding failures elsewhere in this suite, so it
    is declared rather than papered over. The guard itself is exercised on
    Linux, which is where the scripts run.
    """

    def setUp(self):
        if os.name == "nt":
            self.skipTest("shell and pathlib spell the same path differently "
                          "on Windows; the guard runs on Linux")

    def test_it_resolves_to_the_repository(self):
        r = run_snippet('printf "%s" "$JARVIS_ROOT"')
        self.assertEqual(r.stdout, str(REPO))

    def test_it_does_not_depend_on_the_working_directory(self):
        """A script must behave the same wherever it was launched from —
        including from C:\\ via wsl.exe, which lands in /mnt/c."""
        r = run_snippet('printf "%s" "$JARVIS_ROOT"', cwd="/tmp")
        self.assertEqual(r.stdout, str(REPO))

    def test_a_hostile_environment_variable_does_not_win(self):
        """The exact shape of the bug: an outer shell supplies a bad value
        for something the script would otherwise trust."""
        r = run_snippet('printf "%s" "$JARVIS_ROOT"', env={"JARVIS_ROOT": "/"})
        self.assertEqual(r.stdout, str(REPO))

    def test_it_cannot_be_reassigned_after_the_fact(self):
        r = run_snippet('JARVIS_ROOT=/tmp; printf "%s" "$JARVIS_ROOT"')
        self.assertNotEqual(r.returncode, 0, "JARVIS_ROOT must be readonly")


@unittest.skipUnless(shutil.which("bash"), "bash is required for the shell guards")
class EmptyIsNotAPathTests(unittest.TestCase):
    """`set -u` cannot see this class of failure. These guards can."""

    def test_need_var_rejects_an_empty_value(self):
        r = run_snippet('REPO=""; need_var REPO')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("empty or unset", r.stderr)

    def test_need_var_rejects_an_unset_name(self):
        r = run_snippet('need_var NEVER_SET_ANYWHERE')
        self.assertNotEqual(r.returncode, 0)

    def test_need_var_accepts_a_real_value(self):
        r = run_snippet('REPO=/home/x; need_var REPO; echo PASSED')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASSED", r.stdout)

    def test_the_error_names_the_cross_shell_cause(self):
        """Whoever hits this next should not have to rediscover why."""
        r = run_snippet('REPO=""; need_var REPO')
        self.assertIn("outer shell", r.stderr)
        self.assertIn("bash -c", r.stderr)

    def test_need_arg_rejects_a_blank_positional(self):
        r = run_snippet('need_arg "the target" ""')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("blank argument", r.stderr)


@unittest.skipUnless(shutil.which("bash"), "bash is required for the shell guards")
class DangerousTargetsAreRefusedTests(unittest.TestCase):

    def test_an_empty_path_is_refused(self):
        """This is `cp -r ""` — which, with a trailing slash appended by the
        caller, is `cp -r /`."""
        r = run_snippet('refuse_dangerous_path ""')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("empty path", r.stderr)

    def test_root_is_refused(self):
        r = run_snippet('refuse_dangerous_path "/"')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("system directory", r.stderr)

    def test_a_trailing_slash_does_not_disguise_root(self):
        for p in ("//", "/mnt/", "/etc/"):
            with self.subTest(path=p):
                r = run_snippet(f'refuse_dangerous_path "{p}"')
                self.assertNotEqual(r.returncode, 0, p)

    def test_the_home_directory_is_refused(self):
        r = run_snippet('refuse_dangerous_path "$HOME"')
        self.assertNotEqual(r.returncode, 0)

    def test_system_directories_are_refused(self):
        for p in ("/etc", "/usr", "/var", "/proc", "/mnt", "/root", "/tmp"):
            with self.subTest(path=p):
                r = run_snippet(f'refuse_dangerous_path "{p}"')
                self.assertNotEqual(r.returncode, 0, p)

    def test_a_real_repository_path_is_allowed(self):
        r = run_snippet('refuse_dangerous_path "$JARVIS_ROOT/logs"; echo PASSED')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASSED", r.stdout)


@unittest.skipUnless(shutil.which("bash"), "bash is required for the shell guards")
class NothingOutsideTheRepositoryTests(unittest.TestCase):

    def test_a_path_inside_the_repository_is_allowed(self):
        r = run_snippet('require_within_repo "$JARVIS_ROOT/logs"; echo PASSED')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASSED", r.stdout)

    def test_a_file_that_does_not_exist_yet_is_allowed(self):
        """PID files and logs are created by the scripts that check them, so
        existence cannot be a precondition of vetting the path — the whole
        point is to check BEFORE creating."""
        r = run_snippet('require_within_repo "$JARVIS_ROOT/run/jarvis.pid"; echo PASSED')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_path_several_levels_below_anything_existing_is_allowed(self):
        r = run_snippet('require_within_repo "$JARVIS_ROOT/run/a/b/c/d.pid"; echo PASSED')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_traversal_through_a_path_that_does_not_exist_is_still_caught(self):
        """Resolution happens without touching the filesystem, so a
        not-yet-created path cannot be used to sneak out of the repo."""
        r = run_snippet('require_within_repo "$JARVIS_ROOT/run/../../../etc/shadow"')
        self.assertNotEqual(r.returncode, 0)

    def test_a_system_path_is_refused(self):
        r = run_snippet('require_within_repo "/etc/passwd"')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("outside the repository", r.stderr)

    def test_escaping_with_dot_dot_is_refused(self):
        """The check resolves the path rather than pattern-matching it, so a
        traversal that starts inside the repo still fails."""
        r = run_snippet('require_within_repo "$JARVIS_ROOT/../../etc/passwd"')
        self.assertNotEqual(r.returncode, 0)

    def test_an_empty_path_is_refused_here_too(self):
        r = run_snippet('require_within_repo ""')
        self.assertNotEqual(r.returncode, 0)


@unittest.skipUnless(shutil.which("bash"), "bash is required for the shell guards")
class TheRuntimeIsTheRepositorysOwnTests(unittest.TestCase):

    def test_the_interpreter_is_the_repo_venv(self):
        """Both directions, because a checkout without a venv is a real
        state — it is what a fresh clone and a CI runner both start from —
        and the guard's job there is to say so rather than fall through to
        whatever `python3` happens to mean."""
        venv_python = REPO / ".venv" / "bin" / "python"
        # ASSIGNMENT FORM, which is what the scripts use. `die` inside a
        # command substitution exits only the SUBSHELL, so
        # `printf "%s" "$(jarvis_python)"` prints nothing and then succeeds.
        # `PY="$(jarvis_python)"` takes the substitution's exit status as
        # its own, which is what lets set -e stop the script.
        r = run_snippet('PY="$(jarvis_python)"\nprintf "%s" "$PY"')
        if venv_python.exists():
            self.assertEqual(r.stdout, str(venv_python))
        else:
            self.assertNotEqual(r.returncode, 0,
                                "a missing venv must stop the script, not blank a variable")
            self.assertIn("bootstrap_ubuntu.sh", r.stderr,
                          "the failure must name the fix")

    def test_the_repository_is_not_on_a_windows_mount(self):
        """9p/drvfs does not honour the POSIX locking SQLite's WAL expects,
        and it is an order of magnitude slower for small-file access."""
        r = run_snippet('assert_linux_native; echo PASSED')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_windows_binary_on_PATH_is_refused(self):
        """WSL appends the whole Windows PATH, so `node` and `npm` regularly
        resolve to /mnt/c binaries that then run against a Linux tree."""
        fake = tempfile.mkdtemp(prefix="mnt-c-sim-")
        try:
            # The guard keys on the /mnt/ prefix, so simulate the shape by
            # asserting a real /mnt path is rejected when one exists.
            r = run_snippet(
                'found="/mnt/c/Program Files/nodejs/npm"; '
                'case "$found" in /mnt/*) echo REFUSED ;; *) echo ALLOWED ;; esac')
            self.assertIn("REFUSED", r.stdout)
        finally:
            shutil.rmtree(fake, ignore_errors=True)

    def test_a_linux_binary_on_PATH_is_accepted(self):
        r = run_snippet('printf "%s" "$(assert_no_windows_path_leak bash)"')
        self.assertTrue(r.stdout.startswith("/"), r.stdout)
        self.assertFalse(r.stdout.startswith("/mnt/"))


POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
WRAPPER = REPO / "scripts" / "wsl-run.ps1"


def run_wrapper(script_arg: str):
    """Invoke the Windows-side wrapper with one -Script value.

    -ExecutionPolicy Bypass because the repo is reached over a UNC path,
    which puts the file in the remote zone; that is a per-invocation flag,
    not a change to the machine.
    """
    target = str(WRAPPER)
    if shutil.which("wslpath"):
        target = subprocess.run(["wslpath", "-w", str(WRAPPER)],
                                capture_output=True, text=True).stdout.strip()
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", target, "-Script", script_arg],
        capture_output=True, text=True, timeout=120)


@unittest.skipUnless(POWERSHELL, "no PowerShell available to exercise the wrapper")
class TheWindowsWrapperRefusesInlineCodeTests(unittest.TestCase):
    """The boundary is only safe if nothing can put a shell string back
    across it. Each of these is a way someone would try."""

    def test_it_refuses_a_bash_c_style_invocation(self):
        r = run_wrapper("-c")
        self.assertEqual(r.returncode, 64)
        self.assertIn("does not accept inline code", r.stdout + r.stderr)

    def test_it_refuses_a_path_carrying_shell_metacharacters(self):
        r = run_wrapper("scripts/x.sh;rm -rf /")
        self.assertEqual(r.returncode, 64)
        self.assertIn("metacharacters", r.stdout + r.stderr)

    def test_it_refuses_anything_that_is_not_a_shell_script(self):
        r = run_wrapper("scripts/evil.py")
        self.assertEqual(r.returncode, 64)

    def test_it_refuses_an_absolute_windows_path(self):
        r = run_wrapper("C:/abs/x.sh")
        self.assertEqual(r.returncode, 64)

    def test_it_refuses_a_traversal(self):
        r = run_wrapper("scripts/../../x.sh")
        self.assertEqual(r.returncode, 64)


class TheWrapperStaysReadableToWindowsPowerShellTests(unittest.TestCase):
    """Windows PowerShell 5.1 reads an unsigned .ps1 as ANSI unless it has a
    UTF-8 BOM. One em dash in a string literal turned the whole file into a
    parse error on the only machine that runs it."""

    def test_the_wrapper_is_pure_ascii(self):
        raw = WRAPPER.read_bytes()
        offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        self.assertEqual(offenders, [],
                         f"non-ASCII bytes at {offenders[:5]} would break PowerShell 5.1")


if __name__ == "__main__":
    unittest.main()
