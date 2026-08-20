"""SQLite instrumentation must observe the desk, never become its problem.

TWO FAILURE MODES THIS GUARDS AGAINST.

First, mislabelling. "database is locked" is SQLITE_BUSY, while "database
table is locked" is SQLITE_LOCKED -- the opposite of what the English
suggests. Folding disk-full, I/O or corruption errors into BUSY would be
worse than not measuring at all, because the standard response to BUSY is
to retry, and retrying corruption spreads it. Classification therefore
prefers `sqlite_errorname`, which is authoritative, and treats message text
as a fallback.

Second, the instrumentation becoming the incident. Everything is in-memory
and bounded; a metrics table would mean writing to SQLite in order to watch
SQLite, manufacturing exactly the contention it claims to detect. And every
hook is defensive, because a counter that can raise is a counter that can
take the desk down.

The contention test uses TEMPORARY databases only. Deliberately locking an
operator store to see what happens is not a test, it is an incident.
"""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lib import sqlite_observability as OBS


def _err(name, message):
    """A sqlite3.OperationalError carrying a real errorname where the
    running interpreter supports it."""
    exc = sqlite3.OperationalError(message)
    try:
        exc.sqlite_errorname = name          # 3.11+ exposes this
    except Exception:                                        # noqa: BLE001
        pass
    return exc


class ClassificationTests(unittest.TestCase):
    """B, C, D: each class recognised as itself."""

    def test_busy_is_busy(self):
        self.assertEqual(OBS.classify(_err("SQLITE_BUSY", "database is locked")),
                         OBS.BUSY)

    def test_locked_is_not_folded_into_busy(self):
        self.assertEqual(
            OBS.classify(_err("SQLITE_LOCKED", "database table is locked")),
            OBS.LOCKED)

    def test_a_non_lock_operational_error_is_not_labelled_busy(self):
        for name, msg, expected in (
                ("SQLITE_READONLY", "attempt to write a readonly database",
                 OBS.READ_ONLY),
                ("SQLITE_FULL", "database or disk is full", OBS.DISK_FULL),
                ("SQLITE_IOERR", "disk I/O error", OBS.IO_ERROR),
                ("SQLITE_CORRUPT", "database disk image is malformed",
                 OBS.CORRUPT),
                ("SQLITE_CONSTRAINT", "UNIQUE constraint failed",
                 OBS.CONSTRAINT)):
            with self.subTest(name=name):
                got = OBS.classify(_err(name, msg))
                self.assertEqual(got, expected)
                self.assertNotEqual(got, OBS.BUSY,
                                    f"{name} was mislabelled as lock contention")

    def test_extended_result_codes_map_to_their_base_class(self):
        self.assertEqual(
            OBS.classify(_err("SQLITE_IOERR_WRITE", "disk I/O error")),
            OBS.IO_ERROR)

    def test_message_fallback_when_no_errorname(self):
        plain = sqlite3.OperationalError("database is locked")
        try:
            del plain.sqlite_errorname
        except Exception:                                    # noqa: BLE001
            pass
        self.assertIn(OBS.classify(plain), (OBS.BUSY, OBS.LOCKED))

    def test_an_unrecognised_error_is_other_not_busy(self):
        self.assertEqual(
            OBS.classify(sqlite3.OperationalError("something new")), OBS.OTHER)


class CounterTests(unittest.TestCase):
    """A and F: writes counted, statistics bounded."""

    def setUp(self):
        OBS.reset("unit")

    def test_a_successful_write_increments_write_metrics(self):
        OBS.record_transaction("unit", duration_ms=12.0, wrote=True)
        snap = OBS.snapshot()["stores"]["unit"]
        self.assertEqual(snap["recent_write_count"], 1)
        self.assertEqual(snap["max_write_tx_ms"], 12.0)
        self.assertEqual(snap["write_failures"], 0)

    def test_reads_and_writes_are_counted_separately(self):
        OBS.record_transaction("unit", duration_ms=1.0, wrote=False)
        OBS.record_transaction("unit", duration_ms=2.0, wrote=True)
        snap = OBS.snapshot()["stores"]["unit"]
        self.assertEqual(snap["recent_read_count"], 1)
        self.assertEqual(snap["recent_write_count"], 1)

    def test_a_write_failure_is_recorded_with_a_timestamp(self):
        OBS.record_error("unit", _err("SQLITE_FULL", "database or disk is full"),
                         wrote=True)
        snap = OBS.snapshot()["stores"]["unit"]
        self.assertEqual(snap["write_failures"], 1)
        self.assertIsNotNone(snap["last_write_failure_at"])
        self.assertEqual(snap["errors_by_class"][OBS.DISK_FULL], 1)

    def test_statistics_stay_bounded(self):
        for i in range(OBS._WINDOW * 3):
            OBS.record_transaction("unit", duration_ms=float(i), wrote=True)
        with OBS._lock:
            self.assertLessEqual(len(OBS._stores["unit"]["write_tx_ms"]),
                                 OBS._WINDOW)
        snap = OBS.snapshot()["stores"]["unit"]
        # The COUNT keeps rising; only the sample window is capped.
        self.assertEqual(snap["recent_write_count"], OBS._WINDOW * 3)

    def test_busy_records_a_wait_and_a_timestamp(self):
        OBS.record_error("unit", _err("SQLITE_BUSY", "database is locked"),
                         duration_ms=45.0)
        snap = OBS.snapshot()["stores"]["unit"]
        self.assertEqual(snap["busy_count"], 1)
        self.assertIsNotNone(snap["last_busy_at"])
        self.assertEqual(snap["max_busy_wait_ms"], 45.0)


class UnknownIsNotZeroTests(unittest.TestCase):
    """G: an unmeasurable metric says so."""

    def setUp(self):
        OBS.reset("empty")

    def test_timings_with_no_samples_report_unknown(self):
        OBS.record_transaction("empty", duration_ms=1.0, wrote=False)
        snap = OBS.snapshot()["stores"]["empty"]
        self.assertEqual(snap["avg_write_tx_ms"], "UNKNOWN")
        self.assertEqual(snap["max_busy_wait_ms"], "UNKNOWN")

    def test_a_missing_file_reports_unknown_not_zero_bytes(self):
        snap = OBS.snapshot({"empty": "/nonexistent/path/to.db"})["stores"]["empty"]
        self.assertEqual(snap["journal_mode"], "UNKNOWN")

    def test_checkpoint_age_is_declared_unknown_not_faked(self):
        """Reading it requires PRAGMA wal_checkpoint, which checkpoints."""
        self.assertIn("UNKNOWN", OBS.snapshot()["checkpoint_age"])

    def test_coverage_is_stated_so_uncounted_callers_are_not_read_as_zero(self):
        self.assertIn("Raw sqlite3", OBS.snapshot()["coverage"])


class InstrumentationCannotBreakTheCallerTests(unittest.TestCase):
    """E: the counter must never take the desk down."""

    def test_recording_swallows_internal_failure(self):
        OBS.record_transaction("x", duration_ms=None, wrote=True)   # bad input
        OBS.record_error("x", object())                             # not an exc

    def test_snapshot_survives_a_corrupt_internal_state(self):
        OBS.reset("broken")
        with OBS._lock:
            OBS._stores["broken"]["write_tx_ms"] = None             # poison
        out = OBS.snapshot()
        self.assertIsInstance(out, dict)

    def test_the_session_boundary_still_works_if_the_module_is_missing(self):
        """get_db must behave identically when observability cannot import."""
        import builtins
        real_import = builtins.__import__

        def deny(name, *a, **k):
            if name == "lib.sqlite_observability":
                raise ImportError("simulated")
            return real_import(name, *a, **k)

        from app.database import PaperPortfolio, get_db
        builtins.__import__ = deny
        try:
            with get_db() as db:
                db.query(PaperPortfolio).first()
        finally:
            builtins.__import__ = real_import


class ContentionAgainstATemporaryDatabaseTests(unittest.TestCase):
    """Real contention, on a throwaway file. Never an operator store."""

    OPERATOR_STORES = ("jarvis.db", "forward_evidence.db", "events.db",
                       "ohlcv_cache.db")

    def test_the_fixture_is_not_an_operator_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "contention.db"
            for name in self.OPERATOR_STORES:
                self.assertNotIn(name, str(path))

    def test_a_held_write_lock_produces_a_classified_busy(self):
        OBS.reset("contention")
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "contention.db")
            setup = sqlite3.connect(path)
            setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            setup.commit()
            setup.close()

            holder = sqlite3.connect(path, isolation_level=None, timeout=0)
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO t (v) VALUES ('held')")
            try:
                blocked = sqlite3.connect(path, timeout=0)
                started = time.perf_counter()
                with self.assertRaises(sqlite3.OperationalError) as cm:
                    blocked.execute("BEGIN IMMEDIATE")
                    blocked.execute("INSERT INTO t (v) VALUES ('blocked')")
                    blocked.commit()
                waited = (time.perf_counter() - started) * 1000.0
                cls = OBS.record_error("contention", cm.exception,
                                       wrote=True, duration_ms=waited)
                self.assertIn(cls, (OBS.BUSY, OBS.LOCKED),
                              f"real contention was classified {cls}")
                blocked.close()
            finally:
                holder.rollback()
                holder.close()

        snap = OBS.snapshot()["stores"]["contention"]
        self.assertGreaterEqual(snap["busy_count"] + snap["locked_count"], 1)
        self.assertEqual(snap["write_failures"], 1)

    def test_writes_succeed_again_once_contention_clears(self):
        """Recovery matters as much as detection."""
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "recover.db")
            setup = sqlite3.connect(path)
            setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            setup.commit()
            setup.close()

            holder = sqlite3.connect(path, isolation_level=None, timeout=0)
            holder.execute("BEGIN IMMEDIATE")
            holder.rollback()                       # contention clears
            holder.close()

            after = sqlite3.connect(path, timeout=5)
            after.execute("INSERT INTO t DEFAULT VALUES")
            after.commit()
            rows = after.execute("SELECT count(*) FROM t").fetchone()[0]
            after.close()
        self.assertEqual(rows, 1)

    def test_the_wait_was_bounded(self):
        """timeout=0 must fail fast rather than hang; an unbounded wait is
        the failure mode instrumentation is supposed to reveal."""
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "bounded.db")
            setup = sqlite3.connect(path)
            setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            setup.commit()
            setup.close()

            holder = sqlite3.connect(path, isolation_level=None, timeout=0)
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO t DEFAULT VALUES")
            try:
                blocked = sqlite3.connect(path, timeout=0)
                started = time.perf_counter()
                try:
                    blocked.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    pass
                elapsed = time.perf_counter() - started
                blocked.close()
            finally:
                holder.rollback()
                holder.close()
        self.assertLess(elapsed, 5.0, "a zero-timeout wait was not bounded")


class TestsStayOffTheOperatorDatabaseTests(unittest.TestCase):
    """H."""

    def test_the_session_is_routed_to_a_temp_database(self):
        from app.database import DB_PATH
        operator = Path.home() / "jarvis-trading" / "data" / "jarvis.db"
        self.assertNotEqual(Path(DB_PATH).resolve(), operator.resolve(),
                            "the test session is bound to the operator DB")

    def test_the_pytest_guard_is_active(self):
        self.assertEqual(os.getenv("JARVIS_UNDER_PYTEST"), "1")


if __name__ == "__main__":
    unittest.main()
