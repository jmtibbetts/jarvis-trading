"""
SEC EDGAR client for Form 4 (insider transaction) intelligence.

Uses only SEC's free, unauthenticated public endpoints — no paid vendor,
no API key. Verified live against the real APIs while building this:

  - "Latest filings" atom feed (all filers, one form type):
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&count={n}&output=atom
    Each Form 4 filing appears as 1-2 entries (issuer + reporting-owner party);
    dedupe by accession number.

  - Per-filing directory listing:
    https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_nodashes}/index.json
    Lists every document in the filing; the Form 4 XML is whichever entry
    ends in ".xml" (filename varies by filer — "form4-*.xml", "ownership.xml",
    "primary_doc.xml", ...) and isn't the "-index.html"/"-index-headers.html"
    wrapper.

  - The Form 4 XML itself (ownershipDocument schema): issuer CIK/name/ticker,
    reporting owner CIK/name/relationship (director/officer/10%-owner +
    officer title), and non-derivative / derivative transaction tables.

SEC's Fair Access policy (sec.gov/developer) caps usage at 10 requests/second
per user and requires a descriptive User-Agent identifying the requester —
not a browser UA. _sleep_between_requests() keeps this client well under
that ceiling regardless of caller behavior.
"""
from __future__ import annotations

import logging
import os
import time
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

BASE = "https://www.sec.gov"
ARCHIVES = f"{BASE}/Archives/edgar/data"
HTTP_TIMEOUT = 15.0
REQUEST_GAP_SECONDS = 0.15  # ~6-7 req/s, well under SEC's 10 req/s cap

# SEC's Fair Access WAF rejects generic/browser User-Agents on data.sec.gov and
# Archives requests with a 403 — confirmed live while building this. Set a real
# contact identity via SEC_EDGAR_USER_AGENT ("Your Company you@example.com");
# the fallback below is a plausible default, not a deliverable address.
USER_AGENT = os.getenv("SEC_EDGAR_USER_AGENT", "Jarvis Trading AI admin@jarvis-trading.local")

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# SEC Table I/II transaction codes → human labels. P and S are the ones that
# actually represent an open-market buy/sell decision; everything else is
# compensation mechanics (grants, exercises, tax withholding, gifts, ...)
# that shouldn't be read the same way.
TRANSACTION_CODE_LABELS = {
    "P": "Open Market Buy",
    "S": "Open Market Sale",
    "A": "Grant/Award",
    "D": "Disposition to Issuer",
    "F": "Tax Withholding",
    "I": "Discretionary Transaction",
    "M": "Option Exercise",
    "C": "Derivative Conversion",
    "E": "Short Position Expiration",
    "H": "Long Position Expiration",
    "O": "Out-of-Money Exercise",
    "X": "In-the-Money Exercise",
    "G": "Gift",
    "L": "Small Acquisition",
    "W": "Will/Inheritance",
    "Z": "Voting Trust Transfer",
    "J": "Other",
}


def _headers() -> dict:
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}


def _get(url: str, client: httpx.Client | None = None) -> httpx.Response:
    owns_client = client is None
    c = client or httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        resp = c.get(url, headers=_headers())
        resp.raise_for_status()
        return resp
    finally:
        time.sleep(REQUEST_GAP_SECONDS)
        if owns_client:
            c.close()


def fetch_recent_form4_filings(count: int = 100, client: httpx.Client | None = None) -> list[dict]:
    """Recent Form 4 filings across ALL issuers, deduped by accession number.
    Returns [{accession, cik, filed_at, index_url}, ...], newest first."""
    url = f"{BASE}/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&count={count}&output=atom"
    try:
        resp = _get(url, client)
    except httpx.HTTPError as e:
        logger.warning(f"[SEC EDGAR] Failed to fetch latest-filings feed: {e}")
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning(f"[SEC EDGAR] Malformed atom feed: {e}")
        return []

    seen = set()
    filings = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        link_el = entry.find("atom:link", _ATOM_NS)
        index_url = link_el.get("href") if link_el is not None else None
        updated_el = entry.find("atom:updated", _ATOM_NS)
        filed_at = updated_el.text if updated_el is not None else None
        if not index_url:
            continue
        # index_url looks like .../data/{cik}/{accession-nodash}/{accession-with-dashes}-index.htm
        try:
            parts = index_url.rstrip("/").split("/")
            cik = parts[-3]
            accession = parts[-1].removesuffix("-index.htm")
        except (IndexError, AttributeError):
            continue
        if accession in seen:
            continue
        seen.add(accession)
        filings.append({"accession": accession, "cik": cik, "filed_at": filed_at, "index_url": index_url})
    return filings


def fetch_form4_xml_url(cik: str, accession: str, client: httpx.Client | None = None) -> str | None:
    """Resolve the actual Form 4 XML document URL from a filing's directory listing."""
    acc_nodash = accession.replace("-", "")
    dir_url = f"{ARCHIVES}/{cik}/{acc_nodash}/index.json"
    try:
        resp = _get(dir_url, client)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"[SEC EDGAR] Directory listing failed for {accession}: {e}")
        return None

    items = data.get("directory", {}).get("item", [])
    for item in items:
        name = item.get("name", "")
        if name.endswith(".xml") and "index" not in name.lower():
            return f"{ARCHIVES}/{cik}/{acc_nodash}/{name}"
    return None


def _text(el, path: str) -> str | None:
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else None


def _float(el, path: str) -> float | None:
    raw = _text(el, path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_form4_xml(xml_text: str) -> dict | None:
    """Parse a Form 4 ownershipDocument into issuer/owner/transaction dicts.
    A filing can list multiple reportingOwner blocks (joint filers, e.g. a
    trust + an individual) — this takes the first, which covers the common
    single-filer case; the shared transaction tables apply to whichever
    owner(s) filed jointly, but per-owner attribution for joint filings
    isn't split out here."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug(f"[SEC EDGAR] Malformed Form 4 XML: {e}")
        return None

    if root.tag != "ownershipDocument":
        return None

    issuer = root.find("issuer")
    if issuer is None:
        return None
    owner = root.find("reportingOwner")
    if owner is None:
        return None

    owner_id = owner.find("reportingOwnerId")
    relationship = owner.find("reportingOwnerRelationship")

    def _bool(el, path: str) -> bool:
        raw = _text(el, path)
        return raw is not None and raw.strip().lower() in ("true", "1")

    base = {
        "issuer_cik": _text(issuer, "issuerCik"),
        "issuer_name": _text(issuer, "issuerName"),
        "ticker": _text(issuer, "issuerTradingSymbol"),
        "owner_cik": _text(owner_id, "rptOwnerCik") if owner_id is not None else None,
        "owner_name": _text(owner_id, "rptOwnerName") if owner_id is not None else None,
        "owner_title": _text(relationship, "officerTitle") if relationship is not None else None,
        "is_director": _bool(relationship, "isDirector") if relationship is not None else False,
        "is_officer": _bool(relationship, "isOfficer") if relationship is not None else False,
        "is_ten_pct_owner": _bool(relationship, "isTenPercentOwner") if relationship is not None else False,
    }

    transactions = []
    for table_name, table_key in (("nonDerivativeTable", "non_derivative"), ("derivativeTable", "derivative")):
        table = root.find(table_name)
        if table is None:
            continue
        tx_tag = "nonDerivativeTransaction" if table_key == "non_derivative" else "derivativeTransaction"
        for tx in table.findall(tx_tag):
            code = _text(tx, "transactionCoding/transactionCode")
            shares = _float(tx, "transactionAmounts/transactionShares/value")
            price = _float(tx, "transactionAmounts/transactionPricePerShare/value")
            transactions.append({
                "table": table_key,
                "security_title": _text(tx, "securityTitle/value"),
                "transaction_date": _text(tx, "transactionDate/value"),
                "transaction_code": code,
                "transaction_label": TRANSACTION_CODE_LABELS.get(code or "", code or "Unknown"),
                "acquired_disposed": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                "shares": shares,
                "price_per_share": price,
                "total_value": (shares * price) if (shares is not None and price is not None) else None,
                "shares_owned_after": _float(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
            })

    if not transactions:
        return None
    return {**base, "transactions": transactions}
