"""
Jarvis Trading AI — Python Edition v6.8
FastAPI + APScheduler + SQLAlchemy + TA-Lib
Run: python main.py
"""
import os, logging, sys, threading, time, signal
from pathlib import Path
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
Path('data').mkdir(exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('data/jarvis.log', encoding='utf-8')
    ]
)
for noisy in ['httpx','httpcore','alpaca','apscheduler','urllib3','feedparser','yfinance','peewee']:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── FastAPI ────────────────────────────────────────────────────────────────────
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

scheduler = None

@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Startup / shutdown."""
    global scheduler

    # ── Startup ────────────────────────────────────────────────────────────────
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


app = FastAPI(title="Jarvis Trading AI", version="6.7.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Database init ──────────────────────────────────────────────────────────────
from app.database import init_db
init_db()

# ── Routes ────────────────────────────────────────────────────────────────────
from app.routes import router
app.include_router(router, prefix="/api")

# ── Static / SPA ──────────────────────────────────────────────────────────────
STATIC_DIR    = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
