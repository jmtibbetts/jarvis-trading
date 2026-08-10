"""
Jarvis Trading AI — Python Edition v6.8
FastAPI + APScheduler + SQLAlchemy + TA-Lib
Run: python main.py
"""
import asyncio, os, logging, sys, threading, time, signal, math
from logging.handlers import RotatingFileHandler
from pathlib import Path
from contextlib import asynccontextmanager
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

    from app.scheduler import create_scheduler
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("[Server] APScheduler started — jobs firing immediately")

    yield  # ← App runs here

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("[Server] Shutdown initiated...")

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
    version="6.7.0",
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
STATIC_DIR    = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# New Svelte dashboard rebuild (in progress — see the rebuild plan). Lives at
# /next during development alongside the existing dashboard at "/" so the two
# can be compared and the old one keeps working until Phase 7 cutover.
# The Svelte app uses hash-based routing (#command, #signals, ...), which
# never reaches the server, so a plain StaticFiles(html=True) mount — serving
# index.html for any "/next/..." path — is enough; no server-side SPA
# fallback is needed for the new frontend the way spa_fallback provides one
# for the old Jinja-based dashboard.
#
# Registration order matters here, but not the way it looks: Starlette
# doesn't simply take the first route in registration order that matches —
# a Mount only returns a FULL match for "/next/..." (sub-path); the bare
# "/next" (no trailing slash) only PARTIAL-matches the Mount, and Starlette
# keeps searching for a FULL match, which spa_fallback's catch-all
# "/{full_path:path}" then provides — so the bare path silently fell through
# to the OLD dashboard regardless of registration order. The explicit
# redirect below closes that gap.
NEXT_DIST_DIR = STATIC_DIR / "dist"
if NEXT_DIST_DIR.exists():
    app.mount("/next", StaticFiles(directory=str(NEXT_DIST_DIR), html=True), name="next")

    @app.get("/next", include_in_schema=False)
    def next_redirect():
        return RedirectResponse(url="/next/")
else:
    logger.warning(f"[Server] {NEXT_DIST_DIR} not found — run `npm run build` in frontend/ to enable /next")

@app.get("/")
@app.get("/{full_path:path}")
def spa_fallback(full_path: str = ""):
    index = TEMPLATES_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "Frontend not found"}


# ── Banner ─────────────────────────────────────────────────────────────────────
def print_banner():
    port = int(os.getenv('PORT', 3000))
    print("\n" + "═"*65)
    print("  🤖  JARVIS TRADING AI  v6.7  (Python Edition)")
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

    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, log_level="warning")
