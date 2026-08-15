"""Concentration parity across EVERY position book.

The gap this closes, measured 2026-08-17: `lib/concentration` was written,
tested (13 tests) and wired into `paper_engine.open_paper_position` — and
it bounded nothing. Two independent defects, neither visible to a test
that hands `check()` a hand-built list:

  1. `check_against_book` filtered `status == "open"` while every writer
     stores `"Open"`. SQLite's `=` is case-sensitive, so the guard loaded
     an EMPTY book on every call and judged each position against nothing.
  2. Auto Sim is a second book (`auto_sim_positions`, its own portfolio
     table) that never called the guard at all, and stored no `notional`
     for it to read even if it had.

Both are shape defects rather than arithmetic defects, so they survived a
suite that only ever tested the arithmetic. These tests exercise the guard
through the DATABASE, generically, for every book at once — a third book
inherits the whole file the moment it is registered, and fails the first
test until it is.
"""
from __future__ import annotations

import unittest

from app.database import Base, get_db
from lib import concentration
from lib.concentration import MAX_SYMBOL_EXPOSURE_PCT, POSITION_BOOKS

EQUITY = 100_000.0


def _position_models() -> dict[str, type]:
    """Every mapped model that is shaped like a position book: it names an
    instrument, records a size and an entry, and can be Open or Closed.

    Discovery is by SHAPE, not by a hand-kept list, so a book added later
    is found here whether or not anyone remembered this file. The live
    broker mirror (`positions_cache`) is correctly excluded — it has no
    status and no entry price because nothing in JARVIS opens into it.
    """
    needed = {"symbol", "status", "qty", "entry_price"}
    out = {}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        cols = {c.key for c in mapper.columns}
        if needed <= cols:
            out[model.__tablename__] = model
    return out


class EveryBookIsRegisteredTests(unittest.TestCase):
    def test_no_position_book_escapes_the_registry(self):
        """A new position book must be registered in POSITION_BOOKS.

        This is the test that makes the constraint structural. Auto Sim
        existed for weeks as an unguarded second book; the next one fails
        here on the commit that introduces it.
        """
        discovered = set(_position_models())
        registered = set(POSITION_BOOKS.values())
        missing = discovered - registered
        self.assertFalse(
            missing,
            f"position book(s) {sorted(missing)} are not in "
            f"concentration.POSITION_BOOKS — they open positions that "
            f"nothing bounds. Register them and wire the guard into their "
            f"open path.")

    def test_registry_names_real_tables(self):
        discovered = set(_position_models())
        stale = set(POSITION_BOOKS.values()) - discovered
        self.assertFalse(stale, f"POSITION_BOOKS names non-book table(s): "
                                f"{sorted(stale)}")

    def test_every_registered_book_resolves_to_a_model(self):
        for book in POSITION_BOOKS:
            with self.subTest(book=book):
                model = concentration._book_model(book)
                self.assertEqual(model.__tablename__, POSITION_BOOKS[book])

    def test_an_unregistered_book_name_is_refused_not_guessed(self):
        v = concentration.check_against_book("BTC/USD", 1000, 10, EQUITY,
                                             book="nonexistent")
        self.assertFalse(v["ok"])
        self.assertEqual(v["limit"], "error")


class TheGuardActuallySeesTheBookTests(unittest.TestCase):
    """The regression tests for defect #1 — run against every book.

    Each writes ONE open row using the model's own status default, then
    asks the guard whether a second position in the same instrument may
    open. If the guard cannot see the row it will happily say yes.
    """

    def setUp(self):
        self.models = _position_models()
        for model in self.models.values():
            with get_db() as db:
                db.query(model).delete()
                db.commit()

    tearDown = setUp

    def _open_row(self, model, symbol: str, notional: float):
        """A minimal open position, using whatever the model calls things."""
        cols = {c.key for c in model.__mapper__.columns}
        entry = 100.0
        row = {"symbol": symbol, "qty": notional / entry, "entry_price": entry,
               "asset_class": "Crypto", "direction": "Long", "side": "long",
               "leverage": 1.0, "current_price": entry}
        if "notional" in cols:
            row["notional"] = notional
        if "margin_used" in cols:
            row["margin_used"] = notional
        if "signal_id" in cols:          # auto_sim requires it (NOT NULL)
            row[f"signal_id"] = f"sig-{symbol}"
        row = {k: v for k, v in row.items() if k in cols}
        with get_db() as db:
            db.add(model(**row))         # status left at its own default
            db.commit()

    def test_an_open_position_is_visible_to_the_guard(self):
        for table, model in self.models.items():
            with self.subTest(book=table):
                self.setUp()
                self._open_row(model, "DOGE/USD", EQUITY * 0.20)
                book = next(b for b, t in POSITION_BOOKS.items() if t == table)
                with get_db() as db:
                    rows = concentration.open_rows(book, db)
                self.assertEqual(
                    len(rows), 1,
                    f"{table}: the guard loaded an EMPTY book while a "
                    f"position was open — every check against it is a no-op")

    def test_accumulation_in_one_symbol_is_refused(self):
        """20% already open + 10% more = 30% > 25% cap.

        Neither piece breaches the cap alone, so this fails on exactly the
        defect that let DOGE reach a third of equity: a guard that cannot
        see the book still refuses a single oversized position, which is
        why it looked alive.
        """
        for table, model in self.models.items():
            with self.subTest(book=table):
                self.setUp()
                self._open_row(model, "DOGE/USD", EQUITY * 0.20)
                book = next(b for b, t in POSITION_BOOKS.items() if t == table)
                v = concentration.check_against_book(
                    "DOGE/USD", EQUITY * 0.10, EQUITY * 0.005, EQUITY,
                    book=book)
                self.assertFalse(v["ok"], f"{table}: {v}")
                self.assertEqual(v["limit"], "symbol")
                self.assertAlmostEqual(v["symbol_exposure_pct"], 30.0, places=1)

    def test_a_different_symbol_still_opens(self):
        """The guard must bound concentration, not trading."""
        for table, model in self.models.items():
            with self.subTest(book=table):
                self.setUp()
                self._open_row(model, "DOGE/USD", EQUITY * 0.20)
                book = next(b for b, t in POSITION_BOOKS.items() if t == table)
                v = concentration.check_against_book(
                    "BTC/USD", EQUITY * 0.20, EQUITY * 0.005, EQUITY,
                    book=book)
                self.assertTrue(v["ok"], f"{table}: {v}")

    def test_books_are_judged_independently(self):
        """A position in one book must not refuse a trade in another.

        Paper and Auto Sim are separate simulations with separate capital.
        Measuring one against the other would reject trades on the strength
        of an unrelated experiment.
        """
        if len(self.models) < 2:
            self.skipTest("only one position book exists")
        tables = list(self.models)
        self._open_row(self.models[tables[0]], "DOGE/USD", EQUITY * 0.90)
        other = next(b for b, t in POSITION_BOOKS.items() if t == tables[1])
        v = concentration.check_against_book(
            "DOGE/USD", EQUITY * 0.10, EQUITY * 0.005, EQUITY, book=other)
        self.assertTrue(v["ok"], f"{tables[1]} was judged against "
                                 f"{tables[0]}'s positions: {v}")


class ExposureIsMeasuredNotAssumedTests(unittest.TestCase):
    """Defect #2's arithmetic half: a book that stores no notional reported
    zero exposure. Derivation from qty x entry keeps legacy rows honest."""

    def test_a_row_without_notional_still_counts_as_exposure(self):
        book = [{"symbol": "DOGE/USD", "qty": 200.0, "entry_price": 100.0}]
        v = concentration.check("DOGE/USD", EQUITY * 0.10, EQUITY * 0.005,
                                EQUITY, book)
        self.assertFalse(v["ok"], "a row without a notional column read as "
                                  "zero exposure")
        self.assertAlmostEqual(v["symbol_exposure_pct"], 30.0, places=1)

    def test_stored_notional_wins_over_derivation(self):
        """Leverage and futures multipliers make qty x entry the wrong
        number; the solved notional is the right one when present."""
        book = [{"symbol": "DOGE/USD", "qty": 1.0, "entry_price": 1.0,
                 "notional": EQUITY * 0.24}]
        v = concentration.check("DOGE/USD", EQUITY * 0.02, EQUITY * 0.005,
                                EQUITY, book)
        self.assertFalse(v["ok"])
        self.assertAlmostEqual(v["symbol_exposure_pct"], 26.0, places=1)


class StatusIsComparedCaseInsensitivelyTests(unittest.TestCase):
    """No query may compare a position status to a lowercase literal.

    This defect landed THREE separate times before anyone noticed, because
    each instance fails silently — the query returns an empty set, which
    looks like "nothing is open" rather than like a bug:

      lib/concentration.py    the guard bounded nothing on either book
      app/routers/intel.py    the chart's position overlay was always empty
      lib/morning_brief.py    the brief reported 0 open positions daily

    Writers store "Open"; SQLite's `=` is case-sensitive. A source scan is
    a blunt instrument, but three silent recurrences earn one — and the
    fix it demands (wrap in LOWER/func.lower) is the correct code anyway.
    """

    # Matches a status compared to a lowercase literal, ORM or raw SQL.
    PATTERN = __import__("re").compile(
        r"""status\s*==?\s*["']open["']""", __import__("re").I)

    def test_no_case_sensitive_open_status_comparison(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for sub in ("lib", "app", "jobs"):
            for path in (root / sub).rglob("*.py"):
                for n, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), 1):
                    m = self.PATTERN.search(line)
                    if not m:
                        continue
                    # The literal must be lowercase AND unwrapped to be a
                    # bug: LOWER(status) = 'open' is the correct form.
                    if "'open'" not in line and '"open"' not in line:
                        continue          # "Open" — matches the writer
                    if "lower" in line.lower():
                        continue          # already normalized
                    offenders.append(
                        f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertFalse(
            offenders,
            "status compared to a lowercase literal — writers store "
            '"Open" and SQLite\'s `=` is case-sensitive, so these match '
            "NOTHING and fail silently as an empty result:\n  "
            + "\n  ".join(offenders))


class AutoSimOpenPathIsGuardedTests(unittest.TestCase):
    """The wiring itself: Auto Sim's open path must consult the guard.

    Asserted at the seam rather than by running a full sim cycle, which
    needs live signals, prices and a portfolio. `test_auto_simulator.py`
    covers the cycle; this covers the fact that the cycle asks.
    """

    def test_the_open_path_calls_the_concentration_check(self):
        import inspect

        from lib import auto_simulator
        src = inspect.getsource(auto_simulator)
        self.assertIn("concentration.check(", src,
                      "Auto Sim's open path no longer consults the "
                      "concentration guard — it is an unbounded second book "
                      "again")

    def test_auto_sim_persists_the_exposure_it_opened(self):
        from app.database import AutoSimPosition
        cols = {c.key for c in AutoSimPosition.__mapper__.columns}
        self.assertIn("notional", cols,
                      "without a stored notional the guard reads every Auto "
                      "Sim position as zero exposure")


if __name__ == "__main__":
    unittest.main()
