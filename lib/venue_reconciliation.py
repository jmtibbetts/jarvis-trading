"""Compare simulated leveraged-product economics with what real venues do.

WHY THIS EXISTS. The simulator prices perpetual executions from its own
models: fee schedules, funding extrapolation, spread and slippage
attribution. Those models are falsifiable only against what a real venue
actually charged a real position — and the first honest observation is
usually humbling. This module is the harness for that comparison.

IT AUTHORIZES NOTHING. No live execution, no order placement, no private
endpoint automation. Real exchange observations enter as CALIBRATION
EVIDENCE, hand-carried and sanitized, and the framework's whole job is to
keep the model honest about what those observations do and do not prove.

EVIDENCE HAS RANK, AND LOWER RANK IS NOT DELETED. A screenshot of a close
dialog (OBSERVED_UI) is real evidence — of what the UI displayed, not of
how the venue accounts. Realized history outranks it; an official API
outranks nothing it hasn't measured. Higher-authority evidence ARRIVING
does not erase the lower-authority record: both persist, with provenance,
because the difference between them is itself information.

THE CENTRAL DISCIPLINE: an incomplete lifecycle cannot yield realized
numbers. A position observed mid-flight has no final P&L, no actual
funding, no proven fee treatment — and every function here refuses to
manufacture them. UNKNOWN stays UNKNOWN. MISSING IS NOT ZERO.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

RECONCILIATION_MODEL_VERSION = "venue_reconciliation_v1"

# ── Evidence authorities, strongest first ────────────────────────────────
REALIZED_HISTORY = "REALIZED_HISTORY"
OFFICIAL_API = "OFFICIAL_API"
OFFICIAL_STATEMENT = "OFFICIAL_STATEMENT"
OBSERVED_UI = "OBSERVED_UI"
MODEL_DERIVED = "MODEL_DERIVED"

EVIDENCE_RANK = (REALIZED_HISTORY, OFFICIAL_API, OFFICIAL_STATEMENT,
                 OBSERVED_UI, MODEL_DERIVED)

# Lifecycle completeness.
COMPLETE_LIFECYCLE = "COMPLETE_LIFECYCLE"
INCOMPLETE_LIFECYCLE = "INCOMPLETE_LIFECYCLE"

UNKNOWN = "UNKNOWN"


class IncompleteLifecycle(Exception):
    """A realized quantity was requested from evidence that cannot supply
    one. Raising is the point: returning None would let a caller average
    it into a statistic as a quiet zero."""


@dataclass(frozen=True)
class FundingContext:
    """Funding is venue- and product-specific, and mostly UNKNOWN here.

    A two-minute scalp and an eight-hour hold on the same perpetual can
    cross zero or several funding events; two venues with the same nominal
    interval can differ on eligibility (open-at-timestamp vs held-through).
    None of that is guessable, so every field defaults to UNKNOWN and the
    aggregate refuses to pretend otherwise.
    """
    interval_hours: float | str = UNKNOWN
    events_crossed: int | str = UNKNOWN
    rates: tuple = ()                 # (timestamp, rate, source) triples
    rate_source: str = UNKNOWN
    eligibility_rule: str = UNKNOWN
    paid_usd: float | str = UNKNOWN
    received_usd: float | str = UNKNOWN

    @property
    def net_usd(self):
        """Net funding, or UNKNOWN if either leg is. Never a quiet zero."""
        if isinstance(self.paid_usd, str) or isinstance(self.received_usd, str):
            return UNKNOWN
        return float(self.received_usd) - float(self.paid_usd)


@dataclass(frozen=True)
class VenueExecutionObservation:
    """One observed real-venue position, at whatever completeness it has."""
    observation_id: str
    venue: str
    account_type: str
    instrument: str
    product: str
    side: str
    leverage: float
    margin_mode: str
    quantity: float
    quantity_unit: str
    entry_price: float

    evidence_type: str
    evidence_source: str
    lifecycle: str

    # Mid-flight observables. None = not observed (distinct from UNKNOWN
    # strings inside computed answers).
    observed_price: float | None = None
    mark_price: float | None = None
    notional: float | None = None
    margin: float | None = None
    displayed_pnl: float | None = None
    displayed_pnl_pct: float | None = None
    estimated_close_fee: float | None = None
    estimated_liquidation_price: float | None = None
    risk_ratio_pct: float | None = None

    funding: FundingContext = field(default_factory=FundingContext)

    opened_at: str | None = None
    observed_at: str | None = None
    venue_timezone_if_known: str | None = None
    hold_duration_hours: float | str = UNKNOWN

    # Completed-lifecycle facts. Present ONLY when evidence proves them.
    actual_exit_price: float | None = None
    actual_opening_fee: float | None = None
    actual_closing_fee: float | None = None
    actual_funding: float | None = None
    actual_realized_pnl: float | None = None
    actual_total_cost: float | None = None
    closed_at: str | None = None

    confidence: str = "OBSERVED"
    provenance: dict = field(default_factory=dict)

    # ── What the observation CAN answer ──────────────────────────────
    def raw_price_pnl_at(self, price: float) -> float:
        """Pure arithmetic on observed facts: qty x (price - entry),
        signed. This is a computation, not a claim about venue accounting."""
        sign = 1.0 if self.side.upper() == "LONG" else -1.0
        return sign * float(self.quantity) * (float(price)
                                              - float(self.entry_price))

    def display_consistency(self) -> dict:
        """Does the venue's own display cohere internally?

        Checking that displayed_pnl / margin ≈ displayed_pnl_pct is
        legitimate arithmetic ON the observation. It verifies the venue is
        showing return-on-margin — it proves nothing about which costs are
        inside displayed_pnl.
        """
        out: dict = {}
        if (self.displayed_pnl is not None and self.margin
                and self.displayed_pnl_pct is not None):
            implied_pct = 100.0 * self.displayed_pnl / self.margin
            out["implied_return_on_margin_pct"] = round(implied_pct, 2)
            out["displayed_pnl_pct"] = self.displayed_pnl_pct
            out["consistent_with_return_on_margin"] = (
                abs(implied_pct - self.displayed_pnl_pct) < 1.0)
        if self.observed_price is not None and self.displayed_pnl is not None:
            raw = self.raw_price_pnl_at(self.observed_price)
            out["raw_price_pnl_at_observed"] = round(raw, 4)
            out["displayed_minus_raw"] = round(self.displayed_pnl - raw, 4)
            out["displayed_minus_raw_note"] = (
                "a nonzero gap means the display includes SOMETHING beyond "
                "raw price P&L — fee treatment, mark-vs-last, funding, "
                "rounding, or another cost. WHICH one is not provable from "
                "a UI observation; see unexplained_cost_status().")
        return out

    def unexplained_cost_status(self) -> dict:
        """The honest name for the -0.24 question.

        The gap between displayed and raw price P&L is measured; its CAUSE
        is not. Candidates are enumerated, none is selected, and the status
        stays UNKNOWN until realized-history evidence proves the venue's
        accounting."""
        if self.observed_price is None or self.displayed_pnl is None:
            return {"status": UNKNOWN,
                    "reason": "no observed price/pnl pair to compare"}
        gap = self.displayed_pnl - self.raw_price_pnl_at(self.observed_price)
        return {
            "status": "UNEXPLAINED_VENUE_COST",
            "gap_usd": round(gap, 4),
            "candidate_causes": ("entry fee", "exit fee treatment",
                                 "mark vs last price", "funding",
                                 "rounding", "venue display convention",
                                 "another cost"),
            "resolvable_by": (REALIZED_HISTORY, OFFICIAL_API,
                              OFFICIAL_STATEMENT),
            "note": "the estimated closing fee displayed nearby has a "
                    "similar magnitude; similarity of magnitude is not "
                    "identity of cause",
        }

    # ── What it CANNOT answer, enforced ───────────────────────────────
    def _require_complete(self, what: str):
        if self.lifecycle != COMPLETE_LIFECYCLE:
            raise IncompleteLifecycle(
                f"{what} requested from {self.lifecycle} evidence "
                f"({self.evidence_type}). A position observed mid-flight "
                f"has no realized truth to report, and inventing one is "
                f"how a simulator learns from fiction.")

    def realized_pnl(self) -> float:
        self._require_complete("realized P&L")
        if self.actual_realized_pnl is None:
            raise IncompleteLifecycle("lifecycle marked complete but no "
                                      "actual_realized_pnl was evidenced")
        return float(self.actual_realized_pnl)

    def realized_funding(self) -> float:
        self._require_complete("realized funding")
        if self.actual_funding is None:
            raise IncompleteLifecycle("funding was never evidenced — "
                                      "UNKNOWN is not zero")
        return float(self.actual_funding)

    def as_dict(self) -> dict:
        return asdict(self)


# ── Cross-margin liquidation: estimate honestly or refuse ────────────────
def estimate_cross_liquidation(*, entry_price: float, leverage: float,
                               side: str,
                               account_collateral_usd: float | None = None,
                               other_positions: list | None = None,
                               maintenance_margin_rate: float | None = None,
                               venue_rules_known: bool = False) -> dict:
    """Cross-margin liquidation depends on the whole account, not the
    position. Without collateral, other positions, maintenance tiers and
    venue rules, an exact price is unknowable — so this returns either a
    clearly-labelled bound or UNKNOWN, never a fabricated exact price."""
    if not venue_rules_known or account_collateral_usd is None:
        return {
            "liquidation_price": UNKNOWN,
            "reason": ("cross-margin liquidation depends on account "
                       "collateral, other positions, maintenance tiers, "
                       "mark price and venue rules; "
                       + ("account state unavailable"
                          if account_collateral_usd is None
                          else "venue rules not evidenced")),
            "isolated_approximation_note": (
                "an isolated-margin approximation exists but would be "
                "WRONG for a cross position with shared collateral — "
                "refusing beats fabricating"),
        }
    if maintenance_margin_rate is None or other_positions is None:
        return {"liquidation_price": UNKNOWN,
                "reason": "maintenance tier or portfolio context missing"}
    # With full context an estimate could be computed; no venue's full
    # context has been evidenced yet, so this branch remains unreachable
    # by construction rather than pretending to a formula.
    return {"liquidation_price": UNKNOWN,
            "reason": "full-context estimation not yet implemented for any "
                      "evidenced venue rule set"}


# ── Model <-> venue reconciliation, for COMPLETED lifecycles ─────────────
@dataclass(frozen=True)
class ModelCosts:
    """What the simulator's models say this position should have cost."""
    raw_price_pnl: float
    entry_fee: float | str = UNKNOWN
    exit_fee: float | str = UNKNOWN
    spread_cost: float | str = UNKNOWN
    slippage: float | str = UNKNOWN
    funding: float | str = UNKNOWN
    other_carry: float | str = UNKNOWN
    execution_model_version: str = UNKNOWN
    venue_cost_model_version: str = UNKNOWN

    def total_cost(self):
        vals = (self.entry_fee, self.exit_fee, self.spread_cost,
                self.slippage, self.funding, self.other_carry)
        if any(isinstance(v, str) for v in vals):
            return UNKNOWN
        return float(sum(vals))

    def net_realized_pnl(self):
        total = self.total_cost()
        if isinstance(total, str):
            return UNKNOWN
        return self.raw_price_pnl - total


def reconcile(observation: VenueExecutionObservation,
              model: ModelCosts) -> dict:
    """Model vs venue, on realized evidence only.

    The model is NOT bent to fit. If a gap survives after every modeled
    component, it is preserved as UNEXPLAINED — an unexplained delta is a
    measurement of the model's ignorance, and papering over it would delete
    the one number that says how wrong the simulator still is."""
    venue_pnl = observation.realized_pnl()      # raises if incomplete
    model_net = model.net_realized_pnl()
    if isinstance(model_net, str):
        return {
            "status": "MODEL_INCOMPLETE",
            "reason": "one or more model cost components is UNKNOWN; a "
                      "reconciliation against an incomplete model would "
                      "attribute the venue's costs to the missing pieces",
            "venue_realized_pnl": venue_pnl,
            "model_components_unknown": [
                name for name, v in (("entry_fee", model.entry_fee),
                                     ("exit_fee", model.exit_fee),
                                     ("spread_cost", model.spread_cost),
                                     ("slippage", model.slippage),
                                     ("funding", model.funding),
                                     ("other_carry", model.other_carry))
                if isinstance(v, str)],
            "reconciliation_model_version": RECONCILIATION_MODEL_VERSION,
        }
    delta = venue_pnl - model_net
    notional = (observation.notional
                or abs(observation.quantity * observation.entry_price))
    return {
        "status": "RECONCILED",
        "venue_realized_pnl": venue_pnl,
        "model_net_realized_pnl": model_net,
        "model_raw_price_pnl": model.raw_price_pnl,
        "model_total_cost": model.total_cost(),
        "reconciliation_delta_usd": round(delta, 6),
        "reconciliation_delta_bps": (round(1e4 * delta / notional, 3)
                                     if notional else UNKNOWN),
        "unexplained_delta_usd": round(delta, 6),
        "unexplained_note": ("nonzero means the venue charged or credited "
                             "something the model does not represent — "
                             "preserved as UNEXPLAINED_VENUE_COST, never "
                             "absorbed into a fitted parameter"),
        "execution_model_version": model.execution_model_version,
        "venue_cost_model_version": model.venue_cost_model_version,
        "reconciliation_model_version": RECONCILIATION_MODEL_VERSION,
        "evidence_type": observation.evidence_type,
    }


# ── The first real-world fixture: BTCC LINKUSDT, observed mid-flight ─────
def btcc_linkusdt_observation() -> VenueExecutionObservation:
    """Sanitized OBSERVED_UI evidence of a live BTCC LINKUSDT perpetual.

    INCOMPLETE by declaration: the position had not closed when observed,
    so it carries no realized P&L, no actual funding, no proven fee
    treatment — and the accessor methods refuse to invent them. The open
    timestamp is recorded exactly as the UI displayed it; the venue's
    timezone is NOT assumed, so the hold duration is bounded, not exact.

    No account identifiers, no API material — evidence only.
    """
    return VenueExecutionObservation(
        observation_id="btcc-linkusdt-2026-08-19-ui",
        venue="BTCC",
        account_type="UNKNOWN",
        instrument="LINKUSDT",
        product="perpetual",
        side="LONG",
        leverage=50.0,
        margin_mode="CROSS",
        quantity=50.0,
        quantity_unit="LINK",
        entry_price=10.727,
        observed_price=10.692,
        notional=534.60,
        margin=10.73,
        displayed_pnl=-1.99,
        displayed_pnl_pct=-18.55,
        estimated_close_fee=0.24,
        estimated_liquidation_price=9.801,
        risk_ratio_pct=8.34,
        opened_at="2026-08-19 14:05:01 (venue-local, timezone UNPROVEN)",
        observed_at="2026-08-19 (approx 6-8+ hours after open)",
        venue_timezone_if_known=None,
        hold_duration_hours="approx 6-8+ (bounded by observation, not exact)",
        evidence_type=OBSERVED_UI,
        evidence_source="operator screenshot of the close dialog",
        lifecycle=INCOMPLETE_LIFECYCLE,
        confidence="OBSERVED_UI_EVIDENCE",
        provenance={
            "sanitized": True,
            "displayed_values_are_approximate": True,
            "close_fee_is_venue_estimate_not_realized": True,
        },
    )


# ── Per-component mismatch classification (product-aware financing) ─────
def classify_cost_mismatch(*, component: str, model_usd: float,
                           venue_usd: float, product: str,
                           tolerance_usd: float = 0.05) -> dict:
    """Name WHY a modeled cost disagrees with a realized one.

    Three different diseases, three different names:

      WRONG_PRODUCT_COST_MODEL   the model charged a cost the product does
                                 not have — e.g. borrow interest on a
                                 perpetual. The venue realized ~0 because
                                 nothing was ever borrowed. Absorbing this
                                 into slippage or "unexplained" would hide
                                 a structural modelling error behind noise.
      MISSING_COST_CATEGORY      the venue charged something the model
                                 does not represent at all — a real
                                 exchange/NFA/clearing fee, a real funding
                                 payment. The model's silence is the bug.
      ESTIMATION_GAP             both sides priced the same real mechanism
                                 and disagree on magnitude. This is the
                                 only one calibration may tune.
    """
    from lib.product_cost_profile import profile_for

    model = float(model_usd or 0.0)
    venue = float(venue_usd or 0.0)
    prof = profile_for(product)

    financing_components = ("borrow_fees", "margin_financing",
                            "rollover_fees")
    if (component in financing_components
            and abs(venue) <= tolerance_usd and abs(model) > tolerance_usd
            and prof.actual_borrowing_required is False):
        return {"classification": "WRONG_PRODUCT_COST_MODEL",
                "component": component, "model_usd": model,
                "venue_usd": venue, "product_class": prof.product_class,
                "detail": (f"the model charged {component} ${model:.4f} on "
                           f"{prof.product_class}, where that cost does "
                           f"not exist for this product — nothing is "
                           f"borrowed or financed. Fix the routing, do "
                           f"not tune the rate.")}
    if abs(model) <= tolerance_usd and abs(venue) > tolerance_usd:
        return {"classification": "MISSING_COST_CATEGORY",
                "component": component, "model_usd": model,
                "venue_usd": venue, "product_class": prof.product_class,
                "detail": (f"the venue realized {component} ${venue:.4f} "
                           f"that the model does not represent — the "
                           f"model is silent about a real cost")}
    return {"classification": "ESTIMATION_GAP",
            "component": component, "model_usd": model,
            "venue_usd": venue, "product_class": prof.product_class,
            "delta_usd": round(venue - model, 6),
            "detail": "both sides price the same mechanism; the gap is "
                      "magnitude, which calibration may legitimately tune"}
