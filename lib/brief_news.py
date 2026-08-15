"""The brief's news surface — grouped by what it could move, not by who published it.

16,657 items were already being ingested every 15 minutes from ~50 curated
feeds plus GDELT, with source URLs, categories, sentiment and an
`affected_assets` list. None of it appeared on the Morning Brief. The
ingestion was never the gap; the surface was.

Two decisions shape this module:

**Grouping is by consequence, not by publisher.** "Bloomberg Markets" is
not a reason to read something. Feed categories are a taxonomy of
newsrooms; an operator wants "defense and conflict", "policy and leaders",
"chips and AI" — the buckets a position actually lives or dies in. The map
below is that translation, and it is deliberately lossy.

**The first bucket is the operator's own book.** An article naming a
symbol that is open right now, or on the watchlist, outranks anything
else no matter how large the headline. That intersection is the whole
difference between a news reader and a desk tool.

What this module does NOT do: predict. An item is tagged with the
instruments it plausibly bears on and its source's sentiment, and that is
where it stops. §26's rule for exchange flow applies here too — a
hypothesis is offered as a hypothesis, and the operator draws the line.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Feed category -> the bucket an operator reads in. Several categories
# collapse: "politics", "social_politics" and "geopolitics" are one
# question when you hold a position, which is whether policy is moving.
BUCKETS: list[tuple[str, str, tuple[str, ...]]] = [
    ("defense", "Defense & Conflict", ("conflict", "defense")),
    ("policy", "Policy & Leaders", ("geopolitics", "politics", "social_politics")),
    ("chips", "Chips, AI & Data Centers",
     ("semiconductors", "ai_infrastructure", "data_centers", "tech")),
    ("energy", "Energy & Supply Chain", ("energy", "supply_chain")),
    ("crypto", "Crypto", ("crypto",)),
    ("markets", "Markets", ("finance",)),
]

_CATEGORY_TO_BUCKET = {c: key for key, _, cats in BUCKETS for c in cats}
_BUCKET_LABEL = {key: label for key, label, _ in BUCKETS}


def _parse(ts) -> datetime | None:
    """`published_at` carries MIXED formats and both must parse.

    RSS hands over RFC-2822 ('Wed, 29 Apr 2026 23:58:07 +0000' and the
    'GMT' variant); other writers store ISO. Parsing only ISO returned None
    for the RFC rows, which is why every bucket reported a newest age of
    None on first run.
    """
    if not ts:
        return None
    raw = str(ts)
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_text(dt: datetime | None, now: datetime) -> str | None:
    if dt is None:
        return None
    mins = max(0, int((now - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m ago"
    if mins < 1440:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def _assets(raw) -> list[str]:
    """`affected_assets` is a COMMA-SEPARATED STRING — 'NVDA,AVGO'.

    It is not JSON, despite looking like a list field. Parsing it as JSON
    threw on every populated row and the except returned [], so the
    your-book intersection matched nothing across 7,009 tagged articles.
    JSON is still accepted in case a writer ever changes.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(a).strip().upper() for a in raw if str(a).strip()]
    text = str(raw).strip()
    if text.startswith("["):
        try:
            v = json.loads(text)
            if isinstance(v, list):
                return [str(a).strip().upper() for a in v if str(a).strip()]
        except ValueError:
            pass
    return [p.strip().upper() for p in text.split(",") if p.strip()]


def _book_symbols(db) -> set[str]:
    """Everything the desk currently has skin in, or is watching.

    Both books plus the watchlist, because a headline about a symbol you
    are ABOUT to trade matters as much as one you already hold. Matching
    is on the bare root (BTC from BTC/USD) as well as the full ticker, so
    an article tagged 'BTC' still reaches a BTC/USD position.
    """
    out: set[str] = set()
    try:
        from app.database import (AutoSimPosition, MarketAsset, PaperPosition,
                                  TradingSignal)
        from sqlalchemy import func
        for model, col in ((PaperPosition, "symbol"), (AutoSimPosition, "symbol")):
            for (sym,) in db.query(getattr(model, col)).filter(
                    func.lower(model.status) == "open").all():
                if sym:
                    out.add(str(sym).upper())
        for (sym,) in db.query(MarketAsset.symbol).all():
            if sym:
                out.add(str(sym).upper())
        for (sym,) in db.query(TradingSignal.asset_symbol).filter(
                func.lower(TradingSignal.status) == "active").all():
            if sym:
                out.add(str(sym).upper())
    except Exception as e:
        logger.debug(f"[BriefNews] book symbols unavailable (non-fatal): {e}")
    # Roots too: BTC/USD -> BTC, so a 'BTC'-tagged article still matches.
    return out | {s.split("/")[0] for s in out if "/" in s}


def _dedupe(items: list[dict]) -> list[dict]:
    """One story per headline, keeping the first (most recent) sighting.

    Normalization is deliberately mild — case, punctuation and whitespace
    only. Anything cleverer starts merging genuinely different stories that
    share an opening clause, and a brief that hides a real second event to
    look tidy is worse than one that shows a near-duplicate.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = "".join(ch for ch in str(it.get("title") or "").lower()
                      if ch.isalnum() or ch == " ").strip()
        key = " ".join(key.split())[:90]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _item(row, now, book: set[str]) -> dict:
    pub = _parse(getattr(row, "published_at", None))
    assets = _assets(getattr(row, "affected_assets", None))
    hits = sorted({a for a in assets if a in book or a.split("/")[0] in book})
    return {
        "id": row.id,
        "title": row.title,
        "url": getattr(row, "url", None),
        "source": getattr(row, "source", None),
        "category": getattr(row, "category", None),
        "sentiment": getattr(row, "sentiment", None),
        "published_at": getattr(row, "published_at", None),
        "age": _age_text(pub, now),
        "affected_assets": assets[:8],
        # The instruments YOU hold or watch that this names. Empty is the
        # common case and is the honest answer.
        "book_hits": hits,
    }


def brief_news(hours: int = 24, per_bucket: int = 6,
               book_limit: int = 8) -> dict:
    """News for the Morning Brief, bucketed by consequence.

    Returns `buckets` in reading order plus a `your_book` section that
    outranks all of them, and the freshness numbers §30 requires: how old
    the newest item is and how many were considered.
    """
    from app.database import NewsItem, get_db

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()

    with get_db() as db:
        book = _book_symbols(db)
        # Filtered and ordered on created_date, NOT published_at. Both name
        # a time, but only created_date is consistently ISO — published_at
        # mixes ISO and RFC-2822, and SQLite compares them as TEXT, so
        # `published_at >= <iso cutoff>` sorted 'Wed, 29 Apr...' above
        # everything and the 24-hour window silently returned months of
        # archive. created_date is ingestion time, which for a 15-minute
        # poll is within minutes of publication anyway.
        rows = (db.query(NewsItem)
                .filter(NewsItem.created_date >= cutoff)
                .order_by(NewsItem.created_date.desc())
                .limit(1200).all())
        items = [_item(r, now, book) for r in rows]

    # The same story arrives from several feeds, and twice from one feed
    # when a headline is edited. Dedup on the normalized title so a bucket
    # of six is six stories rather than three shown twice.
    items = _dedupe(items)

    # The book section first, and it is allowed to take from any bucket —
    # a chip export control that names a semi you are long belongs at the
    # top of the page, not filed under "Chips".
    your_book = [i for i in items if i["book_hits"]][:book_limit]
    seen = {i["id"] for i in your_book}

    buckets = []
    for key, label, _cats in BUCKETS:
        picked = [i for i in items
                  if i["id"] not in seen
                  and _CATEGORY_TO_BUCKET.get(i["category"]) == key][:per_bucket]
        if not picked:
            continue
        seen.update(i["id"] for i in picked)
        newest = _parse(picked[0]["published_at"])
        buckets.append({
            "key": key, "label": label, "items": picked,
            "newest_age": _age_text(newest, now),
        })

    newest_overall = _parse(items[0]["published_at"]) if items else None
    return {
        "window_hours": hours,
        "considered": len(items),
        "your_book": your_book,
        "book_symbols_watched": len(book),
        "buckets": buckets,
        "newest_age": _age_text(newest_overall, now),
        "as_of": now.isoformat(),
        "note": ("Grouped by what an item could plausibly move, not by "
                 "publisher. Asset tags are hypotheses drawn from the "
                 "article, never predictions."),
    }
