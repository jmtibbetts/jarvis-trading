"""Curated sources for the news and threat intelligence pipeline."""

RSS_FEEDS = [
    # Global news and newspapers
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "source": "BBC World", "category": "geopolitics", "kind": "television"},
    {"url": "https://www.aljazeera.com/xml/rss/all.xml", "source": "Al Jazeera", "category": "geopolitics", "kind": "television"},
    {"url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "source": "NYT World", "category": "geopolitics", "kind": "newspaper"},
    {"url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml", "source": "WSJ World", "category": "geopolitics", "kind": "newspaper"},
    {"url": "https://www.theguardian.com/world/rss", "source": "The Guardian World", "category": "geopolitics", "kind": "newspaper"},
    {"url": "https://rss.politico.com/politics-news.xml", "source": "Politico", "category": "politics", "kind": "newspaper"},
    {"url": "https://feeds.npr.org/1001/rss.xml", "source": "NPR News", "category": "geopolitics", "kind": "radio"},
    {"url": "https://www.pbs.org/newshour/feeds/rss/headlines", "source": "PBS NewsHour", "category": "geopolitics", "kind": "television"},
    {"url": "https://www.cbsnews.com/latest/rss/main", "source": "CBS News", "category": "geopolitics", "kind": "television"},
    {"url": "https://feeds.nbcnews.com/nbcnews/public/news", "source": "NBC News", "category": "geopolitics", "kind": "television"},
    {"url": "https://abcnews.go.com/abcnews/topstories", "source": "ABC News", "category": "geopolitics", "kind": "television"},
    {"url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", "source": "BBC Middle East", "category": "conflict", "kind": "television"},
    {"url": "https://feeds.bbci.co.uk/news/world/europe/rss.xml", "source": "BBC Europe", "category": "conflict", "kind": "television"},
    {"url": "https://feeds.bbci.co.uk/news/world/asia/rss.xml", "source": "BBC Asia", "category": "conflict", "kind": "television"},

    # Defense and security
    {"url": "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml", "source": "Defense News", "category": "conflict", "kind": "trade_press"},
    {"url": "https://breakingdefense.com/feed/", "source": "Breaking Defense", "category": "conflict", "kind": "trade_press"},

    # Markets and business
    {"url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "source": "WSJ Markets", "category": "finance", "kind": "newspaper"},
    {"url": "https://finance.yahoo.com/rss/topstories", "source": "Yahoo Finance", "category": "finance", "kind": "digital"},
    {"url": "https://www.marketwatch.com/rss/topstories", "source": "MarketWatch", "category": "finance", "kind": "digital"},
    {"url": "https://www.cnbc.com/id/10001147/device/rss/rss.html", "source": "CNBC Markets", "category": "finance", "kind": "television"},
    {"url": "https://feeds.bloomberg.com/markets/news.rss", "source": "Bloomberg Markets", "category": "finance", "kind": "television"},

    # Crypto
    {"url": "https://cointelegraph.com/rss", "source": "CoinTelegraph", "category": "crypto", "kind": "trade_press"},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml", "source": "CoinDesk", "category": "crypto", "kind": "trade_press"},
    {"url": "https://decrypt.co/feed", "source": "Decrypt", "category": "crypto", "kind": "trade_press"},

    # AI platforms, chips, data centers, power, and supply chains
    {"url": "https://blogs.nvidia.com/feed/", "source": "NVIDIA Blog", "category": "ai_infrastructure", "kind": "manufacturer"},
    {"url": "https://ir.amd.com/rss/news-releases.xml", "source": "AMD Newsroom", "category": "semiconductors", "kind": "manufacturer"},
    {"url": "https://newsroom.intel.com/feed", "source": "Intel Newsroom", "category": "semiconductors", "kind": "manufacturer"},
    {"url": "https://investors.micron.com/rss/news-releases.xml", "source": "Micron Newsroom", "category": "semiconductors", "kind": "manufacturer"},
    {"url": "https://ir.appliedmaterials.com/rss/news-releases.xml", "source": "Applied Materials Newsroom", "category": "semiconductors", "kind": "equipment_manufacturer"},
    {"url": "https://investors.broadcom.com/rss/news-releases.xml", "source": "Broadcom Newsroom", "category": "semiconductors", "kind": "manufacturer"},
    {"url": "https://aws.amazon.com/blogs/machine-learning/feed/", "source": "AWS Machine Learning", "category": "ai_infrastructure", "kind": "cloud_provider"},
    {"url": "https://www.datacenterdynamics.com/en/rss/", "source": "Data Center Dynamics", "category": "data_centers", "kind": "trade_press"},
    {"url": "https://www.servethehome.com/feed/", "source": "ServeTheHome", "category": "data_centers", "kind": "trade_press"},
    {"url": "https://www.hpcwire.com/feed/", "source": "HPCwire", "category": "ai_infrastructure", "kind": "trade_press"},
    {"url": "https://www.eetimes.com/feed/", "source": "EE Times", "category": "semiconductors", "kind": "trade_press"},
    {"url": "https://semiengineering.com/feed/", "source": "Semiconductor Engineering", "category": "semiconductors", "kind": "trade_press"},
    {"url": "https://www.supplychaindive.com/feeds/news/", "source": "Supply Chain Dive", "category": "supply_chain", "kind": "trade_press"},
    {"url": "https://www.freightwaves.com/feed", "source": "FreightWaves", "category": "supply_chain", "kind": "trade_press"},
    {"url": "https://www.eia.gov/rss/todayinenergy.xml", "source": "U.S. EIA", "category": "energy", "kind": "government"},
    {"url": "https://www.utilitydive.com/feeds/news/", "source": "Utility Dive", "category": "energy", "kind": "trade_press"},

    # General technology and energy
    {"url": "https://feeds.arstechnica.com/arstechnica/index", "source": "Ars Technica", "category": "tech", "kind": "digital"},
    {"url": "https://techcrunch.com/feed/", "source": "TechCrunch", "category": "tech", "kind": "digital"},
    {"url": "https://oilprice.com/rss/main", "source": "OilPrice.com", "category": "energy", "kind": "trade_press"},
]


GDELT_QUERY = (
    '("data center" OR semiconductor OR "AI chip" OR GPU OR HBM OR '
    '"power grid" OR transformer OR "supply chain" OR tariff OR sanctions OR cyberattack) '
    'sourcelang:english'
)


CATEGORY_ORDER = [
    "social_politics", "conflict", "geopolitics", "politics", "finance",
    "crypto", "energy", "ai_infrastructure", "semiconductors",
    "data_centers", "supply_chain", "tech",
]
