"""Main check loop: queries sources, detects alerts, persists, notifies."""
from __future__ import annotations

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
        # FX rates needed
        needed = set(cfg.currencies) | {"THB"}
        rates = fx.fetch_rates(cfg.currency, list(needed))

        for trip in cfg.trips:
            try:
                trip_alerts = _check_trip(cfg, trip, rates)
                summary["alerts_generated"] += trip_alerts
                summary["trips_checked"] += 1
            except Exception as e:
                summary["errors"].append(f"{trip.name}: {e}")

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
    """Check one trip across all currencies/markets. Returns alert count."""
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    captured_at = now

    # 1) Tequila in primary currency (EUR)
    primary_flights = sources.search_tequila(
        origins=cfg.origins, destinations=cfg.destinations,
        outbound_window=trip.outbound_window,
        return_window=trip.return_window,
        currency=cfg.currency, adults=cfg.adults,
        max_fly_duration_h=cfg.max_fly_duration_hours,
        min_nights=trip.min_nights, max_nights=trip.max_nights,
    )
    valid = _filter_duration(primary_flights, cfg.max_fly_duration_hours)
    print(f"\n→ {trip.name}: {len(valid)} valid Tequila results")
    if not valid:
        return 0

    best = min(valid, key=lambda f: f["price"])
    parsed_best = sources.tequila_parse(best)
    best_price_eur = parsed_best["price"]

    # 2) Persist per-(origin, dest) results for the breakdown view
    with db.conn() as c:
        # Best per (origin, destination) for this run
        by_pair: dict[tuple[str, str], dict] = {}
        for f in valid:
            p = sources.tequila_parse(f)
            key = (p["origin"], p["destination"])
            if key not in by_pair or p["price"] < by_pair[key]["price"]:
                by_pair[key] = p
        for p in by_pair.values():
            db.insert_check(c, {
                "check_date": today, "trip_name": trip.name,
                "source": "tequila_eur",
                "origin": p["origin"], "destination": p["destination"],
                "price_local": p["price"], "currency": cfg.currency,
                "price_eur": p["price"],
                "outbound_date": p["outbound_date"],
                "return_date": p["return_date"],
                "out_h": p["out_h"], "ret_h": p["ret_h"],
                "out_stops": p["out_stops"], "ret_stops": p["ret_stops"],
                "airlines": p["airlines"], "booking_url": p["booking_url"],
                "captured_at": captured_at,
            })

        # 3) State update / alert detection
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
        update = {"rolling": rolling, "last_check_at": now}
        if new_low or prev_low is None:
            update.update({
                "lowest_price_eur": best_price_eur,
                "lowest_seen_date": today,
                "lowest_origin": parsed_best["origin"],
                "lowest_destination": parsed_best["destination"],
                "lowest_booking_url": parsed_best["booking_url"],
            })
        db.upsert_state(c, trip.name, **update)

    alert_count = 0

    # 4) On alerts only: run secondary Tequila currency + fast-flights cross-checks
    if new_low or hit_threshold or rise:
        cross_checks = _build_cross_checks(cfg, trip, parsed_best, rates)
        # Persist cross-check rows too
        with db.conn() as c:
            for cc in cross_checks:
                db.insert_check(c, {
                    "check_date": today, "trip_name": trip.name,
                    "source": cc["source"],
                    "origin": parsed_best["origin"],
                    "destination": parsed_best["destination"],
                    "price_local": cc["price"],
                    "currency": cc["currency"],
                    "price_eur": cc.get("eur_equiv"),
                    "outbound_date": parsed_best["outbound_date"],
                    "return_date": parsed_best["return_date"],
                    "out_h": parsed_best["out_h"],
                    "ret_h": parsed_best["ret_h"],
                    "out_stops": parsed_best["out_stops"],
                    "ret_stops": parsed_best["ret_stops"],
                    "airlines": cc.get("airlines", ""),
                    "booking_url": "",
                    "captured_at": captured_at,
                })

        if new_low or hit_threshold:
            payload = {
                "kind": "new_low", "trip": trip.name,
                "price": best_price_eur,
                "previous_low": prev_low,
                "hit_threshold": hit_threshold,
                **parsed_best,
                "cross_checks": cross_checks,
            }
            notify.send_ntfy(cfg, payload)
            with db.conn() as c:
                db.log_alert(c, trip.name, "new_low",
                             best_price_eur, payload)
            alert_count += 1
            print(f"  ⚠️  new_low alert sent")
        elif rise:
            payload = {"kind": "rise", "trip": trip.name,
                       "price": best_price_eur, **rise, **parsed_best}
            notify.send_ntfy(cfg, payload)
            with db.conn() as c:
                db.log_alert(c, trip.name, "rise", best_price_eur, payload)
            alert_count += 1
            print(f"  📈 rise alert sent")

    return alert_count


def _filter_duration(flights: list[dict], max_h: int) -> list[dict]:
    out = []
    for f in flights:
        d = f.get("duration", {})
        oh = d.get("departure", 0) / 3600
        rh = d.get("return", 0) / 3600
        if oh <= max_h and rh <= max_h:
            out.append(f)
    return out


def _build_cross_checks(cfg: Config, trip, parsed_best: dict,
                        rates: dict[str, float]) -> list[dict]:
    """Tequila THB + fast-flights FR + fast-flights TH (via VPN)."""
    checks = []

    # Tequila in other currencies
    for cur in cfg.currencies:
        if cur == cfg.currency:
            continue
        try:
            alt = sources.search_tequila(
                origins=cfg.origins, destinations=cfg.destinations,
                outbound_window=trip.outbound_window,
                return_window=trip.return_window,
                currency=cur, adults=cfg.adults,
                max_fly_duration_h=cfg.max_fly_duration_hours,
                min_nights=trip.min_nights, max_nights=trip.max_nights,
            )
            valid_alt = _filter_duration(alt, cfg.max_fly_duration_hours)
            if valid_alt:
                alt_best = min(valid_alt, key=lambda f: f["price"])
                eur_eq = fx.to_eur(alt_best["price"], cur, rates)
                checks.append({
                    "label": f"Kiwi {cur}",
                    "source": f"tequila_{cur.lower()}",
                    "price": alt_best["price"], "currency": cur,
                    "eur_equiv": eur_eq,
                    "airlines": "+".join(
                        {seg.get("airline", "")
                         for seg in alt_best.get("route", [])
                         if seg.get("airline")}
                    ),
                })
                print(f"  Kiwi {cur}: {alt_best['price']:.0f} ≈ "
                      f"{eur_eq:.0f}€" if eur_eq else
                      f"  Kiwi {cur}: {alt_best['price']:.0f}")
        except Exception as e:
            print(f"  Kiwi {cur} fail: {e}")

    # fast-flights from France
    try:
        ff_fr = sources.search_fast_flights(
            origin=parsed_best["origin"],
            destination=parsed_best["destination"],
            outbound_date=parsed_best["outbound_date"],
            return_date=parsed_best["return_date"],
            adults=cfg.adults, via_vpn=False, market_label="Google FR",
        )
        if ff_fr.price:
            eur_eq = fx.to_eur(ff_fr.price, ff_fr.currency, rates)
            checks.append({
                "label": ff_fr.market_label,
                "source": "fast_flights_fr",
                "price": ff_fr.price, "currency": ff_fr.currency,
                "eur_equiv": eur_eq, "airlines": ff_fr.airlines,
            })
            print(f"  Google FR: {ff_fr.price:.0f} {ff_fr.currency}")
    except Exception as e:
        print(f"  Google FR fail: {e}")

    # fast-flights from Thailand (via VPN)
    try:
        ff_th = sources.search_fast_flights(
            origin=parsed_best["origin"],
            destination=parsed_best["destination"],
            outbound_date=parsed_best["outbound_date"],
            return_date=parsed_best["return_date"],
            adults=cfg.adults, via_vpn=True, market_label="Google TH (VPN)",
        )
        if ff_th.price:
            eur_eq = fx.to_eur(ff_th.price, ff_th.currency, rates)
            checks.append({
                "label": ff_th.market_label,
                "source": "fast_flights_th",
                "price": ff_th.price, "currency": ff_th.currency,
                "eur_equiv": eur_eq, "airlines": ff_th.airlines,
            })
            print(f"  Google TH: {ff_th.price:.0f} {ff_th.currency}")
    except Exception as e:
        print(f"  Google TH fail: {e}")

    return checks
