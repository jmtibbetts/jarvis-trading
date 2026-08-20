"""Touching a real broker account is a SEPARATE authority from being allowed to.

THE DISTINCTION THIS EXISTS TO MAKE.

`lib.platform_mode` answers "may real exposure INCREASE?" and deliberately
lets risk REDUCTION through in every mode -- because refusing to close a
real position while the desk is in VIRTUAL_ONLY would trap real capital
behind a training flag, making the guard the cause of the loss it prevents.

That asymmetry is correct and is preserved. But it was being read as
something stronger than it says. "Closing is permitted" is a statement
about the ACTION. It is not a statement that a scheduled job should start
reaching into a brokerage account on its own initiative the moment normal
FULL_VIRTUAL operation begins.

Those are two questions:

    PERMISSION TO REDUCE REAL RISK        — platform_mode, always yes
    ACTIVATION OF ACCOUNT MANAGEMENT      — this module, default NO

An operator running the virtual desk with credentials still in .env and old
positions still sitting at the broker has not asked JARVIS to manage that
account. Until they say so explicitly, `guardian` and `positions` observe
and report; they do not mutate.

WHAT THIS IS NOT. It does not remove the ability to reduce risk later, and
it does not make risk reduction depend on permission to open new risk --
that would recreate exactly the trap described above. It adds one switch,
defaulting off, that says "yes, act on that account".

EVERY CONDITION MUST HOLD. Fail-closed on each, including on not knowing.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CONNECTOR_ENV = "JARVIS_ENABLE_EXTERNAL_BROKER_CONNECTOR"
MANAGEMENT_ENV = "JARVIS_ENABLE_EXTERNAL_ACCOUNT_MANAGEMENT"

_TRUE = ("1", "true", "yes", "on")

# Exposure effect of a requested action.
REDUCES = "REDUCES_EXPOSURE"
INCREASES = "INCREASES_EXPOSURE"
UNKNOWN = "UNKNOWN_EXPOSURE_EFFECT"


class ExternalAccountManagementDisabled(RuntimeError):
    """A real-account mutation was attempted without the capability."""


def connector_enabled() -> bool:
    """Is the external broker connector switched on at all?"""
    return os.getenv(CONNECTOR_ENV, "").lower() in _TRUE


def management_enabled() -> bool:
    """Has the operator asked JARVIS to ACT on the external account?

    Defaults to False. Credentials existing is not a request, and neither
    is a broker account containing positions.
    """
    return os.getenv(MANAGEMENT_ENV, "").lower() in _TRUE


def status() -> dict:
    """Capability state, for posture reporting. Never includes secrets."""
    return {
        "external_broker_connector_enabled": connector_enabled(),
        "external_account_management_enabled": management_enabled(),
        "effective": "may act on the external account"
        if (connector_enabled() and management_enabled())
        else "observe only — no external account mutation",
    }


def assert_may_manage_external_account(
        what: str, *, exposure_effect: str, identity: str | None) -> None:
    """Every condition for a SCHEDULED real-account mutation, or refuse.

    `exposure_effect` must be stated by the caller. UNKNOWN is rejected:
    an action nobody can prove reduces exposure is not a risk reduction,
    it is an unreviewed order.

    `identity` is the account/position the action names. A mutation with no
    named target cannot be shown to be reduce-only.
    """
    if exposure_effect == INCREASES:
        raise ExternalAccountManagementDisabled(
            f"{what}: refused — this action can INCREASE exposure. "
            f"Increasing real exposure is governed by platform mode and is "
            f"never reached through the risk-reduction path.")
    if exposure_effect != REDUCES:
        raise ExternalAccountManagementDisabled(
            f"{what}: refused — exposure effect is {exposure_effect!r}. "
            f"An action that cannot be PROVEN reduce-only is not a risk "
            f"reduction; an operator must authorise it explicitly.")
    if not identity:
        raise ExternalAccountManagementDisabled(
            f"{what}: refused — no account or position identity was named, "
            f"so the action cannot be shown to be reduce-only.")
    if not connector_enabled():
        raise ExternalAccountManagementDisabled(
            f"{what}: refused — the external broker connector is disabled "
            f"({CONNECTOR_ENV}).")
    if not management_enabled():
        raise ExternalAccountManagementDisabled(
            f"{what}: refused — external account management is not enabled "
            f"({MANAGEMENT_ENV}). Credentials and open positions are not a "
            f"request to manage the account.")

    # Permitted. Logged at WARNING: a real order leaving a virtual-mode desk
    # is something an operator should see even when it is the right thing.
    logger.warning("[ExternalAccount] %s on %s — permitted: reduce-only, "
                   "connector and management both explicitly enabled",
                   what, identity)
