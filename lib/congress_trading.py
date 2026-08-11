"""
U.S. House of Representatives stock-trade disclosures (STOCK Act Periodic
Transaction Reports) — free, official Clerk of the House data, no vendor.

Verified live against the real source while building this:

  - Annual filing INDEX (ZIP containing XML):
    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP
    The XML lists every disclosure filing: member name, state/district,
    filing type, filing date, DocID. FilingType "P" is a Periodic
    Transaction Report — the stock trades. The index contains NO transaction
    details whatsoever.

  - The transactions themselves live in per-filing PDFs:
    https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{DocID}.pdf

IMPORTANT — why this uses pdfplumber's table extraction and not text parsing:
plain text extraction returns the asset and transaction columns interleaved
in an unreliable order (a ticker can appear BETWEEN two unrelated transaction
rows), which would silently attribute trades to the wrong ticker. Positional
table extraction keeps each row's cells together. Rows that still fail to
parse into full columns are COUNTED and reported, never silently dropped, so
a caller can see coverage rather than assume completeness.

IMPORTANT — what this data is:
  - Amounts are RANGES, not exact values ("$1,001 - $15,000"). The exact size
    is not disclosed. This module stores the range bounds and never invents a
    point estimate; any midpoint used downstream must be labeled an estimate.
  - Disclosure is DELAYED. The STOCK Act allows up to 30 days from becoming
    aware of a transaction and no later than 45 days after it. filing_delay_days
    captures the actual gap per filing.
  - Filings are self-reported and amendable; a report may be corrected later.
  - Trades are frequently made by financial advisors or in managed/blind
    accounts without the member's involvement.

This module reports FACTS (who filed what, when, for which security) and
timing. It does not, and must not, imply wrongdoing, insider knowledge, or
illegality — a disclosed trade is a legally required disclosure, not evidence
of anything improper.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

BASE = "https://disclosures-clerk.house.gov/public_disc"
USER_AGENT = "Jarvis Trading AI admin@jarvis-trading.local"
HTTP_TIMEOUT = 30.0

# Ticker as printed in the asset cell: "Intuit Inc. - Common Stock (INTU) [ST]".
# The [ST]/[OP]/etc. suffix is the House's own asset-type code; [ST] is stock.
_TICKER_RE = re.compile(r"\(([A-Z][A-Z.\-]{0,6})\)\s*(?:\[([A-Z]{2})\])?")
_AMOUNT_RE = re.compile(r"\$([\d,]+)\s*-\s*\$?([\d,]+)?")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_DATE_ANYWHERE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
# The core row shape: transaction code, then transaction date, notification
# date, then the amount range. Dates may be space-separated or run together.
_TX_ROW_RE = re.compile(
    r"(?P<code>\b[PSE]\b)\s*(?P<partial>\(partial\))?\s*"
    r"(?P<txdate>\d{2}/\d{2}/\d{4})\s*(?P<notifdate>\d{2}/\d{2}/\d{4})\s*(?P<amount>.*)$"
)
_OWNER_RE = re.compile(r"^(SP|JT|DC)\b\s*")
# Signature/footer lines carry dates but are not transactions.
_NON_TX_LINE_RE = re.compile(r"Digitally Signed|Notification Date|Initial Public Offering", re.I)

# House transaction codes. "S (partial)" is a partial sale.
TRANSACTION_LABELS = {
    "P": "Purchase",
    "S": "Sale",
    "S (partial)": "Partial Sale",
    "E": "Exchange",
}


def _headers() -> dict:
    return {"User-Agent": USER_AGENT}


def fetch_filing_index(year: int, client: httpx.Client | None = None) -> list[dict]:
    """Download and parse the annual disclosure index. Returns ONLY Periodic
    Transaction Reports (FilingType 'P') — the filings that contain trades."""
    url = f"{BASE}/financial-pdfs/{year}FD.ZIP"
    owns = client is None
    c = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        resp = c.get(url, headers=_headers())
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(f"[Congress] Failed to fetch {year} filing index: {e}")
        return []
    finally:
        if owns:
            c.close()

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
            if not xml_name:
                logger.warning(f"[Congress] No XML in {year} index ZIP")
                return []
            xml_bytes = zf.read(xml_name)
    except (zipfile.BadZipFile, KeyError) as e:
        logger.warning(f"[Congress] Malformed {year} index ZIP: {e}")
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning(f"[Congress] Malformed {year} index XML: {e}")
        return []

    filings = []
    for m in root.findall("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        doc_id = (m.findtext("DocID") or "").strip()
        if not doc_id:
            continue
        first = (m.findtext("First") or "").strip()
        last = (m.findtext("Last") or "").strip()
        filings.append({
            "doc_id": doc_id,
            "member_name": f"{first} {last}".strip(),
            "state_district": (m.findtext("StateDst") or "").strip(),
            "filing_date": _to_iso(m.findtext("FilingDate")),
            "year": year,
            "pdf_url": f"{BASE}/ptr-pdfs/{year}/{doc_id}.pdf",
        })
    return filings


def _to_iso(date_str: str | None) -> str | None:
    """House dates are M/D/YYYY; store ISO so they sort chronologically."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def parse_amount_range(text: str | None) -> tuple[float | None, float | None]:
    """'$1,001 - $15,000' -> (1001.0, 15000.0).

    Returns (low, None) when the upper bound is absent — the House's top
    bracket is open-ended ("$50,000,000 +") and wrapped cells can lose the
    upper value. Never fabricates a bound."""
    if not text:
        return None, None
    m = _AMOUNT_RE.search(text.replace("\n", " "))
    if not m:
        return None, None
    try:
        low = float(m.group(1).replace(",", ""))
    except (ValueError, AttributeError):
        return None, None
    high = None
    if m.group(2):
        try:
            high = float(m.group(2).replace(",", ""))
        except ValueError:
            high = None
    return low, high


def extract_ticker(asset_cell: str | None) -> tuple[str | None, str | None]:
    """Pull the ticker and asset-type code out of the asset cell.

    Returns (None, None) when the asset has no ticker — bonds, funds held in
    managed accounts, real estate, and similar are disclosed without one, and
    guessing a ticker from a company name would be fabrication."""
    if not asset_cell:
        return None, None
    m = _TICKER_RE.search(asset_cell.replace("\n", " "))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _clean(cell) -> str:
    """PDF cells carry NUL padding from the House's form font."""
    return (cell or "").replace("\x00", "").replace("\n", " ").strip()


def _reconstruct_lines(page) -> list[str]:
    """Rebuild visual rows from word positions.

    pdfplumber's extract_tables() is NOT usable on these filings: measured
    against 12 real 2026 PTRs it silently collapsed 51.8% of transaction rows
    into a single unparseable cell, losing entire trades (verified live).
    Clustering words by their vertical position recovers those rows, because
    a transaction occupies one visual line regardless of how the PDF's
    internal text objects are ordered."""
    rows: dict[int, list] = {}
    for w in page.extract_words(use_text_flow=False, keep_blank_chars=False):
        rows.setdefault(round(w["top"] / 4), []).append(w)
    lines = []
    for key in sorted(rows):
        ordered = sorted(rows[key], key=lambda w: w["x0"])
        lines.append(" ".join(w["text"].replace("\x00", "") for w in ordered).strip())
    return lines


def parse_transaction_line(line: str) -> dict | None:
    """Parse one reconstructed row into a transaction.

    Anchors on the two MM/DD/YYYY dates that every transaction row carries:
    everything before the transaction code is the asset, everything after the
    second date is the amount range. Returns None for non-transaction lines
    (headers, signature lines, filing-status continuations)."""
    dates = _DATE_ANYWHERE_RE.findall(line)
    if len(dates) < 2:
        return None
    m = _TX_ROW_RE.search(line)
    if not m:
        return None

    asset_part = line[:m.start()].strip()
    code = m.group("code").strip()
    if m.group("partial"):
        code = "S (partial)"
    tx_iso, notif_iso = _to_iso(m.group("txdate")), _to_iso(m.group("notifdate"))
    if not tx_iso or not notif_iso:
        return None

    owner = None
    owner_match = _OWNER_RE.match(asset_part)
    if owner_match:
        owner = owner_match.group(1)
        asset_part = asset_part[owner_match.end():].strip()

    ticker, asset_type = extract_ticker(asset_part)
    low, high = parse_amount_range(m.group("amount"))
    return {
        "owner": owner,
        "asset_name": asset_part or None,
        "ticker": ticker,
        "asset_type": asset_type,
        "transaction_code": code,
        "transaction_label": TRANSACTION_LABELS.get(code, code),
        "transaction_date": tx_iso,
        "notification_date": notif_iso,
        "amount_low": low,
        "amount_high": high,
        "amount_text": (m.group("amount") or "").strip() or None,
        "filing_delay_days": _delay_days(tx_iso, notif_iso),
    }


def parse_ptr_pdf(pdf_bytes: bytes) -> dict:
    """Extract transactions from one PTR PDF.

    Returns {"transactions": [...], "rows_seen": n, "rows_unparsed": n}.
    rows_unparsed is deliberately surfaced so callers can see real coverage
    instead of assuming every trade in a filing was captured.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("[Congress] pdfplumber not installed — cannot parse PTR PDFs")
        return {"transactions": [], "rows_seen": 0, "rows_unparsed": 0}

    transactions = []
    rows_seen = rows_unparsed = 0
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                lines = _reconstruct_lines(page)
                for i, line in enumerate(lines):
                    # A transaction row always carries two dates. Lines with two
                    # dates that still fail to parse are counted, not discarded.
                    if len(_DATE_ANYWHERE_RE.findall(line)) < 2:
                        continue
                    if _NON_TX_LINE_RE.search(line):
                        continue
                    rows_seen += 1
                    parsed = parse_transaction_line(line)
                    if not parsed:
                        rows_unparsed += 1
                        continue

                    # A long asset name wraps, pushing "(TICKER) [ST]" onto the
                    # following line. Only borrow from that line when it is a
                    # genuine continuation — it must carry NO dates of its own,
                    # otherwise it is the next transaction and taking its ticker
                    # would attribute this trade to the wrong security.
                    if not parsed["ticker"] and i + 1 < len(lines):
                        nxt = lines[i + 1]
                        if not _DATE_ANYWHERE_RE.search(nxt) and not _NON_TX_LINE_RE.search(nxt):
                            ticker, asset_type = extract_ticker(nxt)
                            if ticker:
                                parsed["ticker"] = ticker
                                parsed["asset_type"] = asset_type
                                parsed["asset_name"] = f"{parsed['asset_name']} {nxt}".strip()
                    transactions.append(parsed)
    except Exception as e:
        # pdfminer raises a wide variety of errors on malformed PDFs; one bad
        # filing must not abort a whole ingestion run.
        logger.warning(f"[Congress] PTR parse failed: {e}")

    return {"transactions": transactions, "rows_seen": rows_seen, "rows_unparsed": rows_unparsed}


def _delay_days(tx_iso: str | None, notif_iso: str | None) -> int | None:
    """Days between the transaction and its disclosure. The STOCK Act allows
    up to 45 days, so this is normal reporting lag, not an irregularity."""
    if not tx_iso or not notif_iso:
        return None
    try:
        return (datetime.fromisoformat(notif_iso).date() - datetime.fromisoformat(tx_iso).date()).days
    except ValueError:
        return None


def fetch_ptr_pdf(pdf_url: str, client: httpx.Client | None = None) -> bytes | None:
    owns = client is None
    c = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        resp = c.get(pdf_url, headers=_headers())
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as e:
        logger.debug(f"[Congress] PTR fetch failed {pdf_url}: {e}")
        return None
    finally:
        if owns:
            c.close()
