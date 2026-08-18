"""Hermetic test root — every pytest run gets its own throwaway database.

This file runs before any test imports application code, which is the only
moment the redirect can happen: app/database.py builds its engine at import
time. Two incidents made this structural rather than advisory — a test
reset the active paper book, and fixture rows later leaked into the live
candidate tables despite per-test cleanup. The guard in
app.database._resolve_db_path refuses the operator DB outright while
JARVIS_UNDER_PYTEST=1, so a test file that forgets everything below still
cannot touch real state.

The scheduler is disabled for the same reason: a test importing main must
never start background jobs against anything.
"""
import os
import tempfile
from pathlib import Path

os.environ["JARVIS_UNDER_PYTEST"] = "1"
os.environ.setdefault("JARVIS_DISABLE_SCHEDULER", "1")

if not os.getenv("JARVIS_DB_PATH", "").strip():
    _tmp = Path(tempfile.mkdtemp(prefix="jarvis-test-db-"))
    os.environ["JARVIS_DB_PATH"] = str(_tmp / "test.db")

# THE LLM ENDPOINT IS PINNED, FOR THE SAME REASON AS THE DATABASE.
#
# lib/lmstudio resolves its endpoint by PROBING - loopback, then the WSL
# gateway - and a probe is a real network call. Under pytest that meant the
# suite reached the operator's live LM Studio: non-deterministic (green or
# red depending on whether a desktop app is running), slow, and capable of
# sending a prompt to a real model from a "hermetic" test.
#
# An explicit LM_STUDIO_URL is honoured exactly and never probed, so this
# pins resolution to a port nothing listens on. Port 9 is DISCARD: the
# connection is refused immediately rather than timing out. Tests that need
# LLM behaviour patch it; tests that do not must never find one.
# FORCED, not setdefault. setdefault leaves an INHERITED value in place, so
# an operator shell that already exports LM_STUDIO_URL=<real LM Studio>
# handed it straight to pytest and the "pin" pinned nothing. The whole point
# is that no hermetic test can reach a real model, and that cannot depend on
# what the invoking shell happens to contain. Tests that need endpoint
# behaviour monkeypatch it AFTER this bootstrap.
os.environ["LM_STUDIO_URL"] = "http://127.0.0.1:9/v1"

# The raw-event store (Phase 3) is a separate file with the same rule: a
# test run must never append to the operator's event log.
if not os.getenv("JARVIS_EVENTS_DB_PATH", "").strip():
    _tmp_ev = Path(tempfile.mkdtemp(prefix="jarvis-test-events-"))
    os.environ["JARVIS_EVENTS_DB_PATH"] = str(_tmp_ev / "events.db")


def pytest_configure(config):
    """Create the full schema once per session, exactly as the app would."""
    from app.database import DB_PATH, init_db

    assert "jarvis-test-db-" in str(DB_PATH), (
        f"test session resolved to unexpected DB: {DB_PATH}"
    )
    init_db()


# ── Skipped coverage is accounted for, or the run fails ───────────────────
#
# WHY THIS IS A HOOK AND NOT A TEST. Eighteen tests skipped on every run and
# the count was tolerated because the previous platform skipped a similar
# number. Twelve turned out to be core logic — the expectancy cost gate,
# which refuses a setup carrying 13R of round-trip cost — skipping only
# because a hermetic database starts empty. Those assertions had never once
# executed in CI, and the check was green throughout.
#
# A test cannot observe the session it belongs to, so the first version
# forked a second full suite: correct, and it tripled every run. This hook
# reads the results of the run that already happened, for free.
#
# An ALLOWLIST rather than a total. "18 skips permitted" rots into a number
# nobody can defend and cannot tell a hardware skip from a new accident.
# Every skip must match a declared reason with a category and a written
# justification; a new skip for a new reason fails the run until someone
# writes down why. Fixing the test is always the better answer than listing
# it here.

# THE CATEGORY TAXONOMY. A skip states WHICH KIND of coverage is absent,
# and the kinds are not interchangeable:
#
#   HERMETIC                  never skips — it is the release gate
#   REAL_PROVIDER_READ_ONLY   reads a live PUBLIC provider surface; no
#                             credentials, no order path, no mutation
#   OPTIONAL_HARDWARE         needs hardware a runner may not have
#   EXTERNAL_INTEGRATION      a broader external dependency
#   PAPER_BROKER_INTEGRATION  reaches a broker's paper environment
#   LIVE_EXECUTION            real money — must never run in CI at all
#
# Filing a read-only provider check under EXTERNAL_INTEGRATION merely to
# satisfy an existing regex would erase the distinction that makes those
# tests safe to run on demand, so each kind gets its own line and its own
# budget.
ALLOWED_SKIPS = (
    # (regex, category, budget)
    (r"EXTERNAL_INTEGRATION", "EXTERNAL_INTEGRATION", 4),
    (r"hardware-only: no NPU", "OPTIONAL_HARDWARE", 1),
    # READ-ONLY PUBLIC PROVIDER SURFACES, opted into with
    # JARVIS_REAL_PROVIDER_TESTS=1. They prove the DETERMINISTIC TWINS still
    # resemble reality; the twins are hermetic and gate the release, so
    # skipping these loses no assertion about our own logic.
    #
    # The budget is EXACT rather than generous. Each of these has a hermetic
    # counterpart, so a rising count means either a new provider was
    # deliberately characterised, or a hermetic test quietly grew a network
    # dependency — which is precisely what this hook exists to catch.
    #
    #   6  tests/test_bitnomial_real_provider.py
    #      (public product specs + public book WebSocket;
    #       twin: tests/test_bitnomial_market_data.py)
    (r"REAL_PROVIDER_READ_ONLY", "REAL_PROVIDER_READ_ONLY", 6),
)

# Reasons that are never acceptable, with the fix named. These are the three
# shapes that hid real coverage loss on this project.
FORBIDDEN_SKIPS = (
    (r"no (outcome history|market data|data)|not tradeable|empty",
     "the environment starts empty - seed a deterministic fixture instead"),
    (r"RUN_DB_MUTATING_TESTS|live paper book|live auto-sim book",
     "conftest redirects to a temp DB and _resolve_db_path refuses the "
     "operator DB under pytest, so these can simply run"),
)


def classify_skips(reports):
    """(problems, counts_by_category) for a list of (where, reason) skips.

    Extracted from the session hook so the POLICY can be tested directly.
    A hook that only speaks at session end is checkable in exactly one way —
    by running a whole suite and reading an exit code — and this project has
    already lost a cycle to reading the summary line instead of that exit
    code. The rule is now a pure function with tests of its own.
    """
    import re as _re

    problems, counts = [], {}
    for where, reason in reports:
        for pattern, why in FORBIDDEN_SKIPS:
            if _re.search(pattern, reason, _re.I):
                problems.append(f"{where}: {reason}\n      -> {why}")
                break
        else:
            for pattern, category, _budget in ALLOWED_SKIPS:
                if _re.search(pattern, reason):
                    counts[category] = counts.get(category, 0) + 1
                    break
            else:
                problems.append(f"{where}: {reason}\n      -> undeclared skip "
                                "reason; fix the test, or declare it in "
                                "conftest.ALLOWED_SKIPS with a justification")

    for _pattern, category, budget in ALLOWED_SKIPS:
        n = counts.get(category, 0)
        if n > budget:
            problems.append(f"{category}: {n} skips exceeds its budget of {budget}")
    return problems, counts


def pytest_sessionfinish(session, exitstatus):
    reports = getattr(session.config, "_jarvis_skips", [])
    if not reports:
        return

    problems, counts = classify_skips(reports)

    # THE CATEGORY REPORT IS PRINTED EVEN WHEN EVERYTHING PASSES. "10
    # skipped" says nothing about whether the right ten were skipped, and
    # reading that line is how an undeclared skip got mistaken for a green
    # run. The categories are what anyone should be looking at.
    if counts:
        print("\nSKIP CATEGORIES: " + ", ".join(
            f"{c}={counts[c]}/{b}" for _p, c, b in ALLOWED_SKIPS
            if counts.get(c)))

    if problems:
        print("\n" + "=" * 70)
        print("SKIPPED COVERAGE IS NOT ACCOUNTED FOR:")
        for p in problems:
            print(f"  - {p}")
        print("=" * 70)
        session.exitstatus = 1


def pytest_runtest_logreport(report):
    """Collect skip reasons as they happen, for the hook above."""
    if report.skipped and report.when in ("setup", "call"):
        longrepr = getattr(report, "longrepr", None)
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            path, lineno, reason = longrepr
            store = getattr(report.session, "config", None) if hasattr(report, "session") else None
            reason = str(reason).replace("Skipped: ", "")
            _PENDING.append((f"{path}:{lineno}", reason))


_PENDING = []


def pytest_collection_finish(session):
    session.config._jarvis_skips = _PENDING
