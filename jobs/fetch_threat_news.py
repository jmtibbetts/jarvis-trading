"""Fetch, classify, and persist market news and threat intelligence."""

import calendar
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from app.database import (
    IntelligenceIngestionRun, IntelligenceSourceHealth, NewsItem, ThreatEvent, get_db,
)
from lib.geo_lookup import resolve_coords


def _coerce_scalar_str(value, default: str = "") -> str:
    """The LLM classifier's schema asks for a single string (e.g. "country":
    "Iran"), but a multi-country/multi-region story can make it return a JSON
    array instead (e.g. ["Iran", "China", "United States"]) — SQLite can't
    bind a Python list to a TEXT column and the whole batch insert fails.
    Coerce anything list/tuple-shaped into a joined string instead of passing
    the raw LLM type straight through to the DB layer."""
    if value is None:
        return default
    if isinstance(value, (list, tuple, set)):
        joined = ", ".join(str(v) for v in value if v)
        return joined or default
    return str(value)
from lib.intelligence_quality import canonicalize_url, enrich_articles, source_reliability
from lib.intelligence_sources import CATEGORY_ORDER, GDELT_QUERY, RSS_FEEDS
from lib.lmstudio import call_lm_studio, parse_json

logger = logging.getLogger(__name__)

BATCH_SIZE = max(1, int(os.getenv("NEWS_LLM_BATCH_SIZE", "8")))
MAX_ARTICLES = max(10, int(os.getenv("NEWS_MAX_ARTICLES", "120")))
ITEMS_PER_SOURCE = max(1, int(os.getenv("NEWS_ITEMS_PER_SOURCE", "8")))
DEDUP_DAYS = max(1, int(os.getenv("NEWS_DEDUP_DAYS", "7")))
HTTP_TIMEOUT = max(3.0, float(os.getenv("NEWS_HTTP_TIMEOUT", "12")))
USER_AGENT = os.getenv("NEWS_USER_AGENT", "JarvisTradingAI/6.10 intelligence-ingestion")

THREAT_WORDS = {
    "airstrike", "attack", "bombing", "ceasefire", "conflict", "coup",
    "cyberattack", "earthquake", "embargo", "explosion", "fire", "invasion",
    "missile", "outage", "ransomware", "sanction", "shutdown", "strike",
    "tariff", "terror", "war",
}
NEGATIVE_WORDS = THREAT_WORDS | {
    "ban", "bankruptcy", "breach", "collapse", "crash", "delay", "layoff",
    "recall", "shortage", "slump", "warning",
}
POSITIVE_WORDS = {
    "approval", "breakthrough", "expansion", "growth", "investment",
    "partnership", "record", "recovery", "upgrade",
}

ASSET_KEYWORDS = {
    "nvidia": "NVDA", "gpu": "NVDA", "amd": "AMD", "intel": "INTC",
    "tsmc": "TSM", "asml": "ASML", "broadcom": "AVGO", "micron": "MU",
    "super micro": "SMCI", "arista": "ANET", "vertiv": "VRT",
    "applied materials": "AMAT", "lam research": "LRCX", "kla": "KLAC",
    "bitcoin": "BTC/USD", "ethereum": "ETH/USD", "solana": "SOL/USD",
    "oil": "CL", "crude": "CL", "natural gas": "NG", "gold": "GC",
    "dollar": "DXY", "treasury": "TLT", "s&p": "SPY", "nasdaq": "QQQ",
}

def _clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _csv_env(name: str, default: str = "") -> list[str]:
    return [value.strip().lstrip("@") for value in os.getenv(name, default).split(",") if value.strip()]


def _published_iso(entry: dict) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc).isoformat()
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).isoformat()


def parse_feed_content(content: bytes, feed: dict) -> list[dict]:
    parsed = feedparser.parse(content)
    articles = []
    for entry in parsed.entries[:ITEMS_PER_SOURCE]:
        title = _clean_html(entry.get("title", ""))
        if not title:
            continue
        summary = entry.get("summary") or entry.get("description") or ""
        articles.append({
            "title": title,
            "summary": _clean_html(summary)[:600],
            "source": feed["source"],
            "source_kind": feed.get("kind", "rss"),
            "url": entry.get("link", ""),
            "category": feed["category"],
            "published": _published_iso(entry),
            "provider": "rss",
        })
    return articles


def fetch_feed(feed: dict) -> list[dict]:
    try:
        response = httpx.get(
            feed["url"], timeout=HTTP_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        )
        response.raise_for_status()
        return parse_feed_content(response.content, feed)
    except Exception as exc:
        logger.warning("[News] Feed failed %s: %s", feed["source"], exc)
        return []


def parse_x_payload(username: str, payload: dict) -> list[dict]:
    articles = []
    for post in payload.get("data", [])[:ITEMS_PER_SOURCE]:
        text = _clean_html(post.get("text", ""))
        post_id = str(post.get("id", ""))
        if not text or not post_id:
            continue
        articles.append({
            "title": f"@{username}: {text[:180]}",
            "summary": text[:1200],
            "source": f"X @{username}",
            "source_kind": "social",
            "url": f"https://x.com/{username}/status/{post_id}",
            "category": "social_politics",
            "published": post.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "provider": "x",
        })
    return articles


def fetch_x_account(username: str) -> list[dict]:
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
            user_response = client.get(f"https://api.x.com/2/users/by/username/{username}")
            user_response.raise_for_status()
            user_id = str(user_response.json().get("data", {}).get("id", ""))
            if not user_id:
                return []
            posts_response = client.get(
                f"https://api.x.com/2/users/{user_id}/tweets",
                params={
                    "max_results": max(5, min(100, ITEMS_PER_SOURCE)),
                    "exclude": "retweets,replies",
                    "tweet.fields": "created_at,lang",
                },
            )
            posts_response.raise_for_status()
            return parse_x_payload(username, posts_response.json())
    except Exception as exc:
        logger.warning("[News] X @%s failed: %s", username, exc)
        return []


def parse_truth_payload(payload: object) -> list[dict]:
    """Normalize common licensed Truth API response shapes."""
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("posts") or payload.get("statuses") or []
        if isinstance(items, dict):
            items = items.get("posts") or items.get("statuses") or items.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    articles = []
    for post in items[:ITEMS_PER_SOURCE * 3]:
        if not isinstance(post, dict):
            continue
        account = post.get("account") if isinstance(post.get("account"), dict) else {}
        username = str(
            post.get("username") or post.get("account_username") or
            account.get("username") or account.get("acct") or "realDonaldTrump"
        ).lstrip("@")
        text = _clean_html(post.get("text") or post.get("content") or post.get("body") or "")
        post_id = str(post.get("id") or post.get("post_id") or "")
        url = post.get("url") or post.get("uri")
        if not url and post_id:
            url = f"https://truthsocial.com/@{username}/posts/{post_id}"
        if not text or not url:
            continue
        articles.append({
            "title": f"Truth @{username}: {text[:180]}",
            "summary": text[:1200],
            "source": f"Truth Social @{username}",
            "source_kind": "social",
            "url": url,
            "category": "social_politics",
            "published": post.get("created_at") or post.get("published_at") or datetime.now(timezone.utc).isoformat(),
            "provider": "truth_api",
        })
    return articles


def fetch_truth_api() -> list[dict]:
    """Fetch a licensed Truth API URL supplied by TMTG or a data vendor."""
    url = os.getenv("TRUTH_API_URL", "").strip()
    if not url:
        return []
    token = os.getenv("TRUTH_API_TOKEN", "").strip()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {}
    accounts = _csv_env("TRUTH_API_ACCOUNTS", "realDonaldTrump")
    if accounts:
        params["accounts"] = ",".join(accounts)
    try:
        response = httpx.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return parse_truth_payload(response.json())
    except Exception as exc:
        logger.warning("[News] Licensed Truth API failed: %s", exc)
        return []


def _infer_category(text: str) -> str:
    text = text.lower()
    if any(word in text for word in ("data center", "datacenter", "server", "cooling")):
        return "data_centers"
    if any(word in text for word in ("semiconductor", "chip", "gpu", "hbm", "foundry")):
        return "semiconductors"
    if any(word in text for word in ("freight", "shipping", "supply chain", "port", "logistics")):
        return "supply_chain"
    if any(word in text for word in ("power grid", "electricity", "utility", "energy")):
        return "energy"
    return "geopolitics"


def _has_term(text: str, term: str) -> bool:
    if " " in term:
        return term in text
    return bool(re.search(rf"\b{re.escape(term)}(?:s|es|ed|ing)?\b", text))


def _has_any_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(_has_term(text, term) for term in terms)


def parse_gdelt_payload(payload: dict) -> list[dict]:
    articles = []
    for item in payload.get("articles", [])[:ITEMS_PER_SOURCE * 3]:
        title = _clean_html(item.get("title", ""))
        url = item.get("url", "")
        if not title or not url:
            continue
        domain = item.get("domain") or "unknown source"
        seen_date = item.get("seendate", "")
        try:
            published = datetime.strptime(seen_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
        except (ValueError, TypeError):
            published = datetime.now(timezone.utc).isoformat()
        articles.append({
            "title": title,
            "summary": title,
            "source": f"GDELT / {domain}",
            "source_kind": "aggregator",
            "url": url,
            "category": _infer_category(title),
            "published": published,
            "provider": "gdelt",
        })
    return articles


def fetch_gdelt() -> list[dict]:
    try:
        response = httpx.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": GDELT_QUERY, "mode": "ArtList", "maxrecords": 50,
                "format": "json", "sort": "DateDesc", "timespan": "6h",
            },
            timeout=HTTP_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if response.status_code == 429:
            time.sleep(5.25)
            response = httpx.get(
                str(response.request.url), timeout=HTTP_TIMEOUT, follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        response.raise_for_status()
        return parse_gdelt_payload(response.json())
    except Exception as exc:
        logger.warning("[News] GDELT fallback failed: %s", exc)
        return []


def _normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _normalized_url(url: str) -> str:
    return canonicalize_url(url)


def _article_key(article: dict) -> str:
    url = _normalized_url(article.get("url", ""))
    raw = url or _normalized_title(article.get("title", ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def select_balanced(articles: list[dict], limit: int = MAX_ARTICLES) -> list[dict]:
    """Round-robin categories so broad feeds cannot starve specialist coverage."""
    groups = defaultdict(list)
    seen = set()
    for article in articles:
        key = _article_key(article)
        if key in seen:
            continue
        seen.add(key)
        groups[article.get("category", "geopolitics")].append(article)

    for group in groups.values():
        group.sort(key=lambda item: item.get("published", ""), reverse=True)
    categories = [category for category in CATEGORY_ORDER if groups.get(category)]
    categories.extend(sorted(set(groups) - set(categories)))

    selected = []
    while categories and len(selected) < limit:
        active = []
        for category in categories:
            if groups[category] and len(selected) < limit:
                selected.append(groups[category].pop(0))
            if groups[category]:
                active.append(category)
        categories = active
    return selected


def _baseline_analysis(article: dict, index: int) -> dict:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    threats = {word for word in THREAT_WORDS if _has_term(text, word)}
    negative = sum(1 for word in NEGATIVE_WORDS if _has_term(text, word))
    positive = sum(1 for word in POSITIVE_WORDS if _has_term(text, word))
    assets = sorted({symbol for word, symbol in ASSET_KEYWORDS.items() if word in text})

    event_type = "market_event"
    if _has_any_term(text, ("war", "attack", "airstrike", "missile", "invasion", "bombing")):
        event_type = "military_conflict"
    elif _has_any_term(text, ("tariff", "trade war", "export ban")):
        event_type = "trade_war"
    elif _has_any_term(text, ("sanction", "embargo")):
        event_type = "economic_sanctions"
    elif _has_any_term(text, ("cyberattack", "ransomware", "breach")):
        event_type = "cyber_attack"
    elif _has_any_term(text, ("outage", "shortage", "shutdown", "fire", "explosion")):
        event_type = "supply_disruption"

    is_threat = bool(threats)
    severity = "High" if len(threats) >= 2 or event_type in {"military_conflict", "cyber_attack"} else "Medium"
    if not is_threat:
        severity = "Low"
    sentiment = "negative" if negative > positive else "positive" if positive > negative else "neutral"
    return {
        "index": index,
        "is_threat": is_threat,
        "title": article.get("title", ""),
        "description": article.get("summary") or article.get("title", ""),
        "event_type": event_type,
        "severity": severity,
        "country": "",
        "region": "Global",
        "sentiment": sentiment,
        "affected_assets": assets,
        "category": article.get("category", "geopolitics"),
    }


def analyze_batch(articles: list[dict]) -> list[dict]:
    """Classify every article, with deterministic fallback if the LLM is unavailable."""
    baseline = {index: _baseline_analysis(article, index) for index, article in enumerate(articles, start=1)}
    batch_text = "\n".join(
        f"{index}. [{article['source']}] {article['title']} - {article['summary'][:300]}"
        for index, article in enumerate(articles, start=1)
    )
    prompt = f"""Classify all {len(articles)} articles for trading and geopolitical impact.

{batch_text}

Return one JSON object per article in a JSON array. Schema:
{{"i":1,"t":false,"desc":"one factual sentence","type":"military_conflict|political_crisis|economic_sanctions|cyber_attack|terrorism|trade_war|energy_crisis|supply_disruption|market_event","sev":"Critical|High|Medium|Low","country":"","region":"Middle East|Europe|Asia Pacific|North America|South America|Africa|Global","sent":"positive|negative|neutral","assets":["ticker"],"cat":"finance|geopolitics|politics|crypto|energy|tech|conflict|social_politics|ai_infrastructure|semiconductors|data_centers|supply_chain"}}
Use t=true only for a concrete security, policy, infrastructure, supply, energy, or market threat. JSON only."""

    try:
        response = call_lm_studio(
            prompt,
            system="You are a factual market-news classifier. Return only the requested JSON array.",
            max_tokens=min(6144, 700 + len(articles) * 420),
            temperature=0.1,
            thinking=False,
        )
        parsed = parse_json(response)
        if isinstance(parsed, list):
            for result in parsed:
                try:
                    index = int(result.get("i", 0))
                except (TypeError, ValueError):
                    continue
                if index not in baseline:
                    continue
                current = baseline[index]
                current.update({
                    "is_threat": bool(result.get("t", current["is_threat"])),
                    "description": _coerce_scalar_str(result.get("desc")) or current["description"],
                    "event_type": _coerce_scalar_str(result.get("type")) or current["event_type"],
                    "severity": _coerce_scalar_str(result.get("sev")) or current["severity"],
                    "country": _coerce_scalar_str(result.get("country")),
                    "region": _coerce_scalar_str(result.get("region")) or current["region"],
                    "sentiment": _coerce_scalar_str(result.get("sent")) or current["sentiment"],
                    "affected_assets": result.get("assets") or current["affected_assets"],
                    "category": _coerce_scalar_str(result.get("cat")) or current["category"],
                })
    except Exception as exc:
        logger.warning("[News] LLM unavailable; using deterministic classification: %s", exc)
    return [baseline[index] for index in sorted(baseline)]


def _remove_existing(articles: list[dict]) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)).isoformat()
    seen = set()
    with get_db() as db:
        rows = db.query(NewsItem.title, NewsItem.url).filter(NewsItem.created_date > cutoff).all()
    for title, url in rows:
        if title:
            seen.add(hashlib.sha256(_normalized_title(title).encode("utf-8")).hexdigest())
        if url:
            seen.add(hashlib.sha256(_normalized_url(url).encode("utf-8")).hexdigest())

    fresh = []
    for article in articles:
        keys = {_article_key(article)}
        title_key = _normalized_title(article.get("title", ""))
        if title_key:
            keys.add(hashlib.sha256(title_key.encode("utf-8")).hexdigest())
        if keys & seen:
            continue
        seen.update(keys)
        fresh.append(article)
    return fresh


def _timed_fetch(source: str, source_kind: str, provider: str, url: str, fetcher, *args):
    started = time.perf_counter()
    error = ""
    try:
        articles = fetcher(*args)
        if not articles:
            error = "No articles returned"
    except Exception as exc:
        articles = []
        error = str(exc)
    return articles, {
        "source": source,
        "source_kind": source_kind,
        "provider": provider,
        "url": url,
        "success": bool(articles),
        "article_count": len(articles),
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "error": error[:500],
    }


def _persist_source_health(records: list[dict], now_iso: str):
    with get_db() as db:
        for record in records:
            row = db.query(IntelligenceSourceHealth).filter(
                IntelligenceSourceHealth.source == record["source"]
            ).first()
            if not row:
                row = IntelligenceSourceHealth(source=record["source"])
                db.add(row)
            row.source_kind = record.get("source_kind")
            row.provider = record.get("provider")
            row.url = record.get("url")
            row.last_latency_ms = record.get("latency_ms")
            row.last_article_count = record.get("article_count", 0)
            row.updated_at = now_iso
            if record.get("success"):
                row.success_count = int(row.success_count or 0) + 1
                row.consecutive_failures = 0
                row.last_success_at = now_iso
                row.last_error = ""
            else:
                row.failure_count = int(row.failure_count or 0) + 1
                row.consecutive_failures = int(row.consecutive_failures or 0) + 1
                row.last_failure_at = now_iso
                row.last_error = record.get("error", "Unknown source failure")
            total = int(row.success_count or 0) + int(row.failure_count or 0)
            success_rate = int(row.success_count or 0) / total if total else 0.0
            base = source_reliability(row.source_kind or "", row.provider or "")
            row.reliability_score = round(base * 0.8 + success_rate * 0.2, 3)


def _record_ingestion_run(started_at: str, finished_at: str, status: str, health: list[dict],
                          fetched: int, fresh: int, selected: int, news: int, threats: int,
                          error: str = ""):
    with get_db() as db:
        db.add(IntelligenceIngestionRun(
            id=str(uuid.uuid4()), started_at=started_at, finished_at=finished_at,
            status=status, source_count=len(health),
            failed_sources=sum(1 for record in health if not record.get("success")),
            fetched_count=fetched, fresh_count=fresh, selected_count=selected,
            saved_news=news, saved_threats=threats, error=error[:1000],
        ))


def _fetch_direct_sources() -> tuple[list[dict], list[dict]]:
    articles, health = [], []
    futures = {}
    with ThreadPoolExecutor(max_workers=min(16, len(RSS_FEEDS) + 4)) as executor:
        for feed in RSS_FEEDS:
            future = executor.submit(
                _timed_fetch, feed["source"], feed.get("kind", "rss"), "rss",
                feed["url"], fetch_feed, feed,
            )
            futures[future] = feed["source"]
        if os.getenv("X_BEARER_TOKEN", "").strip():
            for username in _csv_env("X_USERNAMES", "realDonaldTrump,POTUS,WhiteHouse"):
                source = f"X @{username}"
                future = executor.submit(
                    _timed_fetch, source, "social", "x", f"https://x.com/{username}",
                    fetch_x_account, username,
                )
                futures[future] = source
        if os.getenv("TRUTH_API_URL", "").strip():
            future = executor.submit(
                _timed_fetch, "Truth API", "social", "truth_api",
                os.getenv("TRUTH_API_URL", ""), fetch_truth_api,
            )
            futures[future] = "Truth API"

        for future in as_completed(futures):
            try:
                source_articles, source_health = future.result()
                articles.extend(source_articles)
                health.append(source_health)
            except Exception as exc:
                logger.warning("[News] Source failed %s: %s", futures[future], exc)
                health.append({"source": futures[future], "success": False, "error": str(exc)})
    return articles, health


def run():
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("[News] Fetching direct news and social sources...")
    all_articles, health = _fetch_direct_sources()
    direct_count = len(all_articles)

    aggregator_mode = os.getenv("NEWS_AGGREGATOR_MODE", "fallback").strip().lower()
    fallback_threshold = max(1, int(os.getenv("NEWS_AGGREGATOR_FALLBACK_THRESHOLD", "40")))
    if aggregator_mode == "always" or (aggregator_mode == "fallback" and direct_count < fallback_threshold):
        gdelt_articles, gdelt_health = _timed_fetch(
            "GDELT", "aggregator", "gdelt", "https://api.gdeltproject.org/api/v2/doc/doc",
            fetch_gdelt,
        )
        all_articles.extend(gdelt_articles)
        health.append(gdelt_health)

    enrich_articles(all_articles)
    eligible = [article for article in all_articles if not article.get("is_stale")]
    fresh = _remove_existing(eligible)
    selected = select_balanced(fresh, MAX_ARTICLES)
    health_now = datetime.now(timezone.utc).isoformat()
    _persist_source_health(health, health_now)
    logger.info(
        "[News] Fetched %d direct / %d total; %d fresh selected for analysis",
        direct_count, len(all_articles), len(selected),
    )
    if not selected:
        _record_ingestion_run(
            started_at, health_now, "ok", health, len(all_articles), len(fresh), 0, 0, 0,
        )
        return {"threats": 0, "news": 0, "fetched": len(all_articles)}

    analyzed = []
    batches = [selected[index:index + BATCH_SIZE] for index in range(0, len(selected), BATCH_SIZE)]
    for batch_number, batch in enumerate(batches, start=1):
        logger.info("[News] Classifying batch %d/%d", batch_number, len(batches))
        results = analyze_batch(batch)
        for result in results:
            index = result["index"] - 1
            article = batch[index]
            result.update({
                "title": article["title"],
                "source_url": article.get("url", ""),
                "source": article.get("source", ""),
                "published": article.get("published", ""),
                "canonical_url": article.get("canonical_url", ""),
                "source_kind": article.get("source_kind", ""),
                "provider": article.get("provider", ""),
                "reliability_score": article.get("reliability_score"),
                "confirmation_status": article.get("confirmation_status"),
                "corroboration_count": article.get("corroboration_count", 0),
                "corroborated_sources": article.get("corroborated_sources", []),
                "claim_confidence": article.get("claim_confidence"),
                "is_stale": bool(article.get("is_stale")),
                "entities": article.get("entities", {}),
                "cluster_id": article.get("cluster_id", ""),
            })
            combined_assets = set(result.get("affected_assets", []) or [])
            combined_assets.update(article.get("affected_assets", []) or [])
            result["affected_assets"] = sorted(combined_assets)
        analyzed.extend(results)

    threat_count = 0
    new_critical_threats = []
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        for item in analyzed:
            assets = item.get("affected_assets", [])
            assets_text = ",".join(assets) if isinstance(assets, list) else str(assets or "")
            published = item.get("published") or now_iso
            if item.get("is_threat"):
                country = _coerce_scalar_str(item.get("country"))
                region = _coerce_scalar_str(item.get("region")) or "Global"
                lat, lon = resolve_coords(country, region)
                db.add(ThreatEvent(
                    id=str(uuid.uuid4()), title=item["title"],
                    description=item.get("description", ""),
                    event_type=item.get("event_type", "market_event"),
                    severity=item.get("severity", "Medium"),
                    country=country, region=region,
                    latitude=lat, longitude=lon,
                    source=item.get("source", ""), source_url=item.get("source_url", ""),
                    status="Active", published_at=published,
                    source_kind=item.get("source_kind", ""),
                    reliability_score=item.get("reliability_score"),
                    confirmation_status=item.get("confirmation_status"),
                    corroboration_count=item.get("corroboration_count", 0),
                    claim_confidence=item.get("claim_confidence"),
                    cluster_id=item.get("cluster_id", ""),
                    created_date=now_iso, updated_date=now_iso,
                ))
                threat_count += 1
                if item.get("severity") == "Critical":
                    new_critical_threats.append(item["title"])

            db.add(NewsItem(
                id=str(uuid.uuid4()), title=item["title"],
                summary=item.get("description", ""), source=item.get("source", ""),
                url=item.get("source_url", ""), category=item.get("category", "geopolitics"),
                sentiment=item.get("sentiment", "neutral"), affected_assets=assets_text,
                region=_coerce_scalar_str(item.get("region")) or "Global", published_at=published,
                canonical_url=item.get("canonical_url", ""),
                source_kind=item.get("source_kind", ""), provider=item.get("provider", ""),
                ingested_at=now_iso, reliability_score=item.get("reliability_score"),
                confirmation_status=item.get("confirmation_status"),
                corroboration_count=item.get("corroboration_count", 0),
                corroborated_sources=json.dumps(item.get("corroborated_sources", [])),
                claim_confidence=item.get("claim_confidence"),
                is_stale=bool(item.get("is_stale")),
                entities=json.dumps(item.get("entities", {}), sort_keys=True),
                cluster_id=item.get("cluster_id", ""),
                created_date=now_iso, updated_date=now_iso,
            ))
        db.commit()

    logger.info("[News] Saved %d threats and %d news items", threat_count, len(analyzed))
    if new_critical_threats:
        try:
            from app.ws import manager as ws_manager
            ws_manager.broadcast_from_thread("critical_threat", {"titles": new_critical_threats[:5], "count": len(new_critical_threats)})
        except Exception:
            pass
    finished_at = datetime.now(timezone.utc).isoformat()
    _record_ingestion_run(
        started_at, finished_at, "ok", health, len(all_articles), len(fresh),
        len(selected), len(analyzed), threat_count,
    )
    try:
        from app.scheduler import notify_new_intelligence
        notify_new_intelligence()
    except Exception as exc:
        logger.debug("[News] Could not notify event-driven signal scheduler: %s", exc)
    return {"threats": threat_count, "news": len(analyzed), "fetched": len(all_articles)}
