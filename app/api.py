"""FastAPI app: web dashboard + JSON API + embedded scheduler.

Routes:
  /            -> dashboard HTML
  /api/trips   -> trip summaries
  /api/trips/{name}/history -> daily best
  /api/trips/{name}/breakdown -> per origin/destination
  /api/alerts  -> recent alerts
  /api/runs    -> last runs
  /api/run-now -> trigger immediate check (POST)
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config, db, watcher

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

scheduler: BackgroundScheduler | None = None
_run_lock = threading.Lock()


RUN_TIMEOUT = 600  # 10 minutes max par run


def _run_safe() -> None:
    if not _run_lock.acquire(blocking=False):
        print("Skip: previous run still in progress")
        return
    try:
        cfg = config.load()
        result = watcher.run_once(cfg)
        print(f"Run complete: {result}")
    except Exception as e:
        print(f"Run error: {e}")
    finally:
        _run_lock.release()


def _cleanup_stale_runs() -> None:
    """Mark orphaned 'running' runs as timed out (from previous crashes)."""
    with db.conn() as c:
        stale = c.execute(
            "UPDATE run_log SET status='timeout', finished_at=started_at, "
            "error='Container restarted during run' "
            "WHERE status='running'"
        ).rowcount
        if stale:
            print(f"Cleaned up {stale} stale run(s)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    cfg = config.load()
    db.init()
    _cleanup_stale_runs()
    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    trigger = CronTrigger.from_crontab(cfg.schedule_cron)
    scheduler.add_job(_run_safe, trigger, id="watcher",
                      max_instances=1, coalesce=True)
    scheduler.start()
    print(f"Scheduler started: {cfg.schedule_cron}")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Flight Watcher", lifespan=lifespan)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/trips")
async def get_trips():
    cfg = config.load()
    with db.conn() as c:
        summary = db.trips_summary(c)
    # Enrich with config (threshold, dates)
    by_name = {t.name: t for t in cfg.trips}
    for s in summary:
        t = by_name.get(s["trip_name"])
        if t:
            s["threshold"] = t.price_threshold
            s["outbound_window"] = t.outbound_window
            s["return_window"] = t.return_window
    # Also add trips that exist in config but have no data yet
    have = {s["trip_name"] for s in summary}
    for t in cfg.trips:
        if t.name not in have:
            summary.append({
                "trip_name": t.name,
                "current_best": None, "all_time_low": None,
                "all_time_high": None, "avg_30d": None,
                "last_check_at": None, "threshold": t.price_threshold,
                "outbound_window": t.outbound_window,
                "return_window": t.return_window,
            })
    # Sort by outbound date
    summary.sort(key=lambda s: s.get("outbound_window", ["9999"])[0])
    return summary


@app.get("/api/trips/{name}/history")
async def get_trip_history(name: str, days: int = 60):
    with db.conn() as c:
        return db.trip_history(c, name, days)


@app.get("/api/trips/{name}/breakdown")
async def get_trip_breakdown(name: str):
    with db.conn() as c:
        return db.trip_breakdown(c, name)


@app.get("/api/alerts")
async def get_alerts(limit: int = 20):
    with db.conn() as c:
        return db.recent_alerts(c, limit)


@app.get("/api/runs")
async def get_runs(limit: int = 10):
    with db.conn() as c:
        return db.last_runs(c, limit)


@app.post("/api/run-now")
async def run_now():
    if _run_lock.locked():
        return JSONResponse({"status": "already_running"}, status_code=409)
    threading.Thread(target=_run_safe, daemon=True).start()
    return {"status": "started"}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
