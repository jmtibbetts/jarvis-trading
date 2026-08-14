"""Threat -> instrument transmission map.

The threat feed already SEES the refinery attack and the carrier
deployment; what it never said is which instruments those touch and
which way the pressure points. This module closes that gap with a
RULES TABLE — human-legible hypotheses, not model output — so the
Morning Brief can say "2 active threats touch CL=F, both up-pressure"
instead of leaving the operator to make the connection at 6am.

Honesty constraints, same as everywhere:
  - these are HYPOTHESES with a stated rationale, labelled as such;
    a transmission row is a watch item, never a signal
  - matching is transparent keyword logic the operator can read and
    edit — an LLM guessing geopolitics into positions is exactly the
    failure mode this desk was rebuilt to avoid
  - every emitted row carries the threat's own reliability score;
    a rumor maps the same as a confirmed strike but WEARS its rumor-ness
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# (name, compiled patterns, [(instrument, direction, rationale)])
# direction is the hypothesized PRESSURE on price: "up" | "down" | "vol".
_R = [
    ("energy infrastructure attack",
     r"refiner|pipeline|oil terminal|oil facility|oil depot|energy infrastructure|tanker attack|drone.*(oil|refin)",
     [("CL=F", "up", "supply disruption risk"),
      ("BZ=F", "up", "supply disruption risk"),
      ("RB=F", "up", "refined product tightness")]),
    ("shipping chokepoint / strait",
     r"strait of hormuz|red sea|suez|bab.el.mandeb|shipping lane|chokepoint|houthi.*(ship|vessel)|tanker.*(seiz|attack)",
     [("CL=F", "up", "freight risk premium"),
      ("BZ=F", "up", "freight risk premium")]),
    ("military escalation / deployment",
     r"carrier (strike )?group|troop deployment|mobiliz|airstrike|missile (test|launch|strike)|military escalation|invasion",
     [("CL=F", "up", "geopolitical risk premium"),
      ("GC=F", "up", "safe-haven bid"),
      ("ES=F", "vol", "risk-off pressure"),
      ("NOC", "up", "defense demand"),
      ("LMT", "up", "defense demand")]),
    ("natgas / LNG disruption",
     r"lng (terminal|export|plant)|natural gas (pipeline|supply)|nord stream|freeport",
     [("NG=F", "up", "supply disruption risk")]),
    ("sanctions on metals / mining",
     r"sanction.*(metal|nickel|copper|aluminum|mining)|export ban.*(metal|mineral)|rare earth.*(restrict|ban)",
     [("HG=F", "up", "supply restriction"),
      ("GC=F", "up", "sanctions hedging")]),
    ("chip / tech export controls",
     r"chip (export|ban|restrict)|semiconductor.*(export|restrict|sanction)|tsmc.*(threat|blockade)|taiwan.*(blockade|invasion|strait)",
     [("SMH", "vol", "supply-chain uncertainty"),
      ("NVDA", "vol", "export exposure"),
      ("ES=F", "vol", "risk-off pressure")]),
    ("black-sea / grain corridor",
     r"grain (deal|corridor|export)|black sea.*(ship|port|attack)|odesa|wheat export",
     [("ZW=F", "up", "export disruption"),
      ("ZC=F", "up", "export disruption")]),
    ("hurricane / gulf weather",
     r"hurricane|tropical storm.*(gulf|landfall)|gulf of mexico.*(storm|evacuat)",
     [("NG=F", "vol", "production shut-ins vs demand loss"),
      ("CL=F", "vol", "production and refining shut-ins")]),
    ("crypto crackdown / exchange failure",
     r"crypto (ban|crackdown)|exchange.*(hack|insolven|collaps)|stablecoin.*(depeg|collaps)",
     [("BTC/USD", "down", "confidence shock"),
      ("ETH/USD", "down", "confidence shock")]),
]
RULES = [(name, re.compile(pat, re.I), targets) for name, pat, targets in _R]


def map_threat(title: str, summary: str = "") -> list[dict]:
    """All transmission hypotheses matching one threat's text."""
    text = f"{title or ''} {summary or ''}"
    out = []
    for name, rx, targets in RULES:
        if rx.search(text):
            for instrument, direction, why in targets:
                out.append({"rule": name, "instrument": instrument,
                            "pressure": direction, "rationale": why})
    return out


_SEVERITY_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}


def transmission_watch(hours: int = 48, min_severity: str = "Medium",
                       limit: int = 200) -> list[dict]:
    """Active threats mapped to instruments, deduped so one
    instrument+pressure pair appears once, wearing its STRONGEST source.

    created_date is the recency key on purpose: published_at in this
    table is RFC-2822 text whose string ordering is alphabetical
    ('Wed' > 'Tue'), which would serve weekday-sorted noise as 'newest'.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from app.database import engine

    floor = _SEVERITY_RANK.get(min_severity, 2)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with engine.connect() as c:
        threats = c.execute(text("""
            SELECT DISTINCT title, description, severity, created_date
            FROM threat_events
            WHERE created_date > :cutoff
            ORDER BY created_date DESC LIMIT :lim
        """), {"cutoff": cutoff, "lim": limit}).fetchall()
    best: dict[tuple, dict] = {}
    for title, desc, severity, at in threats:
        rank = _SEVERITY_RANK.get(severity, 0)
        if rank < floor:
            continue
        for m in map_threat(title or "", desc or ""):
            key = (m["instrument"], m["pressure"])
            row = {**m, "threat": (title or "")[:140],
                   "severity": severity, "detected_at": at}
            if key not in best or rank > _SEVERITY_RANK.get(
                    best[key]["severity"], 0):
                best[key] = row
    return sorted(best.values(),
                  key=lambda r: -_SEVERITY_RANK.get(r["severity"], 0))
