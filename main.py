"""
Jarvis Trading AI — Python Edition
FastAPI + APScheduler + SQLAlchemy + pandas-ta
Run: python main.py
"""
import os, logging, sys, threading, time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('data/jarvis.log', encoding='utf-8')
    ]
)
# Quiet noisy libs
for noisy in ['httpx','httpcore','alpaca','apscheduler','urllib3','feedparser']:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── FastAPI App ────────────────────────────────────────────────────────────────
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Jarvis Trading AI", version="5.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Database init ──────────────────────────────────────────────────────────────
from app.database import init_db
init_db()

# ── Routes ────────────────────────────────────────────────────────────────────
from app.routes import router
app.include_router(router, prefix="/api")

# ── Static / SPA ──────────────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
@app.get("/{full_path:path}")
def spa_fallback(full_path: str = ""):
    index = TEMPLATES_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "Frontend not found"}

# ── Scheduler ──────────────────────────────────────────────────────────────────
scheduler = None

@app.on_event("startup")
async def startup():
    global scheduler
    from app.scheduler import create_scheduler
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("[Server] APScheduler started")
    
    # Initial data fetches — staggered in background threads
    def initial_fetch():
        time.sleep(3)
        logger.info("[Startup] Initial market data fetch...")
        try:
            from jobs.fetch_market_data import run as mrun
            mrun()
        except Exception as e:
            logger.error(f"[Startup] Market fetch error: {e}")
        
        time.sleep(5)
        logger.info("[Startup] Initial threat news fetch...")
        try:
            from jobs.fetch_threat_news import run as trun
            trun()
        except Exception as e:
            logger.error(f"[Startup] News fetch error: {e}")
        
        time.sleep(15)
        logger.info("[Startup] Initial signal generation...")
        try:
            from jobs.generate_signals import run as srun
            srun()
        except Exception as e:
            logger.error(f"[Startup] Signal gen error: {e}")
    
    threading.Thread(target=initial_fetch, daemon=True).start()

@app.on_event("shutdown")
async def shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("[Server] Shutdown complete")

# ── Entry Point ────────────────────────────────────────────────────────────────
def print_banner():
    port = int(os.getenv('PORT', 3000))
    print("\n" + "═"*65)
    print("  🤖  JARVIS TRADING AI  v5.0  (Python Edition)")
    print("═"*65)
    print(f"  Dashboard:  http://localhost:{port}")
    print(f"  API docs:   http://localhost:{port}/docs")
    print("═"*65)
    print("  Schedules:")
    print("    Market Data     → every 15 min")
    print("    Threat News     → every 15 min (offset 7m)")
    print("    Signal Gen      → every 30 min")
    print("    Signal Execute  → every 30 min (offset 3m)")
    print("    Position Mgmt   → every  5 min")
    print("    Telegram Bot    → every  1 min")
    print("═"*65)
    print("  First run: Go to /settings to configure API keys")
    print("═"*65 + "\n")

if __name__ == "__main__":
    import uvicorn
    print_banner()
    port = int(os.getenv('PORT', 3000))
    
    # Auto-open browser
    def open_browser():
        time.sleep(2)
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except:
            pass
    threading.Thread(target=open_browser, daemon=True).start()
    
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, log_level="warning")
