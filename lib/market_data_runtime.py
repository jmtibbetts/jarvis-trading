"""READ-ONLY market data is a different responsibility from TRADING.

THE DISTINCTION THIS MODULE EXISTS TO ENFORCE.

    the autonomous trading scheduler owns    strategy orchestration,
                                             candidate generation,
                                             decisions, simulated execution

    the market-data runtime owns             observing the market,
                                             maintaining books,
                                             feeding forward evidence,
                                             feed health

Switching the scheduler off is a statement about whether JARVIS may ACT. It
is not a statement about whether JARVIS may LOOK. Conflating the two is how
a desk ends up unable to collect the very evidence it needs to decide
whether its trading logic is safe to re-enable — the observation window
closes exactly when it is most needed.

So this must be an expressible state, and after this module it is:

    autonomous trading scheduler   OFF
    Bitnomial public market data   ON
    shared range collection        ON
    real orders                    IMPOSSIBLE
    account mutations              IMPOSSIBLE

The last two are structural, not promised. Every feed here is a public,
unauthenticated read surface; none of them import an order or account API.

WHY IT EXISTS AT ALL. `bitnomial_market_data.start_stream()` was fully
implemented, tested, and called by NOTHING — so the perpetual book was
permanently empty in production and every perp quote refused. The provider
was not missing; its lifecycle owner was. This is that owner.

ONE INGEST, MANY CONSUMERS. There is exactly one Bitnomial connection, and
execution snapshots, the shared range collector, the outcome observer and
diagnostics all read the same maintained books. Per-consumer streams would
produce several independently-timed versions of "the market", and any
disagreement between them would be indistinguishable from a real price move.

    Bitnomial WS ──┬─> ExecutionSnapshot
                   ├─> shared range collector
                   ├─> outcome observer
                   └─> health / diagnostics

WHAT THIS MODULE DOES NOT DO. It does not evaluate strategies, size trades,
submit orders or touch an account. It starts feeds, stops feeds, and reports
what they are doing.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Disables read-only market data explicitly and by its OWN name. It is
# deliberately not spelled in terms of trading: a flag called
# LIVE_TRADING_DISABLED silently turning the market off is precisely the
# coupling this module removes. Tests and offline runs set this; the normal
# virtual runtime leaves it unset.
DISABLE_ENV = "JARVIS_DISABLE_MARKET_DATA"


def market_data_enabled() -> bool:
    """Whether read-only feeds may run.

    NOT tied to JARVIS_DISABLE_SCHEDULER. Observing the market while the
    trading loop is stopped is a supported, and currently the intended,
    configuration.
    """
    return os.getenv(DISABLE_ENV) != "1"


def start(*, bitnomial_symbols=None) -> dict:
    """Start every read-only feed this runtime owns. Idempotent.

    Failure of one feed never prevents the others: a desk that cannot see
    perpetuals should still see spot, and should say so.
    """
    out: dict = {"enabled": market_data_enabled(), "started": {}, "errors": {}}
    if not out["enabled"]:
        logger.warning("[MarketData] disabled by %s=1 — no feeds started",
                       DISABLE_ENV)
        return out

    try:
        from lib import bitnomial_market_data as MD
        out["started"]["bitnomial"] = MD.start_stream(bitnomial_symbols)
    except Exception as e:
        out["errors"]["bitnomial"] = f"{type(e).__name__}: {e}"
        logger.warning("[MarketData] Bitnomial failed to start: %s", e)
    return out


def stop() -> dict:
    """Stop every read-only feed this runtime owns. Idempotent."""
    out: dict = {"stopped": {}, "errors": {}}
    try:
        from lib import bitnomial_market_data as MD
        out["stopped"]["bitnomial"] = MD.stop_stream()
    except Exception as e:
        out["errors"]["bitnomial"] = f"{type(e).__name__}: {e}"
    return out


def health() -> dict:
    """Feed health across every read-only authority, in one place.

    Kraken and the L2 order-book streams keep their existing lifecycles —
    they already start at application startup and already self-heal, and
    wrapping working services purely to match a diagram would add risk for
    no behaviour. Their status is surfaced here so ops has ONE view.
    """
    out: dict = {"enabled": market_data_enabled(), "feeds": {}}

    try:
        from lib import bitnomial_market_data as MD
        out["feeds"]["bitnomial"] = MD.stream_health()
    except Exception as e:
        out["feeds"]["bitnomial"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        from lib.kraken_stream import status as kraken_status
        out["feeds"]["kraken"] = kraken_status()
    except Exception as e:
        out["feeds"]["kraken"] = {"error": f"{type(e).__name__}: {e}"}

    return out
