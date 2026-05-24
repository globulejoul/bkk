"""FastAPI app: web dashboard + JSON API + embedded scheduler.

Routes:
  /            -> dashboard HTML
  /api/trips   -> trip summaries
  /api/trips/{name}/history -> daily best
  /api/trips/{name}/breakdown -> per origin/destination
  /api/trips/{name}/heatmap -> heatmap data
  /api/trips/{name}/stats -> trend + buy score + DOW stats
  /api/alerts  -> recent alerts
  /api/runs    -> last runs
  /api/run-now -> trigger immediate check (POST)
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, db, watcher

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

scheduler: BackgroundScheduler | None = None
_run_lock = threading.Lock()
_run_started_at: datetime | None = None


RUN_TIMEOUT = 900  # 15 minutes max par run (flights + hotels)


def _run_safe() -> None:
    global _run_started_at
    if not _run_lock.acquire(blocking=False):
        print("Skip: previous run still in progress")
        return
    run_token = datetime.now()
    _run_started_at = run_token
    try:
        cfg = config.load()
        result = watcher.run_once(cfg)
        print(f"Run complete: {result}")
    except Exception as e:
        print(f"Run error: {e}")
    finally:
        # Ne release que si c'est toujours NOTRE run
        # (le watchdog a pu release + un autre run a pu prendre le lock)
        if _run_started_at is run_token:
            _run_started_at = None
            try:
                _run_lock.release()
            except RuntimeError:
                pass


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


def _watchdog() -> None:
    """Détecte les runs bloqués et force le nettoyage."""
    global _run_started_at
    if not _run_lock.locked() or _run_started_at is None:
        return
    elapsed = (datetime.now() - _run_started_at).total_seconds()
    if elapsed < RUN_TIMEOUT:
        return
    print(f"WATCHDOG: run bloqué depuis {elapsed:.0f}s (>{RUN_TIMEOUT}s), nettoyage forcé")
    with db.conn() as c:
        now_iso = datetime.now().isoformat()
        c.execute(
            "UPDATE run_log SET status='timeout', finished_at=?, "
            "error=? WHERE status='running'",
            (now_iso, f"Watchdog timeout after {elapsed:.0f}s"),
        )
    _run_started_at = None
    try:
        _run_lock.release()
    except RuntimeError:
        pass


def _flash_check() -> None:
    """Check if any trip is in flash mode and trigger a lightweight check."""
    if _run_lock.locked():
        return
    now_iso = datetime.now().isoformat()
    trips_in_flash: list[str] = []
    with db.conn() as c:
        rows = c.execute(
            "SELECT trip_name, flash_until FROM state "
            "WHERE flash_until IS NOT NULL AND flash_until > ?",
            (now_iso,),
        ).fetchall()
        trips_in_flash = [r[0] for r in rows]

    if not trips_in_flash:
        return

    print(f"Flash mode: {len(trips_in_flash)} trip(s) en flash — "
          f"{', '.join(trips_in_flash)}")

    if not _run_lock.acquire(blocking=False):
        return
    try:
        from app import fx, sources
        cfg = config.load()
        rates = fx.fetch_rates(cfg.currency, ["THB"])
        by_name = {t.name: t for t in cfg.trips}
        for trip_name in trips_in_flash:
            trip = by_name.get(trip_name)
            if not trip:
                continue
            # Lightweight check: Duffel only, mid-date only
            out_mid = sources.mid_date(trip.outbound_window)
            ret_mid = sources.mid_date(trip.return_window)
            print(f"  Flash check {trip_name}: Duffel {out_mid}/{ret_mid}")
            results = sources.search_duffel(
                origins=cfg.origins, destinations=cfg.destinations,
                outbound_dates=[out_mid], return_dates=[ret_mid],
                adults=cfg.adults, currency=cfg.currency,
                max_fly_h=cfg.max_fly_duration_hours,
            )
            if results:
                print(f"  Flash {trip_name}: {len(results)} résultats, "
                      f"best {results[0].price} {results[0].currency}")
    except Exception as e:
        print(f"Flash check error: {e}")
    finally:
        _run_lock.release()


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
    # Flash mode: vérification toutes les 5 minutes
    scheduler.add_job(_flash_check, IntervalTrigger(minutes=5),
                      id="flash_check", max_instances=1, coalesce=True)
    scheduler.add_job(_watchdog, IntervalTrigger(minutes=2),
                      id="watchdog", max_instances=1, coalesce=True)
    scheduler.start()
    print(f"Scheduler started: {cfg.schedule_cron} + flash every 5min + watchdog every 2min")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Flight Watcher", lifespan=lifespan)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/robots.txt")
async def robots():
    return FileResponse(STATIC_DIR / "robots.txt")


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


@app.get("/api/trips/{name}/heatmap")
async def get_trip_heatmap(name: str):
    with db.conn() as c:
        return db.heatmap_data(c, name)


@app.get("/api/trips/{name}/stats")
async def get_trip_stats(name: str):
    cfg = config.load()
    by_name = {t.name: t for t in cfg.trips}
    trip = by_name.get(name)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    with db.conn() as c:
        dow_stats = db.day_of_week_stats(c, name)
        trend_data = db.price_trend(c, name, days=7)
        trend = watcher._calc_trend(trend_data)

        # Compute buy score from current state
        state = db.get_state(c, name)
        buy_score = None
        if state and state.get("lowest_price_eur") is not None:
            current_price = state["lowest_price_eur"]
            pct = db.percentile_rank(c, name, current_price)
            from app import fx
            rates = fx.fetch_rates(cfg.currency, ["THB"])
            buy_score = watcher._calc_buy_score(
                current_price, trip, cfg, rates, pct, trend,
            )

    return {
        "day_of_week": dow_stats,
        "trend": trend,
        "buy_score": buy_score,
    }


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
    global _run_started_at
    if _run_lock.locked():
        # Vérifier si un run est vraiment en cours en base
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM run_log WHERE status='running' LIMIT 1"
            ).fetchone()
        if not row:
            # Lock fantôme : aucun run en cours en base, on force le reset
            print("run-now: lock fantôme détecté, reset forcé")
            _run_started_at = None
            try:
                _run_lock.release()
            except RuntimeError:
                pass
        else:
            return JSONResponse({"status": "already_running"}, status_code=409)
    threading.Thread(target=_run_safe, daemon=True).start()
    return {"status": "started"}


@app.get("/api/config-summary")
async def config_summary():
    cfg = config.load()
    return {
        "origins": cfg.origins,
        "destinations": cfg.destinations,
        "adults": cfg.adults,
        "children": cfg.children,
        "max_fly_duration_hours": cfg.max_fly_duration_hours,
        "schedule_cron": cfg.schedule_cron,
    }


# ── Admin API ────────────────────────────────────────────────


@app.get("/api/admin/config")
async def get_admin_config():
    """Return full editable config."""
    data = config.load_raw()
    return data


class ConfigUpdate(BaseModel):
    origins: list[str]
    destinations: list[str]
    adults: int | None = None
    children: list[int] | None = None
    max_fly_duration_hours: int | None = None
    schedule_cron: str | None = None
    trips: list[dict] | None = None


@app.put("/api/admin/config")
async def update_admin_config(body: ConfigUpdate):
    """Update config.yml with new values."""
    data = config.load_raw()
    data["origins"] = [o.strip().upper() for o in body.origins if o.strip()]
    data["destinations"] = [d.strip().upper() for d in body.destinations if d.strip()]
    if body.adults is not None:
        data["adults"] = body.adults
    if body.children is not None:
        data["children"] = body.children
    if body.max_fly_duration_hours is not None:
        data["max_fly_duration_hours"] = body.max_fly_duration_hours
    if body.schedule_cron is not None:
        data["schedule_cron"] = body.schedule_cron
    if body.trips is not None:
        data["trips"] = body.trips
    try:
        config.save_raw(data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # Reschedule if cron changed
    if body.schedule_cron and scheduler:
        try:
            scheduler.remove_job("watcher")
            trigger = CronTrigger.from_crontab(body.schedule_cron)
            scheduler.add_job(_run_safe, trigger, id="watcher",
                              max_instances=1, coalesce=True)
            print(f"Scheduler rescheduled: {body.schedule_cron}")
        except Exception as e:
            print(f"Reschedule error: {e}")
    return {"status": "ok"}


# ── Hotels API ───────────────────────────────────────────────

@app.get("/api/hotels")
async def get_hotels():
    cfg = config.load()
    with db.conn() as c:
        summary = db.hotel_summary(c)
    by_name = {h.name: h for h in cfg.hotels}
    for s in summary:
        h = by_name.get(s["hotel_name"])
        if h:
            s["checkin"] = h.checkin
            s["checkout"] = h.checkout
            s["threshold"] = h.price_threshold
            s["enabled"] = h.enabled
    # Add hotels from config that have no data yet
    have = {s["hotel_name"] for s in summary}
    for h in cfg.hotels:
        if h.name not in have:
            checkin_dt = datetime.strptime(h.checkin, "%Y-%m-%d") if h.checkin else None
            checkout_dt = datetime.strptime(h.checkout, "%Y-%m-%d") if h.checkout else None
            nights = (checkout_dt - checkin_dt).days if checkin_dt and checkout_dt else None
            summary.append({
                "hotel_name": h.name, "trip_name": h.name,
                "current_best": None, "lowest_price_eur": None,
                "avg_30d": None, "last_check_at": None,
                "checkin": h.checkin, "checkout": h.checkout,
                "nights": nights,
                "threshold": h.price_threshold,
                "enabled": h.enabled,
            })
    return summary


@app.get("/api/hotels/{hotel_name}/history")
async def get_hotel_history(hotel_name: str, days: int = 60):
    with db.conn() as c:
        return db.hotel_history(c, hotel_name, hotel_name, days)


@app.get("/api/hotels/{hotel_name}/breakdown")
async def get_hotel_breakdown(hotel_name: str):
    with db.conn() as c:
        return db.hotel_breakdown(c, hotel_name, hotel_name)


@app.get("/api/fx-history")
async def fx_history(months: int = 6):
    """EUR/THB history proxied to avoid CORS."""
    import requests as req
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    try:
        r = req.get(f"https://api.frankfurter.app/{start}..{end}",
                    params={"from": "EUR", "to": "THB"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates", {})
        dates = sorted(rates.keys())
        return {"dates": dates, "rates": [rates[d]["THB"] for d in dates]}
    except Exception:
        return JSONResponse({"error": "FX fetch failed"}, status_code=502)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
