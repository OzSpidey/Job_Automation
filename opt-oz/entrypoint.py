"""
Docker entrypoint: runs the paper trading loop in a background thread
and serves the FastAPI dashboard in the foreground.
"""
import logging
import os
import threading
import time

import uvicorn

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


def run_trading_loop():
    from apscheduler.schedulers.background import BackgroundScheduler
    from scripts.run_paper import PaperTradingLoop

    loop = PaperTradingLoop()
    scheduler = BackgroundScheduler(timezone="America/New_York")
    scheduler.add_job(loop.run_once, "cron", day_of_week="mon-fri", hour=16, minute=30)
    scheduler.start()
    loop.run_once()

    try:
        while True:
            time.sleep(60)
    except Exception as exc:
        log.error("Trading loop crashed: %s", exc)
        scheduler.shutdown()


if __name__ == "__main__":
    log.info("Starting Opt-Oz — paper mode")

    t = threading.Thread(target=run_trading_loop, daemon=True, name="trading-loop")
    t.start()

    uvicorn.run(
        "optoz.monitor.app:app",
        host="0.0.0.0",
        port=8080,
        log_level="warning",
        reload=False,
    )
