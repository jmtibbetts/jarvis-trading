"""Fetch and persist the IPO registration pipeline — free EDGAR data, no
vendor. See lib/ipo_intelligence.py for the source details and the honesty
constraints on what is (and is not) extracted.

Stage handling: a company's row advances filed -> amended -> priced and never
regresses — an S-1/A arriving after a 424B4 (rare, but filers amend) must not
demote a priced deal back to "amended".
"""
import logging
from datetime import datetime, timezone

import httpx

from app.database import IpoFiling, get_db
from lib.ipo_intelligence import (
    FORM_STAGES, fetch_main_document_url, fetch_registration_filings, parse_cover_page,
)
from lib.sec_edgar import _get

logger = logging.getLogger(__name__)

_STAGE_RANK = {"filed": 0, "amended": 1, "priced": 2}
FEED_COUNT = 40
# 424B4 prospectuses run to multiple MB each; cap per-run downloads so one run
# can't burn SEC's fair-access budget. Uncovered filings surface next run.
MAX_COVER_DOWNLOADS_PER_RUN = 10


def run():
    with httpx.Client(timeout=30) as client:
        by_form = {
            form: fetch_registration_filings(form, count=FEED_COUNT, client=client)
            for form in FORM_STAGES
        }

        with get_db() as db:
            existing = {row.cik: row for row in db.query(IpoFiling).all()}
            known_accessions = {row.latest_accession for row in existing.values()}

        now_iso = datetime.now(timezone.utc).isoformat()
        new_rows = updated = priced_extracted = 0
        cover_budget = MAX_COVER_DOWNLOADS_PER_RUN

        # Process in ascending stage order so a company appearing in several
        # feeds in one run lands on its highest stage.
        for form in ("S-1", "S-1/A", "424B4"):
            stage = FORM_STAGES[form]
            for f in by_form[form]:
                if f["accession"] in known_accessions:
                    continue

                cover = {}
                if form == "424B4" and cover_budget > 0:
                    doc_url = fetch_main_document_url(f["cik"], f["accession"], client)
                    if doc_url:
                        cover_budget -= 1
                        try:
                            cover = parse_cover_page(_get(doc_url, client).text)
                            priced_extracted += 1
                        except httpx.HTTPError as e:
                            logger.debug(f"[IPO] Cover fetch failed {f['accession']}: {e}")

                with get_db() as db:
                    row = db.query(IpoFiling).filter(IpoFiling.cik == f["cik"]).first()
                    if row is None:
                        db.add(IpoFiling(
                            cik=f["cik"], company_name=f["company_name"], stage=stage,
                            latest_form=form, latest_accession=f["accession"],
                            first_seen_at=f["filed_at"] or now_iso,
                            latest_filed_at=f["filed_at"],
                            is_likely_spac=f["is_likely_spac"],
                            filing_url=f["index_url"], updated_date=now_iso,
                            **{k: v for k, v in cover.items()},
                        ))
                        new_rows += 1
                    else:
                        # Advance-only: never demote a priced deal.
                        if _STAGE_RANK[stage] >= _STAGE_RANK.get(row.stage, 0):
                            row.stage = stage
                            row.latest_form = form
                        row.latest_accession = f["accession"]
                        row.latest_filed_at = f["filed_at"]
                        row.filing_url = f["index_url"]
                        row.updated_date = now_iso
                        for key, value in cover.items():
                            if value is not None:
                                setattr(row, key, value)
                        updated += 1
                existing[f["cik"]] = True  # noqa: not re-read; only membership matters
                known_accessions.add(f["accession"])

    logger.info(
        f"[IPO] {new_rows} new company(ies), {updated} updated, "
        f"{priced_extracted} cover page(s) extracted"
    )
