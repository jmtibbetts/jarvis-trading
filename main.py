"""
Jarvis Trading AI — Python Edition (version: app/version.py)
FastAPI + APScheduler + SQLAlchemy + TA-Lib
Run: python main.py
"""
import asyncio, os, logging, sys, threading, time, signal, math
from logging.handlers import RotatingFileHandler
from pathlib import Path
from contextlib import asynccontextmanager
from app.version import VERSION
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
# Windows consoles often default to cp1252, which cannot encode the banner's
# box-drawing and emoji characters. Configure streams before any prints/logs.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
Path('data').mkdir(exist_ok=True)


def _file_log_handler():
    """Use a bounded log and keep startup working if Windows holds the main file."""
    candidates = [Path("data/jarvis.log"), Path(f"data/jarvis-{os.getpid()}.log")]
    for path in candidates:
        try:
            return RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=3,
                encoding="utf-8",
            )
        except (OSError, PermissionError):
            continue
    return logging.NullHandler()


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        _file_log_handler(),
    ]
)
for noisy in ['httpx','httpcore','alpaca','apscheduler','urllib3','feedparser','yfinance','peewee']:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── FastAPI ────────────────────────────────────────────────────────────────────
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

def _json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(_json_safe(content))


scheduler = None

@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Startup / shutdown."""
    global scheduler

    # ── Startup ────────────────────────────────────────────────────────────────
    from app.ws import manager as ws_manager
    ws_manager.bind_loop(asyncio.get_running_loop())

    # Wallet registry bootstrap: exclusions, then operator seeds. This runs
    # BEFORE the scheduler so the first discovery/activity pass sees the
    # seeded population rather than an empty table.
    #
    # It had never been wired. `import_seeds()` existed, was tested, and had
    # zero production callers — and the tests that assert "seeds are
    # re-imported on every boot" call it directly, so they passed against a
    # startup path that never invoked it. Registry bootstrap is idempotent
    # (upsert never downgrades a promotion or overwrites an earned score),
    # so doing it on every boot costs nothing.
    try:
        from lib.wallet_registry import bootstrap as wallet_bootstrap
        logger.info(f"[Server] Wallet registry bootstrap — {wallet_bootstrap()}")
    except Exception as e:
        # A seeding failure must not stop the desk from starting.
        logger.warning(f"[Server] Wallet registry bootstrap failed: {e}")

    # JARVIS_DISABLE_SCHEDULER=1 runs the API/UI without any background jobs —
    # for dev/debug instances that must never fetch data or place orders.
    if os.getenv("JARVIS_DISABLE_SCHEDULER") == "1":
        logger.warning("[Server] Scheduler DISABLED (JARVIS_DISABLE_SCHEDULER=1) — UI/API only")
        app.state.scheduler_lease = None
    else:
        # ONE ECONOMIC SCHEDULER PER BOOK. Two processes against the same
        # database would both open entries, both manage the same positions
        # and both project learning — silently, because APScheduler's
        # max_instances is a per-PROCESS guard. The second process still
        # serves the API; it simply does not act.
        from datetime import datetime as _dt, timezone as _tz

        from lib import scheduler_lease as SL
        lease = SL.acquire(now_iso=_dt.now(_tz.utc).isoformat())
        app.state.scheduler_lease = lease
        if lease.granted:
            from app.scheduler import create_scheduler
            scheduler = create_scheduler()
            scheduler.start()
            logger.info("[Server] APScheduler started — jobs firing immediately")
        elif lease.state == SL.STANDBY:
            owner = lease.owner or {}
            logger.warning(
                "[Server] Scheduler STANDBY — pid %s on %s already owns the "
                "economic scheduler for this database. Serving API/UI only; "
                "this process will not trade.",
                owner.get("pid"), owner.get("host"))
        else:
            # The guard itself could not run. Say so loudly and do NOT act:
            # an unprovable claim to exclusivity is not a claim.
            logger.error(
                "[Server] Scheduler NOT started — ownership could not be "
                "established (%s). Refusing to run economic jobs without "
                "proof that nothing else is.", lease.detail)

    # Crypto L2 order book streams (Binance + Coinbase, free public WS feeds).
    # Unlike APScheduler jobs these are long-lived connections, so they're
    # started here as asyncio tasks on the SAME loop uvicorn serves requests
    # on — broadcast_from_thread() exists specifically to bridge INTO this
    # loop from a different thread (APScheduler's pool); these tasks are
    # already on it, so they call manager.broadcast() directly.
    from lib.orderbook_stream import start_orderbook_streams

    async def _broadcast_orderbook_update(snapshot: dict):
        await ws_manager.broadcast("orderbook", snapshot)

    orderbook_tasks = start_orderbook_streams(on_update=_broadcast_orderbook_update)
    logger.info(f"[Server] Order book streams started — {len(orderbook_tasks)} connections (Binance + Coinbase)")

    # Kraken WebSocket: live bid/ask and the trade tape. Runs in its own
    # daemon thread rather than on this loop, because it self-heals across
    # reconnects and must never be able to stall request handling. Symbols
    # are the ones the desk actually prices most often.
    try:
        from lib.kraken_stream import start as start_kraken_stream
        ks = start_kraken_stream([
            "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "LINK/USD",
            "DOT/USD", "ARB/USD", "AVAX/USD",
        ])
        if ks.get("ok"):
            logger.info(f"[Server] Kraken stream started — {len(ks.get('streaming', []))} symbols "
                        f"(live spreads + trade flow)")
        else:
            logger.info(f"[Server] Kraken stream unavailable: {ks.get('reason')}")
    except Exception as e:
        logger.warning(f"[Server] Kraken stream failed to start: {e}")

    # READ-ONLY MARKET DATA RUNTIME. Deliberately OUTSIDE the scheduler
    # branch above: switching the trading scheduler off says JARVIS may not
    # ACT, not that it may not LOOK. The forward-evidence work needs the
    # market observed precisely while the trading loop is stopped, so
    # coupling the two would close the observation window exactly when it
    # matters most. `JARVIS_DISABLE_MARKET_DATA=1` turns feeds off by their
    # own name, for tests and deliberately offline runs.
    #
    # This is also where the Bitnomial perpetual book finally gets an
    # owner: the provider was fully implemented and had no runtime caller
    # at all, so every US perpetual quote refused for want of a stream.
    try:
        from lib.market_data_runtime import start as start_market_data
        md = start_market_data()
        logger.info(f"[Server] Read-only market data — {md}")
    except Exception as e:
        logger.warning(f"[Server] Market-data runtime failed to start: {e}")

    # EVIDENCE RUNTIME. Also outside the scheduler branch, and for the same
    # reason one layer up: a decision made at 09:00 with a 4-hour horizon
    # comes due at 13:00 whether or not JARVIS is trading in between. If
    # resolution rode on the trading loop, pausing trading would silently
    # stop the measurement that says whether trading should resume.
    try:
        from lib.evidence_runtime import start as start_evidence
        ev = start_evidence()
        logger.info(f"[Server] Evidence runtime — {ev}")
    except Exception as e:
        logger.warning(f"[Server] Evidence runtime failed to start: {e}")

    # Twelve Data WebSocket: live forex majors — the one asset class with
    # no real-time feed (Kraken covers crypto, Alpaca covers equities).
    # Same daemon-thread pattern; inert without a TD key.
    try:
        from lib.td_forex_stream import start as start_td_forex
        fx = start_td_forex()
        if fx.get("ok"):
            logger.info(f"[Server] TD forex stream started — "
                        f"{len(fx.get('streaming', []))} pairs")
        else:
            logger.info(f"[Server] TD forex stream unavailable: {fx.get('reason')}")
    except Exception as e:
        logger.warning(f"[Server] TD forex stream failed to start: {e}")

    yield  # ← App runs here

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("[Server] Shutdown initiated...")

    try:
        from lib.kraken_stream import stop as stop_kraken_stream
        stop_kraken_stream()
    except Exception:
        pass

    try:
        from lib.td_forex_stream import stop as stop_td_forex
        stop_td_forex()
    except Exception:
        pass

    try:
        from lib.evidence_runtime import stop as stop_evidence
        stop_evidence()
    except Exception:
        pass

    try:
        from lib.market_data_runtime import stop as stop_market_data
        stop_market_data()
    except Exception:
        pass

    for task in orderbook_tasks:
        task.cancel()

    # Signal any in-flight LLM calls to abort
    try:
        from lib.lmstudio import _shutdown_event
        _shutdown_event.set()
    except Exception:
        pass

    # Shut down scheduler without waiting for running jobs
    if scheduler:
        scheduler.shutdown(wait=False)

    logger.info("[Server] Shutdown complete")


app = FastAPI(
    title="Jarvis Trading AI",
    # ONE VERSION SOURCE. app/version.py said 8.0.0 while this said 6.7.0
    # and the banner said 6.7 — three answers to "what is running", which
    # is the question a frontend compatibility handshake has to rely on.
    version=VERSION,
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
)
# CORS is restricted, not wildcard: this API exposes state-changing trade
# endpoints with no authentication, so `allow_origins=["*"]` meant any web
# page the operator's browser visited could fire mutating requests at the
# desk. The UI is same-origin (served by this process), so CORS only needs
# the dev-server origins; anything else is opt-in via env.
_cors_origins = [o.strip() for o in os.getenv(
    "JARVIS_CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173",
).split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_methods=["*"], allow_headers=["*"])

# ── API request/error tracking (in-memory, resets on restart — this is a
# live-ops signal, not an audit record, so it doesn't need to survive a
# restart the way the DB-backed job/decision logs do) ───────────────────────
from app.request_metrics import record as _record_api_request

@app.middleware("http")
async def _track_api_requests(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        _record_api_request(request.url.path, response.status_code)
    return response

# ── Database init ──────────────────────────────────────────────────────────────
from app.database import init_db
init_db()

# ── Routes ────────────────────────────────────────────────────────────────────
from app.routes import router
app.include_router(router, prefix="/api")

from app.ws import websocket_endpoint
app.websocket("/ws")(websocket_endpoint)

# ── Static / SPA ──────────────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# The Svelte dashboard (frontend/) is the only dashboard as of the Phase 16
# cutover — the old Jinja2/vanilla-JS dashboard (templates/index.html,
# static/js/jarvis.js) has been removed. It is served entirely from "/":
# the shell at "/" (and any unmatched path, since routing is hash-based —
# #command, #signals, ... — and never reaches the server), its bundle from
# /assets/.
#
# Both used to come from different places: the shell from "/" but its assets
# from the /next staging mount, because the Vite base still said so long
# after the cutover. Splitting one page across two routes is what let a
# no-cache header fix one and miss the other.
DIST_DIR = STATIC_DIR / "dist"
if DIST_DIR.exists():
    # The build now emits /assets/... (vite base '/'), matching where the app
    # actually lives. This mount must be registered BEFORE spa_fallback's
    # catch-all, or a missing asset would return index.html as text/html
    # instead of a 404 — which is exactly how a bare /assets/... URL behaved
    # before this existed, and it made diagnosing a cache problem harder
    # than it needed to be.
    ASSETS_DIR = DIST_DIR / "assets"
    if ASSETS_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

    # /next is GONE as a place to serve from. It was a staging path used
    # while this dashboard was built alongside the old one; the cutover moved
    # everything to "/", but the Vite base kept emitting /next/assets/... so
    # the app on port 3000 still quietly depended on the staging mount. That
    # is what made a plain caching bug hard to reason about — the shell came
    # from one route and its assets from another, and a header fix applied to
    # one did not apply to the other.
    #
    # The redirect stays so an old bookmark lands somewhere sensible rather
    # than on a 404.
    @app.get("/next", include_in_schema=False)
    @app.get("/next/", include_in_schema=False)
    def next_redirect():
        return RedirectResponse(url="/", status_code=301)
else:
    logger.warning(f"[Server] {DIST_DIR} not found — run `npm run build` in frontend/ to enable the dashboard")

@app.get("/")
@app.get("/{full_path:path}")
def spa_fallback(full_path: str = ""):
    index = DIST_DIR / "index.html"
    if index.exists():
        # The shell must NEVER be cached. It is the only file that names the
        # content-hashed bundle, so a cached copy pins the browser to a build
        # that no longer exists on disk — the UI silently stays one deploy
        # behind and the only cure is a manual hard refresh. Observed: a new
        # per-coin scan button was live in index-IV7ij3VY.js while the
        # browser kept loading the previous bundle and showed no button at
        # all. FileResponse sends etag/last-modified but no Cache-Control,
        # which leaves browsers free to heuristically cache it.
        #
        # The hashed assets under /assets/ are the opposite case: their names
        # change whenever their contents do, so they are safe to cache hard
        # and are left alone.
        return FileResponse(str(index))
    return {"error": "Frontend not found — run `npm run build` in frontend/"}


@app.middleware("http")
async def no_cache_html(request, call_next):
    """Never let a browser cache the SPA shell.

    index.html is the only file that names the content-hashed bundle, so a
    cached copy pins the browser to a build that no longer exists on disk.
    The UI silently stays one deploy behind and no amount of Ctrl+F5 helps
    once the response is already in cache with no revalidation directive.
    Observed: a new per-coin scan button was live in the built bundle while
    the browser kept loading the previous one and rendered no button at all.

    This is middleware rather than a header on spa_fallback because the same
    index.html is served by TWO routes — spa_fallback AND the /next
    StaticFiles mount, which builds its own response and ignored the fix
    applied to the other. Anything that returns HTML gets the header, so a
    third path added later cannot quietly reintroduce the problem.

    The content-hashed assets under /assets/ are deliberately untouched:
    their names change whenever their contents do, so they are safe to cache
    hard, and doing so is the entire point of hashing them.
    """
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ── Banner ─────────────────────────────────────────────────────────────────────
def print_banner():
    port = int(os.getenv('PORT', 3000))
    print("\n" + "═"*65)
    print(f"  🤖  JARVIS TRADING AI  v{VERSION}  (Python Edition)")
    print("═"*65)
    print(f"  Dashboard:  http://localhost:{port}")
    print(f"  API docs:   http://localhost:{port}/docs")
    print("═"*65)
    print("  Startup sequence (fires immediately):")
    print("    T+0s    Market Data  + Threats + Telegram")
    print("    T+30s   Position Management")
    print("    T+90s   Signal Generation")
    print("    T+3m    Signal Execution")
    print("═"*65)
    print("  Recurring schedules:")
    print("    Market Data     → every 15 min")
    print("    Threat News     → every 15 min")
    print("    Signal Gen      → every 30 min")
    print("    Signal Execute  → every 30 min")
    print("    Position Mgmt   → every  5 min")
    print("    Telegram Bot    → every  1 min")
    print("═"*65 + "\n")


# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print_banner()
    port = int(os.getenv('PORT', 3000))

    def open_browser():
        time.sleep(2)
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except:
            pass
    threading.Thread(target=open_browser, daemon=True).start()

    # Loopback by default. `0.0.0.0` exposed an unauthenticated API with
    # trade-mutation and destructive-admin endpoints to the whole LAN;
    # "probably behind a firewall" is not a security boundary. Operators
    # who genuinely want LAN access opt in explicitly — and should front
    # it with authentication when they do.
    bind_host = os.getenv("JARVIS_BIND_HOST", "127.0.0.1")
    if bind_host != "127.0.0.1":
        logger.warning(f"[Server] binding {bind_host} — API is unauthenticated; "
                       "ensure network-level access control")
    uvicorn.run("main:app", host=bind_host, port=port, reload=False, log_level="warning")
