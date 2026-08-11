"""
SEC Form 13F institutional-holdings intelligence — free EDGAR data, no vendor.

Reuses the same unauthenticated endpoints as lib/sec_edgar.py (Form 4), just
a different form type. Verified live against real filings while building this.

A 13F-HR filing contains two documents:
  - primary_doc.xml  — cover page: filing manager name, period of report
    (namespace http://www.sec.gov/edgar/thirteenffiler)
  - infotable.xml    — the holdings themselves
    (namespace http://www.sec.gov/edgar/document/thirteenf/informationtable)
    Each holding: nameOfIssuer, titleOfClass, cusip, figi, value,
    shrsOrPrnAmt/sshPrnamt, investmentDiscretion, votingAuthority.

IMPORTANT — what 13F data honestly is, and isn't:
  - QUARTERLY and STALE. Managers have 45 days after quarter-end to file, so
    a holding shown here can be up to ~4.5 months old. It is a snapshot of
    what was held on the quarter-end date, NOT current positioning. Never
    present it as live institutional flow.
  - LONG US EQUITY ONLY. 13F covers 13(f)-listed securities: no short
    positions, no cash, no non-US listings, no most derivatives. A manager
    appearing "fully long" here may be hedged in instruments 13F never shows.
  - ONLY MANAGERS >$100M AUM must file.
  - Holdings identify securities by CUSIP/FIGI, NOT ticker. CUSIP-to-ticker
    is a licensed dataset (CUSIP Global Services), so this module resolves
    it through OpenFIGI's free public API and caches results locally — see
    resolve_cusips(). Unresolvable CUSIPs are kept as-is rather than dropped
    or guessed at.
  - Coverage accumulates going forward from first ingestion; there is no
    historical backfill here, so quarter-over-quarter comparison only works
    once two quarters have actually been ingested.
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

import httpx

from lib.sec_edgar import ARCHIVES, BASE, _ATOM_NS, _get

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
# OpenFIGI's keyless tier allows 25 requests/minute at up to 10 identifiers per
# request; a free API key raises that to 100 identifiers per request. Batch size
# is chosen per call from whether a key was supplied — a 10x throughput
# difference for CUSIP backfill, which is the slow part of 13F ingestion.
OPENFIGI_BATCH_SIZE = 10
OPENFIGI_BATCH_SIZE_KEYED = 100


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag. 13F documents are filed with
    varying namespace prefixes (some use ns1:, some a default namespace),
    so matching on local names is the only robust approach."""
    return tag.rsplit("}", 1)[-1]


def _find_local(el, name: str):
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text_local(el, name: str) -> str | None:
    found = _find_local(el, name)
    return found.text.strip() if found is not None and found.text else None


def fetch_recent_13f_filings(count: int = 100, client: httpx.Client | None = None) -> list[dict]:
    """Recent 13F-HR filings across all managers, deduped by accession number."""
    url = (
        f"{BASE}/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR"
        f"&company=&dateb=&owner=include&count={count}&output=atom"
    )
    try:
        resp = _get(url, client)
    except httpx.HTTPError as e:
        logger.warning(f"[SEC 13F] Failed to fetch latest-filings feed: {e}")
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning(f"[SEC 13F] Malformed atom feed: {e}")
        return []

    seen = set()
    filings = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        link_el = entry.find("atom:link", _ATOM_NS)
        index_url = link_el.get("href") if link_el is not None else None
        updated_el = entry.find("atom:updated", _ATOM_NS)
        title_el = entry.find("atom:title", _ATOM_NS)
        if not index_url:
            continue
        # Amendments (13F-HR/A) restate a prior period; the feed type filter
        # already excludes them, but a title check keeps this honest if SEC
        # ever changes the filter's behavior.
        title = title_el.text if title_el is not None else ""
        if "/A" in (title or "").split("-")[0]:
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
            "accession": accession, "cik": cik,
            "filed_at": updated_el.text if updated_el is not None else None,
            "index_url": index_url,
        })
    return filings


def fetch_13f_documents(cik: str, accession: str, client: httpx.Client | None = None) -> dict | None:
    """Resolve the primary_doc.xml and infotable.xml URLs for a 13F filing.
    Filenames are conventional but not guaranteed, so this reads the actual
    directory listing rather than assuming."""
    acc_nodash = accession.replace("-", "")
    dir_url = f"{ARCHIVES}/{cik}/{acc_nodash}/index.json"
    try:
        data = _get(dir_url, client).json()
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"[SEC 13F] Directory listing failed for {accession}: {e}")
        return None

    primary = infotable = None
    for item in data.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name.endswith(".xml"):
            continue
        lowered = name.lower()
        if "primary" in lowered or "prim_doc" in lowered:
            primary = name
        elif "index" not in lowered:
            # Anything else ending in .xml that isn't the index wrapper is the
            # information table (filers name it infotable.xml, form13fInfoTable.xml, ...)
            infotable = name
    if not infotable:
        return None
    base = f"{ARCHIVES}/{cik}/{acc_nodash}"
    return {
        "primary_doc_url": f"{base}/{primary}" if primary else None,
        "infotable_url": f"{base}/{infotable}",
    }


def parse_primary_doc(xml_text: str) -> dict | None:
    """Extract the cover-page fields: filing manager name and report period."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug(f"[SEC 13F] Malformed primary_doc: {e}")
        return None

    manager = None
    filing_manager_el = _find_local(root, "filingManager")
    if filing_manager_el is not None:
        manager = _text_local(filing_manager_el, "name")

    period = _text_local(root, "periodOfReport") or _text_local(root, "reportCalendarOrQuarter")
    return {
        "filer_name": manager,
        "period_of_report": _normalize_period(period),
        "is_amendment": (_text_local(root, "isAmendment") or "").lower() == "true",
    }


def _normalize_period(period: str | None) -> str | None:
    """SEC writes the period as MM-DD-YYYY; store ISO so it sorts correctly."""
    if not period:
        return None
    parts = period.split("-")
    if len(parts) == 3 and len(parts[0]) == 2:
        month, day, year = parts
        return f"{year}-{month}-{day}"
    return period


def parse_infotable(xml_text: str) -> list[dict]:
    """Parse the holdings table. `value` is reported in whole US dollars for
    filings using schema X0202+ (older filings reported thousands) — the
    schemaVersion isn't in this document, so callers should treat very small
    values with suspicion rather than this module silently rescaling them."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug(f"[SEC 13F] Malformed infotable: {e}")
        return []

    holdings = []
    for node in root.iter():
        if _local(node.tag) != "infoTable":
            continue
        cusip = _text_local(node, "cusip")
        if not cusip:
            continue

        def _num(name: str) -> float | None:
            raw = _text_local(node, name)
            if raw is None:
                return None
            try:
                return float(raw.replace(",", ""))
            except ValueError:
                return None

        holdings.append({
            "issuer_name": _text_local(node, "nameOfIssuer"),
            "title_of_class": _text_local(node, "titleOfClass"),
            "cusip": cusip.strip().upper(),
            "figi": _text_local(node, "figi"),
            "value_usd": _num("value"),
            "shares": _num("sshPrnamt"),
            "shares_type": _text_local(node, "sshPrnamtType"),
            "investment_discretion": _text_local(node, "investmentDiscretion"),
        })
    return holdings


def resolve_cusips(cusips: list[str], api_key: str | None = None,
                   client: httpx.Client | None = None) -> tuple[dict[str, str], set[str]]:
    """Map CUSIP -> US ticker via OpenFIGI's free public API.

    Returns (resolved, attempted):
      resolved  — CUSIPs that mapped to a US-listed equity ticker.
      attempted — CUSIPs OpenFIGI actually answered for, whether or not they
                  matched. Callers need this to distinguish "asked, genuinely
                  no match" (safe to cache as a permanent NULL) from "never
                  asked because we hit the rate limit and stopped" (must be
                  retried next run). Conflating those two would permanently
                  blacklist CUSIPs that were simply never looked up.

    Prefers the composite US listing (exchCode "US") because 13F reports the
    security, not a specific venue's line.
    """
    if not cusips:
        return {}, set()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    batch_size = OPENFIGI_BATCH_SIZE_KEYED if api_key else OPENFIGI_BATCH_SIZE

    owns_client = client is None
    c = client or httpx.Client(timeout=20.0)
    resolved: dict[str, str] = {}
    attempted: set[str] = set()
    try:
        for start in range(0, len(cusips), batch_size):
            batch = cusips[start:start + batch_size]
            payload = [{"idType": "ID_CUSIP", "idValue": cu} for cu in batch]
            try:
                resp = c.post(OPENFIGI_URL, json=payload, headers=headers)
                if resp.status_code == 429:
                    logger.info("[SEC 13F] OpenFIGI rate limit hit — stopping this pass, will resume next run")
                    break
                resp.raise_for_status()
                rows = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"[SEC 13F] OpenFIGI mapping failed: {e}")
                break

            attempted.update(batch)
            for cusip, row in zip(batch, rows):
                matches = (row or {}).get("data") or []
                if not matches:
                    continue
                preferred = next((m for m in matches if m.get("exchCode") == "US"), matches[0])
                ticker = preferred.get("ticker")
                if ticker:
                    resolved[cusip] = ticker.strip().upper()
    finally:
        if owns_client:
            c.close()
    return resolved, attempted
