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

import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, db, watcher

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

scheduler: BackgroundScheduler | None = None
_run_lock = threading.Lock()
_run_started_at: datetime | None = None


RUN_TIMEOUT = 900  # 15 minutes max par run (flights + hotels)
# Le scheduler hérite du fuseau, pas les triggers construits explicitement :
# sans ce paramètre, CronTrigger suit la TZ du process (UTC hors Docker).
SCHED_TZ = "Europe/Paris"
# Un redémarrage pile à l'heure du cron faisait sauter l'occurrence
# (grâce par défaut : 1 s). Une heure de retard reste acceptable ici.
MISFIRE_GRACE = 3600
# Borne des paramètres `days` : days=10**10 levait OverflowError (500).
# Reste très au-dessus du `days=9999` que le dashboard envoie pour « tout ».
DAYS_MAX = 36500


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
        from app import sources
        cfg = config.load()
        by_name = {t.name: t for t in cfg.trips}
        for trip_name in trips_in_flash:
            trip = by_name.get(trip_name)
            # Une période désactivée dans l'admin continuait d'être
            # interrogée toutes les 5 min jusqu'à expiration du flash.
            if not trip or not trip.enabled:
                continue
            # Le flash dure 48 h : sans ce filtre il continuait d'interroger
            # une période dont la fenêtre venait d'expirer, avec une date
            # de départ passée. Même borne que run_once.
            if trip.outbound_window[1] < datetime.now().date().isoformat():
                continue
            # Lightweight check: Duffel only, mid-date only
            mids = watcher.mid_combo(trip)
            if mids is None:
                continue
            out_mid, ret_mid = mids
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


def _on_job_skipped(event) -> None:
    """Trace les occurrences perdues : sinon les trous d'historique sont muets."""
    reason = ("run précédent encore en cours"
              if event.code == EVENT_JOB_MAX_INSTANCES else "misfire")
    print(f"Scheduler: occurrence sautée ({event.job_id}) — {reason}")


def _next_run_iso() -> str | None:
    """Prochain déclenchement du cron, None si le job a disparu.

    Un reschedule raté laissait le dashboard sans aucun signe que plus
    rien n'était programmé.
    """
    if not scheduler:
        return None
    job = scheduler.get_job("watcher")
    nxt = getattr(job, "next_run_time", None) if job else None
    return nxt.isoformat() if nxt else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    cfg = config.load()
    db.init()
    _cleanup_stale_runs()
    scheduler = BackgroundScheduler(timezone=SCHED_TZ)
    trigger = CronTrigger.from_crontab(cfg.schedule_cron, timezone=SCHED_TZ)
    scheduler.add_job(_run_safe, trigger, id="watcher",
                      max_instances=1, coalesce=True,
                      misfire_grace_time=MISFIRE_GRACE)
    # Flash mode: vérification toutes les 5 minutes
    scheduler.add_job(_flash_check, IntervalTrigger(minutes=5),
                      id="flash_check", max_instances=1, coalesce=True)
    scheduler.add_job(_watchdog, IntervalTrigger(minutes=2),
                      id="watchdog", max_instances=1, coalesce=True)
    scheduler.add_listener(_on_job_skipped,
                           EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)
    scheduler.start()
    print(f"Scheduler started: {cfg.schedule_cron} + flash every 5min + watchdog every 2min")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


# Sans auth devant, /docs offrait un formulaire « Try it out » sur
# PUT /api/admin/config et POST /api/run-now. DEBUG=1 les rétablit en local.
_DEBUG = os.getenv("DEBUG") == "1"

app = FastAPI(
    title="Flight Watcher", lifespan=lifespan,
    docs_url="/docs" if _DEBUG else None,
    redoc_url="/redoc" if _DEBUG else None,
    openapi_url="/openapi.json" if _DEBUG else None,
)


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/robots.txt")
async def robots():
    return FileResponse(STATIC_DIR / "robots.txt")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/trips")
def get_trips():
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
def get_trip_history(name: str, days: int = Query(60, ge=1, le=DAYS_MAX)):
    with db.conn() as c:
        return db.trip_history(c, name, days)


@app.get("/api/trips/{name}/history-by-route")
def get_trip_history_by_route(name: str,
                              days: int = Query(60, ge=1, le=DAYS_MAX)):
    with db.conn() as c:
        return db.trip_history_by_route(c, name, days)


@app.get("/api/trips/{name}/breakdown")
def get_trip_breakdown(name: str):
    with db.conn() as c:
        return db.trip_breakdown(c, name)


@app.get("/api/trips/{name}/heatmap")
def get_trip_heatmap(name: str, days: int = Query(180, ge=1, le=DAYS_MAX)):
    # Fenêtre large par défaut : un run ne persiste qu'une combinaison de
    # dates par paire, donc une borne courte viderait la grille.
    with db.conn() as c:
        flat = db.heatmap_data(c, name, days)
    if not flat:
        return {"outbound_dates": [], "return_dates": [], "prices": []}
    # Build 2D matrix expected by frontend
    out_set: dict[str, int] = {}
    ret_set: dict[str, int] = {}
    for row in flat:
        od = row["outbound_date"]
        rd = row["return_date"]
        if od and od not in out_set:
            out_set[od] = len(out_set)
        if rd and rd not in ret_set:
            ret_set[rd] = len(ret_set)
    outbound_dates = sorted(out_set.keys())
    return_dates = sorted(ret_set.keys())
    out_idx = {d: i for i, d in enumerate(outbound_dates)}
    ret_idx = {d: i for i, d in enumerate(return_dates)}
    prices = [[None] * len(return_dates) for _ in range(len(outbound_dates))]
    for row in flat:
        od, rd = row["outbound_date"], row["return_date"]
        if od in out_idx and rd in ret_idx:
            prices[out_idx[od]][ret_idx[rd]] = row["best_eur"]
    return {"outbound_dates": outbound_dates, "return_dates": return_dates,
            "prices": prices}


@app.get("/api/trips/{name}/stats")
def get_trip_stats(name: str):
    cfg = config.load()
    by_name = {t.name: t for t in cfg.trips}
    trip = by_name.get(name)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    # _calc_buy_score ne lit jamais les taux : les deux appels frankfurter
    # qui étaient faits ici coûtaient 2 requêtes HTTP par carte, sans effet.
    no_rates: dict[str, float] = {}

    with db.conn() as c:
        trend_data = db.price_trend(c, name, days=7)
        trend = watcher._calc_trend(trend_data)

        # Compute buy score from current state
        state = db.get_state(c, name)
        buy_score = None
        pct_cache: dict[float, float | None] = {}
        if state and state.get("lowest_price_eur") is not None:
            current_price = state["lowest_price_eur"]
            pct = db.percentile_rank(c, name, current_price)
            pct_cache[current_price] = pct
            buy_score = watcher._calc_buy_score(
                current_price, trip, cfg, no_rates, pct, trend,
            )

    # Buy score history: compute score for each historical price point
    score_history = []
    with db.conn() as c:
        hist = db.price_trend(c, name, days=30)
        if len(hist) >= 2:
            for i in range(len(hist)):
                sub = hist[:i + 1]
                t_trend = watcher._calc_trend(sub)
                price = sub[-1][1]
                # Le percentile ne dépend que du prix : un seul scan de
                # l'historique par prix distinct au lieu d'un par point.
                if price not in pct_cache:
                    pct_cache[price] = db.percentile_rank(c, name, price)
                score_i = watcher._calc_buy_score(
                    price, trip, cfg, no_rates, pct_cache[price], t_trend)
                score_history.append({
                    "date": sub[-1][0], "score": score_i, "price": price})

    return {
        "trend": trend,
        "buy_score": buy_score,
        "score_history": score_history,
    }


@app.get("/api/alerts")
def get_alerts(limit: int = Query(20, ge=1, le=200)):
    with db.conn() as c:
        return db.recent_alerts(c, limit)


@app.get("/api/runs")
def get_runs(limit: int = Query(10, ge=1, le=200)):
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
def config_summary():
    cfg = config.load()
    return {
        "origins": cfg.origins,
        "destinations": cfg.destinations,
        "adults": cfg.adults,
        "children": cfg.children,
        "max_fly_duration_hours": cfg.max_fly_duration_hours,
        "schedule_cron": cfg.schedule_cron,
        "next_run": _next_run_iso(),
        "running": _run_lock.locked(),
    }


# ── Admin API ────────────────────────────────────────────────


@app.get("/api/admin/config")
def get_admin_config():
    """Return full editable config."""
    data = config.load_raw()
    # Le topic ntfy est une clé d'écriture publique : ni l'admin ni le
    # PUT n'en ont besoin (save_raw repart de load_raw, il est préservé).
    return {k: v for k, v in data.items() if k != "ntfy"}


class ConfigUpdate(BaseModel):
    # Bornes serveur : le garde min/max du navigateur ne protégeait rien.
    # adults=10**9 remplissait la mémoire du conteneur, adults=0 coupait
    # silencieusement la collecte (Duffel 422 avalés).
    origins: list[str] = Field(max_length=20)
    destinations: list[str] = Field(max_length=20)
    adults: int | None = Field(default=None, ge=1, le=9)
    children: list[int] | None = Field(default=None, max_length=8)
    max_fly_duration_hours: int | None = Field(default=None, ge=6, le=48)
    schedule_cron: str | None = Field(default=None, max_length=64)
    trips: list[dict] | None = Field(default=None, max_length=20)
    hotels: list[dict] | None = Field(default=None, max_length=20)


@app.put("/api/admin/config")
def update_admin_config(body: ConfigUpdate):
    """Update config.yml with new values."""
    # Un cron invalide faisait échouer from_crontab APRÈS remove_job :
    # le job disparaissait et plus rien n'était planifié.
    new_trigger = None
    if body.schedule_cron is not None:
        try:
            new_trigger = CronTrigger.from_crontab(body.schedule_cron,
                                                   timezone=SCHED_TZ)
        except ValueError as e:
            raise HTTPException(status_code=422,
                                detail=f"Cron invalide : {e}")
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
    if body.hotels is not None:
        data["hotels"] = body.hotels
    try:
        config.save_raw(data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # Reschedule if cron changed
    if new_trigger is not None and scheduler:
        try:
            scheduler.add_job(_run_safe, new_trigger, id="watcher",
                              max_instances=1, coalesce=True,
                              misfire_grace_time=MISFIRE_GRACE,
                              replace_existing=True)
            print(f"Scheduler rescheduled: {body.schedule_cron}")
        except Exception as e:
            print(f"Reschedule error: {e}")
    return {"status": "ok"}


# ── Hotels API ───────────────────────────────────────────────

def _nights_between(checkin: str, checkout: str) -> int | None:
    """Nombre de nuits, ou None si les dates sont absentes ou invalides."""
    if not checkin or not checkout:
        return None
    try:
        ci = datetime.strptime(str(checkin)[:10], "%Y-%m-%d")
        co = datetime.strptime(str(checkout)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    nights = (co - ci).days
    return nights if nights > 0 else None


@app.get("/api/hotels")
def get_hotels():
    cfg = config.load()
    with db.conn() as c:
        summary = db.hotel_summary(c)
    by_name = {h.name: h for h in cfg.hotels}
    # Un hôtel retiré de la config laissait une carte fantôme définitive.
    summary = [s for s in summary if s["hotel_name"] in by_name]
    for s in summary:
        h = by_name[s["hotel_name"]]
        s["checkin"] = h.checkin
        s["checkout"] = h.checkout
        s["threshold"] = h.price_threshold
        s["enabled"] = h.enabled
        s["nights"] = _nights_between(h.checkin, h.checkout)
    # Hôtels configurés mais sans aucune donnée
    have = {s["hotel_name"] for s in summary}
    for h in cfg.hotels:
        if h.name not in have:
            summary.append({
                "hotel_name": h.name, "trip_name": h.name,
                "current_best": None, "lowest_price_eur": None,
                "avg_30d": None, "last_check_at": None,
                "last_captured_at": None, "last_error": None,
                "consecutive_failures": 0,
                "checkin": h.checkin, "checkout": h.checkout,
                "nights": _nights_between(h.checkin, h.checkout),
                "threshold": h.price_threshold,
                "enabled": h.enabled,
            })
    return summary


@app.get("/api/hotels/{hotel_name}/history")
def get_hotel_history(hotel_name: str,
                      days: int = Query(60, ge=1, le=DAYS_MAX)):
    with db.conn() as c:
        return db.hotel_history(c, hotel_name, hotel_name, days)


@app.get("/api/hotels/{hotel_name}/breakdown")
def get_hotel_breakdown(hotel_name: str):
    with db.conn() as c:
        return db.hotel_breakdown(c, hotel_name, hotel_name)


# Cache mémoire du proxy FX : la BCE publie une fois par jour ouvré, mais
# chaque onglet ouvert reproxifiait frankfurter depuis l'IP du VPS.
_fx_cache: dict[int, tuple[datetime, dict]] = {}
_FX_TTL = timedelta(hours=24)


@app.get("/api/fx-history")
def fx_history(months: int = Query(6, ge=1, le=24)):
    """EUR/THB history proxied to avoid CORS."""
    import requests as req
    cached = _fx_cache.get(months)
    if cached and datetime.now() - cached[0] < _FX_TTL:
        return cached[1]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    try:
        r = req.get(f"https://api.frankfurter.app/{start}..{end}",
                    params={"from": "EUR", "to": "THB"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates", {})
        dates = sorted(rates.keys())
        payload = {"dates": dates,
                   "rates": [rates[d]["THB"] for d in dates]}
        _fx_cache[months] = (datetime.now(), payload)
        return payload
    except Exception:
        # Mieux vaut une série un peu datée qu'un trou dans le graphe.
        if cached:
            return cached[1]
        return JSONResponse({"error": "FX fetch failed"}, status_code=502)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "next_run": _next_run_iso(),
            "running": _run_lock.locked()}
