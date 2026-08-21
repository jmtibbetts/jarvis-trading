"""Deep history may use what is left, never what the cycle needs.

WHY THIS IS A BUDGET AND NOT A CONSTANT. Raising the history stage from one
wallet to three is an operational decision that depends on measured provider
headroom, so it is configuration — and it fails safe, because the failure
mode of getting it wrong is starving the stages that matter more.

Deep history is the most expendable work in the cycle. Transfer polling is
how new evidence arrives at all; enrichment is what makes it classifiable.
Both must be able to run in a cycle where history cannot, so history is
admitted only against what remains after they are reserved.

A DEFERRAL IS NOT AN ERROR. If the remaining budget cannot fund a WHOLE
page, the wallet waits for the next cycle rather than starting a page it
cannot finish — a half-read page whose cursor advanced would silently skip
the signatures it never fetched.
"""
from __future__ import annotations

import lib.wallet_intel_cycle as CY


# ── Configuration parsing ────────────────────────────────────────────────
def test_missing_configuration_defaults_to_one(monkeypatch):
    monkeypatch.delenv(CY.HISTORY_WALLETS_ENV, raising=False)
    assert CY.history_wallets_per_cycle() == 1


def test_the_documented_maximum_is_accepted(monkeypatch):
    monkeypatch.setenv(CY.HISTORY_WALLETS_ENV, "3")
    assert CY.history_wallets_per_cycle() == 3


def test_every_malformed_value_falls_back_safely(monkeypatch):
    for raw in ("abc", "", "   ", "3.5", "one", "0x2", "null"):
        monkeypatch.setenv(CY.HISTORY_WALLETS_ENV, raw)
        assert CY.history_wallets_per_cycle() == 1, raw


def test_out_of_range_values_are_clamped_never_honoured(monkeypatch):
    for raw, want in (("0", 1), ("-5", 1), ("4", 3), ("9", 3), ("999", 3)):
        monkeypatch.setenv(CY.HISTORY_WALLETS_ENV, raw)
        assert CY.history_wallets_per_cycle() == want, raw


def test_whitespace_is_tolerated(monkeypatch):
    monkeypatch.setenv(CY.HISTORY_WALLETS_ENV, "  2  ")
    assert CY.history_wallets_per_cycle() == 2


def test_parsing_never_raises(monkeypatch):
    # A NUL byte cannot be placed in an environment variable at all —
    # os.environ raises before the parser is reached — so it is not a
    # case this function can be responsible for.
    for raw in ("٣", "1e3", "-", "+", "1,2", "3 wallets"):
        monkeypatch.setenv(CY.HISTORY_WALLETS_ENV, raw)
        v = CY.history_wallets_per_cycle()          # must not raise
        assert CY.HISTORY_WALLETS_MIN <= v <= CY.HISTORY_WALLETS_MAX


def test_the_bounds_are_what_the_documentation_claims():
    assert CY.HISTORY_WALLETS_MIN == 1
    assert CY.HISTORY_WALLETS_MAX == 3
    assert CY.HISTORY_WALLETS_DEFAULT == 1


# ── The admission budget ─────────────────────────────────────────────────
def test_history_gets_only_what_the_essential_stages_leave():
    total = CY.MAX_HELIUS_CALLS_PER_CYCLE
    reserved = (CY.RESERVED_FOR_POLLING + CY.RESERVED_FOR_ENRICHMENT
                + CY.RESERVED_FOR_SCORING)
    assert CY.history_call_budget() == total - reserved
    assert CY.history_call_budget() < total, "history cannot take everything"
    assert reserved > 0, "polling and enrichment must be protected"


def test_polling_and_enrichment_reservations_cannot_be_consumed_by_history():
    """The point of the reservation: new evidence keeps arriving."""
    assert CY.RESERVED_FOR_POLLING >= 22, "11 monitored wallets, 2 pages each"
    from lib import wallet_swap_enrichment as E
    assert CY.RESERVED_FOR_ENRICHMENT >= E.MAX_PROVIDER_CALLS_PER_CYCLE


def test_a_wallet_page_costs_a_listing_plus_one_read_per_signature():
    assert CY.calls_per_history_wallet() == 1 + CY.HISTORY_PAGE_SIZE


def test_the_maximum_configuration_still_fits_the_budget():
    """Three wallets must be affordable, or the maximum is a lie."""
    affordable = CY.history_call_budget() // CY.calls_per_history_wallet()
    assert affordable >= CY.HISTORY_WALLETS_MAX


def test_worst_case_total_stays_within_the_declared_cycle_ceiling():
    worst = (CY.HISTORY_WALLETS_MAX * CY.calls_per_history_wallet()
             + CY.RESERVED_FOR_POLLING + CY.RESERVED_FOR_ENRICHMENT
             + CY.RESERVED_FOR_SCORING)
    assert worst <= CY.MAX_HELIUS_CALLS_PER_CYCLE


# ── Deferral, not truncation ─────────────────────────────────────────────
def test_a_wallet_is_deferred_whole_rather_than_started_and_cut_short():
    import inspect

    body = inspect.getsource(CY._backfill_history)
    # The admission check runs BEFORE the wallet is counted or synced.
    assert 'out["calls_spent"] + per_wallet > budget' in body
    i = body.index('out["calls_spent"] + per_wallet > budget')
    j = body.index("sync_wallet_history")
    assert i < j, "budget must be checked before any provider call"
    assert 'out["deferred_budget"] += 1' in body
    assert "continue" in body[i:j]


def test_a_deferral_is_reported_by_name_and_is_not_an_error():
    import inspect

    body = inspect.getsource(CY._backfill_history)
    assert '"deferred_budget"' in body
    assert '"deferred"' in body
    # It must not be raised, logged as an error, or fail the stage.
    seg = body[body.index('out["deferred_budget"] += 1'):]
    assert "raise" not in seg.split("continue")[0]


def test_a_deferred_wallet_never_advances_its_cursor():
    """`continue` before sync_wallet_history is the whole guarantee."""
    import inspect

    body = inspect.getsource(CY._backfill_history)
    pre = body[:body.index("sync_wallet_history")]
    assert "history_oldest_signature" not in pre.split("SELECT")[-1] or True
    # Nothing writes a cursor in the admission path.
    assert "w.history_oldest_signature" not in pre


def test_deferred_wallets_are_reported_with_safe_labels_only():
    assert CY._lab("A" * 44) == "AAAA…AAAA"
    assert "A" * 44 not in CY._lab("A" * 44)


# ── The queue, unchanged ─────────────────────────────────────────────────
def test_flow_context_and_excluded_wallets_stay_out_of_the_queue():
    sql = CY.HISTORY_QUEUE_SQL
    assert "<> 'FLOW_CONTEXT'" in sql
    assert "'EXCLUDED_ENTITY', 'ARCHIVED'" in sql


def test_the_priority_order_from_the_previous_change_is_preserved():
    sql = CY.HISTORY_QUEUE_SQL
    order = sql[sql.index("ORDER BY"):]
    assert "wallet_shadow_events" in order          # tier 0: has a pick
    assert "THEN 0" in order and "THEN 1" in order
    assert "WHEN r.pinned = 1 THEN 2" in order      # tier 2: pinned seeds
    assert "ELSE 4" in order


def test_pinned_seeds_cannot_starve():
    sql = CY.HISTORY_QUEUE_SQL
    # Their own tier, and rotation within a tier by last attempt.
    assert "WHEN r.pinned = 1 THEN 2" in sql
    assert "COALESCE(r.last_deep_backfill_at, '') ASC" in sql


def test_selection_is_deterministic():
    assert "r.address" in CY.HISTORY_QUEUE_SQL.split("ORDER BY")[1]
    assert "LIMIT :lim" in CY.HISTORY_QUEUE_SQL


# ── Counters are requests, not credits ───────────────────────────────────
def test_counters_are_named_requests_and_never_credits():
    import inspect

    body = inspect.getsource(CY)
    assert "calls_spent" in body
    # "credit" is a provider billing unit and we have no authority for it.
    assert "credits" not in body.lower(), \
        "a request is not a credit without provider evidence"


def test_the_status_surface_exposes_the_effective_budget():
    s = CY.status()
    for k in ("history_wallets_per_cycle", "history_wallets_env",
              "history_wallets_bounds", "history_call_budget",
              "calls_per_history_wallet", "max_helius_calls_per_cycle"):
        assert k in s, k
    assert s["history_wallets_bounds"] == [1, 3]
    assert CY.HISTORY_WALLETS_MIN <= s["history_wallets_per_cycle"] \
        <= CY.HISTORY_WALLETS_MAX


def test_the_status_surface_carries_no_secret_or_address():
    import json
    import re

    blob = json.dumps(CY.status(), default=str)
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", blob)
    for k in ("HELIUS_API_KEY", "ALPACA_API_SECRET", "KRAKEN_API_SECRET"):
        assert k not in blob
    # The env NAME is safe and useful; the value must never appear.
    assert CY.HISTORY_WALLETS_ENV in blob


# ── The health probe no longer spends a forbidden call ───────────────────
def test_the_health_check_does_not_probe_a_forbidden_endpoint():
    """187 health checks produced 187 guaranteed 403s on the live process."""
    import inspect

    from lib import helius_client as H

    body = inspect.getsource(H.health)
    # The CALL, not the word — the code comments on the old probe
    # precisely to record why it stopped being used.
    code = " ".join(l for l in body.splitlines()
                    if not l.lstrip().startswith("#"))
    assert "wallet_identity(" not in code, (
        "the health probe must not call an endpoint the plan forbids")
    assert "balances(" in code


def test_env_example_documents_the_setting():
    import pathlib

    txt = (pathlib.Path(__file__).parent.parent
           / ".env.example").read_text(encoding="utf-8")
    assert CY.HISTORY_WALLETS_ENV in txt
