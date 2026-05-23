"""Main check loop: queries sources, detects alerts, persists, notifies."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

from app import db, fx, notify, sources
from app.config import Config


def run_once(cfg: Config) -> dict[str, Any]:
    """Execute one full check across all trips. Returns summary."""
    db.init()
    with db.conn() as c:
        run_id = db.start_run(c)

    summary = {"trips_checked": 0, "alerts_generated": 0, "errors": []}
    try:
        rates = fx.fetch_rates(cfg.currency, ["THB"])

        for trip in cfg.trips:
            try:
                trip_alerts = _check_trip(cfg, trip, rates)
                summary["alerts_generated"] += trip_alerts
                summary["trips_checked"] += 1
            except Exception as e:
                summary["errors"].append(f"{trip.name}: {e}")
                print(f"  ❌ {trip.name}: {e}")

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
        db.upsert_state(c, trip.name, **update)

    alert_count = 0

    # 6) Percentile rank
    pct = None
    with db.conn() as c:
        pct = db.percentile_rank(c, trip.name, best_price_eur)
    if pct is not None:
        print(f"  Percentile: {pct:.0f}e (0=cheapest)")

    # Alerte percentile : prix dans le 10e percentile historique
    in_low_percentile = pct is not None and pct <= 10.0

    # 7) On alerts: cross-check VPN + comparaison RT vs 2 one-ways
    should_alert = new_low or hit_threshold or rise or in_low_percentile
    if should_alert:
        cross_checks = _build_cross_checks(cfg, best, rates)
        ow_comparison = _compare_oneway(cfg, best, rates)

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
                "origin": best.origin, "destination": best.destination,
                "outbound_date": best.outbound_date,
                "return_date": best.return_date,
                "out_h": best.out_h, "ret_h": best.ret_h,
                "out_stops": best.out_stops, "ret_stops": best.ret_stops,
                "airlines": best.airlines,
                "booking_url": best.booking_url,
                "cross_checks": cross_checks,
                "oneway_comparison": ow_comparison,
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


def _build_cross_checks(cfg: Config, best: sources.FlightResult,
                        rates: dict[str, float]) -> list[dict]:
    """Cross-checks additionnels (extensible)."""
    # VPN cross-check désactivé pour l'instant (fast-flights retiré).
    # Pourra être réimplémenté avec fli si besoin.
    return []
