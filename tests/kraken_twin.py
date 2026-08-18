"""A deterministic stand-in for Kraken's PUBLIC HTTP surface.

WHY. `.github/workflows/ci.yml` states the suite is hermetic by
construction, and it was not: fifteen tests in test_venues.py and
test_us_venue_fees.py reached api.kraken.com and futures.kraken.com live.
Run with the network removed, all fifteen fail. Whether GitHub happened to
have working egress to Kraken on any given day is not a property a release
gate may depend on — not rate limiting, not DNS, not provider maintenance.

WHAT IS AND IS NOT REPLACED. This patches `httpx.get` INSIDE lib.venues and
nothing else. The captured payloads then flow through the REAL pair-spec
parser, the REAL tick and minimum-size validation, the REAL fee-tier
selection, the REAL margin-tier arithmetic and the REAL spread computation.

That distinction is the whole point. A test that stubbed
`kraken_pair_specs()` to return a tidy dict would assert only that the stub
matches the assertion; it would not notice Kraken renaming a field,
changing `ordermin`, or restructuring the fee ladder. Mocking the transport
keeps every line of our own logic under test and removes only the part we
do not own.

THE PAYLOADS ARE REAL, captured 2026-08-17 from the public endpoints and
trimmed to the instruments the tests exercise. Refresh them with
tests/test_kraken_real_provider.py, which is classified
REAL_PROVIDER_READ_ONLY and checks the live service still looks like this.
"""
from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager
from unittest.mock import patch

PAYLOADS = json.loads(
    (pathlib.Path(__file__).parent / "fixtures"
     / "kraken_public_payloads.json").read_text(encoding="utf-8"))


class _Response:
    """Only what lib.venues actually reads off an httpx response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _route(url: str, params: dict | None):
    """Map a request to its captured payload, or refuse it by name.

    An unrecognised URL raises rather than returning empty: a new network
    call appearing inside a hermetic test is exactly the regression this
    file exists to catch, and a silent empty response would let it pass as
    "the venue had nothing to say".
    """
    if url.endswith("/AssetPairs"):
        return _Response(PAYLOADS["AssetPairs"])
    if url.endswith("/Spread"):
        pair = (params or {}).get("pair")
        captured = PAYLOADS["Spread"]
        if pair in captured:
            return _Response(captured[pair])
        return _Response({"error": [], "result": {}})
    if url.endswith("/instruments"):
        return _Response(PAYLOADS["instruments"])
    if url.endswith("/feeschedules"):
        return _Response(PAYLOADS["feeschedules"])
    raise AssertionError(
        f"a hermetic test tried to reach {url!r}. Either add a captured "
        f"payload for it, or classify the test REAL_PROVIDER_READ_ONLY.")


def _clear_caches():
    """lib.venues caches for 12h, so a live response fetched by an earlier
    test would otherwise satisfy a later one and hide the dependency."""
    from lib import venues as V
    for name in ("_pair_cache", "_futures_cache", "_fee_schedule_cache",
                 "_spread_cache", "_account_fee_cache"):
        cache = getattr(V, name, None)
        if isinstance(cache, dict):
            cache.clear()


@contextmanager
def kraken_offline():
    """Serve lib.venues from captured payloads, and from nothing else."""
    _clear_caches()
    try:
        with patch("lib.venues.httpx.get",
                   side_effect=lambda url, **kw: _route(url, kw.get("params"))):
            yield
    finally:
        _clear_caches()


class KrakenOfflineMixin:
    """Mix into a TestCase whose subject reads Kraken's public surface."""

    def setUp(self):
        super().setUp()
        ctx = kraken_offline()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
