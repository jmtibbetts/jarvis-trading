"""Fetch and persist Form 13F-HR institutional holdings — free EDGAR data,
no paid vendor. See lib/sec_13f.py for endpoint details and the honesty
caveats about what 13F data actually represents.

Scope control matters here: ~5,000+ managers file each quarter, some with
thousands of holdings, which would be millions of rows. This job only
PERSISTS holdings whose resolved ticker is in the app's tracked universe
(market_assets), so the table stays proportional to what the app can
actually act on. Unresolved and untracked holdings are counted in the log
but not stored.
"""
import logging
import os
from datetime import datetime, timezone

import httpx

from app.database import (
    CusipTickerMap, InstitutionalHolding, MarketAsset, Processed13FFiling, get_db,
)
from lib.sec_13f import (
    fetch_13f_documents, fetch_recent_13f_filings, parse_infotable,
    parse_primary_doc, resolve_cusips,
)
from lib.sec_edgar import _get

logger = logging.getLogger(__name__)

FETCH_COUNT = 40
# Cap CUSIP resolutions per run: OpenFIGI's keyless tier is 25 req/min at 10
# identifiers each. Unresolved CUSIPs simply carry over to the next run
# rather than being dropped, so coverage fills in over successive passes.
MAX_NEW_CUSIPS_PER_RUN = 200


def _tracked_tickers(db) -> set[str]:
    return {
        row[0].upper() for row in db.query(MarketAsset.symbol).all()
        if row[0] and "/" not in row[0]  # crypto pairs aren't 13F-reportable
    }


def run():
    filings = fetch_recent_13f_filings(count=FETCH_COUNT)
    if not filings:
        logger.info("[13F] No filings returned from EDGAR feed")
        return

    with get_db() as db:
        # Only skip filings we finished — see Processed13FFiling's docstring for
        # why "has saved holdings" is not a safe completion signal.
        done = {
            row[0] for row in db.query(Processed13FFiling.accession_number)
            .filter(Processed13FFiling.fully_resolved.is_(True)).all()
        }
        tracked = _tracked_tickers(db)
        cusip_cache = {row.cusip: row.resolved_ticker for row in db.query(CusipTickerMap).all()}

    new_filings = [f for f in filings if f["accession"] not in done]
    if not new_filings:
        logger.info(f"[13F] {len(filings)} filings checked, all already fully processed")
        return

    parsed_filings = []
    all_cusips: set[str] = set()
    with httpx.Client(timeout=25) as client:
        for f in new_filings:
            docs = fetch_13f_documents(f["cik"], f["accession"], client)
            if not docs:
                continue
            cover = {}
            if docs["primary_doc_url"]:
                try:
                    cover = parse_primary_doc(_get(docs["primary_doc_url"], client).text) or {}
                except httpx.HTTPError as e:
                    logger.debug(f"[13F] Cover page fetch failed for {f['accession']}: {e}")
            try:
                holdings = parse_infotable(_get(docs["infotable_url"], client).text)
            except httpx.HTTPError as e:
                logger.debug(f"[13F] Infotable fetch failed for {f['accession']}: {e}")
                continue
            if not holdings:
                continue
            parsed_filings.append({"filing": f, "cover": cover, "holdings": holdings})
            all_cusips.update(h["cusip"] for h in holdings)

        unknown = [c for c in all_cusips if c not in cusip_cache][:MAX_NEW_CUSIPS_PER_RUN]
        newly_resolved, attempted = (
            resolve_cusips(unknown, api_key=os.getenv("OPENFIGI_API_KEY"), client=client)
            if unknown else ({}, set())
        )

    if attempted:
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            # Only cache CUSIPs OpenFIGI actually answered for. A NULL here means
            # "asked, no US equity match" — CUSIPs skipped due to a rate limit are
            # left uncached so the next run retries them.
            for cusip in attempted:
                db.merge(CusipTickerMap(
                    cusip=cusip, resolved_ticker=newly_resolved.get(cusip), resolved_at=now_iso,
                ))
        cusip_cache.update({c: newly_resolved.get(c) for c in attempted})

    saved = untracked = unresolved = complete = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        for entry in parsed_filings:
            f, cover = entry["filing"], entry["cover"]
            already_saved = {
                row[0] for row in db.query(InstitutionalHolding.cusip)
                .filter(InstitutionalHolding.accession_number == f["accession"]).all()
            }
            filing_unresolved = 0
            for h in entry["holdings"]:
                ticker = cusip_cache.get(h["cusip"])
                if not ticker:
                    # cusip_cache holds None for "asked, no US equity match" — those are
                    # settled, not pending. Only genuinely un-looked-up CUSIPs block completion.
                    if h["cusip"] not in cusip_cache:
                        filing_unresolved += 1
                    unresolved += 1
                    continue
                if ticker not in tracked:
                    untracked += 1
                    continue
                if h["cusip"] in already_saved:
                    continue  # reprocessed filing — don't duplicate rows already stored
                db.add(InstitutionalHolding(
                    accession_number=f["accession"], filer_cik=f["cik"],
                    filer_name=cover.get("filer_name"), period_of_report=cover.get("period_of_report"),
                    cusip=h["cusip"], ticker=ticker, issuer_name=h["issuer_name"],
                    title_of_class=h["title_of_class"], value_usd=h["value_usd"],
                    shares=h["shares"], shares_type=h["shares_type"],
                    filed_at=f["filed_at"], created_date=now_iso,
                ))
                saved += 1

            if filing_unresolved == 0:
                complete += 1
            db.merge(Processed13FFiling(
                accession_number=f["accession"], filer_cik=f["cik"],
                fully_resolved=(filing_unresolved == 0),
                unresolved_count=filing_unresolved, processed_at=now_iso,
            ))

    logger.info(
        f"[13F] {saved} holding(s) saved from {len(parsed_filings)} filing(s); "
        f"{complete} fully resolved, {len(parsed_filings) - complete} will be reprocessed "
        f"({untracked} untracked tickers, {unresolved} unresolved CUSIPs, "
        f"{len(newly_resolved)}/{len(attempted)} newly mapped this run)"
    )
