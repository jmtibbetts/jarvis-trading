"""WHO executed the order, and against which economy.

THE AXIS THE VENUE TAXONOMY CANNOT ANSWER. `lib/execution_venue.py` names
venue FAMILIES — where a plan is sent and which adapter fills it. Every one
of them presumes JARVIS submitted something. `lib/realized_outcome.py` names
EVIDENCE SOURCES — where a learning row came from. Neither answers the
question a manual desk forces:

    did this program place the order, or did the human?

That question is orthogonal to both. A manual trade happens on a REAL venue
with REAL money and is not a virtual fill; it is also not a live autonomous
fill, because no code submitted it. Collapsing it into either is a lie with
economic consequences: recorded as virtual it would fund a training economy
with real-world numbers, and recorded as autonomous it would credit the bot
with an order it never placed and cannot repeat.

MANUAL EXECUTION IS NOT FAILED AUTOMATION. Plenty of venues are tradable by
a human and not by a program — `lib/venue_capabilities.py` already carries
that status as `UI_ONLY`, and a UI_ONLY venue is exactly where an operator
trades by hand. A missing execution API is a fact about the venue, not a
defect in this system, and the evidence such a trade produces is worth as
much as any other.

THE MODES

    VIRTUAL_CEX      JARVIS submitted. Simulated order book, virtual money.
    VIRTUAL_DEX      JARVIS submitted. Simulated pool, virtual money.
    SHADOW           No order existed anywhere. A control policy's opinion.
    MANUAL_OPERATOR  A HUMAN submitted, on a real venue, with real money.
                     JARVIS may have recommended it; JARVIS did not place it.
    LIVE_AUTONOMOUS  JARVIS submits with real money. RESERVED AND REFUSED —
                     see below.

LIVE_AUTONOMOUS IS A GRADUATION STATE, NOT A FEATURE FLAG. It exists here so
the taxonomy is complete and so nothing has to be renamed when it is earned;
`assert_executable()` refuses it unconditionally. Real capital is authorised
by forward-observed evidence, not by the existence of a constant.

THE STRINGS ARE IMPORTED, NOT RETYPED. `VIRTUAL_CEX`, `VIRTUAL_DEX` and
`SHADOW` come from `lib/execution_venue.py` so a second vocabulary cannot
drift into existence beside the first. If those names ever change, this
module changes with them or fails to import — which is the point.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# THE SAME OBJECTS, not copies of their spelling. See the module docstring.
from lib.execution_venue import SHADOW, VIRTUAL_CEX, VIRTUAL_DEX

EXECUTION_MODE_VERSION = "execution_mode_v1"

# The one genuinely new member of the taxonomy.
MANUAL_OPERATOR = "MANUAL_OPERATOR"

# Reserved. Present so the vocabulary is complete; refused so its presence
# authorises nothing.
LIVE_AUTONOMOUS = "LIVE_AUTONOMOUS"

MODES = (VIRTUAL_CEX, VIRTUAL_DEX, SHADOW, MANUAL_OPERATOR, LIVE_AUTONOMOUS)


class ExecutionModeError(ValueError):
    """An unknown mode, or a mode used for something it may not do."""


@dataclass(frozen=True)
class ExecutionModeSpec:
    """What a mode is allowed to claim about itself.

    Every field answers a question some part of the system would otherwise
    infer, and each of those inferences has a way of being wrong.
    """

    mode: str

    # DID THIS PROGRAM PLACE THE ORDER? The single most consequential bit.
    # False for MANUAL_OPERATOR: crediting the bot with a human's order
    # would put fills in its execution record that it cannot reproduce.
    submitted_by_jarvis: bool

    # Was real capital at risk? Virtual and shadow modes are not, so their
    # results are training evidence rather than account history.
    real_money: bool

    # MAY RECORDING THIS MOVE THE VIRTUAL BOOK'S CASH? Only the virtual
    # modes may. A manual trade is EVIDENCE ABOUT AN EXTERNAL ACCOUNT; if
    # writing one moved paper cash, the training economy would be funded and
    # drained by trades it never made, and every simulated result after that
    # point would be measured against a corrupted balance.
    mutates_virtual_economy: bool

    # May this mode produce new records TODAY?
    executable_today: bool

    # NOTE: there is deliberately NO `outcome_source` field here. It would
    # be a second, unpopulated home for something `outcome_source()` already
    # answers — and SHADOW cannot answer it from the mode alone, so half the
    # table would have to sit empty. A field that nothing writes is how a
    # value that looks authoritative turns out never to have been set.

    # The `lib.execution_venue` family, where one exists. MANUAL_OPERATOR
    # has none by construction: there is no adapter, because there is
    # nothing for this program to submit to.
    venue_family: str | None = None

    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _sources_by_mode() -> dict:
    """Imported inside the call to keep module import order unconstrained."""
    from lib import realized_outcome as ro

    return {
        VIRTUAL_CEX: ro.VIRTUAL_CEX_AGENT,
        VIRTUAL_DEX: ro.VIRTUAL_DEX_AGENT,
        SHADOW: None,
        MANUAL_OPERATOR: ro.MANUAL_OPERATOR,
        LIVE_AUTONOMOUS: None,
    }


_SPECS: dict[str, ExecutionModeSpec] = {
    VIRTUAL_CEX: ExecutionModeSpec(
        VIRTUAL_CEX, submitted_by_jarvis=True, real_money=False,
        mutates_virtual_economy=True, executable_today=True,
        venue_family=VIRTUAL_CEX,
        reason="simulated order book; the training economy's own fills"),
    VIRTUAL_DEX: ExecutionModeSpec(
        VIRTUAL_DEX, submitted_by_jarvis=True, real_money=False,
        mutates_virtual_economy=True, executable_today=True,
        venue_family=VIRTUAL_DEX,
        reason="simulated pool; the on-chain training economy's own fills"),
    SHADOW: ExecutionModeSpec(
        SHADOW, submitted_by_jarvis=False, real_money=False,
        mutates_virtual_economy=False, executable_today=True,
        venue_family=SHADOW,
        reason=("a control policy's opinion; no order existed at any venue, "
                "so nothing was submitted by anyone")),
    MANUAL_OPERATOR: ExecutionModeSpec(
        MANUAL_OPERATOR, submitted_by_jarvis=False, real_money=True,
        mutates_virtual_economy=False, executable_today=True,
        venue_family=None,
        reason=("the operator placed this order by hand at a real venue. "
                "JARVIS may have recommended it and did not execute it; "
                "there is no adapter because there is nothing to submit to")),
    LIVE_AUTONOMOUS: ExecutionModeSpec(
        LIVE_AUTONOMOUS, submitted_by_jarvis=True, real_money=True,
        mutates_virtual_economy=False, executable_today=False,
        venue_family=None,
        reason=("reserved. Real capital under program control is earned "
                "through forward-observed evidence and granted explicitly; "
                "it is never enabled by the existence of a constant")),
}


def spec(mode: str) -> ExecutionModeSpec:
    """The mode's contract. An unrecognised mode RAISES.

    Returning a permissive default for an unknown mode is how a typo
    acquires execution rights.
    """
    found = _SPECS.get(str(mode or "").strip())
    if found is None:
        raise ExecutionModeError(
            f"{mode!r} is not an execution mode. Known modes: "
            f"{', '.join(MODES)}")
    return found


def assert_executable(mode: str) -> ExecutionModeSpec:
    """Refuse a mode that may not produce records today.

    This is the interlock on LIVE_AUTONOMOUS. It refuses at the CALL SITE,
    so re-adding the constant to a table or a Pydantic enum cannot quietly
    turn it on.
    """
    s = spec(mode)
    if not s.executable_today:
        raise ExecutionModeError(f"{s.mode} may not execute: {s.reason}")
    return s


def submitted_by_jarvis(mode: str) -> bool:
    """Did this program place the order? Never inferred from anything else."""
    return spec(mode).submitted_by_jarvis


def mutates_virtual_economy(mode: str) -> bool:
    """May recording this move virtual cash? Only the virtual books may."""
    return spec(mode).mutates_virtual_economy


def outcome_source(mode: str, *, venue_type: str | None = None) -> str:
    """The `lib.realized_outcome` SOURCES member for a closed trade.

    SHADOW needs the venue type because it splits into SHADOW_CEX and
    SHADOW_DEX; asking for it without one refuses rather than picking.
    """
    from lib import realized_outcome as ro

    s = spec(mode)
    direct = _sources_by_mode().get(s.mode)
    if direct is not None:
        return direct
    if s.mode == SHADOW:
        vt = str(venue_type or "").upper()
        if vt == "CEX":
            return ro.SHADOW_CEX
        if vt == "DEX":
            return ro.SHADOW_DEX
        raise ExecutionModeError(
            "SHADOW resolves to SHADOW_CEX or SHADOW_DEX by venue type; "
            f"venue_type={venue_type!r} names neither, and choosing one "
            f"would mislabel half of them")
    raise ExecutionModeError(
        f"{s.mode} has no realized-outcome source: {s.reason}")


def as_dict() -> dict:
    """The whole taxonomy, for the API and for the UI's mode filter.

    `outcome_source` is COMPUTED here rather than stored on the spec, and
    stays null for SHADOW because the venue type decides it.
    """
    out = []
    for m in MODES:
        row = _SPECS[m].as_dict()
        try:
            row["outcome_source"] = outcome_source(m)
        except ExecutionModeError:
            row["outcome_source"] = None
        out.append(row)
    return {"version": EXECUTION_MODE_VERSION, "modes": out}
