"""Flight data sources: fli (Google Flights) + Duffel API.

fli is the primary source — reverse-engineered Google Flights API (no scraping).
Duffel provides direct airline pricing (AF, Emirates, QR, EY...).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests


# ─────────────────────────── Normalized result ────────────────────

@dataclass
class FlightResult:
    """Normalized result from any source."""
    price: float | None
    currency: str
    origin: str
    destination: str
    outbound_date: str
    return_date: str
    out_h: float
    ret_h: float
    out_stops: int
    ret_stops: int
    airlines: str
    booking_url: str
    source: str
    market_label: str = ""
    raw: Any = None


def _empty_result(origin: str, destination: str, out_date: str,
                  ret_date: str, source: str, label: str = "") -> FlightResult:
    return FlightResult(
        price=None, currency="EUR", origin=origin, destination=destination,
        outbound_date=out_date, return_date=ret_date, out_h=0, ret_h=0,
        out_stops=0, ret_stops=0, airlines="", booking_url="",
        source=source, market_label=label,
    )


# ─────────────────────────── fli (Google Flights) ─────────────────

def _get_airport_enum(iata: str):
    """Get Airport enum member from IATA code string."""
    from fli.models import Airport
    try:
        return Airport(iata)
    except ValueError:
        # Try by name
        return Airport[iata] if iata in Airport.__members__ else None


def _search_fli(*, origin: str, destination: str,
                outbound_date: str, return_date: str | None = None,
                adults: int = 1, max_fly_h: int = 18,
                label: str = "") -> FlightResult:
    """Single Google Flights search via fli."""
    try:
        from fli.models import (
            Airport, FlightSearchFilters, FlightSegment,
            MaxStops, PassengerInfo, SeatType, SortBy,
        )
        from fli.search import SearchFlights
    except ImportError:
        print("  fli not installed")
        return _empty_result(origin, destination, outbound_date,
                             return_date or "", "google_flights", label)

    orig_enum = _get_airport_enum(origin)
    dest_enum = _get_airport_enum(destination)
    if not orig_enum or not dest_enum:
        print(f"  fli: unknown airport {origin} or {destination}")
        return _empty_result(origin, destination, outbound_date,
                             return_date or "", "google_flights", label)

    try:
        segments = [
            FlightSegment(
                departure_airport=[[orig_enum, 0]],
                arrival_airport=[[dest_enum, 0]],
                travel_date=outbound_date,
            )
        ]
        if return_date:
            segments.append(FlightSegment(
                departure_airport=[[dest_enum, 0]],
                arrival_airport=[[orig_enum, 0]],
                travel_date=return_date,
            ))

        filters = FlightSearchFilters(
            passenger_info=PassengerInfo(adults=adults),
            flight_segments=segments,
            seat_type=SeatType.ECONOMY,
            stops=MaxStops.ANY,
            sort_by=SortBy.CHEAPEST,
            max_duration=max_fly_h * 60,  # minutes
        )

        search = SearchFlights()
        results = search.search(filters)

        if not results:
            return _empty_result(origin, destination, outbound_date,
                                 return_date or "", "google_flights", label)

        best = results[0]  # Already sorted by cheapest
        price = getattr(best, "price", None)
        currency = getattr(best, "currency", "EUR") or "EUR"
        duration = getattr(best, "duration", 0) or 0  # minutes
        stops = getattr(best, "stops", 0) or 0

        # Airlines from legs
        airlines_list: list[str] = []
        for leg in getattr(best, "legs", []) or []:
            airline = getattr(leg, "airline", "") or ""
            if airline and airline not in airlines_list:
                airlines_list.append(airline)

        dur_h = duration / 60 if isinstance(duration, (int, float)) else 0

        return FlightResult(
            price=float(price) if price else None,
            currency=currency, origin=origin, destination=destination,
            outbound_date=outbound_date, return_date=return_date or "",
            out_h=round(dur_h, 2), ret_h=round(dur_h, 2),
            out_stops=stops, ret_stops=stops,
            airlines="+".join(airlines_list), booking_url="",
            source="google_flights", market_label=label, raw=best,
        )
    except Exception as e:
        print(f"  fli ({label}) error: {e}")
        return _empty_result(origin, destination, outbound_date,
                             return_date or "", "google_flights", label)


def search_google_flights_multi(*, origins: list[str], destinations: list[str],
                                outbound_date: str, return_date: str,
                                adults: int = 1, max_fly_h: int = 18,
                                delay: float = 2.0) -> list[FlightResult]:
    """Search all origin×destination combinations via fli (Google Flights).
    Returns list of valid results, sorted by price."""
    results: list[FlightResult] = []
    for orig in origins:
        for dest in destinations:
            r = _search_fli(
                origin=orig, destination=dest,
                outbound_date=outbound_date, return_date=return_date,
                adults=adults, max_fly_h=max_fly_h,
                label=f"GF {orig}→{dest}",
            )
            if r.price is not None:
                results.append(r)
            time.sleep(delay)
    results.sort(key=lambda r: r.price or 1e9)
    return results


def search_google_flights_oneway(*, origin: str, destination: str,
                                 dep_date: str, adults: int = 1,
                                 max_fly_h: int = 18,
                                 label: str = "") -> FlightResult:
    """One-way Google Flights search via fli."""
    return _search_fli(
        origin=origin, destination=destination,
        outbound_date=dep_date, return_date=None,
        adults=adults, max_fly_h=max_fly_h, label=label,
    )


# ─────────────────────────── Duffel API ───────────────────────────

DUFFEL_BASE = "https://api.duffel.com"


def search_duffel(*, origins: list[str], destinations: list[str],
                  outbound_date: str, return_date: str,
                  adults: int = 1, currency: str = "EUR",
                  max_fly_h: int = 18) -> list[FlightResult]:
    """Search flights via Duffel API.
    One call per (origin, dest) pair. Regroups results."""
    token = os.environ.get("DUFFEL_API_KEY") or os.environ.get("DUFFEL")
    if not token:
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": "v2",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    all_results: list[FlightResult] = []

    for orig in origins:
        for dest in destinations:
            try:
                body = {
                    "data": {
                        "slices": [
                            {"origin": orig, "destination": dest,
                             "departure_date": outbound_date},
                            {"origin": dest, "destination": orig,
                             "departure_date": return_date},
                        ],
                        "passengers": [{"type": "adult"} for _ in range(adults)],
                        "cabin_class": "economy",
                    }
                }
                r = requests.post(
                    f"{DUFFEL_BASE}/air/offer_requests",
                    json=body, headers=headers,
                    params={"return_offers": "true", "supplier_timeout": "30000"},
                    timeout=45,
                )
                if r.status_code not in (200, 201):
                    print(f"  Duffel {orig}→{dest}: HTTP {r.status_code}")
                    continue

                data = r.json().get("data", {})
                offers = data.get("offers", [])

                for offer in offers:
                    parsed = _parse_duffel_offer(offer, orig, dest,
                                                 outbound_date, return_date,
                                                 max_fly_h)
                    if parsed:
                        all_results.append(parsed)

            except Exception as e:
                print(f"  Duffel {orig}→{dest} error: {e}")

            time.sleep(0.5)

    all_results.sort(key=lambda r: r.price or 1e9)
    return all_results


def _parse_duffel_offer(offer: dict, origin: str, destination: str,
                        outbound_date: str, return_date: str,
                        max_fly_h: int) -> FlightResult | None:
    """Parse a Duffel offer into a normalized FlightResult."""
    try:
        total = float(offer.get("total_amount", 0))
        currency = offer.get("total_currency", "EUR")
        if total <= 0:
            return None

        slices = offer.get("slices", [])
        if len(slices) < 2:
            return None

        # Outbound
        out_slice = slices[0]
        out_dur = _parse_iso_duration(out_slice.get("duration", ""))
        out_segments = out_slice.get("segments", [])
        out_stops = max(0, len(out_segments) - 1)
        out_origin = out_slice.get("origin", {}).get("iata_code", origin)
        out_dest = out_slice.get("destination", {}).get("iata_code", destination)
        out_dep = (out_segments[0].get("departing_at", "")[:10]
                   if out_segments else outbound_date)

        # Return
        ret_slice = slices[1]
        ret_dur = _parse_iso_duration(ret_slice.get("duration", ""))
        ret_segments = ret_slice.get("segments", [])
        ret_stops = max(0, len(ret_segments) - 1)
        ret_dep = (ret_segments[0].get("departing_at", "")[:10]
                   if ret_segments else return_date)

        # Duration filter
        if out_dur > max_fly_h or ret_dur > max_fly_h:
            return None

        # Airlines
        airlines_set: list[str] = []
        for seg in out_segments + ret_segments:
            carrier = (seg.get("operating_carrier", {}).get("iata_code") or
                       seg.get("marketing_carrier", {}).get("iata_code", ""))
            if carrier and carrier not in airlines_set:
                airlines_set.append(carrier)

        return FlightResult(
            price=total, currency=currency,
            origin=out_origin, destination=out_dest,
            outbound_date=out_dep, return_date=ret_dep,
            out_h=round(out_dur, 2), ret_h=round(ret_dur, 2),
            out_stops=out_stops, ret_stops=ret_stops,
            airlines="+".join(airlines_set), booking_url="",
            source="duffel",
        )
    except Exception as e:
        print(f"  Duffel parse error: {e}")
        return None


def search_duffel_oneway(*, origin: str, destination: str,
                         dep_date: str, adults: int = 1,
                         currency: str = "EUR",
                         max_fly_h: int = 18) -> FlightResult | None:
    """One-way Duffel search. Returns cheapest result or None."""
    token = os.environ.get("DUFFEL_API_KEY") or os.environ.get("DUFFEL")
    if not token:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": "v2",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "data": {
            "slices": [{"origin": origin, "destination": destination,
                        "departure_date": dep_date}],
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": "economy",
        }
    }
    try:
        r = requests.post(
            f"{DUFFEL_BASE}/air/offer_requests",
            json=body, headers=headers,
            params={"return_offers": "true", "supplier_timeout": "20000"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            return None
        offers = r.json().get("data", {}).get("offers", [])
        if not offers:
            return None

        best = None
        best_price = 1e9
        for offer in offers:
            total = float(offer.get("total_amount", 0))
            if total <= 0:
                continue
            slices = offer.get("slices", [])
            if not slices:
                continue
            dur = _parse_iso_duration(slices[0].get("duration", ""))
            if dur > max_fly_h:
                continue
            if total < best_price:
                best_price = total
                segs = slices[0].get("segments", [])
                airlines_list = []
                for seg in segs:
                    c = (seg.get("operating_carrier", {}).get("iata_code") or
                         seg.get("marketing_carrier", {}).get("iata_code", ""))
                    if c and c not in airlines_list:
                        airlines_list.append(c)
                best = FlightResult(
                    price=total, currency=offer.get("total_currency", currency),
                    origin=origin, destination=destination,
                    outbound_date=dep_date, return_date="",
                    out_h=round(dur, 2), ret_h=0,
                    out_stops=max(0, len(segs) - 1), ret_stops=0,
                    airlines="+".join(airlines_list), booking_url="",
                    source="duffel_ow",
                )
        return best
    except Exception as e:
        print(f"  Duffel OW {origin}→{destination} error: {e}")
        return None


def _parse_iso_duration(s: str) -> float:
    """Parse ISO 8601 duration like 'PT14H30M' to hours."""
    if not s:
        return 0.0
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", s)
    if m:
        return int(m.group(1) or 0) + int(m.group(2) or 0) / 60
    return 0.0


# ─────────────────────────── Date helpers ─────────────────────────

def mid_date(window: tuple[str, str]) -> str:
    """Return the middle date of a window (YYYY-MM-DD)."""
    d1 = datetime.strptime(window[0], "%Y-%m-%d")
    d2 = datetime.strptime(window[1], "%Y-%m-%d")
    mid = d1 + (d2 - d1) / 2
    return mid.strftime("%Y-%m-%d")
