"""Raw market evidence is KEPT. Archived if it ever must move — never deleted.

THE FAILURE THIS POLICY EXISTS TO PREVENT.

11,775 historical rejected candidates have no usable forward evidence. Not
because anyone decided those decisions did not matter, but because the
evidence was discarded before anyone thought to ask. Six months later the
question arrived and the answer was gone. A 30-day pruning rule would
reproduce that exact failure on a timer, and it is the default almost
everywhere, which is why it is written down here as a refusal rather than
left as an omission.

WHAT THE MEASUREMENTS SAY (live venue, 2026-08-18).

    active perpetuals              16   (17 discovered, SHIB fails closed)
    provider messages              ~180 /s
    top-of-book changes            ~31 /s   (~115 per product per minute)
    measured SQLite cost           353.3 bytes/row at 300k rows
    index overhead                 66% of table bytes
    insert throughput              ~83,500 rows/s

    => ~2.7M rows/day  ~0.95 GB/day  ~345 GB/year for the perpetual set

Against roughly 8 TB of NVMe and 60 TB of SATA SSD, a year of complete
top-of-book chronology for every US perpetual costs about 4% of one NVMe
drive. Under the operator's decision table that is CASE 1 — under 1 TB/year —
so the default is to KEEP RAW EVIDENCE, currently without a deletion horizon.

WHY THIS IS WORTH MORE THAN THE DISK.

These rows are the only target-product history that has ever existed for
these instruments: no Bitnomial history was in events.db or the OHLCV cache,
so before this there was nothing to replay. Retained, they support
rejected-trade analysis, threshold research, exact forward MFE/MAE, stop and
target chronology, spread and liquidity studies, basis work against spot,
regime analysis and later training sets. Most of those questions have not
been asked yet, which is precisely the argument for keeping the data — the
old system's mistake was assuming the unasked question would never come.

WHAT IS DELIBERATELY NOT BUILT.

No pruning job. No compaction job. No storage-engine migration: SQLite
absorbs the write rate two orders of magnitude over, and every Phase B/C
query runs off one composite index in single-digit milliseconds, so replacing
it now would be rebuilding the database stack instead of capturing evidence.
A warm 1-minute aggregate is worth adding when long-window analytics
actually need it — for query speed, NOT to save space, and NEVER as a
replacement for raw chronology.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Measured inputs, dated so they can be re-measured rather than trusted ──
MEASURED_ON = "2026-08-18"
MEASURED_BYTES_PER_ROW = 353.3
MEASURED_TOB_CHANGES_PER_S = 31.0
MEASURED_ACTIVE_PRODUCTS = 16

# ── The policy ───────────────────────────────────────────────────────────
#
# ARCHIVE > DELETE. If raw rows ever have to leave the active database they
# are copied to a dated archive first, and removal from the active file is a
# separate, explicit act. There is no code path here that deletes evidence as
# a side effect of anything else.
RAW_RETENTION_DAYS = None          # None == keep indefinitely
DELETE_ENABLED = False             # never true without an operator decision

# Only once the active evidence file passes this does compaction become a
# question worth reopening. At the measured rate that is roughly three years.
REVIEW_THRESHOLD_GB = 1000.0


def projection(*, products: int = MEASURED_ACTIVE_PRODUCTS,
               tob_changes_per_s: float = MEASURED_TOB_CHANGES_PER_S,
               bytes_per_row: float = MEASURED_BYTES_PER_ROW) -> dict:
    """Storage from MEASURED rates, not assumed ones.

    The first version of this system assumed 6 samples per product per
    minute. The venue produces roughly 115. Keeping the arithmetic in one
    place, fed by dated measurements, is how that stays visible.
    """
    rows_day = tob_changes_per_s * 86400
    bytes_day = rows_day * bytes_per_row
    gb_year = bytes_day * 365 / 1e9
    if gb_year < 1000:
        case, policy = "CASE_1", "keep raw indefinitely; revisit at review threshold"
    elif gb_year < 5000:
        case, policy = "CASE_2", "keep raw 6-12 months minimum before compaction"
    else:
        case, policy = "CASE_3", "evaluate sampling and warm compaction; still keep substantial raw history"
    return {
        "measured_on": MEASURED_ON,
        "products": products,
        "tob_changes_per_s": tob_changes_per_s,
        "bytes_per_row": bytes_per_row,
        "rows_per_day": int(rows_day),
        "gb_per_day": round(bytes_day / 1e9, 3),
        "gb_per_30d": round(bytes_day * 30 / 1e9, 2),
        "gb_per_year": round(gb_year, 1),
        "storage_case": case,
        "policy": policy,
        "raw_retention_days": RAW_RETENTION_DAYS,
        "delete_enabled": DELETE_ENABLED,
    }


def policy_summary() -> dict:
    """What a future window needs to know before touching this data."""
    p = projection()
    return {
        **p,
        "rule": "ARCHIVE, NEVER DELETE",
        "pruning_job": "deliberately not built",
        "compaction_job": "deliberately not built; warm aggregates are a "
                          "query-speed decision, not a storage one, and never "
                          "replace raw chronology",
        "storage_engine": "SQLite retained — measured 83.5k inserts/s and "
                          "single-digit-ms indexed range queries; a migration "
                          "now would be unmotivated",
    }


def assert_no_delete_path() -> bool:
    """Deletion is off, and turning it on is a deliberate act.

    Kept as a callable so a test can assert the property rather than a
    comment claiming it.
    """
    return DELETE_ENABLED is False and RAW_RETENTION_DAYS is None
