"""Intelligence & market data routes — Phase 7 split of app/routes.py. Bodies are
verbatim; shared helpers live in app.routers.common."""
from fastapi import APIRouter

from app.routers.common import *  # noqa: F401,F403
from app.routers.common import _CONGRESS_DISCLAIMER, _FOCUS_SCANS, _FOCUS_SCAN_LOCK, _asset_dict, _build_crypto_markets, _congress_trade_dict, _focus_signal_ids, _holdings_for_period, _ingestion_run_dict, _insider_tx_dict, _institutional_disclaimer, _institutional_periods, _news_dict, _parse_datetime, _select_comparison_periods, _source_health_dict, _threat_dict  # noqa: E501

router = APIRouter()


@router.get("/threats")
def get_threats(limit: int = 60, confirmation: str = None, min_reliability: float = None):
    with get_db() as db:
        query = db.query(ThreatEvent).filter(ThreatEvent.status == "Active")
        if confirmation:
            query = query.filter(ThreatEvent.confirmation_status == confirmation)
        if min_reliability is not None:
            query = query.filter(ThreatEvent.reliability_score >= min_reliability)
        rows = query.order_by(ThreatEvent.created_date.desc()).limit(min(max(limit, 1), 500)).all()
        return [_threat_dict(t) for t in rows]

@router.get("/news")
def get_news(limit: int = 80, source: str = None, category: str = None,
             asset: str = None, confirmation: str = None,
             min_reliability: float = None, stale: Optional[bool] = None,
             freshness_hours: int = None):
    with get_db() as db:
        query = db.query(NewsItem)
        if source:
            query = query.filter(NewsItem.source == source)
        if category:
            query = query.filter(NewsItem.category == category)
        if asset:
            query = query.filter(NewsItem.affected_assets.contains(asset.upper()))
        if confirmation:
            query = query.filter(NewsItem.confirmation_status == confirmation)
        if min_reliability is not None:
            query = query.filter(NewsItem.reliability_score >= min_reliability)
        # Publication dates were historically stored as both RFC 2822 and ISO strings.
        # Parse them before sorting/filtering so old RFC rows cannot outrank fresh data.
        rows = query.order_by(
            NewsItem.ingested_at.desc(), NewsItem.created_date.desc()
        ).limit(2500).all()
        items = [_news_dict(row) for row in rows]
        if stale is not None:
            items = [item for item in items if bool(item.get("is_stale")) == stale]
        if freshness_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, freshness_hours))
            items = [item for item in items if (
                _parse_datetime(item.get("published_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ) >= cutoff]
        items.sort(
            key=lambda item: _parse_datetime(item.get("published_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return items[:min(max(limit, 1), 500)]


@router.get("/intelligence/sources")
def get_intelligence_sources():
    with get_db() as db:
        rows = db.query(IntelligenceSourceHealth).order_by(
            IntelligenceSourceHealth.consecutive_failures.desc(),
            IntelligenceSourceHealth.source.asc(),
        ).all()
        return [_source_health_dict(row) for row in rows]


@router.get("/intelligence/status")
def get_intelligence_status():
    """Snapshot-served: 18 seconds for a 507-byte answer, measured
    2026-08-15. Source health changes on the ingestion cycle, not per
    request (§140.3)."""
    from lib.snapshot_cache import cached
    return cached("intel:status", 300, _intelligence_status)


def _intelligence_status():
    with get_db() as db:
        sources = db.query(IntelligenceSourceHealth).all()
        latest = db.query(IntelligenceIngestionRun).order_by(
            IntelligenceIngestionRun.finished_at.desc()
        ).first()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            row for row in db.query(NewsItem).all()
            if (_parse_datetime(row.published_at)
                or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
        ]

        source_rows = [_source_health_dict(row) for row in sources]
        healthy = sum(1 for row in source_rows if row["status"] == "healthy")
        failing = sum(1 for row in source_rows if row["status"] == "failing")
        return {
            "status": "degraded" if failing else "healthy" if source_rows else "not_run",
            "source_count": len(source_rows),
            "healthy_sources": healthy,
            "failing_sources": failing,
            "recent_news": len(recent),
            "corroborated_recent": sum(1 for row in recent if row.confirmation_status == "corroborated"),
            "social_unconfirmed_recent": sum(1 for row in recent if row.confirmation_status == "unconfirmed_social"),
            "latest_run": _ingestion_run_dict(latest) if latest else None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

@router.get("/options/{symbol}/summary")
def get_options_summary(symbol: str, current_price: float = None, dte_max: int = 45):
    """Options chain intelligence for one underlying — real chain data from
    Alpaca's options market data API (same broker/credentials as the rest
    of this app, no new vendor). See lib/options_analytics.py for exactly
    what's computed and, just as importantly, what's deliberately excluded
    (no open-interest-based "unusual activity" detection — Alpaca's
    snapshot doesn't expose open interest, so this doesn't approximate it)."""
    from lib.options_analytics import get_chain_summary
    price = current_price
    if price is None:
        with get_db() as db:
            asset = db.query(MarketAsset).filter(MarketAsset.symbol == symbol.upper()).first()
            price = asset.price if asset else None
    if not price:
        raise HTTPException(400, f"No current price available for {symbol} — pass current_price explicitly")
    summary = get_chain_summary(symbol, current_price=price, dte_max=dte_max)
    if not summary:
        raise HTTPException(503, f"Options data unavailable for {symbol} — no chain data returned")
    return summary


@router.get("/market/{symbol:path}/chart")
def market_chart(symbol: str, timeframe: str = "1H", limit: int = 3000):
    """Everything the chart surface needs in one read: cached bars plus
    this symbol's signals and open paper positions, so a trade can be
    SEEN on price — entries, stops and targets where they actually sit.
    Serves the deep-history cache; no external fetch happens here."""
    from datetime import datetime, timedelta, timezone

    from lib.instruments import canonical, variants
    from lib.signal_replay import load_cached_bars

    sym = canonical(symbol)
    limit = max(50, min(int(limit), 20_000))
    bars_df = load_cached_bars(sym, timeframe)
    bars = []
    if bars_df is not None and len(bars_df):
        tail = bars_df.tail(limit)
        for ts, row in tail.iterrows():
            bars.append({
                "time": int(ts.timestamp()),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or 0),
            })

    forms = list(variants(sym))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with get_db() as db:
        sigs = (db.query(TradingSignal)
                  .filter(TradingSignal.asset_symbol.in_(forms))
                  .filter(TradingSignal.generated_at > cutoff)
                  .order_by(TradingSignal.generated_at.desc())
                  .limit(200).all())
        signals = [{
            "id": s.id, "direction": s.direction,
            "entry_price": s.entry_price, "stop_loss": s.stop_loss,
            "target_price": s.target_price, "status": s.status,
            "generated_at": s.generated_at, "timeframe": s.timeframe,
            "llm_model": s.llm_model,
        } for s in sigs]
        from sqlalchemy import func

        from app.database import PaperPosition
        # LOWER() is load-bearing: writers store "Open" and SQLite's `=` is
        # case-sensitive, so this filter matched nothing and the chart's
        # position overlay was permanently empty. Same defect as the
        # concentration guard (lib/concentration.open_rows).
        poss = (db.query(PaperPosition)
                  .filter(PaperPosition.symbol.in_(forms))
                  .filter(func.lower(PaperPosition.status) == "open").all())
        positions = [{
            "id": p.id, "direction": p.direction, "qty": p.qty,
            "entry_price": p.entry_price, "stop_loss": p.stop_loss,
            "target_price": p.target_price,
            "initial_stop_loss": p.initial_stop_loss,
            "opened_at": p.opened_at,
        } for p in poss]
    return {"symbol": sym, "timeframe": timeframe, "bars": bars,
            "signals": signals, "positions": positions,
            "bar_count": len(bars)}


@router.get("/market/{symbol:path}/analogs")
def market_analogs(symbol: str, timeframe: str = "15m", top_k: int = 12):
    """Historical analogs of the current moment — what followed the most
    similar non-overlapping past shapes. History, not prediction."""
    from lib.analogs import analogs_for
    from lib.instruments import canonical

    out = analogs_for(canonical(symbol), timeframe,
                      top_k=max(5, min(top_k, 25)))
    if out is None:
        raise HTTPException(
            404, f"not enough cached history for {symbol}@{timeframe} — "
                 "analogs need a deep corpus, not anecdotes")
    return out


@router.get("/dex/discovery")
def dex_discovery(confirm: bool = True):
    """New DEX listings that cleared the quality floors — the second
    desk's discovery layer. Keyless (GeckoTerminal + DEX Screener).

    Surviving here means 'start collecting history', NOT 'trade this':
    no signal, no sizing, and nothing enters the majors book's gate. The
    rejection tally is reported because the feed is overwhelmingly noise
    and hiding that would make the survivors look more special than they
    are."""
    from lib.dex_discovery import discover
    return discover(confirm=confirm)


@router.get("/onchain/context")
def onchain_context():
    """Network-level on-chain state — the SLOW context layer.

    MVRV is the classic cycle gauge, and the level alone means nothing:
    2.4 is euphoric for one asset and ordinary for another, so what is
    reported is the percentile against the asset's own trailing 2 years.
    This describes conditions, never entries — it is the weather the
    signals happen in.

    Reports staleness EXPLICITLY. lib.onchain.latest_context returns an
    empty dict both when the series is stale past its 4-day tolerance and
    when it was never synced at all, which is the right call for a join
    key (a week-old reading must not masquerade as today's network state)
    but the wrong call for an ops panel — "no data" and "data we refuse to
    use" need different fixes, so they get different labels here.
    """
    from datetime import datetime, timezone

    from lib.onchain import ASSETS, latest_context
    from lib.sector_engine import _pctile, _series as released

    asof = datetime.now(timezone.utc)
    out = []
    for symbol in ASSETS:
        row: dict = {"symbol": symbol}
        try:
            mvrv = released(symbol, "cm_CapMVRVCur", asof)
            active = released(symbol, "cm_AdrActCnt", asof)
            if not mvrv:
                row["state"] = "never_synced"
                row["detail"] = "no released MVRV rows in the event store"
            else:
                age = (asof.date() - mvrv[-1][0]).days
                row["mvrv"] = round(mvrv[-1][1], 3)
                row["mvrv_pctile_2y"] = _pctile(mvrv, years=2)
                row["mvrv_age_days"] = age
                row["as_of"] = mvrv[-1][0].isoformat()
                row["observations"] = len(mvrv)
                # Same 4-day tolerance the join applies, named here.
                row["state"] = "stale" if age > 4 else "fresh"
                if row["state"] == "stale":
                    row["detail"] = (f"{age}d old — past the 4-day tolerance, "
                                     f"so signals are NOT joined against it")
            if active:
                row["active_addresses"] = active[-1][1]
                row["active_addr_pctile_2y"] = _pctile(active, years=2)
            # What the candidate join would actually receive right now.
            row["joined"] = bool(latest_context(symbol, asof))
        except Exception as e:
            row["state"] = "error"
            row["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
        out.append(row)
    return {"assets": out,
            "note": ("daily-frequency data on a daily clock; MVRV percentile "
                     "is the cycle gauge, not a trade trigger")}


@router.get("/wallet/activity/status")
def wallet_activity_status():
    """Configuration and recent yield of the Solana wallet-flow collector.

    Reports what is stored, not just that the poller ran: a collector that
    fetches happily and lands nothing is the failure this desk keeps
    meeting, and "0 events" must be distinguishable from "not configured".
    """
    from lib.wallet_activity import status as wa_status

    out = wa_status()
    try:
        from lib.event_store import get_store
        # kind_summary, NOT read(): read() filters on an exact symbol, so
        # read(None, "onchain", ...) returns [] regardless of what is
        # stored — a panel built on it would report "0 events" forever.
        s = get_store().kind_summary("onchain", source="helius")
        out["events_stored"] = s["events"]
        out["newest_ingest_ts"] = s["newest_ingest_ts"]
        out["top_symbols"] = s["by_symbol"][:8]
    except Exception as e:
        out["store_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


@router.get("/helius/health")
def helius_health():
    """Reachability and per-endpoint metrics for the single Helius door.

    `lib/helius_client.py` has recorded latency, call counts and error
    counts per endpoint since it was written, and none of it was readable
    without a Python prompt. Cheap: health() makes two calls and never
    raises, metrics() makes none.
    """
    from lib import helius_client

    out = {"configured": helius_client.configured(),
           "metrics": helius_client.metrics()}
    if not out["configured"]:
        out["detail"] = "HELIUS_API_KEY not set"
        return out
    out["health"] = helius_client.health()
    return out


# How much live API spend one /wallet/intel call is allowed to make. The
# analysis needs transfers per wallet, one batched identity call, and one
# funded-by per wallet; without ceilings a large watchlist turns a page
# refresh into a rate-limit incident.
_WALLET_INTEL_MAX_WALLETS = 12
_WALLET_INTEL_MAX_TRANSFERS = 100


@router.get("/wallet/intel")
def wallet_intel_report(limit: int = _WALLET_INTEL_MAX_TRANSFERS):
    """Wallet Alpha over the watched Solana wallets: whales, exchange flow,
    clusters, coordination, and USD coverage.

    Everything here already existed in `lib/wallet_intel.py` and
    `lib/token_pricing.py` with no route and no UI. The analysis is pure;
    this endpoint is the I/O around it.

    Two invariants the response is shaped by:

    §116 — every record is stamped WALLET_ALPHA. This is a research
    population and must never reach majors expectancy, calibration, the
    Gate, training or risk. Surfacing it in the UI is not contamination;
    feeding it to a model would be.

    §117 — coordination reports BOTH the raw wallet count and the
    independent cluster count, because one actor splitting a position
    across three addresses is one opinion, not three.

    This makes live Helius calls (bounded above), so it is deliberately
    not on any poll loop.
    """
    from collections import defaultdict

    from lib import helius_client, token_pricing, wallet_intel
    from lib.wallet_activity import _page_limit, parse_transfers

    from lib import wallet_registry

    import os
    key = os.getenv("HELIUS_API_KEY", "").strip()
    page_limit = _page_limit()
    # THE population — the same selector wallet_activity uses. This route
    # previously called `wallet_activity._config()`, which read
    # `HELIUS_WATCH_WALLETS`, so it analysed a different population from the
    # one discovery was filling while importing wallet_registry on the very
    # next line for its counts.
    wallets = wallet_registry.get_monitorable_wallets()

    # §21: `configured` reflects whether the SUBSYSTEM can work — a Helius
    # connection and the feature switch — not whether somebody hand-typed a
    # wallet list. Reporting configured:false for a blank env var made an
    # environment variable the database and hid a working client behind an
    # empty string.
    enabled = wallet_registry.intelligence_enabled()
    registry_counts = wallet_registry.counts()
    base = {
        "configured": enabled,
        "enabled": enabled,
        "helius": {"connected": bool(key)},
        "discovery": {"enabled": wallet_registry.discovery_enabled()},
        "wallets": registry_counts,
        "research_population": wallet_intel.RESEARCH_POPULATION,
        "features": {
            "whale_detection": True, "smart_money": True,
            "alpha_scoring": True, "copy_scoring": True,
            "coordination": True, "clustering": True,
            "funding_analysis": True,
        },
    }

    if not key:
        return {**base, "configured": False, "enabled": False,
                "detail": "HELIUS_API_KEY not set"}

    # The live analysis below reads TRANSFERS for specific addresses, so it
    # still needs at least one wallet. That is a different statement from
    # "the subsystem is unconfigured", and the response now says which.
    if not wallets:
        why = wallet_registry.monitorable_breakdown().get("reason", "")
        return {**base,
                "detail": (f"No monitorable wallets yet — {why}. Discovery is "
                           + ("enabled and will populate the registry."
                              if base["discovery"]["enabled"]
                              else "disabled; set HELIUS_WALLET_DISCOVERY_ENABLED=true "
                                   "or add seeds to HELIUS_WATCH_WALLETS.")),
                "population_source": "wallet_registry",
                "analysis": None}

    watched = wallets[:_WALLET_INTEL_MAX_WALLETS]
    lim = max(1, min(int(limit or page_limit), _WALLET_INTEL_MAX_TRANSFERS))
    errors: list[str] = []

    # ── 1. transfers per wallet ──────────────────────────────────────────
    rows: list[dict] = []
    for addr in watched:
        try:
            rows.extend(parse_transfers(helius_client.transfers(addr, lim), addr))
        except Exception as e:
            errors.append(f"{addr[:8]}… transfers: {type(e).__name__}: {str(e)[:120]}")

    if not rows:
        return {"configured": True, "wallets_watched": len(wallets),
                "wallets_queried": len(watched), "transfers": 0,
                "errors": errors,
                "detail": ("no transfers returned for the watched wallets — "
                           "a quiet chain and a broken parser look the same "
                           "here only if `errors` is empty"),
                "research_population": wallet_intel.RESEARCH_POPULATION}

    # ── 2. price them (peg -> helius -> market -> abstain) ───────────────
    mints = [r.get("mint") for r in rows if r.get("mint")]
    try:
        prices = token_pricing.resolve_prices(mints, address_for_helius=watched[0])
    except Exception as e:
        prices, _ = {}, errors.append(f"pricing: {type(e).__name__}: {str(e)[:120]}")
    valued = token_pricing.value_transfers(rows, prices)
    cov = token_pricing.coverage(valued)

    # ── 3. classify counterparties in ONE batched call ───────────────────
    identities: dict[str, dict] = {}
    cps = [r.get("counterparty") for r in valued if r.get("counterparty")]
    if cps:
        try:
            identities = helius_client.batch_identity(list(dict.fromkeys(cps)))
        except Exception as e:
            errors.append(f"batch-identity: {type(e).__name__}: {str(e)[:120]}")

    flows = wallet_intel.exchange_flows(valued, identities)

    # ── 4. whales, judged both absolutely and per-wallet ─────────────────
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in valued:
        by_wallet[t.get("wallet")].append(t)
    baselines = {w: wallet_intel.wallet_baseline(ts) for w, ts in by_wallet.items()}

    whales = []
    for t in valued:
        s = wallet_intel.whale_score(t, baselines.get(t.get("wallet")))
        if s["score"] > 0:
            whales.append({**t, "whale": s})
    whales.sort(key=lambda w: -w["whale"]["score"])

    # ── 5. clusters. funded-by 404s legitimately and returns {} ──────────
    funding: dict[str, dict] = {}
    for addr in watched:
        try:
            funding[addr] = helius_client.funded_by(addr)
        except Exception as e:
            errors.append(f"{addr[:8]}… funded-by: {type(e).__name__}: {str(e)[:120]}")
    clusters = wallet_intel.cluster_by_funder(funding, identities)
    cluster_map = {m: c["funder"] for c in clusters
                   if not c["is_infrastructure_funder"] and c["size"] > 1
                   for m in c["members"]}
    independence = wallet_intel.independent_clusters(
        [r.get("wallet") for r in valued], cluster_map)

    # ── 6. coordination, scored on clusters not wallets (§117) ───────────
    coordination = wallet_intel.coordination_score(
        [{"wallet": t.get("wallet"), "symbol": t.get("symbol"),
          "direction": t.get("direction"), "timestamp": t.get("timestamp")}
         for t in valued],
        cluster_map=cluster_map)

    return wallet_intel.stamp_population({
        "configured": True,
        "wallets_watched": len(wallets),
        "wallets_queried": len(watched),
        "wallets_truncated": max(0, len(wallets) - len(watched)),
        "transfer_limit": lim,
        "transfers": len(valued),
        "pricing": cov,
        "exchange_flows": [wallet_intel.stamp_population(f) for f in flows[:40]],
        "whales": [wallet_intel.stamp_population(w) for w in whales[:25]],
        "clusters": clusters[:20],
        # BOTH numbers, always — §117. The UI shows them side by side.
        "independence": independence,
        "coordination": coordination,
        "errors": errors,
        "boundary_note": (
            "WALLET_ALPHA is a separate research population (§116). It is "
            "shown here and cross-links freely, but never enters majors "
            "expectancy, calibration, the Gate, training or risk."),
    })


@router.get("/market/chart-symbols")
def market_chart_symbols():
    """Distinct (symbol, timeframe) coverage of the bar cache — the chart
    surface's picker is honest about what it can actually draw."""
    from sqlalchemy import text as _t

    from lib.ohlcv_cache import get_cache_db

    # Served from backfill_status (1,528 rows), NOT from ohlcv_bars (38.2
    # MILLION rows). The DISTINCT was a full table scan measured at 2,114 ms
    # to produce a list the tracking table already holds exactly — same
    # 1,528 (symbol, timeframe) pairs, 0 ms. That scan ran every time the
    # Charts page built its symbol picker.
    out: dict[str, list[str]] = {}
    with get_cache_db() as conn:
        for sym, tf in conn.execute(
                _t("SELECT symbol, timeframe FROM backfill_status")):
            out.setdefault(sym, []).append(tf)
    return {"symbols": [{"symbol": s, "timeframes": sorted(tfs)}
                        for s, tfs in sorted(out.items())]}


@router.get("/orderbook/{symbol}")
def get_orderbook(symbol: str):
    """Latest in-memory Level 2 snapshot for a symbol from both exchanges
    (Binance + Coinbase) — populated by the long-lived WS streams started in
    main.py's lifespan. Used for initial page load; live updates arrive over
    the app's own /ws WebSocket as "orderbook" messages."""
    from lib.orderbook_stream import get_latest_snapshot
    symbol = symbol.upper()
    binance = get_latest_snapshot("binance", symbol)
    coinbase = get_latest_snapshot("coinbase", symbol)
    if not binance and not coinbase:
        raise HTTPException(503, f"No order book data yet for {symbol} — streams may still be connecting")
    return {"symbol": symbol, "binance": binance, "coinbase": coinbase}


@router.get("/alerts")
def get_alerts(hours: int = 24, severity: str = None, limit: int = 200):
    """Cross-module alert feed (lib/alert_engine.py) — insider notable buys,
    large crypto liquidations, kill-switch trips, etc. Complements the
    live WS "alert" broadcast with a page-load-time snapshot."""
    from lib.alert_engine import get_recent_alerts
    return get_recent_alerts(hours=hours, severity=severity, limit=limit)


@router.get("/shortinterest/{symbol}")
def get_short_interest(symbol: str):
    """FINRA consolidated short interest + squeeze-fuel score for one symbol
    — free FINRA Query API, no vendor. This is SEMI-MONTHLY, DELAYED data
    (~8 business day publish lag; see reporting_lag_days), not a live short
    book. Short-interest-as-%-of-float is deliberately absent: FINRA
    publishes no shares-outstanding figure, so it can't be computed
    honestly. See lib/short_interest.py."""
    from lib.short_interest import fetch_symbol_short_interest
    result = fetch_symbol_short_interest(symbol)
    if not result:
        raise HTTPException(404, f"No published short interest for {symbol.upper()} at the latest settlement date")
    return result


@router.get("/shortinterest/squeeze/top")
def get_top_squeeze(limit: int = 25, min_days_to_cover: float = 3.0, exclude_funds: bool = True):
    """Highest squeeze-fuel symbols for the latest published settlement
    date. Excludes OTC, FINRA's 999.99 days-to-cover sentinel, and (by
    default) ETFs/ETNs/SPAC units/warrants — vehicles that structurally
    can't squeeze and would otherwise dominate the ranking. Everything
    dropped is reported in the `excluded` counts rather than hidden."""
    from lib.short_interest import get_top_squeeze_candidates
    result = get_top_squeeze_candidates(
        limit=limit, min_days_to_cover=min_days_to_cover, exclude_funds=exclude_funds
    )
    if not result:
        raise HTTPException(503, "FINRA short interest data unavailable")
    return result


@router.get("/crypto/markets")
def get_crypto_markets():
    """CoinGecko market structure for every mapped tracked coin — price,
    1h/24h/7d change, volume, market cap, distance from ATH. Serve-stale-
    while-revalidate via the persisted cache (demo tier 30 req/min; this
    costs ~1 call per 5 min)."""
    from lib.api_cache import serve_with_refresh
    payload, stale = serve_with_refresh("crypto_markets", 300, _build_crypto_markets)
    if payload is None:
        return {"coins": [], "as_of": None, "note": "CoinGecko unavailable"}
    return {**payload, "stale": stale}


@router.get("/news/web")
def get_web_news():
    """The exact FRESH WEB NEWS block injected into signal-generation LLM
    prompts (tavily-first, exa fallback, 30-min cache) — surfaced so the
    user sees what the model sees. Reads the shared cache; costs nothing."""
    from jobs.generate_signals import _mcp_market_headlines, _web_headlines_cache
    text = _mcp_market_headlines()
    at = _web_headlines_cache.get("at")
    items = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if ln.startswith("- "):
            head, _, body = ln[2:].partition(": ")
            items.append({"title": head.strip(), "snippet": body.strip() or None})
    if not items and text:
        items = [{"title": t.strip(), "snippet": None} for t in text.split(" | ") if t.strip()]
    return {
        "items": items,
        "as_of": at.isoformat() if at else None,
        "raw": text or "",
        "note": "Unverified live web search results — exactly what signal generation injects into the LLM. Refreshes every 30 min.",
    }


@router.get("/calendar/catalysts")
def get_catalyst_calendar():
    """Universal catalyst calendar — deterministic market dates plus events
    from feeds this system already ingests. Every entry states its
    granularity/approximation; see lib/catalyst_calendar.py for what is
    deliberately absent (economic-release schedules) and why."""
    from lib.catalyst_calendar import assemble_calendar

    earnings = set()
    try:
        from lib.earnings_calendar import get_earnings_this_week
        earnings = get_earnings_this_week()
    except Exception as e:
        logger.debug(f"[Calendar] Earnings fetch failed: {e}")

    with get_db() as db:
        tracked = {
            r[0].upper() for r in db.query(MarketAsset.symbol)
            .filter(MarketAsset.asset_class == "Equity").all() if r[0]
        }
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        priced = [{
            "company_name": r.company_name, "ticker": r.ticker,
            "latest_filed_at": r.latest_filed_at,
        } for r in db.query(IpoFiling)
            .filter(IpoFiling.stage == "priced", IpoFiling.latest_filed_at >= cutoff)
            .order_by(IpoFiling.latest_filed_at.desc()).limit(10).all()
            # follow-ons by listed companies are not IPO catalysts
            if r.cover_mentions_ipo is not False]

    return assemble_calendar(
        datetime.now(timezone.utc).date(),
        earnings_tickers=earnings, tracked_equities=tracked, priced_ipos=priced,
    )


@router.post("/watchlist/focus/{symbol:path}/scan")
def scan_focus_symbol(symbol: str):
    """Interrogate ONE watched coin now, and report what it found.

    Same pipeline the scheduler runs, narrowed to this symbol: the cached
    multi-timeframe TA and indicators, the coin's accumulated behavioural
    profile, live threat/news/regime context, then scoring, level
    provenance, ATR and cost gates. Deliberately NOT a second
    implementation — a parallel scanner would eventually grade setups by
    different rules than the ones that trade them.

    Per symbol rather than per list: asking about one coin should not spend
    an LLM call on every other one, and two coins can be asked at once.

    Runs in a thread; poll GET /watchlist/focus/{symbol}/scan.

    Silence is a real answer — focus setups must clear FOCUS_MIN_SCORE, so
    "nothing ready" is the expected output most of the time.
    """
    from app.database import MarketAsset
    sym = (symbol or "").upper().strip()
    with get_db() as db:
        row = db.query(MarketAsset).filter(
            MarketAsset.symbol == sym,
            MarketAsset.is_focus == True,  # noqa: E712
        ).first()
    if not row:
        raise HTTPException(404, f"{sym} is not on the coins-to-watch list.")

    with _FOCUS_SCAN_LOCK:
        state = _FOCUS_SCANS.get(sym)
        if state and state.get("running"):
            return {"status": "already_running", "symbol": sym,
                    "started_at": state["started_at"]}
        _FOCUS_SCANS[sym] = {
            "symbol": sym, "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None, "result": None, "error": None,
        }

    def _run():
        state = _FOCUS_SCANS[sym]
        try:
            # The breaker opens after any provider failure and stays open for
            # a few seconds. A scheduled sweep can shrug that off and try
            # again next cycle; an operator who just pressed a button cannot,
            # and would get a failure for a condition that clears itself in
            # under a minute. Wait it out first.
            from jobs.paper_trading import _wait_out_llm_cooldown
            _wait_out_llm_cooldown(max_wait=45.0)

            from jobs.generate_signals import run as gen_run
            before = _focus_signal_ids(sym)
            outcome = gen_run(focus_only=True, only_symbols=[sym]) or {}
            after = _focus_signal_ids(sym)
            # A batch that never reached the model produced no verdict. Zero
            # new signals then means "nothing was considered", not "nothing
            # was ready" — reporting the second when the first is true is the
            # failure-looks-benign pattern this codebase keeps paying for.
            # Observed live: the LLM was in a post-failure cooldown, the batch
            # errored, and the UI still said "silence is a real answer here".
            failures = outcome.get("batch_failures") or []
            if failures:
                state["error"] = (
                    f"the model was not reached — {failures[0].get('error', 'batch failed')}. "
                    f"No verdict was formed for {sym}; this is not 'no setup found'."
                )
                return
            near = next((r for r in (outcome.get("focus_rejections") or [])
                         if r.get("symbol") == sym), None)
            state["result"] = {"symbol": sym,
                               "new_signal_ids": sorted(after - before),
                               "new_signals": len(after - before),
                               "evaluated": True,
                               # How close it came. The difference between
                               # "nothing ready" and "a Long at 61, ten short
                               # of the bar" is the difference between a mute
                               # button and an answer.
                               "near_miss": near}
        except Exception as e:
            logger.error(f"[Focus] scan failed for {sym}: {e}", exc_info=True)
            state["error"] = str(e)
        finally:
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
            state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "symbol": sym,
            "note": ("Focus setups must clear the focus score floor. "
                     "No new signal is a valid result, not a failure.")}


@router.get("/watchlist/focus/{symbol:path}/scan")
def focus_scan_status(symbol: str):
    """Where this coin's last on-demand scan got to."""
    sym = (symbol or "").upper().strip()
    return _FOCUS_SCANS.get(sym, {
        "symbol": sym, "running": False, "started_at": None,
        "finished_at": None, "result": None, "error": None,
    })


@router.get("/sector/{sector}")
def sector_view(sector: str):
    """4C sector engines: fundamentals with seasonal context (where a
    feed exists), COT positioning percentiles, curve structure —
    point-in-time from released data, abstaining where sources are
    stale. Shadow-only."""
    from lib.sector_engine import SECTORS, sector_snapshot
    if sector not in SECTORS:
        raise HTTPException(404, f"unknown sector '{sector}' — "
                                 f"registered: {sorted(SECTORS)}")
    return sector_snapshot(sector)


@router.get("/watchlist/focus")
def list_focus():
    """The "coins to watch" list, with each symbol's accumulated profile."""
    from app.database import MarketAsset
    out = []
    with get_db() as db:
        rows = db.query(MarketAsset).filter(MarketAsset.is_focus == True).all()  # noqa: E712
        symbols = [(r.symbol, r.focus_note, r.focus_added, r.price, r.change_percent) for r in rows]
    for sym, note, added, price, chg in symbols:
        profile = None
        try:
            from lib.focus_profile import get_or_build as _gob
            profile = _gob(sym)
        except Exception as e:
            logger.debug(f"[Focus] profile load failed for {sym}: {e}")
        out.append({
            "symbol": sym, "note": note, "added": added,
            "price": price, "change_percent": chg,
            "profile": profile,
        })
    from jobs.generate_signals import FOCUS_MIN_SCORE
    return {
        "focus": out,
        "min_score": FOCUS_MIN_SCORE,
        "note": (
            "Focus symbols are analysed every cycle ahead of everything else, across the "
            f"full indicator set, and only emit a signal at composite score >= {FOCUS_MIN_SCORE:.0f}. "
            "Silence means no setup was ready — that is the intended behaviour, not a failure."
        ),
    }


@router.post("/watchlist/focus")
def set_focus(body: FocusRequest):
    """Add or remove a symbol from the focus list. The symbol must already
    be a tracked asset (add it to the watchlist first) so prices exist."""
    from app.database import MarketAsset
    # Same venue-ticker resolution as the watchlist add, or "watch SPCX/USD"
    # would fail on a symbol the operator can see priced on their screen.
    from lib.symbol_aliases import resolve as _resolve_alias
    sym, _alias_note = _resolve_alias(body.symbol)
    with get_db() as db:
        row = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
        if not row:
            raise HTTPException(404, f"{sym} is not tracked — add it to the watchlist first")
        current = db.query(MarketAsset).filter(MarketAsset.is_focus == True).count()  # noqa: E712
        from jobs.generate_signals import FOCUS_MAX_SYMBOLS
        if body.focus and not row.is_focus and current >= FOCUS_MAX_SYMBOLS:
            raise HTTPException(
                400,
                f"Focus list is full ({current}/{FOCUS_MAX_SYMBOLS}). It is deliberately small — "
                f"remove one before adding another.",
            )
        row.is_focus = bool(body.focus)
        row.focus_note = body.note if body.focus else None
        row.focus_added = datetime.now(timezone.utc).isoformat() if body.focus else None
    # Build the profile immediately so the UI has something to show.
    if body.focus:
        try:
            from lib.focus_profile import get_or_build
            get_or_build(sym, force=True)
        except Exception as e:
            logger.debug(f"[Focus] initial profile build failed for {sym}: {e}")
    return {"ok": True, "symbol": sym, "focus": bool(body.focus)}


@router.post("/watchlist/add")
def add_watchlist_symbol(body: WatchlistAdd):
    """Add a ticker to the tracked universe (Watchlist 2.0 source). Crypto in
    BASE/USD form; equities as plain tickers. The symbol must be verifiable
    against a real data source before it is added — no unverified rows."""
    raw = (body.symbol or "").strip().upper()
    if not raw or len(raw) > 12:
        raise HTTPException(400, "Provide a symbol like NVDA or BTC/USD")

    # A venue ticker resolves to the symbol the price feed actually quotes.
    # BTCC shows SpaceX as SPCX/USD; the tracked symbol is XSPCX/USD, and the
    # operator confirms the prices match. The substitution is reported back,
    # never silent — asking for one instrument and holding another is not a
    # convenience.
    from lib.symbol_aliases import resolve as _resolve_alias
    raw, alias_note = _resolve_alias(raw)

    is_crypto = "/" in raw or raw.endswith("USD") and len(raw) > 5
    name = None
    price = None
    asset_class = "Equity"
    if is_crypto:
        from lib.crypto_market_data import fetch_crypto_prices, normalize_crypto_symbol
        sym = normalize_crypto_symbol(raw)
        rowp = fetch_crypto_prices([sym]).get(sym)
        if not rowp or not rowp.get("price"):
            # A dead end is not an answer. The same asset is very often
            # already tracked under a different ticker — SPCX/USD does not
            # exist, but SPCXB/USD and XSPCX/USD both do and one had already
            # been traded twice. Offer what the base actually matches.
            base = sym.split("/")[0].replace("X", "", 1) if sym.startswith("X") else sym.split("/")[0]
            near = []
            try:
                with get_db() as db:
                    rows = db.query(MarketAsset.symbol, MarketAsset.name).filter(
                        MarketAsset.symbol.ilike(f"%{base}%")
                    ).limit(6).all()
                    near = [f"{s} ({n})" if n and n != s else s for s, n in rows]
            except Exception:
                pass
            hint = f" Tracked symbols matching '{base}': {', '.join(near)}." if near else ""
            raise HTTPException(404, f"No exchange lists {sym} — not added.{hint}")
        price = rowp["price"]; name = sym; asset_class = "Crypto"
    else:
        sym = raw
        # verify against Massive (if keyed) or Alpaca asset lookup
        verified = False
        try:
            from lib.massive_data import get_market_summary
            summary = get_market_summary(sym, days=3)
            if summary and (summary.get("previous_close") or summary.get("daily_bars")):
                price = (summary.get("previous_close") or {}).get("close")
                verified = True
        except Exception:
            pass
        if not verified:
            try:
                from lib.alpaca_client import get_trading_client
                asset = get_trading_client().get_asset(sym)
                verified = bool(asset and asset.tradable)
                name = getattr(asset, "name", None)
            except Exception:
                pass
        if not verified:
            raise HTTPException(404, f"Could not verify {sym} against any data source — not added")

    with get_db() as db:
        existing = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
        if existing:
            return {"ok": True, "symbol": sym, "already_tracked": True,
                    "resolved_from": alias_note}
        db.add(MarketAsset(
            symbol=sym, name=name or sym, asset_class=asset_class,
            price=price, last_updated=datetime.now(timezone.utc).isoformat(),
        ))
    return {"ok": True, "symbol": sym, "asset_class": asset_class,
            "verified_price": price, "resolved_from": alias_note}


@router.get("/watchlist/enriched")
def get_enriched_watchlist(limit: int = 40, asset_class: str = None):
    """Watchlist 2.0: one row per tracked symbol fusing every intelligence
    source already ingested — price/volume, active-signal score, insider
    clusters, congressional disclosures, institutional holders, dark-pool
    presence, squeeze score. All DB/cache reads, no external calls; a flag's
    absence means no data, not a judgment."""
    from lib.insider_analytics import cluster_summary

    with get_db() as db:
        q = db.query(MarketAsset).order_by(MarketAsset.volume.desc().nullslast())
        if asset_class:
            q = q.filter(MarketAsset.asset_class == asset_class)
        # Plain dicts INSIDE the session — ORM objects read after it closes
        # raise DetachedInstanceError (same failure /opportunities/ranked had).
        assets = [{
            "symbol": a.symbol, "name": a.name, "asset_class": a.asset_class,
            "price": a.price, "change_percent": a.change_percent, "volume": a.volume,
        } for a in q.limit(min(max(limit, 1), 150)).all()]
        symbols = [a["symbol"] for a in assets]
        equity_syms = {a["symbol"].upper() for a in assets if (a["asset_class"] or "").lower() == "equity"}

        sig_by_symbol: dict = {}
        for s in (db.query(TradingSignal)
                  .filter(TradingSignal.status.in_(["Active", "PendingApproval"]),
                          TradingSignal.asset_symbol.in_(symbols)).all()):
            existing = sig_by_symbol.get(s.asset_symbol)
            if not existing or (s.composite_score or 0) > (existing["composite_score"] or 0):
                sig_by_symbol[s.asset_symbol] = {
                    "composite_score": s.composite_score, "direction": s.direction, "id": s.id,
                }

        cutoff14 = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        insider_tx: dict = {}
        for r in (db.query(InsiderTransaction)
                  .filter(InsiderTransaction.ticker.in_(equity_syms),
                          InsiderTransaction.transaction_date >= cutoff14).all()):
            insider_tx.setdefault(r.ticker, []).append({
                "owner_cik": r.owner_cik, "owner_name": r.owner_name, "is_officer": r.is_officer,
                "transaction_code": r.transaction_code, "total_value": r.total_value,
            })

        cutoff90 = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
        congress_counts: dict = {}
        for r in (db.query(CongressTrade)
                  .filter(CongressTrade.ticker.in_(equity_syms),
                          CongressTrade.transaction_date >= cutoff90).all()):
            e = congress_counts.setdefault(r.ticker, {"purchases": 0, "sales": 0})
            code = (r.transaction_code or "").upper()
            if code.startswith("P"):
                e["purchases"] += 1
            elif code.startswith("S"):
                e["sales"] += 1

        inst_periods = _institutional_periods(db)
        inst_holders: dict = {}
        if inst_periods:
            for r in (db.query(InstitutionalHolding)
                      .filter(InstitutionalHolding.period_of_report == inst_periods[0],
                              InstitutionalHolding.ticker.in_(equity_syms)).all()):
                inst_holders[r.ticker] = inst_holders.get(r.ticker, 0) + 1

    dark_pool_syms = set()
    try:
        from lib.finra_ats import get_top_activity
        dp = get_top_activity(tier="T1", limit=100)  # 12h in-process cache
        dark_pool_syms = {r["symbol"] for r in (dp or {}).get("symbols", [])}
    except Exception:
        pass

    rows = []
    for a in assets:
        sym = a["symbol"]
        usym = sym.upper()
        cluster = cluster_summary(insider_tx[usym]) if usym in insider_tx else None
        rows.append({
            **a,
            "signal": sig_by_symbol.get(sym),
            "insider_flags": cluster["flags"] if cluster else [],
            "insider_net_value": cluster["net_value"] if cluster else None,
            "congress_90d": congress_counts.get(usym),
            "institutional_holders": inst_holders.get(usym),
            "in_dark_pool_top": usym in dark_pool_syms,
        })
    return {
        "rows": rows,
        "note": (
            "Fused view of already-ingested sources. Empty flags mean no data "
            "in the window, not an all-clear. Insider window 14d, congress "
            "window 90d; institutional count is the latest ingested quarter."
        ),
    }


@router.post("/analyst/ask")
def ask_analyst(body: dict):
    """Conversational market analyst over the system's own normalized data.
    One LLM call per user question (user-initiated only — never per tick, per
    the AI-cost-control rule). The prompt carries ONLY data this system
    computed; the system prompt requires citing which blocks informed the
    answer and forbids numbers not present in the context."""
    question = (body or {}).get("question", "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    if len(question) > 500:
        raise HTTPException(400, "question too long (500 chars max)")

    context_blocks: dict = {}
    try:
        from lib.market_regime import get_regime
        r = get_regime()
        context_blocks["regime"] = {
            "label": r.get("label"), "risk": r.get("risk"), "flags": r.get("flags"),
            "spy_trend": r.get("spy_trend"), "recommendation": r.get("recommendation"),
        }
    except Exception:
        pass
    try:
        with get_db() as db:
            snap = (db.query(PsychologySnapshot)
                    .order_by(PsychologySnapshot.created_at.desc()).first())
            if snap:
                context_blocks["psychology"] = {"score": snap.score, "label": snap.label}
            alerts = (db.query(Alert)
                      .filter(Alert.created_at >= (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
                      .order_by(Alert.created_at.desc()).limit(8).all())
            context_blocks["alerts_24h"] = [
                {"severity": a.severity, "source": a.source, "title": a.title} for a in alerts
            ]
    except Exception:
        pass
    try:
        opportunities = get_ranked_opportunities(limit=5)
        context_blocks["top_opportunities"] = [{
            "symbol": o["symbol"], "direction": o["direction"],
            "opportunity_score": o["opportunity_score"],
            "smart_money_note": o["opportunity_breakdown"]["smart_money_note"],
        } for o in opportunities]
    except Exception:
        pass
    try:
        # Imported HERE, and locally, for two reasons: it lives in the
        # trading router (importing it at module scope would be circular),
        # and it was previously not imported at all — so this whole block
        # raised NameError into the bare `except: pass` below and the
        # analyst silently never saw the book it was being asked about.
        from app.routers.trading import get_portfolio_risk
        risk = get_portfolio_risk()
        context_blocks["portfolio"] = {
            "positions": risk.get("positions"),
            "var": {k: (risk.get("var") or {}).get(k) for k in ("var_pct", "var_usd", "coverage_pct_of_gross")},
            "concentration": (risk.get("concentration") or {}).get("interpretation"),
            "high_correlation_pairs": (risk.get("concentration") or {}).get("high_correlation_pairs"),
        }
    except Exception:
        pass

    import json as _json
    from lib.lmstudio import call_lm_studio, parse_json

    # MCP research step (bounded: at most ONE web search per question).
    # LM Studio's own MCP config never reaches API callers, so Jarvis drives
    # the tools itself via lib/mcp_client.py. A fast /no_think triage call
    # decides whether the question needs fresh external information and with
    # what query; the search result then becomes one more cited context
    # block. Deterministic two-step rather than model-native function
    # calling — bounded cost, graceful degradation, and the citation rule
    # still holds because the web result IS a context block.
    # Purpose-routing per the user's own map of their servers:
    #   massive -> market data, tavily -> current news, exa -> deeper
    #   research, firecrawl -> retrieve a specific article/URL.
    # Each intent falls back to whichever connected server can serve it.
    try:
        from lib.mcp_client import call_tool, list_tools

        def _first_connected(preferences: list[tuple[str, str, dict]]) -> tuple[str, str, dict] | None:
            for server, tool, args in preferences:
                if any(t.get("name") == tool for t in list_tools(server)):
                    return server, tool, args
            return None

        if any(list_tools(s) for s in ("exa", "firecrawl", "tavily", "massive")):
            from lib import llm_router as llm
            triage_raw = llm.call(
                f"Question from a trading-dashboard user: {question!r}\n\n"
                "Classify whether answering well needs external information, and "
                "which kind. For market_data, query must be the TICKER SYMBOL "
                "(e.g. NVDA, BTC/USD). Reply ONLY with JSON:\n"
                '{"intent": "news"|"research"|"fetch_url"|"market_data"|"none", '
                '"query": "search query, URL, or ticker" or null}',
                task="triage", mode=llm.FAST,
                system="You are a routing classifier. JSON only.",
                max_tokens=120, temperature=0.0,
            )
            triage = parse_json(triage_raw) or {}
            intent = triage.get("intent")
            query = str(triage.get("query") or "")[:300]
            if intent and intent != "none" and query:
                # Tool names verified LIVE against each hosted server —
                # tavily's are underscored (tavily_search/_extract), despite
                # older docs showing hyphens.
                routes_by_intent: dict[str, list] = {
                    # exa/firecrawl first — both hosted MCPs work keyless
                    # (verified live); tavily's 401s without an API key, so it
                    # sits last as an option that activates if a key is added.
                    "news": [
                        ("exa", "web_search_exa", {"query": query, "numResults": 4}),
                        ("firecrawl", "firecrawl_search", {"query": query, "limit": 4}),
                        ("tavily", "tavily_search", {"query": query, "max_results": 4}),
                    ],
                    "research": [
                        ("exa", "web_search_exa", {"query": query, "numResults": 4}),
                        ("tavily", "tavily_search", {"query": query, "max_results": 4}),
                        ("firecrawl", "firecrawl_search", {"query": query, "limit": 4}),
                    ],
                    "fetch_url": [
                        ("firecrawl", "firecrawl_scrape", {"url": query}),
                        ("tavily", "tavily_extract", {"urls": [query]}),
                        ("exa", "web_fetch_exa", {"url": query}),
                    ],
                    "market_data": [
                        # web-search fallback when Massive REST (below) has
                        # nothing for the symbol
                        ("exa", "web_search_exa", {"query": query, "numResults": 4}),
                        ("firecrawl", "firecrawl_search", {"query": query, "limit": 4}),
                    ],
                }

                # market_data goes to Massive REST first — actual numbers from
                # the user's own subscription, not web reporting. (Massive's
                # hosted MCP wants OAuth JWTs; the REST key path is what this
                # plan supports — see lib/massive_data.py for the live-verified
                # entitlement map.) Falls through to web search when the query
                # isn't a resolvable symbol.
                handled = False
                if intent == "market_data":
                    sym_guess = query.split()[0].upper().strip(",.")
                    # Crypto → CoinGecko keyless MCP first (live prices,
                    # verified within basis points of OKX); equity → Massive
                    # REST (the user's own subscription).
                    try:
                        from lib.mcp_client import coingecko_snapshot, COINGECKO_IDS
                        if sym_guess.split("/")[0] in COINGECKO_IDS:
                            cg = coingecko_snapshot([sym_guess])
                            if cg:
                                context_blocks["market_data"] = {"provider": "coingecko", "live": True, "data": str(cg)[:2000]}
                                handled = True
                    except Exception as e:
                        logger.debug(f"[Analyst] CoinGecko lookup failed: {e}")
                    if not handled:
                        # FX pairs → AllRatesToday live interbank rates
                        try:
                            from lib.fx_rates import fx_summary_block
                            fx = fx_summary_block(sym_guess)
                            if fx:
                                context_blocks["market_data"] = {"provider": "allratestoday", "live": True, "data": fx}
                                handled = True
                        except Exception as e:
                            logger.debug(f"[Analyst] AllRates lookup failed: {e}")
                    if not handled:
                        # Equities → Stocklake first: one call returns price,
                        # fundamentals (PE, margins, beta, 52w range) AND
                        # indicator state, fresher and richer than the Massive
                        # summary. Falls through on any failure.
                        try:
                            raw = call_tool("stocklake", "get_stock", {"symbol": sym_guess})
                            if raw and '"symbol"' in str(raw):
                                context_blocks["market_data"] = {
                                    "provider": "stocklake", "live": True,
                                    "data": str(raw)[:2400]}
                                handled = True
                        except Exception as e:
                            logger.debug(f"[Analyst] Stocklake lookup failed: {e}")
                    if not handled:
                        from lib.massive_data import get_market_summary
                        summary = get_market_summary(sym_guess)
                        if summary:
                            context_blocks["market_data"] = summary
                            handled = True

                if not handled:
                    choice = _first_connected(routes_by_intent.get(intent, []))
                    if choice:
                        server, tool, args = choice
                        result = call_tool(server, tool, args)
                        if result:
                            context_blocks["web_search"] = {
                                "provider": server, "tool": tool, "intent": intent, "query": query,
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                "results": result[:4000],
                            }
    except Exception as e:
        logger.debug(f"[Analyst] MCP research step skipped: {e}")

    system = (
        "You are the market analyst inside a trading dashboard. Answer the user's "
        "question using ONLY the data blocks provided. Cite which block(s) each "
        "claim comes from in [brackets]. The market_data block, when present, "
        "carries real exchange data from the Massive subscription (previous "
        "session / end-of-day, not live quotes). The web_search block, when present, is "
        "external content retrieved moments ago — attribute claims from it to "
        "[web_search] and treat it as reporting, not verified system data. If the "
        "provided data cannot answer the question, say exactly that — do not use "
        "outside knowledge for prices, levels, or events, and never invent a "
        "number that is not in the context."
    )
    prompt = f"DATA BLOCKS:\n{_json.dumps(context_blocks, default=str)}\n\nQUESTION: {question}"
    try:
        from lib import llm_router as llm
        # A question answered from one data block is a lookup; one that has
        # to reconcile TA against web reporting against exchange data is
        # synthesis across sources that routinely disagree.
        answer = llm.call(
            prompt, task="analyst_chat", mode=llm.AUTO,
            context={"cross_asset": len(context_blocks) > 2,
                     "contradiction_count": len(context_blocks) - 1},
            system=system, max_tokens=600, temperature=0.2)
    except Exception as e:
        raise HTTPException(503, f"Analyst model unavailable: {e}")
    return {
        "question": question,
        "answer": answer,
        "context_used": sorted(context_blocks.keys()),
        "note": "Answer generated from the listed context blocks only; verify numbers against their panels.",
    }


@router.get("/market/breadth")
def get_market_breadth():
    """Share of tracked equities above their own 20/50/200-day SMAs, computed
    from cached daily bars only, with per-window coverage counts — 5 of 40
    covered is a different claim from 40 of 40 and is never hidden."""
    from datetime import datetime as dt
    from lib.portfolio_risk import breadth_above_smas
    from lib.ohlcv_cache import get_cached_range

    with get_db() as db:
        symbols = [
            r[0] for r in db.query(MarketAsset.symbol)
            .filter(MarketAsset.asset_class == "Equity").all() if r[0]
        ]
    end = dt.now(timezone.utc)
    start = end - timedelta(days=420)
    closes = {}
    for sym in symbols:
        df = get_cached_range(sym, "1D", start, end)
        if df is not None and len(df) >= 20:
            closes[sym] = df["close"]

    breadth = breadth_above_smas(closes)
    breadth["tracked_equities"] = len(symbols)
    breadth["with_cached_history"] = len(closes)
    breadth["note"] = (
        "Cache-only computation over tracked equities; eligible counts per "
        "window show true coverage."
    )
    return breadth


@router.get("/ipo/pipeline")
def get_ipo_pipeline(limit: int = 40):
    """IPO registration pipeline from free EDGAR filings: S-1 (filed) ->
    S-1/A (amended) -> 424B4 (priced). Offering terms come only from 424B4
    cover pages via conservative deterministic extraction — a field the
    pattern couldn't match is null, never estimated. Rows flagged
    cover_mentions_ipo=false are follow-on offerings by already-listed
    companies (Rule 424(b)(4) covers those too), not IPOs."""
    with get_db() as db:
        rows = (
            db.query(IpoFiling)
            .order_by(IpoFiling.latest_filed_at.desc())
            .limit(min(max(limit, 1), 200))
            .all()
        )
        pipeline = [{
            "cik": r.cik, "company_name": r.company_name, "stage": r.stage,
            "latest_form": r.latest_form, "latest_filed_at": r.latest_filed_at,
            "first_seen_at": r.first_seen_at,
            "ticker": r.ticker, "exchange": r.exchange,
            "offer_price": r.offer_price, "shares_offered": r.shares_offered,
            "total_offering_usd": r.total_offering_usd,
            "is_likely_spac": bool(r.is_likely_spac),
            "cover_mentions_ipo": r.cover_mentions_ipo,
            "filing_url": r.filing_url,
        } for r in rows]
        counts = {
            stage: db.query(IpoFiling).filter(IpoFiling.stage == stage).count()
            for stage in ("filed", "amended", "priced")
        }
    return {
        "pipeline": pipeline,
        "stage_counts": counts,
        "note": (
            "Coverage builds from first ingestion (no historical backfill). "
            "is_likely_spac is a company-NAME heuristic only. Offering terms "
            "are extracted from 424B4 cover pages; null means not stated or "
            "not matched, never zero."
        ),
    }


@router.get("/congress/trades")
def get_congress_trades(limit: int = 50, ticker: str = None, days: int = 180):
    """Recent congressional stock-trade disclosures. See the disclaimer in the
    response — amounts are ranges, disclosure is delayed by statute, and none
    of this implies wrongdoing."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).date().isoformat()
    with get_db() as db:
        q = db.query(CongressTrade).filter(CongressTrade.transaction_date >= cutoff)
        if ticker:
            q = q.filter(CongressTrade.ticker == ticker.upper())
        rows = q.order_by(CongressTrade.transaction_date.desc()).limit(min(max(limit, 1), 200)).all()
        trades = [_congress_trade_dict(t) for t in rows]
        coverage = db.query(ProcessedCongressFiling).count()
    return {
        "trades": trades, "count": len(trades),
        "filings_processed": coverage,
        "disclaimer": _CONGRESS_DISCLAIMER,
    }


@router.get("/congress/official/{member_name:path}")
def get_congress_official_detail(member_name: str, days: int = 365):
    """Every disclosed trade for ONE official — the drill-down.

    Split out from the list endpoint because that one inlined every trade
    for every official: 1.4 MB to render forty NAMES. Paying megabytes to
    show a summary is why the list was capped so low in the first place,
    which is what made the panel look like the desk only knew about forty
    people when it holds ninety-six.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).date().isoformat()
    with get_db() as db:
        rows = (db.query(CongressTrade)
                .filter(CongressTrade.member_name == member_name)
                .filter(CongressTrade.transaction_date >= cutoff)
                .order_by(CongressTrade.transaction_date.desc()).all())
        trades = [_congress_trade_dict(t) for t in rows]
        chamber = rows[0].chamber if rows else None
        district = rows[0].state_district if rows else None

    # Price is NOT in the filing and cannot be — the STOCK Act discloses an
    # amount range and a date. This joins the disclosed date to the desk's
    # own daily bars so the row can be read against what the security
    # actually traded at, and every reconstructed number is labelled an
    # estimate in its own field. Rows with no bar for that exact session get
    # None rather than a neighbouring day's price.
    from lib.disclosure_pricing import estimate_trades
    trades, pricing = estimate_trades(trades)

    return {
        "member_name": member_name, "chamber": chamber or "House",
        "state_district": district, "window_days": days,
        "trade_count": len(trades), "trades": trades,
        "pricing_coverage": pricing,
        "disclaimer": _CONGRESS_DISCLAIMER,
    }


@router.get("/congress/by-official")
def get_congress_by_official(days: int = 365, limit: int = 500,
                             include_trades: bool = False):
    """Disclosed trades grouped per official — name, chamber, district, trade
    count, buy/sell split and summed disclosed range bounds.

    Trades are NOT inlined by default any more (`include_trades=true` restores
    the old shape). One summary row is a few hundred bytes; the full trade
    list for forty officials was 1.4 MB, and the resulting cap made a
    ninety-six-member dataset look like a forty-member one.

    The default limit is high enough to cover everyone currently ingested,
    and `total_officials` / `truncated` say plainly when it is not — a
    silently truncated list is the failure this panel already had.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).date().isoformat()
    with get_db() as db:
        rows = (db.query(CongressTrade)
                .filter(CongressTrade.transaction_date >= cutoff)
                .order_by(CongressTrade.transaction_date.desc()).all())
        officials: dict = {}
        for t in rows:
            o = officials.setdefault(t.member_name or "Unknown", {
                "member_name": t.member_name, "state_district": t.state_district,
                "chamber": t.chamber or "House", "purchases": 0, "sales": 0, "other": 0,
                "range_low_total": 0.0, "range_high_total": 0.0, "trades": [],
            })
            code = (t.transaction_code or "").upper()
            if code.startswith("P"): o["purchases"] += 1
            elif code.startswith("S"): o["sales"] += 1
            else: o["other"] += 1
            o["range_low_total"] += t.amount_low or 0.0
            o["range_high_total"] += t.amount_high or 0.0
            o["trades"].append(_congress_trade_dict(t))
            o["last_traded"] = max(o.get("last_traded") or "", t.transaction_date or "")
            if t.ticker:
                o.setdefault("_tickers", {})
                o["_tickers"][t.ticker] = o["_tickers"].get(t.ticker, 0) + 1
    total = len(officials)
    ranked = sorted(officials.values(), key=lambda o: -len(o["trades"]))[:min(max(limit, 1), 1000)]
    for o in ranked:
        o["trade_count"] = len(o["trades"])
        # The tickers this person touched most — enough to recognise a
        # position without pulling their whole filing history.
        o["top_tickers"] = [t for t, _ in sorted(
            (o.pop("_tickers", {}) or {}).items(), key=lambda kv: -kv[1])[:5]]
        if not include_trades:
            o["trades"] = []
    return {
        "officials": ranked, "window_days": days,
        "total_officials": total,
        "returned": len(ranked),
        "truncated": total > len(ranked),
        "trades_inlined": include_trades,
        "note": ("House disclosures only — Senate (efdsearch) and executive-branch "
                 "(OGE 278e) filings use separate systems not yet ingested. Amounts "
                 "are disclosed RANGE bounds, never exact values."),
        "disclaimer": _CONGRESS_DISCLAIMER,
    }


@router.get("/congress/activity/top")
def get_congress_top_activity(limit: int = 20, days: int = 180):
    """Most-disclosed tickers, with buy/sell counts and how many distinct
    members disclosed each. Counts are of DISCLOSURES, not dollar flow —
    exact amounts are never disclosed, so summing them is not possible."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).date().isoformat()
    with get_db() as db:
        rows = (
            db.query(CongressTrade)
            .filter(CongressTrade.transaction_date >= cutoff, CongressTrade.ticker.isnot(None))
            .all()
        )
        by_ticker: dict = {}
        for t in rows:
            e = by_ticker.setdefault(t.ticker, {
                "ticker": t.ticker, "purchases": 0, "sales": 0, "other": 0,
                "members": set(), "range_low_total": 0.0, "range_high_total": 0.0,
                "latest_transaction_date": None,
            })
            code = (t.transaction_code or "").upper()
            if code.startswith("P"):
                e["purchases"] += 1
            elif code.startswith("S"):
                e["sales"] += 1
            else:
                e["other"] += 1
            if t.member_name:
                e["members"].add(t.member_name)
            e["range_low_total"] += t.amount_low or 0.0
            e["range_high_total"] += t.amount_high or 0.0
            if not e["latest_transaction_date"] or (t.transaction_date or "") > e["latest_transaction_date"]:
                e["latest_transaction_date"] = t.transaction_date

    results = []
    for e in by_ticker.values():
        total = e["purchases"] + e["sales"] + e["other"]
        results.append({
            **{k: v for k, v in e.items() if k != "members"},
            "member_count": len(e["members"]),
            "disclosure_count": total,
            # Net direction by COUNT of disclosures, not dollars — the data
            # cannot support a dollar-flow figure.
            "net_direction": "net_buying" if e["purchases"] > e["sales"]
                             else "net_selling" if e["sales"] > e["purchases"] else "mixed",
        })
    results.sort(key=lambda r: (-r["disclosure_count"], -r["member_count"]))
    return {
        "tickers": results[:min(max(limit, 1), 100)],
        "window_days": days,
        "note": (
            "Ranked by number of disclosures. range_low_total/range_high_total are "
            "the summed bounds of the disclosed ranges — the true total lies somewhere "
            "between them and cannot be known more precisely."
        ),
        "disclaimer": _CONGRESS_DISCLAIMER,
    }


@router.get("/institutional/{symbol}")
def get_institutional_holdings(symbol: str):
    """Institutional (13F) holders of one ticker, with quarter-over-quarter
    change when two quarters have been ingested. See lib/sec_13f.py and
    lib/institutional_analytics.py for the full honesty caveats — most
    importantly that this is stale quarterly data, long-only, and never
    evidence of current buying."""
    from lib.institutional_analytics import aggregate_by_ticker, compare_quarters

    ticker = symbol.upper()
    with get_db() as db:
        periods = _institutional_periods(db, ticker)
        if not periods:
            raise HTTPException(404, f"No 13F holdings ingested for {ticker} yet")
        cur_period, prior_period = _select_comparison_periods(periods)
        current = aggregate_by_ticker(_holdings_for_period(db, cur_period, ticker))
        prior = aggregate_by_ticker(_holdings_for_period(db, prior_period, ticker)) if prior_period else {}

    rows = compare_quarters(current, prior)
    row = rows[0] if rows else None
    return {
        "symbol": ticker,
        "current_period": cur_period,
        "prior_period": prior_period,
        "summary": row,
        "holders": current.get(ticker, {}).get("holders", []),
        "disclaimer": _institutional_disclaimer(periods),
    }


@router.get("/institutional/accumulation/top")
def get_institutional_accumulation(limit: int = 25):
    """Tickers ranked by quarter-over-quarter institutional share change.
    Returns an explicit insufficient_history marker (rather than a
    misleading ranking) until two quarters have been ingested."""
    from lib.institutional_analytics import aggregate_by_ticker, compare_quarters

    with get_db() as db:
        periods = _institutional_periods(db)
        if not periods:
            raise HTTPException(503, "No 13F holdings ingested yet")
        cur_period, prior_period = _select_comparison_periods(periods)
        current = aggregate_by_ticker(_holdings_for_period(db, cur_period))
        prior = aggregate_by_ticker(_holdings_for_period(db, prior_period)) if prior_period else {}

    rows = compare_quarters(current, prior)
    return {
        "current_period": cur_period,
        "prior_period": prior_period,
        "insufficient_history": prior_period is None,
        "tickers": rows[:min(max(limit, 1), 100)],
        "disclaimer": _institutional_disclaimer(periods),
    }


@router.get("/opportunities/ranked")
def get_ranked_opportunities(limit: int = 30):
    """JARVIS Opportunity Score: ranks active trade signals by combining the
    existing TA/regime composite_score (lib/signal_scorer.py) with
    smart-money alignment (insider clusters + dark-pool activity for
    equities) and that symbol's historical win rate. See
    lib/signal_fusion.py for the full scoring rationale — every component
    is returned alongside the composite, nothing is hidden behind one
    number. Crypto signals get descriptive derivatives context (funding/OI/
    long-short ratio) rather than a smart-money score — see that module's
    docstring for why funding rate isn't scored as directional "smart
    money." Options are intentionally excluded here: pulling a live chain
    per active signal on every request would mean many synchronous external
    calls per page load; see GET /options/{symbol}/summary for the
    per-symbol version already used in the Signal Analysis Modal."""
    from lib.insider_analytics import cluster_summary
    from lib.finra_ats import get_top_activity
    from lib.signal_fusion import (
        compute_anomaly_flags, compute_dark_pool_component, compute_insider_component,
        compute_opportunity_score, compute_smart_money_alignment,
    )
    from lib.learning_engine import get_all_accuracy

    with get_db() as db:
        signal_rows = (
            db.query(TradingSignal)
            .filter(TradingSignal.status.in_(["Active", "PendingApproval"]))
            .order_by(TradingSignal.generated_at.desc())
            .limit(200)
            .all()
        )
        if not signal_rows:
            return []
        signals = [{
            "id": s.id, "asset_symbol": s.asset_symbol, "asset_class": s.asset_class,
            "direction": s.direction, "timeframe": s.timeframe, "composite_score": s.composite_score,
        } for s in signal_rows]

        tickers = {s["asset_symbol"] for s in signals if (s["asset_class"] or "").lower() == "equity"}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        insider_rows = (
            db.query(InsiderTransaction)
            .filter(InsiderTransaction.ticker.in_(tickers), InsiderTransaction.transaction_date >= cutoff)
            .all() if tickers else []
        )
        insider_by_ticker: dict = {}
        for r in insider_rows:
            insider_by_ticker.setdefault(r.ticker, []).append({
                "owner_cik": r.owner_cik, "owner_name": r.owner_name, "is_officer": r.is_officer,
                "transaction_code": r.transaction_code, "total_value": r.total_value,
            })

        crypto_symbols = {s["asset_symbol"].upper().split("/")[0] for s in signals if (s["asset_class"] or "").lower() == "crypto"}
        crypto_snapshots: dict = {}
        for sym in crypto_symbols:
            row = (
                db.query(CryptoDerivativesSnapshot)
                .filter(CryptoDerivativesSnapshot.symbol == sym)
                .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
                .first()
            )
            if row:
                crypto_snapshots[sym] = {
                    "funding_rate": row.funding_rate, "open_interest_usd": row.open_interest_usd,
                    "long_short_ratio": row.long_short_ratio,
                }

        accuracy_by_symbol: dict = {}
        for row in get_all_accuracy():
            existing = accuracy_by_symbol.get(row["symbol"])
            if not existing or (row["total_trades"] or 0) > (existing["total_trades"] or 0):
                accuracy_by_symbol[row["symbol"]] = row

    dark_pool_snapshot = get_top_activity(tier="T1", limit=100) or {}
    dark_pool_by_symbol = {r["symbol"]: r for r in dark_pool_snapshot.get("symbols", [])}

    results = []
    for s in signals:
        symbol = s["asset_symbol"]
        asset_class = (s["asset_class"] or "").lower()
        smart_money = None
        anomaly = None
        crypto_context = None

        if asset_class == "equity":
            txs = insider_by_ticker.get(symbol)
            cluster = cluster_summary(txs) if txs else None
            insider_comp = compute_insider_component(cluster) if cluster and cluster["flags"] else None
            dark_pool_row = dark_pool_by_symbol.get(symbol)
            dark_pool_comp = compute_dark_pool_component(dark_pool_row) if dark_pool_row else None
            smart_money = compute_smart_money_alignment(insider=insider_comp, dark_pool=dark_pool_comp)
            anomaly = compute_anomaly_flags(dark_pool=dark_pool_row)
        elif asset_class == "crypto":
            crypto_context = crypto_snapshots.get(symbol.upper().split("/")[0])

        historical = accuracy_by_symbol.get(symbol)
        opp = compute_opportunity_score(s["composite_score"], s["direction"], smart_money=smart_money, historical=historical)

        results.append({
            "signal_id": s["id"], "symbol": symbol, "asset_class": s["asset_class"], "direction": s["direction"],
            "timeframe": s["timeframe"], "base_composite_score": s["composite_score"],
            "opportunity_score": opp["opportunity_score"], "opportunity_breakdown": opp["breakdown"],
            "smart_money": smart_money, "anomaly": anomaly, "crypto_context": crypto_context,
            "historical": {"total_trades": historical["total_trades"], "win_rate": historical["win_rate"]} if historical else None,
        })

    results.sort(key=lambda r: r["opportunity_score"], reverse=True)
    return results[:min(max(limit, 1), 100)]


@router.get("/crypto/{symbol}/derivatives")
def get_crypto_derivatives(symbol: str, liquidation_hours: int = 24):
    """Perpetual-futures state (funding rate, open interest, long/short
    account ratio) plus recent liquidations — free OKX public data, no
    vendor. OI/price divergence is computed from the two most recent stored
    snapshots (~10 min apart); a brand-new symbol will show it as null until
    a second snapshot exists. See lib/crypto_derivatives.py for why OKX is
    the sole source (Binance derivatives geo-blocked, Bybit CloudFront-blocked
    from this deployment)."""
    from lib.crypto_derivatives import classify_oi_price_action, summarize_liquidations
    base = symbol.upper().split("/")[0]
    with get_db() as db:
        snapshots = (
            db.query(CryptoDerivativesSnapshot)
            .filter(CryptoDerivativesSnapshot.symbol == base)
            .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
            .limit(2).all()
        )
        if not snapshots:
            raise HTTPException(503, f"No derivatives data yet for {base} — job may still be running")
        latest, prev = snapshots[0], (snapshots[1] if len(snapshots) > 1 else None)

        oi_price_action = None
        if prev and prev.open_interest_usd and prev.price and latest.price:
            oi_change_pct = (latest.open_interest_usd - prev.open_interest_usd) / prev.open_interest_usd * 100
            price_change_pct = (latest.price - prev.price) / prev.price * 100
            oi_price_action = classify_oi_price_action(oi_change_pct, price_change_pct)

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, liquidation_hours))).isoformat()
        liquidations = (
            db.query(CryptoLiquidation)
            .filter(CryptoLiquidation.symbol == base, CryptoLiquidation.liquidated_at >= cutoff)
            .order_by(CryptoLiquidation.liquidated_at.desc())
            .limit(200).all()
        )
        liq_dicts = [{
            "side": l.side, "pos_side": l.pos_side, "price": l.price, "size": l.size,
            "notional_usd": l.notional_usd, "liquidated_at": l.liquidated_at,
        } for l in liquidations]

        return {
            "symbol": base,
            "price": latest.price,
            "funding_rate": latest.funding_rate,
            "open_interest_usd": latest.open_interest_usd,
            "long_short_ratio": latest.long_short_ratio,
            "oi_price_action": oi_price_action,
            "fetched_at": latest.fetched_at,
            "liquidations": liq_dicts,
            "liquidations_summary": summarize_liquidations(liq_dicts),
        }


@router.get("/darkpool/top")
def get_darkpool_top(tier: str = "T1", limit: int = 25):
    """Top symbols by off-exchange (ATS/dark pool) share volume for the
    latest available week — free FINRA Off-Exchange Transparency data, no
    vendor. This is DELAYED, WEEKLY-AGGREGATED data (~2-4 week publish lag,
    see reporting_delay_days per row), not real-time order flow — there is
    no free source for individual dark-pool prints."""
    from lib.finra_ats import get_top_activity
    snapshot = get_top_activity(tier=tier, limit=min(max(limit, 1), 100))
    if not snapshot:
        raise HTTPException(503, "FINRA ATS data unavailable")
    return snapshot


@router.get("/darkpool/{symbol}/venues")
def get_darkpool_venues(symbol: str, week_start: str = None):
    """Per-venue (individual dark pool) breakdown for one symbol. Pass
    week_start from a /darkpool/top row to avoid a redundant week-discovery
    lookup; omit it to use the latest available week."""
    from lib.finra_ats import get_symbol_venues
    result = get_symbol_venues(symbol, week_start=week_start)
    if not result:
        raise HTTPException(503, "FINRA ATS data unavailable")
    return result


@router.get("/macro/yield-curve")
def get_yield_curve():
    """US Treasury daily yield curve — free, unauthenticated Treasury.gov
    data (lib/treasury_yields.py), no vendor. Includes the 2s10s and 3m10y
    inversion spreads, the two classic yield-curve recession indicators."""
    from lib.treasury_yields import get_yield_curve_snapshot
    snapshot = get_yield_curve_snapshot()
    if not snapshot:
        raise HTTPException(503, "Treasury yield curve data unavailable")
    return snapshot


@router.get("/macro/fred")
def get_macro_fred():
    """CPI/core CPI, PCE/core PCE, unemployment, nonfarm payrolls, real GDP,
    fed funds rate, jobless claims — free FRED (St. Louis Fed) data. Requires
    the user's own free FRED_API_KEY (lib/fred_client.py); returns a clear
    "not configured" response rather than an error when the key is missing,
    since this integration is opt-in."""
    from lib.fred_client import get_macro_snapshot, is_configured
    if not is_configured():
        return {"configured": False, "readings": None, "fetched_at": None}
    snapshot = get_macro_snapshot()
    if not snapshot:
        raise HTTPException(503, "FRED data unavailable")
    return {"configured": True, **snapshot}


@router.get("/insider/activity")
def get_insider_activity(ticker: str = None, days: int = 30, limit: int = 200):
    """Recent SEC Form 4 insider transactions — free EDGAR data, no vendor.
    Every transaction code is included (not just buys/sells) so a caller can
    see the full picture; see /insider/clusters for the P/S-only signal view."""
    with get_db() as db:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat()
        query = db.query(InsiderTransaction).filter(InsiderTransaction.transaction_date >= cutoff)
        if ticker:
            query = query.filter(InsiderTransaction.ticker == ticker.upper())
        rows = query.order_by(InsiderTransaction.transaction_date.desc()).limit(min(max(limit, 1), 500)).all()
        return [_insider_tx_dict(r) for r in rows]


@router.get("/insider/clusters")
def get_insider_clusters(days: int = 14):
    """Tickers where multiple insiders bought/sold, an officer bought, or
    activity was one-directional within the window — computed from real Form 4
    data via lib/insider_analytics.rank_clusters. Flags are descriptive only;
    this never asserts wrongdoing or predicts price direction."""
    from lib.insider_analytics import rank_clusters
    with get_db() as db:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat()
        rows = db.query(InsiderTransaction).filter(
            InsiderTransaction.transaction_date >= cutoff,
            InsiderTransaction.transaction_code.in_(["P", "S"]),
        ).all()
        txs = [_insider_tx_dict(r) for r in rows]
    return {"window_days": days, "transactions_analyzed": len(txs), "clusters": rank_clusters(txs)}


@router.get("/market")
def get_market():
    with get_db() as db:
        return [_asset_dict(a) for a in db.query(MarketAsset).order_by(MarketAsset.symbol).all()]

@router.get("/market/full")
def get_market_full():
    with get_db() as db:
        assets = db.query(MarketAsset).order_by(MarketAsset.change_percent.desc()).all()
        # Serialize inside session — avoids DetachedInstanceError
        asset_dicts = [_asset_dict(a) for a in assets]
    equities = [a for a in asset_dicts if a.get("asset_class") != "Crypto"]
    crypto   = [a for a in asset_dicts if a.get("asset_class") == "Crypto"]
    return {"equities": equities, "crypto": crypto, "count": len(asset_dicts)}


@router.get("/earnings/watchlist")
def get_earnings_watchlist():
    """Which currently-held or active-signal equity symbols report earnings
    within the next 5 days (Yahoo Finance calendar, no paid API)."""
    # THE SLOWEST CALL ON THE PAGE, and it was uncached. Measured at
    # 2,546 ms against a Yahoo calendar for data that changes ONCE A DAY —
    # paid on every single page load, by every section that shows earnings
    # risk. Even loaded in parallel with everything else it set the floor
    # for how fast the page could possibly appear.
    #
    # serve_with_refresh returns the stored answer instantly and refreshes
    # behind the request, so only a genuinely cold cache ever waits.
    from lib.api_cache import serve_with_refresh
    from lib.earnings_calendar import get_earnings_this_week

    cached, _stale = serve_with_refresh(
        "earnings:this_week", 6 * 3600,
        lambda: {"symbols": sorted(get_earnings_this_week())})
    reporting = set((cached or {}).get("symbols") or [])

    with get_db() as db:
        symbols = set()
        for row in db.query(TradingSignal.asset_symbol).filter(TradingSignal.status.in_(["Active", "PendingApproval", "Executed"])).all():
            symbols.add((row[0] or "").upper())
        from app.database import PaperPosition
        for row in db.query(PaperPosition.symbol).filter(PaperPosition.status == "Open").all():
            symbols.add((row[0] or "").upper())
    at_risk = sorted(s for s in symbols if s.replace("/USD", "") in reporting)
    return {"at_risk_symbols": at_risk, "checked_at": datetime.now(timezone.utc).isoformat()}


@router.post("/analyze")
def analyze(body: AnalyzeRequest):
    try:
        from lib.ohlcv import fetch_multi_timeframe
        from lib.ta_engine import analyze_symbol, build_ta_prompt_block
        # The same venue-ticker resolution the watchlist and focus paths
        # use. Without it the SAME input behaved three different ways
        # depending on which endpoint saw it: SPCX/USD added fine and
        # focused fine, then analyzed as "insufficient data" on every
        # timeframe because nothing here mapped it to XSPCX/USD.
        from lib.symbol_aliases import resolve as _resolve_alias
        _sym, _alias_note = _resolve_alias(body.symbol)
        bars=fetch_multi_timeframe(_sym, body.timeframes)
        ta=analyze_symbol(bars); pb=build_ta_prompt_block(_sym,ta)
        signal=None
        if body.generate_signal:
            try:
                from lib.lmstudio import call_lm_studio, parse_json
                from lib.market_regime import get_regime
                regime=get_regime()
                # "Long/Bounce only" was here originally, which FORCED a long
                # even when every timeframe read bearish (observed live: an
                # all-bearish chart produced "Long @ 1.3"). Shorts are now
                # allowed, and the model must justify direction against the
                # per-timeframe biases it was shown.
                prompt=f"""Analyze this ticker for a trade setup:\n\n{pb}\n\nRegime: {regime.get("label")} | Risk: {regime.get("risk")}\n\nGenerate ONE signal as JSON object with keys: asset_symbol, asset_class, direction (Long or Short — match the timeframe biases above; shorting a bearish chart is expected), confidence (0-100), timeframe (one of the analyzed timeframes), entry_price, target_price, stop_loss, reasoning, key_risks, momentum. Return ONLY the JSON."""
                from lib import llm_router as llm
                _biases=[(d.get("bias") or "").lower() for d in ta.values()
                         if isinstance(d,dict) and not d.get("error")]
                raw=llm.call(prompt, task="signal_generation", mode=llm.AUTO,
                             context={"symbol": _sym,
                                      # both directions represented = the
                                      # timeframes disagree with each other
                                      "contradiction_count": min(_biases.count("bullish"),
                                                                 _biases.count("bearish")) * 2},
                             symbol=_sym, max_tokens=800, temperature=0.1)
                parsed=parse_json(raw)
                signal=parsed[0] if isinstance(parsed,list) else parsed

                # Deterministic conflict check the LLM cannot talk its way out
                # of: count the per-timeframe biases and flag a signal that
                # trades AGAINST the clear majority.
                if isinstance(signal, dict) and not signal.get("error"):
                    biases = [ (d.get("bias") or "").lower() for d in ta.values()
                               if isinstance(d, dict) and not d.get("error") ]
                    bear = biases.count("bearish"); bull = biases.count("bullish")
                    direction = str(signal.get("direction") or "").lower()
                    conflict = None
                    if bear >= max(3, len(biases) * 0.7) and "short" not in direction:
                        conflict = f"{bear}/{len(biases)} timeframes are bearish but the signal is {signal.get('direction')}"
                    elif bull >= max(3, len(biases) * 0.7) and "short" in direction:
                        conflict = f"{bull}/{len(biases)} timeframes are bullish but the signal is {signal.get('direction')}"
                    if conflict:
                        # One corrective retry with explicit feedback — local
                        # models routinely ignore the first instruction. If the
                        # retry still conflicts, the warning stands and the UI
                        # blocks saving.
                        try:
                            # The retry exists BECAUSE the answer contradicted
                            # the chart — the one case that is unambiguously
                            # reasoning work, not transcription.
                            retry_raw = llm.call(
                                prompt
                                + f"\n\nYOUR PREVIOUS ANSWER WAS WRONG: {conflict}. "
                                "Re-issue the JSON with a direction that MATCHES the timeframe "
                                "biases (Short for a bearish chart), with entry/target/stop "
                                "consistent with that direction.",
                                task="contradiction_review", mode=llm.DEEP, symbol=_sym,
                                max_tokens=800, temperature=0.1)
                            retry = parse_json(retry_raw)
                            retry = retry[0] if isinstance(retry, list) else retry
                            if isinstance(retry, dict) and retry.get("direction"):
                                rdir = str(retry["direction"]).lower()
                                fixed = (bear >= bull and "short" in rdir) or (bull > bear and "short" not in rdir)
                                if fixed:
                                    signal = retry
                                    direction = rdir
                                    conflict = None
                                    signal["corrected_by_retry"] = True
                        except Exception:
                            pass
                    signal["bias_conflict"] = conflict
                    signal["bias_summary"] = {"bullish": bull, "bearish": bear, "total": len(biases)}
                    # horizon + hold estimate for the UI
                    # One table, in lib/trade_horizon. This map lived here, in
                    # the Telegram formatter and in the signal card — three
                    # copies none of which the position-management loop could
                    # read, so it managed every trade on one horizon.
                    from lib.trade_horizon import category, hold_estimate
                    tf = str(signal.get("timeframe") or "")
                    signal["horizon"] = category(tf)
                    signal["hold_estimate"] = hold_estimate(tf)
            except Exception as e:
                signal={"error":str(e)}
        return {"symbol":_sym,"ta":ta,"prompt_block":pb,"signal":signal,
                "resolved_from":_alias_note}
    except Exception as e: raise HTTPException(500,str(e))


@router.post("/scanner/run")
async def run_scanner(request: Request):
    """Manually trigger the opportunity scanner. mode: pre_market|intraday|crypto|futures|all"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = body.get("mode", "all")
    valid_modes = {"pre_market", "intraday", "crypto", "futures", "all"}
    if mode not in valid_modes:
        return JSONResponse({"error": f"Invalid mode. Use: {valid_modes}"}, status_code=400)

    import threading
    from jobs.scan_opportunities import run as scanner_run
    def _run():
        try:
            scanner_run(mode)
        except Exception as e:
            logger.error(f"[Routes] Scanner run({mode}) failed: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started", "mode": mode, "message": f"Opportunity scanner [{mode}] running in background"}


@router.get("/scanner/status")
def get_scanner_status():
    """Get opportunity scanner job status, broken out per scan mode."""
    try:
        from app.scheduler import job_status
        modes = {
            "pre_market": job_status.get("scanner_premarket", {"status": "unknown"}),
            "intraday":   job_status.get("scanner_intraday", {"status": "unknown"}),
            "crypto":     job_status.get("scanner_crypto", {"status": "unknown"}),
            "futures":    job_status.get("scanner_futures", {"status": "unknown"}),
        }
        return {"scanner": modes}
    except Exception as e:
        return {"error": str(e)}


