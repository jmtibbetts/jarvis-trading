"""Fetch and persist SEC Form 4 insider transactions — free EDGAR API, no
paid vendor. See lib/sec_edgar.py for the endpoint details."""
import logging
from datetime import datetime, timezone

import httpx

from app.database import InsiderTransaction, get_db
from lib.sec_edgar import _get, fetch_form4_xml_url, fetch_recent_form4_filings, parse_form4_xml

logger = logging.getLogger(__name__)

# The "getcurrent" atom feed only ever returns the most recent filings (there's
# no deep pagination) — 100 is enough headroom above this job's run interval
# that a normal filing rate won't outrun it between runs.
FETCH_COUNT = 100
# Floor for the WS "notable buy" push — avoids pinging the UI for a $500
# purchase, this is meant for buys big enough to actually mean something.
NOTABLE_BUY_FLOOR = 50_000


def run():
    filings = fetch_recent_form4_filings(count=FETCH_COUNT)
    if not filings:
        logger.info("[Insider] No filings returned from EDGAR feed")
        return

    with get_db() as db:
        existing = {
            row[0] for row in db.query(InsiderTransaction.accession_number)
            .filter(InsiderTransaction.accession_number.in_([f["accession"] for f in filings]))
            .distinct().all()
        }

    new_filings = [f for f in filings if f["accession"] not in existing]
    if not new_filings:
        logger.info(f"[Insider] {len(filings)} filings checked, all already ingested")
        return

    saved = 0
    notable = []
    with httpx.Client(timeout=15) as client:
        for f in new_filings:
            xml_url = fetch_form4_xml_url(f["cik"], f["accession"], client)
            if not xml_url:
                continue
            try:
                xml_text = _get(xml_url, client).text
            except httpx.HTTPError as e:
                logger.debug(f"[Insider] Fetch failed for {f['accession']}: {e}")
                continue
            parsed = parse_form4_xml(xml_text)
            if not parsed:
                continue

            now_iso = datetime.now(timezone.utc).isoformat()
            with get_db() as db:
                for tx in parsed["transactions"]:
                    db.add(InsiderTransaction(
                        accession_number=f["accession"],
                        issuer_cik=parsed["issuer_cik"], issuer_name=parsed["issuer_name"],
                        ticker=parsed["ticker"],
                        owner_cik=parsed["owner_cik"], owner_name=parsed["owner_name"],
                        owner_title=parsed["owner_title"],
                        is_director=parsed["is_director"], is_officer=parsed["is_officer"],
                        is_ten_pct_owner=parsed["is_ten_pct_owner"],
                        security_title=tx["security_title"], table=tx["table"],
                        transaction_date=tx["transaction_date"], transaction_code=tx["transaction_code"],
                        transaction_label=tx["transaction_label"], acquired_disposed=tx["acquired_disposed"],
                        shares=tx["shares"], price_per_share=tx["price_per_share"],
                        total_value=tx["total_value"], shares_owned_after=tx["shares_owned_after"],
                        filing_url=f["index_url"], filed_at=f["filed_at"], created_date=now_iso,
                    ))
                    saved += 1
                    if tx["transaction_code"] == "P" and (tx.get("total_value") or 0) >= NOTABLE_BUY_FLOOR:
                        notable.append((parsed["ticker"], parsed["owner_name"], tx["total_value"]))

    logger.info(f"[Insider] {saved} transaction(s) saved from {len(new_filings)} new filing(s)")

    if notable:
        try:
            from app.ws import manager as ws_manager
            top = max(notable, key=lambda n: n[2])
            ws_manager.broadcast_from_thread("insider_buy", {
                "ticker": top[0], "owner_name": top[1],
                "value": round(top[2], 0), "count": len(notable),
            })
        except Exception:
            pass
