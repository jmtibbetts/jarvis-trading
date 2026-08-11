"""
IPO intelligence — free SEC EDGAR data, no vendor.

Tracks the IPO pipeline through registration-filing progression:

    S-1     initial registration          -> stage "filed"
    S-1/A   amended registration          -> stage "amended"
    424B4   final pricing prospectus      -> stage "priced"

Uses the same getcurrent atom feed as lib/sec_edgar.py, with one trap verified
live: the feed's type filter is a PREFIX match, so requesting type=S-1 also
returns S-11 (real-estate registrations) — form types must be matched exactly
from each entry's title.

Cover-page extraction (424B4 only): the final prospectus front page states the
offering deterministically — verified against a real filing while building this
(Latigo Biotherapeutics: "$18.00 per share", "19,200,000 Shares", 'symbol
"LTGO"', "Nasdaq Global Select Market"). Each field is extracted with a
conservative pattern and left NULL when it doesn't match; nothing is inferred.
S-1/S-1A filings are tracked by stage only — pre-pricing documents often have
no final terms to extract, and pulling multi-megabyte registration statements
for every amendment would hammer SEC's fair-access budget for little data.

What this module does NOT do: no IPO_QUALITY_SCORE or HYPE_SCORE. Revenue,
margins, and dilution live deep in unstructured prospectus prose where
deterministic extraction is unreliable, and a score built on unreliable
extraction would be confidently wrong. The pipeline facts (who filed, what
stage, priced at what) are what the data honestly supports.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from xml.etree import ElementTree as ET

import httpx

from lib.sec_edgar import ARCHIVES, BASE, _ATOM_NS, _get

logger = logging.getLogger(__name__)

FORM_STAGES = {
    "S-1": "filed",
    "S-1/A": "amended",
    "424B4": "priced",
}

# Feed entry titles look like: "S-1 - CURIS INC (0001108205) (Filer)"
_TITLE_RE = re.compile(r"^(?P<form>[A-Z0-9/\-]+)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{7,10})\)")

# Name-based heuristic ONLY — flags blank-check shells by their conventional
# naming. A company can be a SPAC without matching, or match without being one;
# the flag is labeled "likely" everywhere it surfaces.
_SPAC_NAME_RE = re.compile(r"\b(acquisition\s+corp|acquisition\s+company|blank\s+check|SPAC)\b", re.I)

_TICKER_RE = re.compile(r"under the (?:ticker )?symbol\s+[\"'“]([A-Z]{1,5})[\"'”]")
_EXCHANGE_RE = re.compile(
    r"(Nasdaq(?:\s+\w+){0,2}\s+Market|New York Stock Exchange|NYSE\s+American|NYSE)"
)
# Price comes ONLY from the explicit "initial public offering price ... $X"
# phrasing. A generic "$X per share" fallback was removed after it extracted
# $0.0001 from a real filing — the PAR VALUE ("par value $0.0001 per share"),
# on a 424B4 that wasn't an IPO at all (an already-listed company's follow-on;
# Rule 424(b)(4) covers those too). A null price is honest; a par value
# presented as an offer price is fabrication.
_PRICE_RE = re.compile(r"initial public offering price[^.]{0,80}?\$\s?([\d.]+)", re.I)
# Case-insensitive: real covers write "19,200,000 Shares" and "9,375,000
# shares" interchangeably (both observed live).
_SHARES_RE = re.compile(r"([\d,]{4,})\s+shares", re.I)
_IPO_PHRASE_RE = re.compile(r"initial public offering", re.I)


def fetch_registration_filings(form_type: str, count: int = 40,
                               client: httpx.Client | None = None) -> list[dict]:
    """Recent filings of one EXACT form type, deduped by accession.

    form_type is matched against the entry title, not trusted from the query —
    the feed's type parameter is a prefix filter (verified live: type=S-1
    returns S-11 and S-1/A entries too)."""
    url = (
        f"{BASE}/cgi-bin/browse-edgar?action=getcurrent&type={form_type.replace('/', '%2F')}"
        f"&company=&dateb=&owner=include&count={count}&output=atom"
    )
    try:
        resp = _get(url, client)
        root = ET.fromstring(resp.text)
    except (httpx.HTTPError, ET.ParseError) as e:
        logger.warning(f"[IPO] Feed fetch failed for {form_type}: {e}")
        return []

    seen = set()
    filings = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title_el = entry.find("atom:title", _ATOM_NS)
        link_el = entry.find("atom:link", _ATOM_NS)
        updated_el = entry.find("atom:updated", _ATOM_NS)
        title = (title_el.text or "") if title_el is not None else ""
        m = _TITLE_RE.match(title.strip())
        if not m or m.group("form") != form_type:
            continue  # exact-type gate: drops S-11 etc. from the S-1 feed
        index_url = link_el.get("href") if link_el is not None else None
        if not index_url:
            continue
        try:
            parts = index_url.rstrip("/").split("/")
            cik = parts[-3]
            accession = parts[-1].removesuffix("-index.htm")
        except (IndexError, AttributeError):
            continue
        if accession in seen:
            continue
        seen.add(accession)
        filings.append({
            "form_type": form_type,
            "company_name": m.group("name"),
            "cik": cik.lstrip("0") or cik,
            "accession": accession,
            "filed_at": updated_el.text if updated_el is not None else None,
            "index_url": index_url,
            "is_likely_spac": bool(_SPAC_NAME_RE.search(m.group("name"))),
        })
    return filings


def fetch_main_document_url(cik: str, accession: str, client: httpx.Client | None = None) -> str | None:
    """The primary prospectus document: the largest .htm in the filing that
    isn't the index wrapper. Filers name it arbitrarily (d38775d424b4.htm in
    the verified case), but the main document dwarfs the exhibits."""
    acc_nodash = accession.replace("-", "")
    try:
        data = _get(f"{ARCHIVES}/{cik}/{acc_nodash}/index.json", client).json()
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"[IPO] Directory listing failed for {accession}: {e}")
        return None
    best = None
    best_size = -1
    for item in data.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name.lower().endswith((".htm", ".html")) or "index" in name.lower():
            continue
        try:
            size = int(item.get("size") or 0)
        except (ValueError, TypeError):
            size = 0
        if size > best_size:
            best, best_size = name, size
    if not best:
        return None
    return f"{ARCHIVES}/{cik}/{acc_nodash}/{best}"


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    return html_mod.unescape(re.sub(r"\s+", " ", text))


def parse_cover_page(raw_html: str) -> dict:
    """Extract final offering terms from a 424B4's cover page (first ~8k chars
    of stripped text — the cover always leads the document).

    Every field is independently optional: a pattern that doesn't match yields
    NULL, never a guess. total_offering_usd is computed only when BOTH price
    and share count were extracted.

    cover_mentions_ipo distinguishes true IPO pricings from other 424(b)(4)
    prospectuses (follow-ons by already-listed companies use the same form —
    observed live). False means the cover never says "initial public
    offering" and the row should be labeled a follow-on, not an IPO."""
    head = _strip_html(raw_html)[:8000]

    ticker = None
    m = _TICKER_RE.search(head)
    if m:
        ticker = m.group(1)

    exchange = None
    m = _EXCHANGE_RE.search(head)
    if m:
        exchange = m.group(1).strip()

    price = None
    m = _PRICE_RE.search(head)
    if m:
        try:
            price = float(m.group(1))
        except ValueError:
            price = None

    shares = None
    m = _SHARES_RE.search(head)
    if m:
        try:
            shares = float(m.group(1).replace(",", ""))
        except ValueError:
            shares = None

    return {
        "ticker": ticker,
        "exchange": exchange,
        "offer_price": price,
        "shares_offered": shares,
        "total_offering_usd": round(price * shares, 2) if price is not None and shares is not None else None,
        "cover_mentions_ipo": bool(_IPO_PHRASE_RE.search(head)),
    }
