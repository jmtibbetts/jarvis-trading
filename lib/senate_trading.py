"""U.S. Senate STOCK Act disclosures from the Senate eFD system.

The House half of this lives in `lib/congress_trading.py` and has a harder
job: House PTRs are scanned PDFs. The Senate publishes electronic filings
as an HTML table with exactly the columns that matter — transaction date,
owner, ticker, asset name and type, transaction type, amount range — so
this parser reads structure rather than reconstructing it from text.

Two things about eFD that shape the client:

**A session must be established before any search.** The site gates
search behind a one-time agreement form; accepting it sets a `sessionid`
cookie that every later request needs. `open_session()` does that once per
httpx.Client and the job reuses one client for its whole run.

**The agreement quotes 5 U.S.C. app. § 105(c).** Obtaining or using a
disclosure report is unlawful for any unlawful purpose, for any commercial
purpose other than by news media, to establish a credit rating, or in
soliciting money. That is a restriction on USE, not on access, and it
governs the House data in `congress_trading.py` identically. It is
recorded here because the next person to read this file should not have to
go and find it.

Same honesty caveats as the House side: amounts are RANGES, never exact;
filings arrive weeks after the trade; and a disclosure is a legal
obligation, not evidence of anything.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

BASE = "https://efdsearch.senate.gov"
USER_AGENT = "Jarvis Trading AI admin@jarvis-trading.local"
HTTP_TIMEOUT = 30.0

# Report type 11 is the Periodic Transaction Report — the trade disclosure.
# Annual reports are a different filing and are not trades.
PTR_REPORT_TYPE = "[11]"

_CSRF_RE = re.compile(
    r"name=['\"]csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)")
_LINK_RE = re.compile(r"href=['\"]([^'\"]+)['\"]")
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# "Purchase" / "Sale (Full)" / "Sale (Partial)" / "Exchange" -> House codes,
# so both chambers land in one column that means the same thing.
TRANSACTION_CODES = {
    "purchase": ("P", "Purchase"),
    "sale (full)": ("S", "Sale"),
    "sale (partial)": ("S", "Partial Sale"),
    "sale": ("S", "Sale"),
    "exchange": ("E", "Exchange"),
}


def _headers() -> dict:
    return {"User-Agent": USER_AGENT}


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html).replace("&nbsp;", " ")
                  .replace("&amp;", "&")).strip()


def open_session(client: httpx.Client) -> bool:
    """Accept the search agreement so the client carries a valid session.

    Returns False rather than raising: a disclosure feed being unreachable
    must not take down the job that calls it.
    """
    try:
        r = client.get(f"{BASE}/search/home/", headers=_headers())
        m = _CSRF_RE.search(r.text)
        if not m:
            logger.warning("[Senate] no CSRF token on the agreement page")
            return False
        client.post(f"{BASE}/search/home/",
                    data={"prohibition_agreement": "1",
                          "csrfmiddlewaretoken": m.group(1)},
                    headers={**_headers(), "Referer": f"{BASE}/search/home/"})
        ok = "sessionid" in client.cookies
        if not ok:
            logger.warning("[Senate] agreement accepted but no sessionid set")
        return ok
    except Exception as e:
        logger.warning(f"[Senate] session setup failed: {e}")
        return False


def fetch_filing_index(year: int, client: httpx.Client,
                       limit: int = 400) -> list[dict]:
    """Every PTR filed since 1 January `year`, newest first.

    Each entry carries the report UUID as `doc_id`, which is what makes a
    filing processable exactly once.
    """
    token = client.cookies.get("csrftoken")
    if not token:
        logger.warning("[Senate] no csrftoken cookie — session not open")
        return []

    # The endpoint caps a page at 100 REGARDLESS of the requested length —
    # asking for 400 returns 100 and a recordsTotal of 114, so a single
    # request looks complete and silently drops the tail. Paged on `start`
    # until the reported total is covered.
    rows: list = []
    total = None
    start = 0
    while start < limit:
        try:
            r = client.post(
                f"{BASE}/search/report/data/",
                data={"start": str(start), "length": "100",
                      "report_types": PTR_REPORT_TYPE, "filer_types": "[]",
                      "submitted_start_date": f"01/01/{year} 00:00:00",
                      "submitted_end_date": "", "candidate_state": "",
                      "senator_state": "", "office_id": "",
                      "first_name": "", "last_name": "",
                      "csrfmiddlewaretoken": token},
                headers={**_headers(), "Referer": f"{BASE}/search/home/",
                         "X-Requested-With": "XMLHttpRequest"})
            payload = r.json()
        except Exception as e:
            logger.warning(f"[Senate] index page at {start} failed: {e}")
            break
        page = payload.get("data") or []
        if total is None:
            total = payload.get("recordsTotal")
        rows.extend(page)
        if len(page) < 100 or (total is not None and len(rows) >= total):
            break
        start += 100

    if total is not None and len(rows) < total:
        logger.warning(f"[Senate] index returned {len(rows)} of {total} "
                       f"filings — coverage is short")

    out = []
    for row in rows:
        # [first, last, display_name, link_html, filed_date]
        if len(row) < 5:
            continue
        link = _LINK_RE.search(row[3] or "")
        if not link:
            continue
        href = link.group(1)
        uid = href.rstrip("/").rsplit("/", 1)[-1]
        # Paper filings are scanned PDFs under /search/view/paper/ and carry
        # no parseable table. Recorded as skipped rather than silently
        # dropped, so coverage numbers stay honest.
        out.append({
            "doc_id": uid,
            "member_name": _text(row[2] or "").replace(" (Senator)", ""),
            "filing_date": _to_iso(row[4]),
            "url": f"{BASE}{href}" if href.startswith("/") else href,
            "is_paper": "/paper/" in href,
        })
    return out[:limit]


def _to_iso(date_str: str | None) -> str | None:
    if not date_str:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(date_str).strip(), fmt).replace(
                tzinfo=timezone.utc).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount_range(text: str | None) -> tuple[float | None, float | None]:
    """'$50,001 - $100,000' -> (50001.0, 100000.0). Shared shape with the
    House parser so both chambers produce identical columns."""
    if not text:
        return None, None
    nums = re.findall(r"\$\s*([\d,]+)", str(text))
    if not nums:
        return None, None
    lo = float(nums[0].replace(",", ""))
    hi = float(nums[1].replace(",", "")) if len(nums) > 1 else None
    return lo, hi


def parse_ptr_html(html: str) -> dict:
    """Transactions from one PTR report page.

    Returns rows plus the counts needed to say how much of the filing was
    understood — a parser that quietly drops what it cannot read reports
    the same success as one that read everything.
    """
    rows, seen, unparsed = [], 0, 0
    for tr in _TR_RE.findall(html):
        cells = [_text(c) for c in _TD_RE.findall(tr)]
        # #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type,
        # Amount, Comment
        if len(cells) < 8:
            continue
        seen += 1
        tx_date = _to_iso(cells[1])
        if not tx_date:
            unparsed += 1
            continue
        raw_type = (cells[6] or "").strip().lower()
        code, label = TRANSACTION_CODES.get(raw_type, (None, cells[6] or None))
        if code is None:
            unparsed += 1
            continue
        ticker = (cells[3] or "").strip()
        if ticker in ("--", "-", ""):
            ticker = None
        lo, hi = parse_amount_range(cells[7])
        rows.append({
            "transaction_date": tx_date,
            "owner": (cells[2] or "").strip() or None,
            "ticker": ticker.upper() if ticker else None,
            # The asset cell carries coupon/maturity on its own lines for
            # bonds; keep the first line, which is the instrument name.
            "asset_name": (cells[4] or "").split("  ")[0].strip() or None,
            "asset_type": (cells[5] or "").strip() or None,
            "transaction_code": code,
            "transaction_label": label,
            "amount_low": lo,
            "amount_high": hi,
            "amount_text": (cells[7] or "").strip() or None,
        })
    return {"rows": rows, "rows_seen": seen, "rows_unparsed": unparsed}


def fetch_ptr_html(url: str, client: httpx.Client) -> str | None:
    try:
        r = client.get(url, headers={**_headers(), "Referer": f"{BASE}/search/home/"})
        if r.status_code != 200:
            logger.debug(f"[Senate] {url} -> {r.status_code}")
            return None
        return r.text
    except Exception as e:
        logger.debug(f"[Senate] {url} failed: {e}")
        return None


def delay_days(tx_iso: str | None, filing_iso: str | None) -> int | None:
    """How long the disclosure took. The STOCK Act allows 30–45 days, and
    the gap is the single most informative number in the filing."""
    if not tx_iso or not filing_iso:
        return None
    try:
        return (datetime.fromisoformat(filing_iso)
                - datetime.fromisoformat(tx_iso)).days
    except ValueError:
        return None
