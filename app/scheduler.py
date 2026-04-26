"""
APScheduler-based job scheduler. No Windows libuv issues.
Jobs fire immediately on startup (next_run_time=now) — no waiting for first interval.
"""
import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

logger = logging.getLogger(__name__)

job_status = {
    'market':    {'status': 'idle', 'last': None, 'error': None},
    'threats':   {'status': 'idle', 'last': None, 'error': None},
    'signals':   {'status': 'idle', 'last': None, 'error': None},
    'execute':   {'status': 'idle', 'last': None, 'error': None},
    'positions': {'status': 'idle', 'last': None, 'error': None},
    'telegram':  {'status': 'idle', 'last': None, 'error': None},
}

def make_job_runner(name: str, fn):
    def runner():
        if job_status[name]['status'] == 'running':
            logger.info(f"[Scheduler] {name} already running — skipping")
            return
        job_status[name]['status'] = 'running'
        job_status[name]['error'] = None
        try:
            fn()
            job_status[name]['last'] = datetime.now(timezone.utc).isoformat()
            job_status[name]['status'] = 'ok'
        except Exception as e:
            logger.error(f"[Scheduler] {name} error: {e}", exc_info=True)
            job_status[name]['status'] = 'error'
            job_status[name]['error'] = str(e)
    return runner

def create_scheduler() -> BackgroundScheduler:
    executors = {'default': ThreadPoolExecutor(max_workers=4)}
    sched = BackgroundScheduler(executors=executors, timezone='UTC')

    from jobs.fetch_market_data import run as market_run
    from jobs.fetch_threat_news import run as threats_run
    from jobs.generate_signals  import run as signals_run
    from jobs.execute_signals   import run as execute_run
    from jobs.manage_positions  import run as positions_run
    from jobs.telegram_bot      import run as telegram_run

    now = datetime.now(timezone.utc)

    # next_run_time=now means each job fires immediately when the scheduler starts.
    # Stagger the heavy jobs by a few seconds using start_date offsets so they
    # don't all hammer APIs at the exact same moment.
    sched.add_job(make_job_runner('market',    market_run),
                  'interval', minutes=15,  id='market',
                  next_run_time=now)

    sched.add_job(make_job_runner('threats',   threats_run),
                  'interval', minutes=15,  id='threats',
                  next_run_time=now)

    # Signals run after market data — give it a 90s head-start on first run
    from datetime import timedelta
    sched.add_job(make_job_runner('signals',   signals_run),
                  'interval', minutes=30,  id='signals',
                  next_run_time=now + timedelta(seconds=90))

    # Execute runs right after signals — 3 min after startup
    sched.add_job(make_job_runner('execute',   execute_run),
                  'interval', minutes=30,  id='execute',
                  next_run_time=now + timedelta(minutes=3))

    # Position management every 5 min, starts immediately
    sched.add_job(make_job_runner('positions', positions_run),
                  'interval', minutes=5,   id='positions',
                  next_run_time=now + timedelta(seconds=30))

    # Telegram every 1 min, starts immediately
    sched.add_job(make_job_runner('telegram',  telegram_run),
                  'interval', minutes=1,   id='telegram',
                  next_run_time=now)

    logger.info("[Scheduler] All jobs registered — firing immediately on startup")
    return sched
