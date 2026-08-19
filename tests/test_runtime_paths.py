"""Active state must sit on Linux-native storage.

One machine hosts Windows and Linux; that does not give JARVIS two runtimes.
SQLite's correctness rests on filesystem semantics — advisory locking, fsync,
atomic rename — and through WSL's 9p/drvfs translation to NTFS those are
weaker. The failure mode is not a clean error but a corrupted book, or a
lock that silently does not lock.

The check is on the FILESYSTEM, never a string prefix, because the case that
matters is the one a prefix check misses: a symlink at
`~/jarvis-trading/data` pointing into `/mnt/c`.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import runtime_paths as RP

LINUX_ONLY = unittest.skipUnless(
    os.path.exists(RP.MOUNTINFO),
    "filesystem type is only knowable where /proc/self/mountinfo exists")


class TheVerdictComesFromTheMountTests(unittest.TestCase):

    @LINUX_ONLY
    def test_the_repository_itself_is_linux_native(self):
        v = RP.inspect(Path(__file__).resolve().parent.parent)
        self.assertTrue(v.knowable, "could not read the mount table")
        self.assertFalse(v.windows_backed,
                         f"the repo sits on {v.fstype!r} at {v.resolved!r}")

    @LINUX_ONLY
    def test_a_temp_path_is_acceptable(self):
        """Hermetic tests must keep working — /tmp is Linux-native."""
        with tempfile.TemporaryDirectory() as tmp:
            v = RP.inspect(Path(tmp) / "hermetic.db")
            self.assertFalse(v.windows_backed)
            RP.assert_linux_native_runtime_path(
                Path(tmp) / "hermetic.db", purpose="a test database")

    def test_a_path_that_does_not_exist_yet_is_still_judged(self):
        """A database is judged before it is created, by the directory that
        will hold it."""
        with tempfile.TemporaryDirectory() as tmp:
            v = RP.inspect(Path(tmp) / "not" / "created" / "yet.db")
            # Compare on the unique temp-directory name rather than a full
            # prefix: Windows `resolve()` expands 8.3 short names, so the
            # two spellings of the same directory need not match textually.
            self.assertIn(Path(tmp).name, RP._posix(v.resolved),
                          f"{v.resolved} is not under {tmp}")
            self.assertTrue(RP._posix(v.resolved).endswith(
                "not/created/yet.db"))


class WindowsBackedStorageIsRefusedTests(unittest.TestCase):
    """The mount table is substituted so the refusal is provable on any
    platform, including CI."""

    def _with_mounts(self, mounts):
        return patch.object(RP, "_mounts", lambda: sorted(
            mounts, key=lambda kv: len(kv[0]), reverse=True))

    def test_a_drvfs_mount_is_refused(self):
        with self._with_mounts([("/", "ext4"), ("/mnt/c", "drvfs")]):
            with self.assertRaises(RuntimeError) as caught:
                RP.assert_linux_native_runtime_path(
                    "/mnt/c/jarvis-trading/data/jarvis.db",
                    purpose="the canonical economic database")
            msg = str(caught.exception)
        self.assertIn(RP.WINDOWS_BACKED_PERSISTENCE_FORBIDDEN, msg)
        self.assertIn("drvfs", msg)
        self.assertIn("canonical economic database", msg)

    def test_a_9p_mount_is_refused(self):
        with self._with_mounts([("/", "ext4"), ("/mnt/d", "9p")]):
            with self.assertRaises(RuntimeError):
                RP.assert_linux_native_runtime_path(
                    "/mnt/d/evidence.db", purpose="evidence")

    def test_an_ntfs_or_cifs_mount_is_refused(self):
        for fstype in ("ntfs", "ntfs3", "fuseblk", "cifs"):
            with self.subTest(fstype=fstype):
                with self._with_mounts([("/", "ext4"), ("/mnt/x", fstype)]):
                    with self.assertRaises(RuntimeError):
                        RP.assert_linux_native_runtime_path(
                            "/mnt/x/jarvis.db", purpose="state")

    def test_ext4_is_accepted(self):
        with self._with_mounts([("/", "ext4")]):
            v = RP.assert_linux_native_runtime_path(
                "/home/someone/jarvis-trading/data/jarvis.db", purpose="db")
        self.assertEqual(v.fstype, "ext4")
        self.assertTrue(v.safe)

    def test_the_longest_mount_wins(self):
        """`/` is ext4 and `/mnt/c` is drvfs; a path under /mnt/c must be
        judged by /mnt/c, not by the root mount that also matches."""
        with self._with_mounts([("/", "ext4"), ("/mnt/c", "drvfs")]):
            v = RP.inspect("/mnt/c/anything/at/all.db")
        self.assertEqual(v.fstype, "drvfs")
        self.assertTrue(v.windows_backed)


class ASymlinkCannotHideTheStorageTests(unittest.TestCase):
    """The case a string-prefix check misses entirely."""

    @LINUX_ONLY
    def test_a_symlink_is_resolved_before_judging(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real_storage"
            real.mkdir()
            link = Path(tmp) / "innocent_looking_data"
            link.symlink_to(real)
            v = RP.inspect(link / "jarvis.db")
            self.assertTrue(v.resolved.startswith(str(real.resolve())),
                            f"the symlink was not resolved: {v.resolved}")

    @LINUX_ONLY
    def test_a_symlink_into_windows_storage_is_still_refused(self):
        """The whole point, with a REAL symlink rather than a patched one:
        the path looks Linux-native, the storage underneath is not. A
        string-prefix check on "/mnt/c" passes this case happily."""
        with tempfile.TemporaryDirectory() as tmp:
            pretend_windows = Path(tmp) / "pretend_mnt_c"
            pretend_windows.mkdir()
            link = Path(tmp) / "looks_linux_native"
            link.symlink_to(pretend_windows)

            # Declare the link's TARGET drvfs, exactly as /mnt/c is.
            mounts = [(str(pretend_windows.resolve()), "drvfs"),
                      (tmp, "ext4"), ("/", "ext4")]
            with patch.object(RP, "_mounts", lambda: sorted(
                    mounts, key=lambda kv: len(kv[0]), reverse=True)):
                v = RP.inspect(link / "jarvis.db")
                self.assertEqual(v.fstype, "drvfs",
                                 f"the symlink hid the storage: {v}")
                with self.assertRaises(RuntimeError) as caught:
                    RP.assert_linux_native_runtime_path(
                        link / "jarvis.db", purpose="the economic database")
        self.assertIn("drvfs", str(caught.exception))


class UnknowableIsNotUnsafeTests(unittest.TestCase):
    """On a platform that cannot see the mount table the guard declines to
    refuse, because refusing would assert something it does not know."""

    def test_no_mount_table_means_no_refusal(self):
        with patch.object(RP, "_mounts", lambda: []):
            v = RP.assert_linux_native_runtime_path(
                "C:/whatever/jarvis.db", purpose="a Windows test run")
        self.assertFalse(v.knowable)
        self.assertTrue(v.safe)


class TheRuntimeReportIsHonestTests(unittest.TestCase):

    def test_it_names_the_runtime_and_every_store(self):
        r = RP.runtime_report()
        for key in ("os", "python", "venv", "repo_root", "stores",
                    "canonical_runtime_linux", "active_db_windows_backed"):
            self.assertIn(key, r)
        self.assertIn("database", r["stores"])

    @LINUX_ONLY
    def test_on_linux_no_active_store_is_windows_backed(self):
        r = RP.runtime_report()
        self.assertFalse(r["active_db_windows_backed"],
                         f"unsafe stores: {r['unsafe_stores']}")


if __name__ == "__main__":
    unittest.main()
