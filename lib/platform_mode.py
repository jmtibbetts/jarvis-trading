"""What JARVIS is allowed to do with real money — one authority, one answer.

JARVIS IS CURRENTLY A VIRTUAL TRADING AND TRAINING LABORATORY, not a
live-trading platform. Every eligible thesis routes to a virtual venue so
the system can make thousands of decisions, receive realistic consequences
and learn which part of its own process creates or destroys alpha.

THE MODE IS A SAFETY BOUNDARY, NOT A PREFERENCE. It is checked at the
point of order submission rather than at the point of intent, because
intent is generated in a dozen places and submission happens in two. A
guard that lives near the decision is a guard that a new caller can walk
past without noticing.

WHY THIS EXISTS AT ALL. Routing was decided by a `paper_mode` boolean
whose real meaning was "this direction is not a plain long" — shorts,
leverage and futures went to the simulator and everything else went to
Alpaca. That is a live-first architecture from a different project phase,
and under it an ordinary long equity signal reached a real broker by
DEFAULT. In a training laboratory that is backwards: the default must be
that nothing reaches a broker, and reaching one must be an explicit,
audited exception.

Modes, least to most capable:

    VIRTUAL_ONLY   no real order submission, at all. Market and account
                   data still flow — reading a broker is not trading.
    LIVE_SHADOW    real orders still refused; live-adjacent paths may run
                   so their behaviour can be observed.
    LIVE_LIMITED   real submission permitted within explicit per-order and
                   per-day caps.
    LIVE_ENABLED   real submission permitted.

Only VIRTUAL_ONLY and the refusal path are active today. The others exist
so the boundary has somewhere to grow that is not "delete the check".
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

VIRTUAL_ONLY = "VIRTUAL_ONLY"
LIVE_SHADOW = "LIVE_SHADOW"
LIVE_LIMITED = "LIVE_LIMITED"
LIVE_ENABLED = "LIVE_ENABLED"

MODES = (VIRTUAL_ONLY, LIVE_SHADOW, LIVE_LIMITED, LIVE_ENABLED)

# Modes in which a real order may be submitted. Deliberately a small
# explicit set rather than "not VIRTUAL_ONLY" — a mode added later is
# refused until somebody adds it here on purpose.
_LIVE_CAPABLE = frozenset({LIVE_LIMITED, LIVE_ENABLED})

DEFAULT_MODE = VIRTUAL_ONLY


class LiveExecutionDisabled(RuntimeError):
    """Raised when real order submission is attempted outside a live mode.

    An exception rather than a falsy return: a caller that ignores a
    return value would submit anyway, and this is the boundary where that
    mistake costs real money.
    """


def current_mode() -> str:
    """The platform mode. Unrecognised values fail CLOSED to VIRTUAL_ONLY.

    A typo in an environment variable must never be the thing that enables
    live trading.
    """
    raw = (os.getenv("JARVIS_PLATFORM_MODE") or "").strip().upper()
    if raw in MODES:
        return raw
    if raw:
        logger.warning(
            f"[PlatformMode] unrecognised JARVIS_PLATFORM_MODE={raw!r} — "
            f"falling back to {DEFAULT_MODE}")
    return DEFAULT_MODE


def is_virtual_only() -> bool:
    return current_mode() == VIRTUAL_ONLY


def live_execution_allowed() -> bool:
    return current_mode() in _LIVE_CAPABLE


def assert_live_execution_allowed(what: str = "order submission") -> None:
    """THE hard guard. Call immediately before any real order leaves.

    Raises rather than returns, so a caller cannot proceed by ignoring the
    answer.
    """
    mode = current_mode()
    if mode in _LIVE_CAPABLE:
        return
    raise LiveExecutionDisabled(
        f"{what} refused: platform mode is {mode}. JARVIS is running as a "
        f"virtual training laboratory and real orders are disabled. This is "
        f"the current platform state, not a fault.")


def assert_may_increase_exposure(what: str = "order submission") -> None:
    """Guard for anything that OPENS or ADDS to real exposure.

    Same rule as `assert_live_execution_allowed` — named separately so the
    call sites read as what they protect.
    """
    assert_live_execution_allowed(what)


def note_risk_reducing_action(what: str, symbol: str | None = None) -> None:
    """Closes and cancels are ALLOWED in every mode, and logged loudly.

    A deliberate asymmetry. If the platform is switched to VIRTUAL_ONLY
    while real positions are still open, refusing to close them would trap
    real capital behind a training-mode flag — the guard would then be the
    thing causing the loss it exists to prevent. Reducing risk is always
    permitted; only INCREASING it is gated.

    Logged at WARNING because a real order leaving a virtual-mode desk is
    something an operator should see, even when it is the right thing.
    """
    if not live_execution_allowed():
        logger.warning(
            f"[PlatformMode] {current_mode()}: permitting RISK-REDUCING "
            f"{what}{f' for {symbol}' if symbol else ''} — closing and "
            f"cancelling are never blocked, so a mode change cannot strand "
            f"an open real position.")


def status() -> dict:
    """For the UI. `live_execution_disabled` is a STATE, not an error —
    the UI must present it as the current configuration rather than as a
    degraded feed, or an operator will try to repair it."""
    mode = current_mode()
    return {
        "mode": mode,
        "virtual_only": mode == VIRTUAL_ONLY,
        "live_execution_allowed": mode in _LIVE_CAPABLE,
        "market_data_allowed": True,
        "account_data_allowed": True,
        "detail": ("LIVE EXECUTION DISABLED — TRAINING MODE"
                   if mode not in _LIVE_CAPABLE else
                   "live execution permitted"),
        "modes_available": list(MODES),
    }
