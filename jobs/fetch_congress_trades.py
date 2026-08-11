"""Fetch and persist U.S. House STOCK Act trade disclosures — free Clerk of
the House data, no vendor. See lib/congress_trading.py for source details
and the honesty caveats around ranges, delays, and what the data does and
does not mean.

Each PTR filing is a separate PDF, so the job is paced: it processes a bounded
number of NEW filings per run and records every one it handles, meaning a
filing's PDF is downloaded exactly once. Coverage builds across runs.
"""
import logging
from datetime import datetime, timezone

import httpx

from app.database import CongressTrade, ProcessedCongressFiling, get_db
from lib.congress_trading import fetch_filing_index, fetch_ptr_pdf, parse_ptr_pdf

logger = logging.getLogger(__name__)

# Bounded per run: each filing is a PDF download plus parse, and the House
# publishes hundreds per year. Unprocessed filings are simply picked up next run.
MAX_FILINGS_PER_RUN = 75
# Alert threshold — the top of the disclosed range, since the exact amount is
# never known. Deliberately high so this flags only genuinely large disclosures.
NOTABLE_AMOUNT_FLOOR = 250_000


def run(year: int | None = None):
    year = year or datetime.now(timezone.utc).year

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        filings = fetch_filing_index(year, client)
        if not filings:
            logger.info(f"[Congress] No {year} PTR filings in index")
            return

        with get_db() as db:
            done = {
                row[0] for row in db.query(ProcessedCongressFiling.doc_id)
                .filter(ProcessedCongressFiling.doc_id.in_([f["doc_id"] for f in filings])).all()
            }

        pending = [f for f in filings if f["doc_id"] not in done]
        if not pending:
            logger.info(f"[Congress] {len(filings)} filings checked, all already processed")
            return

        batch = pending[:MAX_FILINGS_PER_RUN]
        saved = seen = unparsed = 0
        notable = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for f in batch:
            pdf = fetch_ptr_pdf(f["pdf_url"], client)
            if pdf is None:
                # Not recorded as processed — a transient fetch failure should retry.
                continue
            result = parse_ptr_pdf(pdf)
            seen += result["rows_seen"]
            unparsed += result["rows_unparsed"]

            with get_db() as db:
                for tx in result["transactions"]:
                    db.add(CongressTrade(
                        doc_id=f["doc_id"], member_name=f["member_name"],
                        state_district=f["state_district"], chamber="House",
                        owner=tx["owner"], asset_name=tx["asset_name"],
                        ticker=tx["ticker"], asset_type=tx["asset_type"],
                        transaction_code=tx["transaction_code"],
                        transaction_label=tx["transaction_label"],
                        transaction_date=tx["transaction_date"],
                        notification_date=tx["notification_date"],
                        filing_date=f["filing_date"], filing_delay_days=tx["filing_delay_days"],
                        amount_low=tx["amount_low"], amount_high=tx["amount_high"],
                        amount_text=tx["amount_text"], pdf_url=f["pdf_url"],
                        created_date=now_iso,
                    ))
                    saved += 1
                    if tx["ticker"] and (tx["amount_high"] or 0) >= NOTABLE_AMOUNT_FLOOR:
                        notable.append((f["member_name"], tx["ticker"], tx["transaction_label"], tx["amount_text"]))

                db.merge(ProcessedCongressFiling(
                    doc_id=f["doc_id"], member_name=f["member_name"],
                    rows_seen=result["rows_seen"], rows_unparsed=result["rows_unparsed"],
                    transactions_saved=len(result["transactions"]), processed_at=now_iso,
                ))

    remaining = len(pending) - len(batch)
    logger.info(
        f"[Congress] {saved} trade(s) saved from {len(batch)} filing(s); "
        f"{seen} rows seen, {unparsed} unparsed; {remaining} filing(s) queued for next run"
    )

    for member, ticker, label, amount in notable[:3]:
        try:
            from lib.alert_engine import raise_alert
            raise_alert(
                source="congress",
                severity="WATCH",
                title=f"Large disclosed trade: {ticker}",
                detail=(
                    f"{member} disclosed a {label} of {ticker} ({amount}). "
                    f"Legally required STOCK Act disclosure, reported with statutory delay."
                ),
                dedup_key=f"congress:{member}:{ticker}:{label}",
                extra={"ticker": ticker, "member": member},
            )
        except Exception as e:
            logger.debug(f"[Congress] Alert failed: {e}")
