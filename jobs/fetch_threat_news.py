"""
Job: Fetch Threat News v6.2 — RSS → LLM analysis → DB storage.
v6.2: thinking=False for news classification (no chain-of-thought needed), batch capped at 20.
v6.1: Dedup window reduced from 24h to 2h so new articles get through each run.
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

def fetch_feed(feed: dict) -> list[dict]:
    try:
        parsed = feedparser.parse(feed['url'])
        articles = []
        for entry in parsed.entries[:10]:
            title = entry.get('title', '').strip()
            if not title:
                continue
            summary = entry.get('summary', '') or entry.get('description', '')
            # Strip HTML tags
            summary = re.sub(r'<[^>]+>', ' ', summary).strip()[:800]
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
    """Send a batch of articles to LLM for threat/sentiment analysis."""
    batch_text = '\n'.join([
        f"{i+1}. [{a['source']}] {a['title']} — {a['summary'][:200]}"
        for i, a in enumerate(articles)
    ])
    
    prompt = f"""Analyze these {len(articles)} news articles for geopolitical threats and market relevance.

{batch_text}

For each article that is a significant geopolitical threat OR has clear market impact, output a JSON object.
Skip routine/low-importance news.

Output a JSON array:
[{{
  "index": 1,
  "is_threat": true,
  "title": "cleaned title",
  "description": "2-3 sentence summary",
  "event_type": "one of: military_conflict, political_crisis, economic_sanctions, natural_disaster, cyber_attack, terrorism, trade_war, energy_crisis, political_turmoil, market_event",
  "severity": "one of: Critical, High, Medium, Low",
  "country": "primary country/region affected",
  "region": "one of: Middle East, Europe, Asia Pacific, North America, South America, Africa, Global",
  "sentiment": "one of: positive, negative, neutral",
  "affected_assets": ["list", "of", "ticker", "symbols", "affected"],
  "category": "finance/geopolitics/crypto/energy/tech/conflict"
}}]

Only include articles that are genuinely significant. Return ONLY the JSON array."""

    try:
        response = call_lm_studio(prompt, max_tokens=4096, temperature=0.1, thinking=False)
        parsed = parse_json(response)
        if isinstance(parsed, list):
            return parsed
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
    #    (24h window caused all articles to be marked seen after first run)
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
    
    # 3. Analyze in ONE consolidated LLM call (max 30 articles)
    # Capping at 30 prevents context overflow and avoids hogging the local LLM
    # with repeated calls that block signal generation.
    analyzed = []
    cap = new_articles[:20]
    if cap:
        logger.info(f"[News] Sending {len(cap)} articles to LLM for analysis (thinking=False, fast classify)...")
        results = analyze_batch(cap)
        for r in results:
            idx = r.get('index', 1) - 1
            if 0 <= idx < len(cap):
                r['source_url'] = cap[idx].get('url', '')
                r['source']     = cap[idx].get('source', r.get('source', ''))
                r['published']  = cap[idx].get('published', '')
        analyzed.extend(results)
        logger.info(f"[News] LLM returned {len(analyzed)} classified items")
    
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
            
            # Save as ThreatEvent if it's flagged as a threat
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
            
            # Always save as NewsItem
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
        
        # Prune old records (>7 days)
        prune_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        db.query(ThreatEvent).filter(ThreatEvent.created_date < prune_cutoff).delete()
        db.query(NewsItem).filter(NewsItem.created_date < prune_cutoff).delete()
    
    logger.info(f"[News] Saved {threat_count} threats, {news_count} news items")

    # Notify event bus if new intelligence arrived — triggers immediate signal re-generation
    if threat_count > 0 or news_count > 0:
        try:
            from app.scheduler import notify_new_intelligence
            notify_new_intelligence()
        except Exception as e:
            logger.debug(f"[News] Event notify failed: {e}")

    return {'threats': threat_count, 'news': news_count}

