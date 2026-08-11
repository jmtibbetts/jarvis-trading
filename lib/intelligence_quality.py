"""Deterministic provenance, entity, freshness, and corroboration scoring."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "in", "is", "it", "its", "new", "of", "on", "or", "says", "that", "the",
    "this", "to", "was", "will", "with",
}

SOURCE_KIND_RELIABILITY = {
    "government": 0.92,
    "manufacturer": 0.88,
    "equipment_manufacturer": 0.88,
    "cloud_provider": 0.86,
    "newspaper": 0.82,
    "television": 0.80,
    "radio": 0.80,
    "trade_press": 0.78,
    "digital": 0.72,
    "social": 0.45,
    "aggregator": 0.55,
}

COMPANIES = {
    "nvidia": ("NVIDIA", "NVDA"),
    "advanced micro devices": ("AMD", "AMD"),
    "amd": ("AMD", "AMD"),
    "intel": ("Intel", "INTC"),
    "tsmc": ("TSMC", "TSM"),
    "taiwan semiconductor": ("TSMC", "TSM"),
    "asml": ("ASML", "ASML"),
    "broadcom": ("Broadcom", "AVGO"),
    "micron": ("Micron", "MU"),
    "applied materials": ("Applied Materials", "AMAT"),
    "lam research": ("Lam Research", "LRCX"),
    "kla": ("KLA", "KLAC"),
    "vertiv": ("Vertiv", "VRT"),
    "arista": ("Arista Networks", "ANET"),
    "super micro": ("Super Micro Computer", "SMCI"),
    "microsoft": ("Microsoft", "MSFT"),
    "amazon": ("Amazon", "AMZN"),
    "google": ("Alphabet", "GOOGL"),
    "alphabet": ("Alphabet", "GOOGL"),
    "meta": ("Meta", "META"),
    "apple": ("Apple", "AAPL"),
    "tesla": ("Tesla", "TSLA"),
    "openai": ("OpenAI", None),
}

ASSETS = {
    "bitcoin": "BTC/USD", "ethereum": "ETH/USD", "solana": "SOL/USD",
    "xrp": "XRP/USD", "crude oil": "CL", "oil": "CL", "natural gas": "NG",
    "gold": "GC", "silver": "SI", "s&p 500": "SPY", "nasdaq": "QQQ",
}

PEOPLE = {
    "donald trump": "Donald Trump", "president trump": "Donald Trump",
    "jerome powell": "Jerome Powell", "elon musk": "Elon Musk",
    "jensen huang": "Jensen Huang", "sam altman": "Sam Altman",
}

COUNTRIES = {
    "united states": "United States", "u.s.": "United States", "china": "China",
    "taiwan": "Taiwan", "russia": "Russia", "ukraine": "Ukraine",
    "israel": "Israel", "iran": "Iran", "japan": "Japan", "india": "India",
    "south korea": "South Korea", "germany": "Germany", "france": "France",
    "united kingdom": "United Kingdom", "uk": "United Kingdom",
}

INDUSTRIES = {
    "data center": "Data Centers", "datacenter": "Data Centers",
    "semiconductor": "Semiconductors", "chip": "Semiconductors",
    "foundry": "Semiconductor Foundries", "hbm": "Memory",
    "transformer": "Power Equipment", "power grid": "Utilities",
    "nuclear": "Nuclear Energy", "supply chain": "Supply Chain",
    "freight": "Logistics", "shipping": "Logistics", "cloud": "Cloud Computing",
    "artificial intelligence": "Artificial Intelligence", " ai ": "Artificial Intelligence",
}


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
        ]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))
    except ValueError:
        return url


def source_reliability(source_kind: str, provider: str = "") -> float:
    score = SOURCE_KIND_RELIABILITY.get((source_kind or "").lower(), 0.65)
    if (provider or "").lower() == "gdelt":
        score = min(score, SOURCE_KIND_RELIABILITY["aggregator"])
    return round(score, 3)


def _contains(text: str, term: str) -> bool:
    if term.startswith(" ") or term.endswith(" "):
        return term in f" {text} "
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))


def extract_entities(article: dict) -> dict[str, list[str]]:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    companies, assets, people, countries, industries = set(), set(), set(), set(), set()

    for term, (company, ticker) in COMPANIES.items():
        if _contains(text, term):
            companies.add(company)
            if ticker:
                assets.add(ticker)
    for term, ticker in ASSETS.items():
        if _contains(text, term):
            assets.add(ticker)
    for term, name in PEOPLE.items():
        if _contains(text, term):
            people.add(name)
    for term, country in COUNTRIES.items():
        if _contains(text, term):
            countries.add(country)
    for term, industry in INDUSTRIES.items():
        if _contains(text, term):
            industries.add(industry)

    for ticker in re.findall(r"\$([A-Z][A-Z0-9.]{0,7})\b", f"{article.get('title', '')} {article.get('summary', '')}"):
        assets.add(ticker)
    for ticker in article.get("affected_assets", []) or []:
        if ticker:
            assets.add(str(ticker).upper())

    return {
        "companies": sorted(companies),
        "assets": sorted(assets),
        "people": sorted(people),
        "countries": sorted(countries),
        "industries": sorted(industries),
    }


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _headline_tokens(article: dict) -> set[str]:
    tokens = re.findall(r"[a-z0-9]{2,}", (article.get("title") or "").lower())
    return {token for token in tokens if token not in STOP_WORDS}


def _entity_keys(article: dict) -> set[str]:
    entities = article.get("entities") or {}
    return {
        f"{kind}:{value.lower()}"
        for kind in ("companies", "assets", "people", "countries", "industries")
        for value in entities.get(kind, [])
    }


def _same_story(left: dict, right: dict) -> bool:
    left_tokens, right_tokens = _headline_tokens(left), _headline_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    shared_entities = _entity_keys(left) & _entity_keys(right)
    return overlap >= 0.48 or (overlap >= 0.24 and bool(shared_entities))


def enrich_articles(articles: list[dict], now: datetime | None = None) -> list[dict]:
    """Annotate articles in place and return them for pipeline composition."""
    now = now or datetime.now(timezone.utc)
    max_age_hours = max(1, int(os.getenv("NEWS_MAX_AGE_HOURS", "72")))

    for article in articles:
        article["canonical_url"] = canonicalize_url(article.get("url", ""))
        article["entities"] = extract_entities(article)
        extracted_assets = set(article["entities"]["assets"])
        extracted_assets.update(article.get("affected_assets", []) or [])
        article["affected_assets"] = sorted(extracted_assets)
        article["reliability_score"] = source_reliability(
            article.get("source_kind", ""), article.get("provider", "")
        )
        published = _parse_time(article.get("published", ""))
        age_hours = max(0.0, (now - published).total_seconds() / 3600) if published else 0.0
        article["age_hours"] = round(age_hours, 2)
        article["is_stale"] = age_hours > max_age_hours

    parent = list(range(len(articles)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(articles)):
        for right in range(left + 1, len(articles)):
            if articles[left].get("source") == articles[right].get("source"):
                continue
            if _same_story(articles[left], articles[right]):
                union(left, right)

    clusters: dict[int, list[dict]] = {}
    for index, article in enumerate(articles):
        clusters.setdefault(find(index), []).append(article)

    for group in clusters.values():
        source_names = sorted({article.get("source", "") for article in group if article.get("source")})
        cluster_seed = "|".join(sorted(article.get("canonical_url") or article.get("title", "") for article in group))
        cluster_id = hashlib.sha256(cluster_seed.encode("utf-8")).hexdigest()[:16]
        independent = {
            article.get("source") for article in group
            if article.get("source_kind") not in {"social", "aggregator"}
        }
        for article in group:
            corroborators = [source for source in source_names if source != article.get("source")]
            kind = article.get("source_kind")
            if kind == "social":
                confirmation = "corroborated" if independent else "unconfirmed_social"
            elif len(independent) >= 2:
                confirmation = "corroborated"
            elif kind == "aggregator":
                confirmation = "aggregated"
            else:
                confirmation = "single_source"

            confidence = article["reliability_score"] * 100
            confidence += min(18, len(corroborators) * 7)
            if kind == "social" and confirmation != "corroborated":
                confidence = min(confidence, 45)
            if article["is_stale"]:
                confidence -= 25
            article.update({
                "cluster_id": cluster_id,
                "corroboration_count": len(corroborators),
                "corroborated_sources": corroborators,
                "confirmation_status": confirmation,
                "claim_confidence": round(max(0, min(100, confidence)), 1),
            })
    return articles
