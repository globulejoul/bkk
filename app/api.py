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
  /api/test-notification -> send a test ntfy push (POST, admin)
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app import config, db, notify, watcher

log = logging.getLogger(__name__)

# fcntl n'existe pas sous Windows (dev local) : on y retombe sur le seul
# verrou mémoire, ce qui suffit puisqu'il n'y a pas de conteneur.
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

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

# Le tableau de bord reste consultable sans mot de passe ; seules les
# routes qui écrivent la configuration ou déclenchent un run (donc de la
# consommation d'API) sont protégées. Le nom de domaine est public : tout
# certificat émis par Caddy est publié dans les journaux Certificate
# Transparency, donc « URL non devinable » n'est pas une protection.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# `docker compose exec watcher python -m app run` tourne dans un AUTRE
# processus que le scheduler : le threading.Lock ne l'y voit pas, d'où
# deux runs simultanés. Un fichier posé à côté de la base sert d'arbitre
# commun aux deux processus.
RUN_LOCK_PATH = db.DB_PATH.parent / "run.lock"
# flock est attaché au descripteur ouvert, pas au processus : si le thread
# d'un run bloqué garde le sien, le watchdog a beau libérer le verrou
# mémoire, tout run suivant se heurte au verrou fichier de son propre
# processus. On garde donc la référence pour pouvoir la fermer de force.
_run_lock_fh: Any = None


def _release_file_lock() -> None:
    """Ferme le descripteur du verrou fichier, donc libère le flock."""
    global _run_lock_fh
    fh, _run_lock_fh = _run_lock_fh, None
    if fh is not None:
        try:
            fh.close()
        except (OSError, ValueError):
            pass


@contextmanager
def file_run_lock() -> Iterator[bool]:
    """Verrou inter-processus. Cède True s'il a été obtenu, False sinon."""
    global _run_lock_fh
    if fcntl is None:
        yield True
        return
    try:
        RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(RUN_LOCK_PATH, "w")
    except OSError as e:
        # Mieux vaut un run sans garde inter-process qu'un run refusé.
        log.warning(f"Verrou fichier indisponible ({e}), run sans garde inter-process")
        yield True
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()
        except (OSError, ValueError):
            pass
        yield False
        return
    _run_lock_fh = fh
    try:
        yield True
    finally:
        # _release_file_lock a pu être appelé entre-temps par le watchdog.
        if _run_lock_fh is fh:
            _release_file_lock()
        else:
            try:
                fh.close()
            except (OSError, ValueError):
                pass


def require_admin(
    x_admin_password: str | None = Header(default=None),
) -> None:
    """Refuse la requête si le mot de passe admin ne correspond pas."""
    if not ADMIN_PASSWORD:
        return  # non configuré (dev local) : pas de blocage
    if not x_admin_password or not hmac.compare_digest(
            x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401,
                            detail="Mot de passe administrateur requis")


def _run_safe() -> None:
    global _run_started_at
    if not _run_lock.acquire(blocking=False):
        log.warning("Skip: previous run still in progress")
        return
    run_token = datetime.now()
    _run_started_at = run_token
    try:
        with file_run_lock() as acquired:
            if not acquired:
                log.warning("Skip: un run tourne déjà dans un autre processus")
                return
            cfg = config.load()
            result = watcher.run_once(cfg)
            log.info(f"Run complete: {result}")
    except Exception as e:
        log.error(f"Run error: {e}")
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
            log.info(f"Cleaned up {stale} stale run(s)")


def _watchdog() -> None:
    """Détecte les runs bloqués et force le nettoyage."""
    global _run_started_at
    if not _run_lock.locked() or _run_started_at is None:
        return
    elapsed = (datetime.now() - _run_started_at).total_seconds()
    if elapsed < RUN_TIMEOUT:
        return
    log.error(f"WATCHDOG: run bloqué depuis {elapsed:.0f}s (>{RUN_TIMEOUT}s), nettoyage forcé")
    with db.conn() as c:
        now_iso = datetime.now().isoformat()
        c.execute(
            "UPDATE run_log SET status='timeout', finished_at=?, "
            "error=? WHERE status='running'",
            (now_iso, f"Watchdog timeout after {elapsed:.0f}s"),
        )
    _run_started_at = None
    # Le thread bloqué garde son descripteur ouvert : sans cette
    # fermeture, le flock survit au reset et bloque tous les runs suivants.
    _release_file_lock()
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

    log.info(f"Flash mode: {len(trips_in_flash)} trip(s) en flash — "
          f"{', '.join(trips_in_flash)}")

    if not _run_lock.acquire(blocking=False):
        return
    try:
        with file_run_lock() as acquired:
            if not acquired:
                # Un run en ligne de commande écrit dans les mêmes tables :
                # le flash doit respecter le même arbitre inter-processus.
                log.info("Flash: un run tourne déjà dans un autre processus")
                return
            from app import fx
            cfg = config.load()
            rates = fx.fetch_rates(cfg.currency, ["THB"])
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
                # Relevé allégé (Duffel, date médiane) mais passé dans la
                # même chaîne que le run complet : les prix sont enregistrés
                # et un nouveau plus bas déclenche bien une alerte.
                try:
                    rows, alerts = watcher.flash_check_trip(cfg, trip, rates)
                except Exception as e:
                    log.error(f"  Flash {trip_name}: échec — {e}")
                    continue
                if rows:
                    log.info(f"  Flash {trip_name}: {rows} lignes enregistrées, "
                          f"{alerts} alerte(s)")
    except Exception as e:
        log.error(f"Flash check error: {e}")
    finally:
        _run_lock.release()


def _on_job_skipped(event) -> None:
    """Trace les occurrences perdues : sinon les trous d'historique sont muets."""
    reason = ("run précédent encore en cours"
              if event.code == EVENT_JOB_MAX_INSTANCES else "misfire")
    log.info(f"Scheduler: occurrence sautée ({event.job_id}) — {reason}")


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
    # Flash mode : surveillance rapprochée pendant 48 h après un seuil
    # atteint. Il n'interroge que Duffel ; sans clé il prendrait le verrou
    # toutes les 5 minutes pour ne rien faire, en bloquant au passage un
    # run planifié ou un check manuel. On ne le programme donc que si la
    # clé est présente.
    flash_on = bool(os.environ.get("DUFFEL_API_KEY")
                    or os.environ.get("DUFFEL"))
    if flash_on:
        scheduler.add_job(_flash_check, IntervalTrigger(minutes=5),
                          id="flash_check", max_instances=1, coalesce=True)
    scheduler.add_job(_watchdog, IntervalTrigger(minutes=2),
                      id="watchdog", max_instances=1, coalesce=True)
    scheduler.add_listener(_on_job_skipped,
                           EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)
    scheduler.start()
    flash_txt = "flash 5min" if flash_on else "flash OFF (pas de clé Duffel)"
    log.error(f"Scheduler started: {cfg.schedule_cron} + {flash_txt} "
          f"+ watchdog every 2min")
    yield
    # Sans cette trace, un SIGKILL après le délai de grâce était
    # indiscernable d'un crash dans les logs du conteneur.
    en_cours = "OUI (sera coupé net)" if _run_lock.locked() else "non"
    log.info(f"Arrêt demandé : scheduler stoppé, run en cours : {en_cours}")
    if scheduler:
        scheduler.shutdown(wait=False)
    log.info("Arrêt terminé.")


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
    # Sans cet en-tête le document sort du cache heuristique du
    # navigateur et le nouveau ?v= des assets n'est jamais lu.
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


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
    # Une période retirée ou renommée laissait une carte fantôme définitive,
    # sans seuil ni fenêtres, avec son dernier prix figé. Les relevés
    # restent en base : seul l'affichage est filtré.
    summary = [s for s in summary if s["trip_name"] in by_name]
    for s in summary:
        t = by_name[s["trip_name"]]
        s["threshold"] = t.price_threshold
        s["outbound_window"] = t.outbound_window
        s["return_window"] = t.return_window
        s["enabled"] = t.enabled
    # Also add trips that exist in config but have no data yet
    have = {s["trip_name"] for s in summary}
    for t in cfg.trips:
        if t.name not in have:
            summary.append({
                "trip_name": t.name,
                "current_best": None, "all_time_low": None,
                "all_time_high": None, "avg_30d": None,
                "last_check_at": None, "last_captured_at": None,
                "threshold": t.price_threshold,
                "outbound_window": t.outbound_window,
                "return_window": t.return_window,
                "enabled": t.enabled,
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
                current_price, trip, pct, trend,
                lowest_eur=state.get("lowest_price_eur"),
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
                # Score recalculé AU JOUR du relevé : sinon le délai
                # avant départ d'aujourd'hui était appliqué à des points
                # vieux de plusieurs semaines, aplatissant la courbe.
                try:
                    jour = datetime.strptime(sub[-1][0], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    jour = None
                score_i = watcher._calc_buy_score(
                    price, trip, pct_cache[price], t_trend,
                    lowest_eur=min(p for _, p in sub), today=jour)
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


@app.post("/api/run-now", dependencies=[Depends(require_admin)])
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
            log.info("run-now: lock fantôme détecté, reset forcé")
            _run_started_at = None
            _release_file_lock()
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


@app.get("/api/admin/config", dependencies=[Depends(require_admin)])
def get_admin_config():
    """Return full editable config."""
    data = config.load_raw()
    # Le topic ntfy est une clé d'écriture publique : ni l'admin ni le
    # PUT n'en ont besoin (save_raw repart de load_raw, il est préservé).
    return {k: v for k, v in data.items() if k != "ntfy"}


class TripUpdate(BaseModel):
    """Période éditable depuis l'admin.

    Typée (au lieu d'un dict libre) pour que min_nights/max_nights soient
    bornés côté serveur : une contrainte absurde ne produit aucune
    combinaison de dates et rend la période muette. `extra=allow` garde
    les clés que l'admin n'expose pas encore.
    """
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=80)
    outbound_window: list[str] | None = Field(default=None, max_length=2)
    return_window: list[str] | None = Field(default=None, max_length=2)
    price_threshold: float | None = Field(default=None, ge=0, le=100000)
    min_nights: int | None = Field(default=None, ge=1, le=365)
    max_nights: int | None = Field(default=None, ge=1, le=365)
    enabled: bool = True


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
    trips: list[TripUpdate] | None = Field(default=None, max_length=20)
    hotels: list[dict] | None = Field(default=None, max_length=20)


@app.put("/api/admin/config", dependencies=[Depends(require_admin)])
def update_admin_config(body: ConfigUpdate):
    """Update config.yml with new values."""
    data = config.load_raw()
    # L'admin renvoie la config entière : le cron était donc reprogrammé
    # (remove + add) à chaque sauvegarde, même inchangé. On ne touche au
    # scheduler que si l'expression a réellement bougé, et le trigger est
    # construit AVANT toute modification : un cron invalide faisait
    # échouer from_crontab après coup, laissant le job supprimé.
    new_trigger = None
    if body.schedule_cron is not None \
            and body.schedule_cron != data.get("schedule_cron"):
        try:
            new_trigger = CronTrigger.from_crontab(body.schedule_cron,
                                                   timezone=SCHED_TZ)
        except ValueError as e:
            raise HTTPException(status_code=422,
                                detail=f"Cron invalide : {e}")
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
        # exclude_none : un seuil ou un nombre de nuits effacé disparaît du
        # YAML au lieu d'y écrire `null`.
        data["trips"] = [t.model_dump(exclude_none=True) for t in body.trips]
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
            log.info(f"Scheduler rescheduled: {body.schedule_cron}")
        except Exception as e:
            log.error(f"Reschedule error: {e}")
    return {"status": "ok"}


@app.post("/api/test-notification", dependencies=[Depends(require_admin)])
def test_notification() -> dict:
    """Envoie une notification de test.

    Le topic ntfy n'est vérifiable qu'au moment d'une vraie alerte :
    une erreur de saisie restait invisible pendant des semaines.
    """
    cfg = config.load()
    return {"sent": notify.send_test_ntfy(cfg)}


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
