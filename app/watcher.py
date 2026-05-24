"""Main check loop: queries sources, detects alerts, persists, notifies."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

from app import db, fx, hotels, notify, sources
from app.config import Config


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


def _calc_buy_score(price_eur: float, trip, cfg: Config,
                    rates: dict[str, float],
                    pct: float | None,
                    trend: dict[str, Any]) -> int:
    """Retourne un score d'achat 0-100.

    Facteurs:
    - Percentile (40%) : pct bas = score haut
    - Tendance (25%)   : rising after low → achète, falling → attends
    - Jour semaine (10%): mar/mer mieux
    - Délai départ (25%): sweet spot 45-90 jours
    """
    score = 0

    # 1) Percentile factor (40 pts max)
    if pct is not None:
        score += int(40 * (1 - pct / 100))
    else:
        score += 20  # pas assez de données → neutre

    # 2) Trend factor (25 pts max)
    direction = trend.get("direction", "stable")
    if direction == "falling":
        score += 5       # tendance baisse → attendre
    elif direction == "rising":
        score += 25      # rebond → acheter maintenant
    else:
        score += 15      # stable → correct

    # 3) Day of week factor (10 pts max)
    dow = date.today().weekday()  # 0=lun, 1=mar, 2=mer, ...
    if dow in (1, 2):  # mardi, mercredi
        score += 10
    else:
        score += 5

    # 4) Time to departure factor (25 pts max)
    try:
        out_date = datetime.strptime(trip.outbound_window[0], "%Y-%m-%d").date()
        days_to_dep = (out_date - date.today()).days
        if 45 <= days_to_dep <= 90:
            score += 25
        elif 30 <= days_to_dep < 45 or 90 < days_to_dep <= 120:
            score += 20
        elif days_to_dep < 30:
            score += 15
        else:
            score += 10
    except Exception:
        score += 12

    return min(100, max(0, score))


def run_once(cfg: Config) -> dict[str, Any]:
    """Execute one full check across all trips. Returns summary."""
    db.init()
    with db.conn() as c:
        run_id = db.start_run(c)

    summary = {"trips_checked": 0, "alerts_generated": 0, "errors": []}
    try:
        rates = fx.fetch_rates(cfg.currency, ["THB"])

        for trip in cfg.trips:
            if not trip.enabled:
                print(f"  ⏸ {trip.name}: désactivé, skip")
                continue
            try:
                trip_alerts = _check_trip(cfg, trip, rates)
                summary["alerts_generated"] += trip_alerts
                summary["trips_checked"] += 1
            except Exception as e:
                summary["errors"].append(f"{trip.name}: {e}")
                print(f"  ❌ {trip.name}: {e}")

        # Hotel checks
        for hotel in cfg.hotels:
            if not hotel.enabled:
                print(f"  ⏸ Hotel {hotel.name}: désactivé, skip")
                continue
            for trip in cfg.trips:
                if not trip.enabled:
                    continue
                try:
                    _check_hotel(cfg, hotel, trip, rates)
                except Exception as e:
                    summary["errors"].append(f"Hotel {hotel.name}/{trip.name}: {e}")
                    print(f"  ❌ Hotel {hotel.name}/{trip.name}: {e}")

        with db.conn() as c:
            db.finish_run(c, run_id, "ok", summary["trips_checked"],
                          summary["alerts_generated"], None)
    except Exception as e:
        with db.conn() as c:
            db.finish_run(c, run_id, "error", summary["trips_checked"],
                          summary["alerts_generated"], str(e))
        raise

    return summary


def _check_trip(cfg: Config, trip, rates: dict[str, float]) -> int:
    """Check one trip. Returns alert count."""
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    # Toutes les dates des fenêtres
    out_dates = sources.date_range(trip.outbound_window)
    ret_dates = sources.date_range(trip.return_window)
    out_mid = sources.mid_date(trip.outbound_window)
    ret_mid = sources.mid_date(trip.return_window)
    nb_combos = len(out_dates) * len(ret_dates)

    # 1) Google Flights via fli (date médiane uniquement, scraping)
    print(f"\n→ {trip.name}: fli {out_mid}/{ret_mid} + Duffel {nb_combos} combos dates")
    ff_results = sources.search_google_flights_multi(
        origins=cfg.origins, destinations=cfg.destinations,
        outbound_date=out_mid, return_date=ret_mid,
        adults=cfg.adults, max_fly_h=cfg.max_fly_duration_hours,
    )
    print(f"  Google Flights: {len(ff_results)} résultats (date médiane)")

    # 2) Duffel : toutes les combos dates × toutes les paires
    duffel_results = sources.search_duffel(
        origins=cfg.origins, destinations=cfg.destinations,
        outbound_dates=out_dates, return_dates=ret_dates,
        adults=cfg.adults, currency=cfg.currency,
        max_fly_h=cfg.max_fly_duration_hours,
    )
    print(f"  Duffel: {len(duffel_results)} résultats ({nb_combos} combos dates)")

    # 3) Fusionner et garder le best par paire (origin, dest)
    all_results = ff_results + duffel_results
    if not all_results:
        print(f"  ⚠ Aucun résultat pour {trip.name}")
        return 0

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
        return 0

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
        prev_low = state.get("lowest_price_eur")
        rolling = state.get("rolling") or []
        rolling = [x for x in rolling if x[0] != today]
        rolling.append([today, best_price_eur])
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
        update: dict[str, Any] = {"rolling": rolling, "last_check_at": now}
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

    # 6) Percentile rank
    pct = None
    with db.conn() as c:
        pct = db.percentile_rank(c, trip.name, best_price_eur)
    if pct is not None:
        print(f"  Percentile: {pct:.0f}e (0=cheapest)")

    # 6b) Trend calculation from rolling data
    trend = _calc_trend(rolling)
    print(f"  Tendance 7j: {trend['direction']} ({trend['change_pct']:+.1f}%)")

    # 6c) Buy score
    buy_score = _calc_buy_score(best_price_eur, trip, cfg, rates, pct, trend)
    print(f"  Score achat: {buy_score}/100")

    # Alerte percentile : prix dans le 10e percentile historique
    in_low_percentile = pct is not None and pct <= 10.0

    # 7) On alerts: cross-check VPN + comparaison RT vs 2 one-ways + open-jaw
    should_alert = new_low or hit_threshold or rise or in_low_percentile
    if should_alert:
        cross_checks = _build_cross_checks(cfg, best, rates)
        ow_comparison = _compare_oneway(cfg, best, rates)
        oj_comparison = _compare_openjaw(cfg, best, rates)

        with db.conn() as c:
            for cc in cross_checks:
                db.insert_check(c, {
                    "check_date": today, "trip_name": trip.name,
                    "source": cc["source"],
                    "origin": best.origin, "destination": best.destination,
                    "price_local": cc["price"], "currency": cc["currency"],
                    "price_eur": cc.get("eur_equiv"),
                    "outbound_date": best.outbound_date,
                    "return_date": best.return_date,
                    "out_h": best.out_h, "ret_h": best.ret_h,
                    "out_stops": best.out_stops, "ret_stops": best.ret_stops,
                    "airlines": cc.get("airlines", ""),
                    "booking_url": "", "captured_at": now,
                })

        # Determine alert kind
        if new_low or hit_threshold or in_low_percentile:
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
                "cross_checks": cross_checks,
                "oneway_comparison": ow_comparison,
                "openjaw_comparison": oj_comparison,
            }
            notify.send_ntfy(cfg, payload)
            with db.conn() as c:
                db.log_alert(c, trip.name, kind, best_price_eur, payload)
            alert_count += 1
            print(f"  ⚠️  {kind} alert sent ({best_price_eur:.0f}€)")
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

    return alert_count


def _to_eur(r: sources.FlightResult, rates: dict[str, float]) -> float | None:
    """Convert a FlightResult price to EUR."""
    if r.price is None:
        return None
    if r.currency == "EUR":
        return r.price
    return fx.to_eur(r.price, r.currency, rates)


def _compare_oneway(cfg: Config, best: sources.FlightResult,
                    rates: dict[str, float]) -> dict | None:
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
                     rates: dict[str, float]) -> dict | None:
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

    best_oj: dict | None = None
    best_oj_total = 1e9

    # Stratégie 1 : même aller, retour depuis autre destination TH → best.origin
    for alt_dest in thai_dests[:2]:
        try:
            # Aller : best.origin → best.destination (one-way)
            ow_out = sources.search_duffel_oneway(
                origin=best.origin, destination=best.destination,
                dep_date=best.outbound_date, adults=cfg.adults,
                currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
            )
            time.sleep(1.0)

            # Retour : alt_dest → best.origin (one-way)
            ow_ret = sources.search_duffel_oneway(
                origin=alt_dest, destination=best.origin,
                dep_date=best.return_date, adults=cfg.adults,
                currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
            )
            time.sleep(1.0)

            if ow_out and ow_ret:
                out_eur = _to_eur(ow_out, rates)
                ret_eur = _to_eur(ow_ret, rates)
                if out_eur and ret_eur:
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
            print(f"  Open-jaw {best.origin}→{best.destination} / "
                  f"{alt_dest}→{best.origin} error: {e}")

    # Stratégie 2 : même aller, retour vers autre origine FR
    for alt_orig in fr_origins[:1]:
        try:
            ow_out = sources.search_duffel_oneway(
                origin=best.origin, destination=best.destination,
                dep_date=best.outbound_date, adults=cfg.adults,
                currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
            )
            time.sleep(1.0)

            ow_ret = sources.search_duffel_oneway(
                origin=best.destination, destination=alt_orig,
                dep_date=best.return_date, adults=cfg.adults,
                currency=cfg.currency, max_fly_h=cfg.max_fly_duration_hours,
            )
            time.sleep(1.0)

            if ow_out and ow_ret:
                out_eur = _to_eur(ow_out, rates)
                ret_eur = _to_eur(ow_ret, rates)
                if out_eur and ret_eur:
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


def _check_hotel(cfg: Config, hotel, trip, rates: dict[str, float]) -> None:
    """Check hotel prices for one trip period."""
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    # Checkin = début fenêtre aller, checkout = checkin + nights
    checkin = trip.outbound_window[0]
    checkin_dt = datetime.strptime(checkin, "%Y-%m-%d")
    checkout_dt = checkin_dt + timedelta(days=hotel.nights)
    checkout = checkout_dt.strftime("%Y-%m-%d")

    print(f"\n→ Hotel {hotel.name} pour {trip.name}: {checkin} → {checkout}")

    result = hotels.search_hotel(
        entity_id=hotel.entity_id,
        checkin=checkin,
        checkout=checkout,
        adults=cfg.adults,
        children=cfg.children if cfg.children else None,
        currency=cfg.currency,
    )

    if not result or not result.prices:
        print(f"  ⚠ Aucun prix trouvé pour {hotel.name}")
        return

    # Persist tous les prix par provider
    with db.conn() as c:
        for hp in result.prices:
            price_eur = hp.price if hp.currency == "EUR" else fx.to_eur(
                hp.price, hp.currency, rates)
            db.insert_hotel_check(c, {
                "check_date": today,
                "trip_name": trip.name,
                "hotel_name": hotel.name,
                "source": hp.source,
                "price_local": hp.price,
                "currency": hp.currency,
                "price_eur": price_eur,
                "checkin_date": checkin,
                "checkout_date": checkout,
                "nights": hotel.nights,
                "booking_url": hp.url,
                "captured_at": now,
            })

        # State update
        best_eur = None
        if result.best_price is not None:
            best_eur = (result.best_price if result.best_currency == "EUR"
                        else fx.to_eur(result.best_price, result.best_currency,
                                       rates))

        if best_eur is None:
            return

        state = db.get_hotel_state(c, hotel.name, trip.name) or {}
        prev_low = state.get("lowest_price_eur")

        rolling = state.get("rolling") or []
        rolling = [x for x in rolling if x[0] != today]
        rolling.append([today, best_eur])
        cutoff = (date.today()
                  - timedelta(days=cfg.rolling_window_days)).isoformat()
        rolling = [x for x in rolling if x[0] >= cutoff]

        new_low = prev_low is None or best_eur < prev_low - 0.5
        hit_threshold = (hotel.price_threshold is not None
                         and best_eur <= hotel.price_threshold)

        update = {"rolling": rolling, "last_check_at": now}
        if new_low or prev_low is None:
            update.update({
                "lowest_price_eur": best_eur,
                "lowest_seen_date": today,
                "lowest_source": result.best_source,
            })
        db.upsert_hotel_state(c, hotel.name, trip.name, **update)

    # Alertes
    if new_low or hit_threshold:
        payload = {
            "kind": "hotel_low",
            "hotel": hotel.name,
            "trip": trip.name,
            "price": best_eur,
            "previous_low": prev_low,
            "hit_threshold": hit_threshold,
            "source": result.best_source,
            "checkin": checkin,
            "checkout": checkout,
            "nights": hotel.nights,
            "providers": [
                {"source": p.source, "price": p.price, "currency": p.currency}
                for p in result.prices
            ],
        }
        notify.send_hotel_ntfy(cfg, payload)
        with db.conn() as c:
            db.log_alert(c, trip.name, "hotel_low", best_eur, payload)
        tag = "🎯 SEUIL" if hit_threshold else "📉 BAS"
        print(f"  🏨 {tag} Hotel alert: {best_eur:.0f}€ ({result.best_source})")


def _build_cross_checks(cfg: Config, best: sources.FlightResult,
                        rates: dict[str, float]) -> list[dict]:
    """Cross-checks additionnels (extensible)."""
    # VPN cross-check désactivé pour l'instant (fast-flights retiré).
    # Pourra être réimplémenté avec fli si besoin.
    return []
