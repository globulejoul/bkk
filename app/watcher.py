"""Main check loop: queries sources, detects alerts, persists, notifies."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any

from app import db, fx, hotels, notify, sources
from app.config import Config, HotelWatch, Trip


# Même fenêtre que le dashboard (api.py appelle price_trend days=7) :
# la notification annonçait « Tendance 7j » en calculant sur 14 jours,
# d'où deux recommandations opposées pour le même prix.
TREND_DAYS = 7


# ── Trend & buy-score helpers ───────────────────────────────────


def _calc_trend(prices_7d: list[tuple[str, float]]) -> dict[str, Any]:
    """Analyse la tendance sur les N derniers jours.

    *prices_7d*: list de (date_str, price).
    Retourne dict avec direction, change_pct, recommendation.
    """
    if len(prices_7d) < 2:
        return {"direction": "stable", "change_pct": 0.0,
                "recommendation": "Prix stable"}

    mid = len(prices_7d) // 2
    first_half = [p for _, p in prices_7d[:mid]]
    second_half = [p for _, p in prices_7d[mid:]]
    avg_first = sum(first_half) / len(first_half) if first_half else 0
    avg_second = sum(second_half) / len(second_half) if second_half else 0

    if avg_first == 0:
        change_pct = 0.0
    else:
        change_pct = round((avg_second - avg_first) / avg_first * 100, 1)

    # Threshold: ±2% pour considérer un mouvement significatif
    if change_pct < -2:
        direction = "falling"
        recommendation = "Tendance baissière, patiente"
    elif change_pct > 2:
        # Rebond après un creux ?
        direction = "rising"
        recommendation = "Rebond après un creux, achète"
    else:
        direction = "stable"
        recommendation = "Prix stable"

    return {"direction": direction, "change_pct": change_pct,
            "recommendation": recommendation}


def _calc_buy_score(price_eur: float, trip: Trip,
                    pct: float | None,
                    trend: dict[str, Any],
                    lowest_eur: float | None = None,
                    today: date | None = None) -> int:
    """Score d'achat 0-100 : faut-il réserver maintenant ?

    Pondération revue le 19 septembre 2026. L'ancienne version donnait
    25 points sur 100 à une hausse et 5 à une baisse, au motif qu'une
    hausse signale un « rebond après un creux ». C'est faux quand le prix
    monte alors qu'il est déjà haut. Elle accordait aussi 10 points au
    jour de la semaine du RELEVÉ, ce qui ne dit rien du vol et repose sur
    un mythe démonté (cf. HISTORIQUE.md).

    Facteurs retenus, tous vérifiables :
    - Percentile (45) : ce prix est-il bas au regard de son historique ?
    - Écart au plus bas connu (20) : combien on paie au-dessus du record.
    - Délai avant départ (25) : effet réel et documenté.
    - Tendance (10) : modulateur, pas prime. Une hausse ne vaut des
      points que si le prix est par ailleurs bas.
    """
    today = today or date.today()
    score = 0.0

    # 1) Percentile (45 pts) — le signal le plus informatif.
    score += 45 * (1 - pct / 100) if pct is not None else 22.5

    # 2) Écart au plus bas connu (20 pts). Au record : 20 ; 25 % au-dessus
    #    ou plus : 0. Répond à « est-ce que je paie cher pour ce voyage ? »
    if lowest_eur and lowest_eur > 0 and price_eur > 0:
        ecart = price_eur / lowest_eur - 1
        score += 20 * max(0.0, min(1.0, 1 - ecart / 0.25))
    else:
        score += 10

    # 3) Délai avant départ (25 pts) : trop tôt, l'offre n'est pas ouverte ;
    #    trop tard, les tarifs montent.
    try:
        out_date = datetime.strptime(trip.outbound_window[0], "%Y-%m-%d").date()
        jours = (out_date - today).days
        if 45 <= jours <= 90:
            score += 25
        elif 30 <= jours < 45 or 90 < jours <= 120:
            score += 20
        elif 15 <= jours < 30:
            score += 14
        elif jours < 15:
            score += 8
        else:
            score += 12      # très en avance : peu d'information
    except (ValueError, TypeError, IndexError):
        score += 12

    # 4) Tendance (10 pts). Une baisse en cours invite à attendre ; une
    #    hausse ne vaut sa prime que si le niveau reste bas (vrai rebond).
    direction = trend.get("direction", "stable")
    bas = pct is not None and pct <= 30
    if direction == "falling":
        score += 2
    elif direction == "rising":
        score += 10 if bas else 4
    else:
        score += 6

    return min(100, max(0, round(score)))


def run_once(cfg: Config) -> dict[str, Any]:
    """Execute one full check across all trips. Returns summary."""
    db.init()
    with db.conn() as c:
        run_id = db.start_run(c)

    today = date.today().isoformat()
    # Même borne que valid_combos : sinon une période dont la fenêtre
    # aller se termine aujourd'hui passait le test d'expiration puis
    # ressortait sans aucune date, comptée comme « aucune donnée ».
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    summary: dict[str, Any] = {
        "trips_checked": 0, "alerts_generated": 0,
        "errors": [], "expired": [], "status": "ok",
    }
    trips_active = 0
    try:
        rates = fx.fetch_rates(cfg.currency, ["THB"])

        for trip in cfg.trips:
            if not trip.enabled:
                print(f"  ⏸ {trip.name}: désactivé, skip")
                continue
            # Une fenêtre aller entièrement passée n'est plus réservable :
            # chaque combo partait quand même vers Duffel (422 + quota).
            if trip.outbound_window[1] < tomorrow:
                print(f"  🗓 {trip.name}: période échue, skip")
                summary["expired"].append(trip.name)
                continue
            trips_active += 1
            try:
                nb_rows, trip_alerts = _check_trip(cfg, trip, rates)
                summary["alerts_generated"] += trip_alerts
                if nb_rows:
                    summary["trips_checked"] += 1
                else:
                    # Sortie silencieuse (aucun résultat / aucun prix
                    # convertible) : elle ne doit pas passer pour un run sain.
                    summary["errors"].append(f"{trip.name}: aucune donnée")
            except Exception as e:
                summary["errors"].append(f"{trip.name}: {e}")
                print(f"  ❌ {trip.name}: {e}")

        # Hotel checks
        for hotel in cfg.hotels:
            if not hotel.enabled:
                print(f"  ⏸ Hotel {hotel.name}: désactivé, skip")
                continue
            if not hotel.checkin or not hotel.checkout:
                print(f"  ⚠ Hotel {hotel.name}: dates manquantes, skip")
                continue
            # Un séjour dont l'arrivée est passée ne peut plus être tarifé :
            # Google renvoyait alors un prix pour d'autres dates.
            if hotel.checkin < today:
                print(f"  🗓 Hotel {hotel.name}: séjour échu, skip")
                summary["expired"].append(f"Hotel {hotel.name}")
                continue
            try:
                _check_hotel(cfg, hotel, rates)
            except Exception as e:
                summary["errors"].append(f"Hotel {hotel.name}: {e}")
                print(f"  ❌ Hotel {hotel.name}: {e}")

        if trips_active and summary["trips_checked"] == 0:
            summary["status"] = "error"
        elif summary["errors"]:
            summary["status"] = "partial"
        error_txt = "; ".join(summary["errors"])[:1000] or None

        with db.conn() as c:
            db.finish_run(c, run_id, summary["status"],
                          summary["trips_checked"],
                          summary["alerts_generated"], error_txt)

        # Panne totale : sans cette alerte, une source cassée donnait des
        # runs « ✓ 0 alertes » pendant des semaines. On ne notifie qu'au
        # PASSAGE en panne : sinon une panne durable pousse une alerte
        # identique à chaque run, 4 fois par jour.
        if summary["status"] == "error" and not _previous_run_failed(run_id):
            notify.send_ops_ntfy(
                cfg, "✈️ Aucune donnée collectée",
                error_txt or "Toutes les périodes surveillées ont échoué.")
    except Exception as e:
        summary["status"] = "error"
        with db.conn() as c:
            db.finish_run(c, run_id, "error", summary["trips_checked"],
                          summary["alerts_generated"], str(e))
        raise

    return summary


def trip_config_hash(cfg: Config, trip: Trip) -> str:
    """Empreinte des paramètres qui définissent le voyage surveillé.

    Ne contient que ce qui rend deux prix comparables : changer le nom
    d'une période ou son seuil ne doit pas effacer la référence.
    """
    payload = json.dumps([
        sorted(cfg.origins), sorted(cfg.destinations),
        list(trip.outbound_window), list(trip.return_window),
        trip.min_nights, trip.max_nights,
        cfg.adults, len(cfg.children), cfg.max_fly_duration_hours,
    ], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def hotel_config_hash(cfg: Config, hotel: HotelWatch) -> str:
    """Empreinte du séjour surveillé (dates et voyageurs)."""
    payload = json.dumps([
        hotel.checkin, hotel.checkout, cfg.adults, len(cfg.children),
    ], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _previous_run_failed(run_id: int) -> bool:
    """Le run précédent était-il déjà en échec ?"""
    with db.conn() as c:
        row = c.execute(
            "SELECT status FROM run_log WHERE id < ? AND status != 'running' "
            "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    return bool(row) and row[0] in ("error", "timeout")


def valid_combos(trip: Trip) -> dict[str, list[str]]:
    """Combinaisons {date aller: [dates retour]} encore réservables et
    conformes aux contraintes de durée du séjour.

    Partagée avec le flash mode : tant que chaque chemin recalculait ses
    dates de son côté, le flash interrogeait des départs échus ou des
    durées que la période exclut.
    """
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    out_dates = [d for d in sources.date_range(trip.outbound_window)
                 if d >= tomorrow]
    ret_dates = [d for d in sources.date_range(trip.return_window)
                 if d >= tomorrow]
    if not out_dates or not ret_dates:
        return {}

    min_nights = trip.min_nights if trip.min_nights is not None else 1
    max_nights = trip.max_nights if trip.max_nights is not None else 10 ** 6
    combos: dict[str, list[str]] = {}
    for out_d in out_dates:
        out_day = date.fromisoformat(out_d)
        valid = [r for r in ret_dates
                 if min_nights <= (date.fromisoformat(r) - out_day).days
                 <= max_nights]
        if valid:
            combos[out_d] = valid
    return combos


def mid_combo(trip: Trip) -> tuple[str, str] | None:
    """Couple (aller, retour) médian parmi les combinaisons valides."""
    combos = valid_combos(trip)
    if not combos:
        return None
    out_mid = list(combos)[len(combos) // 2]
    rets = combos[out_mid]
    return out_mid, rets[len(rets) // 2]


def _check_trip(cfg: Config, trip: Trip,
                rates: dict[str, float]) -> tuple[int, int]:
    """Check one trip. Returns (lignes persistées, alertes envoyées)."""
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    # Dates échues écartées et contraintes de durée appliquées : sans
    # elles un A/R de 0 ou 40 nuits pouvait devenir le « nouveau plus
    # bas » pour un séjour qui n'est pas celui qu'on surveille.
    combos = valid_combos(trip)
    if not combos:
        print(f"  ⚠ {trip.name}: aucune combinaison réservable")
        return 0, 0
    out_dates = list(combos)
    ret_dates = sorted({r for rets in combos.values() for r in rets})
    nb_combos = sum(len(v) for v in combos.values())
    out_mid, ret_mid = mid_combo(trip)

    # 1) Google Flights via fli (date médiane uniquement, scraping)
    print(f"\n→ {trip.name}: fli {out_mid}/{ret_mid} + Duffel {nb_combos} combos dates")
    ff_results = sources.search_google_flights_multi(
        origins=cfg.origins, destinations=cfg.destinations,
        outbound_date=out_mid, return_date=ret_mid,
        adults=cfg.adults, max_fly_h=cfg.max_fly_duration_hours,
    )
    print(f"  Google Flights: {len(ff_results)} résultats (date médiane)")

    # 2) Duffel. search_duffel croise les deux listes de dates : quand la
    # contrainte de durée écarte des combos, on l'appelle une fois par
    # date aller avec ses seuls retours valides. Sinon un seul appel, pour
    # ne pas réinitialiser son suivi de quota sans raison.
    duffel_results: list[sources.FlightResult] = []
    if nb_combos == len(out_dates) * len(ret_dates):
        duffel_results = sources.search_duffel(
            origins=cfg.origins, destinations=cfg.destinations,
            outbound_dates=out_dates, return_dates=ret_dates,
            adults=cfg.adults, currency=cfg.currency,
            max_fly_h=cfg.max_fly_duration_hours,
        )
    else:
        for out_d, rets in combos.items():
            duffel_results += sources.search_duffel(
                origins=cfg.origins, destinations=cfg.destinations,
                outbound_dates=[out_d], return_dates=rets,
                adults=cfg.adults, currency=cfg.currency,
                max_fly_h=cfg.max_fly_duration_hours,
            )
    print(f"  Duffel: {len(duffel_results)} résultats ({nb_combos} combos dates)")

    # 3) Fusionner et garder le best par paire (origin, dest)
    all_results = ff_results + duffel_results
    if not all_results:
        print(f"  ⚠ Aucun résultat pour {trip.name}")
        return 0, 0

    by_pair: dict[tuple[str, str], sources.FlightResult] = {}
    for r in all_results:
        key = (r.origin, r.destination)
        price_eur = _to_eur(r, rates)
        if price_eur is None:
            continue
        existing = by_pair.get(key)
        if existing is None or price_eur < (_to_eur(existing, rates) or 1e9):
            by_pair[key] = r

    if not by_pair:
        print(f"  ⚠ Aucun prix convertible pour {trip.name}")
        return 0, 0

    # Best overall
    best = min(by_pair.values(), key=lambda r: _to_eur(r, rates) or 1e9)
    best_price_eur = _to_eur(best, rates)

    # 4) Persist tous les résultats par paire
    with db.conn() as c:
        for r in by_pair.values():
            price_eur_val = _to_eur(r, rates)
            db.insert_check(c, {
                "check_date": today, "trip_name": trip.name,
                "source": r.source,
                "origin": r.origin, "destination": r.destination,
                "price_local": r.price, "currency": r.currency,
                "price_eur": price_eur_val,
                "outbound_date": r.outbound_date,
                "return_date": r.return_date,
                "out_h": r.out_h, "ret_h": r.ret_h,
                "out_stops": r.out_stops, "ret_stops": r.ret_stops,
                "airlines": r.airlines, "booking_url": r.booking_url,
                "captured_at": now,
            })

        # 5) State update / alert detection
        state = db.get_state(c, trip.name) or {}
        cfg_hash = trip_config_hash(cfg, trip)
        # Changer les dates, les aéroports ou le nombre de voyageurs
        # change le voyage surveillé : l'ancien « plus bas » devient une
        # référence inatteignable qui bloquerait toute alerte. On remet
        # la référence à zéro, mais la table checks garde tout
        # l'historique des relevés.
        if state and state.get("config_hash") not in (None, cfg_hash):
            print(f"  ♻ {trip.name}: paramètres modifiés, "
                  f"référence d'alerte réinitialisée "
                  f"(historique des prix conservé)")
            state = {}
        prev_low = state.get("lowest_price_eur")
        rolling = state.get("rolling") or []
        # Plusieurs runs par jour : on garde le minimum de la journée.
        # Sinon un creux vu le matin disparaissait dès le run suivant et
        # la hausse +10 % ne se déclenchait jamais.
        same_day = [x[1] for x in rolling
                    if x[0] == today and x[1] is not None]
        rolling = [x for x in rolling if x[0] != today]
        rolling.append([today, min([best_price_eur] + same_day)])
        cutoff = (date.today()
                  - timedelta(days=cfg.rolling_window_days)).isoformat()
        rolling = [x for x in rolling if x[0] >= cutoff]

        new_low = prev_low is None or best_price_eur < prev_low - 0.5
        hit_threshold = (trip.price_threshold is not None
                         and best_price_eur <= trip.price_threshold)

        # Detect rise
        rise = None
        last7 = [p for d, p in rolling
                 if d >= (date.today() - timedelta(days=7)).isoformat()
                 and d != today]
        if last7:
            recent_low = min(last7)
            if best_price_eur >= recent_low * (1 + cfg.rise_threshold_pct):
                rise = {"recent_low": recent_low,
                        "rise_pct": (best_price_eur / recent_low - 1) * 100,
                        "delta_eur": best_price_eur - recent_low}

        # Persist state
        update: dict[str, Any] = {"rolling": rolling, "last_check_at": now,
                                  "config_hash": cfg_hash}
        if new_low or prev_low is None:
            update.update({
                "lowest_price_eur": best_price_eur,
                "lowest_seen_date": today,
                "lowest_origin": best.origin,
                "lowest_destination": best.destination,
                "lowest_booking_url": best.booking_url,
            })
        # Flash mode : si seuil atteint, activer flash pendant 48h
        if hit_threshold:
            flash_until = (datetime.now() + timedelta(hours=48)).isoformat()
            update["flash_until"] = flash_until
            print(f"  ⚡ Flash mode activé jusqu'à {flash_until}")
        db.upsert_state(c, trip.name, **update)

    alert_count = 0

    # 6) Percentile rank, tendance et anti-doublon « hausse »
    with db.conn() as c:
        pct = db.percentile_rank(c, trip.name, best_price_eur)
        # 6b) Même source que le dashboard, sinon les deux affichaient des
        # recommandations opposées pour le même prix.
        trend = _calc_trend(db.price_trend(c, trip.name, days=TREND_DAYS))
        rise_already_sent = _rise_alert_recent(c, trip.name)
    if pct is not None:
        print(f"  Percentile: {pct:.0f}e (0=cheapest)")
    print(f"  Tendance {TREND_DAYS}j: {trend['direction']} "
          f"({trend['change_pct']:+.1f}%)")

    # 6c) Buy score
    buy_score = _calc_buy_score(best_price_eur, trip, pct, trend,
                                lowest_eur=prev_low or best_price_eur)
    print(f"  Score achat: {buy_score}/100")

    # Alerte percentile : prix dans le 10e percentile historique
    in_low_percentile = pct is not None and pct <= 10.0

    # 7) Alertes. Les comparaisons RT vs 2 OW et open-jaw coûtent ~10
    # requêtes et 15 s de pauses : elles ne servent qu'au message « bas »,
    # le payload « hausse » ne les contient pas.
    if new_low or hit_threshold or in_low_percentile:
        ow_comparison = _compare_oneway(cfg, best, rates)
        oj_comparison = _compare_openjaw(cfg, best, rates)

        kind = "new_low"
        payload = {
            "kind": kind, "trip": trip.name,
            "price": best_price_eur,
            "previous_low": prev_low,
            "hit_threshold": hit_threshold,
            "percentile": pct,
            "trend": trend,
            "buy_score": buy_score,
            "origin": best.origin, "destination": best.destination,
            "outbound_date": best.outbound_date,
            "return_date": best.return_date,
            "out_h": best.out_h, "ret_h": best.ret_h,
            "out_stops": best.out_stops, "ret_stops": best.ret_stops,
            "airlines": best.airlines,
            "booking_url": best.booking_url,
            "oneway_comparison": ow_comparison,
            "openjaw_comparison": oj_comparison,
        }
        notify.send_ntfy(cfg, payload)
        with db.conn() as c:
            db.log_alert(c, trip.name, kind, best_price_eur, payload)
        alert_count += 1
        print(f"  ⚠️  {kind} alert sent ({best_price_eur:.0f}€)")
    elif rise and rise_already_sent:
        print("  📈 hausse déjà notifiée il y a moins de 24 h, on se tait")
    elif rise:
        payload = {
            "kind": "rise", "trip": trip.name,
            "price": best_price_eur, **rise,
            "percentile": pct,
            "trend": trend,
            "buy_score": buy_score,
            "origin": best.origin, "destination": best.destination,
            "outbound_date": best.outbound_date,
            "return_date": best.return_date,
            "out_h": best.out_h, "ret_h": best.ret_h,
            "out_stops": best.out_stops, "ret_stops": best.ret_stops,
            "airlines": best.airlines,
            "booking_url": best.booking_url,
        }
        notify.send_ntfy(cfg, payload)
        with db.conn() as c:
            db.log_alert(c, trip.name, "rise", best_price_eur, payload)
        alert_count += 1
        print(f"  📈 rise alert sent ({best_price_eur:.0f}€)")

    return len(by_pair), alert_count


def _rise_alert_recent(c: sqlite3.Connection, trip_name: str,
                       hours: int = 24) -> bool:
    """Vrai si une alerte « hausse » a déjà été envoyée récemment.

    Rien ne mémorisait le creux déjà signalé : tant qu'il restait dans la
    fenêtre 7 jours, la même notification repartait à chaque run.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    row = c.execute(
        "SELECT 1 FROM alerts WHERE trip_name=? AND kind='rise' "
        "AND sent_at > ? LIMIT 1",
        (trip_name, cutoff),
    ).fetchone()
    return row is not None


def _to_eur(r: sources.FlightResult, rates: dict[str, float]) -> float | None:
    """Convert a FlightResult price to EUR."""
    if r.price is None:
        return None
    if r.currency == "EUR":
        return r.price
    return fx.to_eur(r.price, r.currency, rates)


def _compare_oneway(cfg: Config, best: sources.FlightResult,
                    rates: dict[str, float]) -> dict[str, Any] | None:
    """Compare round-trip price vs 2 separate one-ways.
    Returns comparison dict or None if one-ways aren't available."""
    rt_eur = _to_eur(best, rates)
    if rt_eur is None:
        return None

    print(f"  Comparaison RT vs 2 OW pour {best.origin}→{best.destination}...")

    # One-way outbound (best origin → best destination)
    ow_out = None
    # Try Google Flights first
    ff_out = sources.search_google_flights_oneway(
        origin=best.origin, destination=best.destination,
        dep_date=best.outbound_date, adults=cfg.adults,
        label=f"OW {best.origin}→{best.destination}",
    )
    if ff_out.price is not None:
        ow_out = ff_out

    time.sleep(2.0)

    # Try Duffel
    duf_out = sources.search_duffel_oneway(
        origin=best.origin, destination=best.destination,
        dep_date=best.outbound_date, adults=cfg.adults,
        currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
    )
    if duf_out and (ow_out is None or
                    (_to_eur(duf_out, rates) or 1e9) < (_to_eur(ow_out, rates) or 1e9)):
        ow_out = duf_out

    time.sleep(2.0)

    # One-way return (best destination → best origin)
    ow_ret = None
    ff_ret = sources.search_google_flights_oneway(
        origin=best.destination, destination=best.origin,
        dep_date=best.return_date, adults=cfg.adults,
        label=f"OW {best.destination}→{best.origin}",
    )
    if ff_ret.price is not None:
        ow_ret = ff_ret

    time.sleep(2.0)

    duf_ret = sources.search_duffel_oneway(
        origin=best.destination, destination=best.origin,
        dep_date=best.return_date, adults=cfg.adults,
        currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
    )
    if duf_ret and (ow_ret is None or
                    (_to_eur(duf_ret, rates) or 1e9) < (_to_eur(ow_ret, rates) or 1e9)):
        ow_ret = duf_ret

    if ow_out is None or ow_ret is None:
        print("  OW comparison: pas assez de données")
        return None

    out_eur = _to_eur(ow_out, rates)
    ret_eur = _to_eur(ow_ret, rates)
    if out_eur is None or ret_eur is None:
        return None

    total_ow = out_eur + ret_eur
    saving = rt_eur - total_ow

    result = {
        "rt_price": rt_eur,
        "ow_out_price": out_eur,
        "ow_out_airlines": ow_out.airlines,
        "ow_out_source": ow_out.source,
        "ow_ret_price": ret_eur,
        "ow_ret_airlines": ow_ret.airlines,
        "ow_ret_source": ow_ret.source,
        "ow_total": total_ow,
        "saving": saving,
    }
    if saving > 0:
        print(f"  2 OW = {total_ow:.0f}€ vs RT {rt_eur:.0f}€ → économie {saving:.0f}€")
    else:
        print(f"  2 OW = {total_ow:.0f}€ vs RT {rt_eur:.0f}€ → RT moins cher")
    return result


def _compare_openjaw(cfg: Config, best: sources.FlightResult,
                     rates: dict[str, float]) -> dict[str, Any] | None:
    """Recherche open-jaw : aller vers best.destination, retour depuis
    une AUTRE destination thaï, ou retour vers une AUTRE origine française.

    Ne teste que 2-3 combinaisons prometteuses pour limiter les appels API.
    """
    rt_eur = _to_eur(best, rates)
    if rt_eur is None:
        return None

    # Destinations TH alternatives (exclure celle du best)
    thai_dests = [d for d in cfg.destinations if d != best.destination]
    # Origines FR alternatives (exclure celle du best)
    fr_origins = [o for o in cfg.origins if o != best.origin]

    if not thai_dests and not fr_origins:
        return None

    print(f"  Open-jaw: recherche alternatives pour {best.origin}→{best.destination}...")

    # L'aller est le même dans les deux stratégies : une seule requête au
    # lieu de trois, Duffel ne mettant rien en cache.
    ow_out = sources.search_duffel_oneway(
        origin=best.origin, destination=best.destination,
        dep_date=best.outbound_date, adults=cfg.adults,
        currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
    )
    out_eur = _to_eur(ow_out, rates) if ow_out else None
    if ow_out is None or not out_eur:
        print("  Open-jaw: aller introuvable")
        return None
    time.sleep(1.0)

    best_oj: dict[str, Any] | None = None
    best_oj_total = 1e9

    # Stratégie 1 : même aller, retour depuis autre destination TH → best.origin
    for alt_dest in thai_dests[:2]:
        try:
            # Retour : alt_dest → best.origin (one-way)
            ow_ret = sources.search_duffel_oneway(
                origin=alt_dest, destination=best.origin,
                dep_date=best.return_date, adults=cfg.adults,
                currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
            )
            time.sleep(1.0)

            if ow_ret:
                ret_eur = _to_eur(ow_ret, rates)
                if ret_eur:
                    total = out_eur + ret_eur
                    if total < best_oj_total:
                        best_oj_total = total
                        best_oj = {
                            "type": "open_jaw",
                            "rt_price": rt_eur,
                            "oj_out_origin": best.origin,
                            "oj_out_dest": best.destination,
                            "oj_out_price": out_eur,
                            "oj_out_airlines": ow_out.airlines,
                            "oj_ret_origin": alt_dest,
                            "oj_ret_dest": best.origin,
                            "oj_ret_price": ret_eur,
                            "oj_ret_airlines": ow_ret.airlines,
                            "oj_total": total,
                            "saving": rt_eur - total,
                        }
        except Exception as e:
            print(f"  Open-jaw retour {alt_dest}→{best.origin} error: {e}")

    # Stratégie 2 : même aller, retour vers autre origine FR
    for alt_orig in fr_origins[:1]:
        try:
            ow_ret = sources.search_duffel_oneway(
                origin=best.destination, destination=alt_orig,
                dep_date=best.return_date, adults=cfg.adults,
                currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
            )
            time.sleep(1.0)

            if ow_ret:
                ret_eur = _to_eur(ow_ret, rates)
                if ret_eur:
                    total = out_eur + ret_eur
                    if total < best_oj_total:
                        best_oj_total = total
                        best_oj = {
                            "type": "open_jaw",
                            "rt_price": rt_eur,
                            "oj_out_origin": best.origin,
                            "oj_out_dest": best.destination,
                            "oj_out_price": out_eur,
                            "oj_out_airlines": ow_out.airlines,
                            "oj_ret_origin": best.destination,
                            "oj_ret_dest": alt_orig,
                            "oj_ret_price": ret_eur,
                            "oj_ret_airlines": ow_ret.airlines,
                            "oj_total": total,
                            "saving": rt_eur - total,
                        }
        except Exception as e:
            print(f"  Open-jaw retour {best.destination}→{alt_orig} error: {e}")

    if best_oj:
        saving = best_oj["saving"]
        if saving > 0:
            print(f"  Open-jaw = {best_oj_total:.0f}€ vs RT {rt_eur:.0f}€ "
                  f"→ économie {saving:.0f}€")
        else:
            print(f"  Open-jaw = {best_oj_total:.0f}€ vs RT {rt_eur:.0f}€ "
                  f"→ RT moins cher")
    else:
        print("  Open-jaw: aucune combinaison trouvée")

    return best_oj


def _check_hotel(cfg: Config, hotel: HotelWatch,
                 rates: dict[str, float]) -> None:
    """Relève les prix d'un hôtel et déclenche les alertes.

    Un échec identifiable (blocage Google, hôtel introuvable, timeout)
    est tracé dans `hotel_state` au lieu d'être confondu avec « aucun
    prix trouvé » : sans cela la surveillance mourait en silence.
    """
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    checkin = hotel.checkin
    checkout = hotel.checkout
    try:
        checkin_dt = datetime.strptime(checkin, "%Y-%m-%d")
        checkout_dt = datetime.strptime(checkout, "%Y-%m-%d")
    except (ValueError, TypeError):
        _record_hotel_failure(cfg, hotel, now,
                              f"dates invalides ({checkin} → {checkout})")
        return
    nights = (checkout_dt - checkin_dt).days

    print(f"\n→ Hotel {hotel.name}: {checkin} → {checkout} ({nights} nuits)")

    # Conversion injectée : hotels.py reste indépendant de fx.
    def _to_eur_local(amount: float, currency: str) -> float | None:
        return fx.to_eur(amount, currency, rates)

    try:
        result = hotels.search_hotel(
            hotel_name=hotel.name,
            checkin=checkin,
            checkout=checkout,
            adults=cfg.adults,
            children=cfg.children if cfg.children else None,
            currency=cfg.currency,
            to_eur=_to_eur_local,
        )
    except hotels.HotelScrapeError as e:
        print(f"  ❌ Hotel {hotel.name}: {e}")
        _record_hotel_failure(cfg, hotel, now, str(e))
        return

    if not result or not result.prices:
        print(f"  ⚠ Aucun prix exploitable pour {hotel.name}")
        _record_hotel_failure(cfg, hotel, now, "aucun prix exploitable")
        return

    best_eur = result.best_price_eur
    if best_eur is None:
        print(f"  ⚠ {hotel.name}: prix non convertibles en EUR")
        _record_hotel_failure(cfg, hotel, now, "prix non convertibles en EUR")
        return

    # Persistance de tous les prix providers
    with db.conn() as c:
        for hp in result.prices:
            db.insert_hotel_check(c, {
                "check_date": today,
                "trip_name": hotel.name,
                "hotel_name": hotel.name,
                "source": hp.source,
                "price_local": hp.price,
                "currency": hp.currency,
                "price_eur": hp.price_eur,
                "checkin_date": checkin,
                "checkout_date": checkout,
                "nights": nights,
                "booking_url": hp.url,
                "captured_at": now,
            })

        state = db.get_hotel_state(c, hotel.name, hotel.name) or {}
        h_hash = hotel_config_hash(cfg, hotel)
        # Changer les dates du séjour change le produit suivi : même
        # raisonnement que pour les vols, on repart d'une référence
        # neuve sans toucher à l'historique des relevés.
        if state and state.get("config_hash") not in (None, h_hash):
            print(f"  ♻ Hotel {hotel.name}: dates modifiées, "
                  f"référence d'alerte réinitialisée")
            state = {}
        prev_low = state.get("lowest_price_eur")
        last_alert_at = state.get("last_alert_at")

        rolling = state.get("rolling") or []
        rolling = [x for x in rolling if x[0] != today]
        rolling.append([today, best_eur])
        cutoff = (date.today()
                  - timedelta(days=cfg.rolling_window_days)).isoformat()
        rolling = [x for x in rolling if x[0] >= cutoff]

        new_low = prev_low is None or best_eur < prev_low - 0.5
        hit_threshold = (hotel.price_threshold is not None
                         and best_eur <= hotel.price_threshold)

        # Un seuil atteint reste atteint : sans ce délai de garde, une
        # notification partait à chaque run tant que le prix restait bas.
        should_alert = new_low or (
            hit_threshold and _alert_cooldown_passed(last_alert_at, now))

        update: dict[str, Any] = {
            "rolling": rolling,
            "last_check_at": now,
            "last_error": None,
            "consecutive_failures": 0,
            "config_hash": h_hash,
        }
        if new_low or prev_low is None:
            update.update({
                "lowest_price_eur": best_eur,
                "lowest_seen_date": today,
                "lowest_source": result.best_source,
            })
        if should_alert:
            update["last_alert_at"] = now
        db.upsert_hotel_state(c, hotel.name, hotel.name, **update)

    if not should_alert:
        print(f"  🏨 {best_eur:.0f}€ ({result.best_source}) — pas d'alerte")
        return

    payload = {
        "kind": "hotel_low",
        "hotel": hotel.name,
        "price": best_eur,
        "previous_low": prev_low,
        "hit_threshold": hit_threshold,
        "source": result.best_source,
        "checkin": checkin,
        "checkout": checkout,
        "nights": nights,
        # Noms seulement : les montants affichés à côté des liens
        # providers n'ont pas de sémantique fiable (voir hotels.py).
        "providers_seen": result.providers_seen,
    }
    delivered = notify.send_hotel_ntfy(cfg, payload)
    with db.conn() as c:
        db.log_alert(c, hotel.name, "hotel_low", best_eur, payload)
    tag = "🎯 SEUIL" if hit_threshold else "📉 BAS"
    status = "" if delivered else " (ntfy KO)"
    print(f"  🏨 {tag} Hotel alert: {best_eur:.0f}€ "
          f"({result.best_source}){status}")


def _alert_cooldown_passed(last_alert_at: str | None, now: str,
                           hours: int = 24) -> bool:
    """Vrai si la dernière alerte hôtel date de plus de *hours*."""
    if not last_alert_at:
        return True
    try:
        previous = datetime.fromisoformat(last_alert_at)
        current = datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return True
    return (current - previous) >= timedelta(hours=hours)


def _record_hotel_failure(cfg: Config, hotel: HotelWatch, now: str,
                          reason: str) -> None:
    """Trace l'échec dans l'état et prévient après 3 échecs d'affilée."""
    with db.conn() as c:
        state = db.get_hotel_state(c, hotel.name, hotel.name) or {}
        failures = (state.get("consecutive_failures") or 0) + 1
        db.upsert_hotel_state(
            c, hotel.name, hotel.name,
            last_check_at=now,
            last_error=reason[:500],
            consecutive_failures=failures,
        )
    if failures == 3:
        notify.send_ops_ntfy(
            cfg,
            f"🏨 Suivi hôtel en panne — {hotel.name}",
            f"3 échecs consécutifs.\nDernière raison : {reason}",
        )
