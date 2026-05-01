"""
Job: Fetch Threat News v6.9.2 — RSS → LLM analysis → DB storage.
v6.9.2: Batch size increased to 8 articles (LM Studio unlimited). Max articles bumped to 60.
v6.2:  thinking=False for news classification (no chain-of-thought needed).
v6.1:  Dedup window reduced from 24h to 2h so new articles get through each run.
"""
import feedparser, uuid, logging, hashlib, re
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.database import get_db, ThreatEvent, NewsItem
from lib.lmstudio import call_lm_studio, parse_json
import httpx

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    # Geopolitics
    {'url': 'https://feeds.bbci.co.uk/news/world/rss.xml',              'source': 'BBC World',         'category': 'geopolitics'},
    {'url': 'https://www.aljazeera.com/xml/rss/all.xml',                 'source': 'Al Jazeera',        'category': 'geopolitics'},
    {'url': 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',    'source': 'NYT World',         'category': 'geopolitics'},
    {'url': 'https://apnews.com/rss/apf-topnews',                        'source': 'AP News',           'category': 'geopolitics'},
    {'url': 'https://feeds.a.dj.com/rss/RSSWorldNews.xml',              'source': 'WSJ World',         'category': 'geopolitics'},
    {'url': 'https://www.politico.com/rss/politicopicks.xml',            'source': 'Politico',          'category': 'geopolitics'},
    {'url': 'https://feeds.bbci.co.uk/news/world/middle_east/rss.xml',  'source': 'BBC Middle East',   'category': 'conflict'},
    {'url': 'https://feeds.bbci.co.uk/news/world/europe/rss.xml',       'source': 'BBC Europe',        'category': 'conflict'},
    {'url': 'https://feeds.bbci.co.uk/news/world/asia/rss.xml',         'source': 'BBC Asia',          'category': 'conflict'},
    # Defense
    {'url': 'https://www.defensenews.com/rss/news/',                     'source': 'Defense News',      'category': 'conflict'},
    {'url': 'https://breakingdefense.com/feed/',                         'source': 'Breaking Defense',  'category': 'conflict'},
    # Finance
    {'url': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml',            'source': 'WSJ Markets',       'category': 'finance'},
    {'url': 'https://finance.yahoo.com/rss/topstories',                  'source': 'Yahoo Finance',     'category': 'finance'},
    {'url': 'https://www.marketwatch.com/rss/topstories',                'source': 'MarketWatch',       'category': 'finance'},
    {'url': 'https://www.cnbc.com/id/10001147/device/rss/rss.html',     'source': 'CNBC Markets',      'category': 'finance'},
    {'url': 'https://feeds.bloomberg.com/markets/news.rss',             'source': 'Bloomberg',         'category': 'finance'},
    {'url': 'https://www.zerohedge.com/fullrss2.xml',                   'source': 'ZeroHedge',         'category': 'finance'},
    # Crypto
    {'url': 'https://cointelegraph.com/rss',                            'source': 'CoinTelegraph',     'category': 'crypto'},
    {'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml', 'source': 'CoinDesk',   'category': 'crypto'},
    {'url': 'https://decrypt.co/feed',                                  'source': 'Decrypt',           'category': 'crypto'},
    {'url': 'https://cryptopanic.com/news/rss/',                        'source': 'CryptoPanic',       'category': 'crypto'},
    # Energy
    {'url': 'https://oilprice.com/rss/main',                            'source': 'OilPrice.com',      'category': 'energy'},
    # Tech
    {'url': 'https://feeds.arstechnica.com/arstechnica/index',          'source': 'Ars Technica',      'category': 'tech'},
    {'url': 'https://techcrunch.com/feed/',                              'source': 'TechCrunch',        'category': 'tech'},
]

# ── Token budget math ──────────────────────────────────────────────────────────
# LM Studio hard cap ≈ 2,000 tokens (server-side, overrides API params).
# Qwen3 thinking overhead ≈ 800-1200 tokens before any output.
# Input per article (title + 200-char summary + index label) ≈ 60-80 tokens.
# Prompt boilerplate ≈ 150 tokens.
# Batch of 3 → input ≈ 400 tokens → leaves ~1,600 tokens for thinking+output.
# Thinking uses ~800-1000 tokens → leaves ~600-800 tokens for 3-article JSON. Safe.
BATCH_SIZE = 8       # articles per LLM call (LM Studio unlimited — no token cap)
MAX_ARTICLES = 60    # total cap processed per run (LM Studio unlimited)

def fetch_feed(feed: dict) -> list[dict]:
    try:
        parsed = feedparser.parse(feed['url'])
        articles = []
        for entry in parsed.entries[:10]:
            title = entry.get('title', '').strip()
            if not title:
                continue
            summary = entry.get('summary', '') or entry.get('description', '')
            summary = re.sub(r'<[^>]+>', ' ', summary).strip()[:300]
            url = entry.get('link', '')
            pub = entry.get('published', '') or entry.get('updated', '')
            articles.append({
                'title': title,
                'summary': summary,
                'source': feed['source'],
                'url': url,
                'category': feed['category'],
                'published': pub
            })
        return articles
    except Exception as e:
        logger.debug(f"[News] Feed failed {feed['source']}: {e}")
        return []

def analyze_batch(articles: list[dict]) -> list[dict]:
    """Send a batch of articles to LLM for classification."""
    batch_text = '\n'.join([
        f"{i+1}. [{a['source']}] {a['title']} — {a['summary'][:200]}"
        for i, a in enumerate(articles)
    ])

    # Compact schema — shorter field names/values = fewer output tokens
    prompt = f"""Classify {len(articles)} news articles for market/geopolitical impact. Skip routine news.

{batch_text}

JSON array only. Each significant item:
{{"i":<1-based>,"t":true,"title":"short","desc":"1 sentence","type":"military_conflict|political_crisis|economic_sanctions|cyber_attack|terrorism|trade_war|energy_crisis|market_event","sev":"Critical|High|Medium|Low","country":"","region":"Middle East|Europe|Asia Pacific|North America|South America|Africa|Global","sent":"positive|negative|neutral","assets":[],"cat":"finance|geopolitics|crypto|energy|tech|conflict"}}
Return [] if nothing significant."""

    # Scale max_tokens with batch size — smaller batches need less output headroom
    # With Qwen3's ~800-1000 token thinking overhead, leaving 600-800 for JSON is enough
    batch_max_tokens = min(4096, 512 + len(articles) * 350)  # generous headroom — no server cap

    try:
        response = call_lm_studio(prompt,
                               system="You are a data classifier. Answer directly without reasoning. Return ONLY a JSON array, no markdown, no explanation.",
                               max_tokens=batch_max_tokens, temperature=0.1, thinking=False)
        parsed = parse_json(response)
        if isinstance(parsed, list):
            # Remap compact keys to full keys for downstream compatibility
            results = []
            for r in parsed:
                results.append({
                    'index':          r.get('i', 1),
                    'is_threat':      r.get('t', False),
                    'title':          r.get('title', ''),
                    'description':    r.get('desc', ''),
                    'event_type':     r.get('type', 'market_event'),
                    'severity':       r.get('sev', 'Medium'),
                    'country':        r.get('country', ''),
                    'region':         r.get('region', 'Global'),
                    'sentiment':      r.get('sent', 'neutral'),
                    'affected_assets':r.get('assets', []),
                    'category':       r.get('cat', 'geopolitics'),
                })
            return results
    except Exception as e:
        logger.error(f"[News] LLM analysis failed: {e}")
    return []

def run():
    logger.info("[News] Fetching threat news...")

    # 1. Fetch all RSS feeds in parallel
    all_articles = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_feed, f): f for f in RSS_FEEDS}
        for fut in as_completed(futures):
            all_articles.extend(fut.result())

    logger.info(f"[News] Fetched {len(all_articles)} raw articles")

    # 2. Dedup — compare against last 2h of DB titles only
    seen_hashes = set()
    with get_db() as db:
        cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        existing = db.query(NewsItem.title).filter(NewsItem.created_date > cutoff_2h).all()
        for (t,) in existing:
            seen_hashes.add(hashlib.md5(t.lower().encode()).hexdigest())

    logger.info(f"[News] {len(seen_hashes)} titles seen in last 2h (dedup window)")

    new_articles = []
    for a in all_articles:
        h = hashlib.md5(a['title'].lower().encode()).hexdigest()
        if h not in seen_hashes:
            seen_hashes.add(h)
            new_articles.append(a)

    logger.info(f"[News] {len(new_articles)} new articles to analyze (of {len(all_articles)} fetched)")
    if not new_articles:
        logger.info("[News] No new articles since last run — skipping LLM call")
        return {'threats': 0, 'news': 0}

    # 3. Process in batches of BATCH_SIZE
    cap = new_articles[:MAX_ARTICLES]
    batches = [cap[i:i+BATCH_SIZE] for i in range(0, len(cap), BATCH_SIZE)]
    logger.info(f"[News] Processing {len(cap)} articles in {len(batches)} batches of {BATCH_SIZE}")

    analyzed = []
    for batch_num, batch in enumerate(batches):
        logger.info(f"[News] Batch {batch_num+1}/{len(batches)}: sending {len(batch)} articles to LLM...")
        results = analyze_batch(batch)
        # Attach source metadata (index is 1-based within the batch)
        for r in results:
            idx = r.get('index', 1) - 1
            if 0 <= idx < len(batch):
                r['source_url'] = batch[idx].get('url', '')
                r['source']     = batch[idx].get('source', r.get('source', ''))
                r['published']  = batch[idx].get('published', '')
        analyzed.extend(results)
        logger.info(f"[News] Batch {batch_num+1} returned {len(results)} classified items")

    logger.info(f"[News] Total classified: {len(analyzed)} items across {len(batches)} batches")

    # 4. Save to DB
    threat_count = 0
    news_count   = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db() as db:
        for item in analyzed:
            title = item.get('title', '').strip()
            if not title:
                continue

            assets_raw = item.get('affected_assets', [])
            assets_str = ','.join(assets_raw) if isinstance(assets_raw, list) else str(assets_raw)

            pub_at = item.get('published') or now_iso

            # Save as ThreatEvent if flagged as a threat
            if item.get('is_threat') and item.get('event_type'):
                threat = ThreatEvent(
                    id=str(uuid.uuid4()),
                    title=title,
                    description=item.get('description', ''),
                    event_type=item.get('event_type', 'political_crisis'),
                    severity=item.get('severity', 'Medium'),
                    country=item.get('country', ''),
                    region=item.get('region', 'Global'),
                    source=item.get('source', ''),
                    source_url=item.get('source_url', ''),
                    status='Active',
                    published_at=pub_at,
                    created_date=now_iso,
                    updated_date=now_iso
                )
                db.add(threat)
                threat_count += 1

            # Always save as NewsItem (for signal generation context)
            news = NewsItem(
                id=str(uuid.uuid4()),
                title=title,
                summary=item.get('description', ''),
                source=item.get('source', ''),
                url=item.get('source_url', ''),
                category=item.get('category', 'geopolitics'),
                sentiment=item.get('sentiment', 'neutral'),
                affected_assets=assets_str,
                region=item.get('region', 'Global'),
                published_at=pub_at,
                created_date=now_iso,
                updated_date=now_iso
            )
            db.add(news)
            news_count += 1

        db.commit()

    logger.info(f"[News] Saved {threat_count} threats, {news_count} news items")
    return {'threats': threat_count, 'news': news_count}

