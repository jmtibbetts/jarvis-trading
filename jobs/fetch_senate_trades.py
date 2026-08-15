"""Fetch and persist U.S. Senate STOCK Act disclosures.

The House twin is `jobs/fetch_congress_trades.py` and this deliberately
mirrors it: same table, same idempotency record, same pacing shape. Senate
rows land in `CongressTrade` with `chamber="Senate"`, which means every
panel already built for the House picks them up with no UI change —
"Trades by Official" simply starts containing senators.

Cheaper per filing than the House job (an HTML page, not a PDF download
and parse), so the per-run bound is higher. Paper filings are scanned PDFs
with no parseable table; they are recorded as processed with zero rows so
the job does not re-fetch them forever, and the count is reported.
"""
import logging
from datetime import datetime, timezone

import httpx

from app.database import CongressTrade, ProcessedCongressFiling, get_db, new_id
from lib.senate_trading import (delay_days, fetch_filing_index, fetch_ptr_html,
                                open_session, parse_ptr_html)

logger = logging.getLogger(__name__)

MAX_FILINGS_PER_RUN = 120
NOTABLE_AMOUNT_FLOOR = 250_000


def run(year: int | None = None):
    year = year or datetime.now(timezone.utc).year

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        if not open_session(client):
            logger.warning("[Senate] could not open a search session — skipping run")
            return {"ok": False, "reason": "session"}

        filings = fetch_filing_index(year, client)
        if not filings:
            logger.info(f"[Senate] no {year} PTR filings in index")
            return {"ok": True, "filings": 0}

        with get_db() as db:
            done = {
                row[0] for row in db.query(ProcessedCongressFiling.doc_id)
                .filter(ProcessedCongressFiling.doc_id.in_(
                    [f["doc_id"] for f in filings])).all()
            }

        pending = [f for f in filings if f["doc_id"] not in done]
        if not pending:
            logger.info(f"[Senate] {len(filings)} filings checked, all processed")
            return {"ok": True, "filings": len(filings), "new": 0}

        batch = pending[:MAX_FILINGS_PER_RUN]
        saved = seen = unparsed = paper = 0
        notable = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for f in batch:
            if f["is_paper"]:
                # A scan, not a table. Marked done so it is not retried
                # every run; counted so "coverage" never silently means
                # "everything we could be bothered to read".
                paper += 1
                with get_db() as db:
                    db.add(ProcessedCongressFiling(
                        doc_id=f["doc_id"], member_name=f["member_name"],
                        rows_seen=0, processed_at=now_iso))
                continue

            html = fetch_ptr_html(f["url"], client)
            if html is None:
                # Not recorded — a transient failure should retry next run.
                continue
            result = parse_ptr_html(html)
            seen += result["rows_seen"]
            unparsed += result["rows_unparsed"]

            with get_db() as db:
                for row in result["rows"]:
                    db.add(CongressTrade(
                        id=new_id(), doc_id=f["doc_id"],
                        member_name=f["member_name"],
                        state_district=None, chamber="Senate",
                        owner=row["owner"], asset_name=row["asset_name"],
                        ticker=row["ticker"], asset_type=row["asset_type"],
                        transaction_code=row["transaction_code"],
                        transaction_label=row["transaction_label"],
                        transaction_date=row["transaction_date"],
                        notification_date=f["filing_date"],
                        filing_date=f["filing_date"],
                        filing_delay_days=delay_days(row["transaction_date"],
                                                     f["filing_date"]),
                        amount_low=row["amount_low"],
                        amount_high=row["amount_high"],
                        amount_text=row["amount_text"],
                        pdf_url=f["url"], created_date=now_iso,
                    ))
                    saved += 1
                    if (row["amount_high"] or 0) >= NOTABLE_AMOUNT_FLOOR:
                        notable.append(
                            f"{f['member_name']} {row['transaction_label']} "
                            f"{row['ticker'] or row['asset_name']} "
                            f"{row['amount_text']}")
                db.add(ProcessedCongressFiling(
                    doc_id=f["doc_id"], member_name=f["member_name"],
                    rows_seen=result["rows_seen"], processed_at=now_iso))

    logger.info(f"[Senate] {len(batch)} filings: {saved} trades saved, "
                f"{seen} rows seen, {unparsed} unparsed, {paper} paper-only, "
                f"{len(pending) - len(batch)} queued for next run")
    for n in notable[:10]:
        logger.info(f"[Senate] NOTABLE {n}")
    return {"ok": True, "filings": len(filings), "processed": len(batch),
            "saved": saved, "rows_seen": seen, "unparsed": unparsed,
            "paper_skipped": paper, "queued": len(pending) - len(batch)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
