"""Flight data sources: fast-flights (Google Flights) + Duffel API.

fast-flights is the primary source (free, no API key).
Duffel provides direct airline pricing (AF, Emirates, QR, EY...).
fast-flights can be routed through an HTTP proxy (Gluetun) to get
prices from a different country (e.g. Thailand).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests


# ─────────────────────────── fast-flights (Google Flights) ─────────

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


def search_fast_flights(*, origin: str, destination: str,
                        outbound_date: str, return_date: str,
                        adults: int = 1,
                        via_vpn: bool = False,
                        market_label: str = "Google FR") -> FlightResult:
    """Single point-in-time Google Flights search.

    When via_vpn=True, the request is routed through the Gluetun HTTP proxy
    (set via VPN_HTTP_PROXY env). Google then returns prices and currency
    matching the VPN exit country.
    """
    try:
        # v3 API
        from fast_flights import FlightQuery, Passengers, create_query, get_flights
        _v3 = True
    except ImportError:
        try:
            # v2 fallback
            from fast_flights import FlightData as FlightQuery, Passengers, get_flights
            _v3 = False
        except ImportError:
            print("fast-flights not installed")
            return FlightResult(
                price=None, currency="EUR", origin=origin,
                destination=destination, outbound_date=outbound_date,
                return_date=return_date, out_h=0, ret_h=0,
                out_stops=0, ret_stops=0, airlines="", booking_url="",
                source="fast_flights", market_label=market_label,
            )

    # Configure proxy via environment if VPN routing requested
    old_proxies = {}
    if via_vpn:
        proxy = os.environ.get("VPN_HTTP_PROXY")
        if not proxy:
            print(f"  VPN requested for {market_label} but VPN_HTTP_PROXY not set")
            return FlightResult(
                price=None, currency="EUR", origin=origin,
                destination=destination, outbound_date=outbound_date,
                return_date=return_date, out_h=0, ret_h=0,
                out_stops=0, ret_stops=0, airlines="", booking_url="",
                source="fast_flights_th", market_label=market_label,
            )
        old_proxies = {
            "http_proxy": os.environ.get("http_proxy", ""),
            "https_proxy": os.environ.get("https_proxy", ""),
            "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
            "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
        }
        os.environ["http_proxy"] = proxy
        os.environ["https_proxy"] = proxy
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy

    try:
        if _v3:
            query = create_query(
                flights=[
                    FlightQuery(date=outbound_date,
                                from_airport=origin, to_airport=destination),
                    FlightQuery(date=return_date,
                                from_airport=destination, to_airport=origin),
                ],
                trip="round-trip", seat="economy",
                passengers=Passengers(adults=adults),
            )
            result = get_flights(query)
        else:
            result = get_flights(
                flight_data=[
                    FlightQuery(date=outbound_date,
                                from_airport=origin, to_airport=destination),
                    FlightQuery(date=return_date,
                                from_airport=destination, to_airport=origin),
                ],
                trip="round-trip", seat="economy",
                passengers=Passengers(adults=adults, children=0,
                                      infants_in_seat=0, infants_on_lap=0),
                fetch_mode="fallback",
            )
        flights = getattr(result, "flights", []) or []
        if not flights:
            return FlightResult(
                price=None, currency="EUR", origin=origin,
                destination=destination, outbound_date=outbound_date,
                return_date=return_date, out_h=0, ret_h=0,
                out_stops=0, ret_stops=0, airlines="", booking_url="",
                source="fast_flights_th" if via_vpn else "fast_flights",
                market_label=market_label, raw=result,
            )
        cheapest = min(flights, key=lambda f: _parse_price(f))
        price = _parse_price(cheapest)
        currency = _detect_currency(cheapest, via_vpn=via_vpn)
        airline = getattr(cheapest, "name", "") or ""
        duration_str = getattr(cheapest, "duration", "") or ""
        stops = getattr(cheapest, "stops", 0)
        if isinstance(stops, str):
            stops = int(re.search(r"\d+", stops).group()) if re.search(r"\d+", stops) else 0
        dur_h = _parse_duration(duration_str)
        return FlightResult(
            price=price if price < 1e8 else None,
            currency=currency, origin=origin, destination=destination,
            outbound_date=outbound_date, return_date=return_date,
            out_h=dur_h, ret_h=dur_h,  # fast-flights donne la durée totale
            out_stops=stops, ret_stops=stops,
            airlines=airline, booking_url="",
            source="fast_flights_th" if via_vpn else "fast_flights",
            market_label=market_label, raw=result,
        )
    except Exception as e:
        print(f"  fast-flights ({market_label}) error: {e}")
        return FlightResult(
            price=None, currency="EUR", origin=origin,
            destination=destination, outbound_date=outbound_date,
            return_date=return_date, out_h=0, ret_h=0,
            out_stops=0, ret_stops=0, airlines="", booking_url="",
            source="fast_flights_th" if via_vpn else "fast_flights",
            market_label=market_label,
        )
    finally:
        if via_vpn:
            for k, v in old_proxies.items():
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)


def search_fast_flights_multi(*, origins: list[str], destinations: list[str],
                              outbound_date: str, return_date: str,
                              adults: int = 1, max_fly_h: int = 18,
                              delay: float = 2.0) -> list[FlightResult]:
    """Search all origin×destination combinations via fast-flights.
    Returns list of valid results, sorted by price."""
    results: list[FlightResult] = []
    for orig in origins:
        for dest in destinations:
            r = search_fast_flights(
                origin=orig, destination=dest,
                outbound_date=outbound_date, return_date=return_date,
                adults=adults, market_label=f"GF {orig}→{dest}",
            )
            if r.price is not None and r.out_h <= max_fly_h:
                results.append(r)
            # Respecter Google : pause entre chaque requête
            time.sleep(delay)
    results.sort(key=lambda r: r.price or 1e9)
    return results


def _parse_price(flight: Any) -> float:
    """Extract price from fast-flights Flight object."""
    p = getattr(flight, "price", None)
    if p is None:
        return 1e9
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, str):
        digits = "".join(ch for ch in p if ch.isdigit() or ch == ".")
        try:
            return float(digits) if digits else 1e9
        except ValueError:
            return 1e9
    return 1e9


def _detect_currency(flight: Any, *, via_vpn: bool) -> str:
    """Try to detect currency from the price string."""
    p = getattr(flight, "price", None)
    if isinstance(p, str):
        if "฿" in p or "THB" in p:
            return "THB"
        if "€" in p or "EUR" in p:
            return "EUR"
        if "$" in p or "USD" in p:
            return "USD"
        if "£" in p:
            return "GBP"
    return "THB" if via_vpn else "EUR"


def _parse_duration(s: str) -> float:
    """Parse duration like '14 hr 30 min' or 'PT14H30M' to hours."""
    if not s:
        return 0.0
    # ISO format
    m = re.match(r"PT(\d+)H(\d+)?M?", s)
    if m:
        return int(m.group(1)) + int(m.group(2) or 0) / 60
    # Human format
    hours = 0.0
    h = re.search(r"(\d+)\s*h", s, re.IGNORECASE)
    if h:
        hours += int(h.group(1))
    mins = re.search(r"(\d+)\s*m", s, re.IGNORECASE)
    if mins:
        hours += int(mins.group(1)) / 60
    return round(hours, 2)


def search_fast_flights_oneway(*, origin: str, destination: str,
                               dep_date: str, adults: int = 1,
                               label: str = "") -> FlightResult:
    """One-way fast-flights search."""
    try:
        from fast_flights import FlightQuery, Passengers, create_query, get_flights
        _v3 = True
    except ImportError:
        try:
            from fast_flights import FlightData as FlightQuery, Passengers, get_flights
            _v3 = False
        except ImportError:
            return FlightResult(
                price=None, currency="EUR", origin=origin,
                destination=destination, outbound_date=dep_date,
                return_date="", out_h=0, ret_h=0, out_stops=0, ret_stops=0,
                airlines="", booking_url="", source="fast_flights_ow",
                market_label=label,
            )

    try:
        if _v3:
            query = create_query(
                flights=[
                    FlightQuery(date=dep_date,
                                from_airport=origin, to_airport=destination),
                ],
                trip="one-way", seat="economy",
                passengers=Passengers(adults=adults),
            )
            result = get_flights(query)
        else:
            result = get_flights(
                flight_data=[
                    FlightQuery(date=dep_date,
                                from_airport=origin, to_airport=destination),
                ],
                trip="one-way", seat="economy",
                passengers=Passengers(adults=adults, children=0,
                                      infants_in_seat=0, infants_on_lap=0),
                fetch_mode="fallback",
            )
        flights = getattr(result, "flights", []) or []
        if not flights:
            return FlightResult(
                price=None, currency="EUR", origin=origin,
                destination=destination, outbound_date=dep_date,
                return_date="", out_h=0, ret_h=0, out_stops=0, ret_stops=0,
                airlines="", booking_url="", source="fast_flights_ow",
                market_label=label, raw=result,
            )
        cheapest = min(flights, key=lambda f: _parse_price(f))
        price = _parse_price(cheapest)
        currency = _detect_currency(cheapest, via_vpn=False)
        airline = getattr(cheapest, "name", "") or ""
        dur_h = _parse_duration(getattr(cheapest, "duration", "") or "")
        stops = getattr(cheapest, "stops", 0)
        if isinstance(stops, str):
            stops = int(re.search(r"\d+", stops).group()) if re.search(r"\d+", stops) else 0
        return FlightResult(
            price=price if price < 1e8 else None,
            currency=currency, origin=origin, destination=destination,
            outbound_date=dep_date, return_date="",
            out_h=dur_h, ret_h=0, out_stops=stops, ret_stops=0,
            airlines=airline, booking_url="",
            source="fast_flights_ow", market_label=label,
        )
    except Exception as e:
        print(f"  fast-flights OW ({label}) error: {e}")
        return FlightResult(
            price=None, currency="EUR", origin=origin,
            destination=destination, outbound_date=dep_date,
            return_date="", out_h=0, ret_h=0, out_stops=0, ret_stops=0,
            airlines="", booking_url="", source="fast_flights_ow",
            market_label=label,
        )


# ─────────────────────────── Duffel API ───────────────────────────

DUFFEL_BASE = "https://api.duffel.com"


def search_duffel(*, origins: list[str], destinations: list[str],
                  outbound_date: str, return_date: str,
                  adults: int = 1, currency: str = "EUR",
                  max_fly_h: int = 18) -> list[FlightResult]:
    """Search flights via Duffel API.
    Duffel supporte 1 origin + 1 dest par slice, donc on fait
    un call par paire (origin, dest). On regroupe les résultats."""
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
                            {
                                "origin": orig,
                                "destination": dest,
                                "departure_date": outbound_date,
                            },
                            {
                                "origin": dest,
                                "destination": orig,
                                "departure_date": return_date,
                            },
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
                if r.status_code != 200:
                    print(f"  Duffel {orig}→{dest}: HTTP {r.status_code} "
                          f"{r.text[:200]}")
                    continue

                data = r.json().get("data", {})
                offers = data.get("offers", [])
                dictionaries = r.json().get("dictionaries", {})

                for offer in offers:
                    parsed = _parse_duffel_offer(offer, orig, dest,
                                                 outbound_date, return_date,
                                                 max_fly_h)
                    if parsed:
                        all_results.append(parsed)

            except Exception as e:
                print(f"  Duffel {orig}→{dest} error: {e}")

            # Pause entre requêtes
            time.sleep(0.5)

    all_results.sort(key=lambda r: r.price or 1e9)
    return all_results


def _parse_duffel_offer(offer: dict, origin: str, destination: str,
                        outbound_date: str, return_date: str,
                        max_fly_h: int) -> FlightResult | None:
    """Parse un offer Duffel en FlightResult normalisé."""
    try:
        total = float(offer.get("total_amount", 0))
        currency = offer.get("total_currency", "EUR")
        if total <= 0:
            return None

        slices = offer.get("slices", [])
        if len(slices) < 2:
            return None

        # Outbound slice
        out_slice = slices[0]
        out_dur = _parse_iso_duration(out_slice.get("duration", ""))
        out_segments = out_slice.get("segments", [])
        out_stops = max(0, len(out_segments) - 1)
        out_origin = out_slice.get("origin", {}).get("iata_code", origin)
        out_dest = out_slice.get("destination", {}).get("iata_code", destination)
        out_dep = out_segments[0].get("departing_at", "")[:10] if out_segments else outbound_date

        # Return slice
        ret_slice = slices[1]
        ret_dur = _parse_iso_duration(ret_slice.get("duration", ""))
        ret_segments = ret_slice.get("segments", [])
        ret_stops = max(0, len(ret_segments) - 1)
        ret_dep = ret_segments[0].get("departing_at", "")[:10] if ret_segments else return_date

        # Filtre durée max
        if out_dur > max_fly_h or ret_dur > max_fly_h:
            return None

        # Airlines
        airlines_set: list[str] = []
        for seg in out_segments + ret_segments:
            carrier = seg.get("operating_carrier", {}).get("iata_code") or \
                      seg.get("marketing_carrier", {}).get("iata_code", "")
            if carrier and carrier not in airlines_set:
                airlines_set.append(carrier)

        return FlightResult(
            price=total,
            currency=currency,
            origin=out_origin,
            destination=out_dest,
            outbound_date=out_dep,
            return_date=ret_dep,
            out_h=round(out_dur, 2),
            ret_h=round(ret_dur, 2),
            out_stops=out_stops,
            ret_stops=ret_stops,
            airlines="+".join(airlines_set),
            booking_url="",
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
            "slices": [{
                "origin": origin, "destination": destination,
                "departure_date": dep_date,
            }],
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
        if r.status_code != 200:
            return None
        offers = r.json().get("data", {}).get("offers", [])
        if not offers:
            return None
        # Find cheapest valid offer
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
                    c = seg.get("operating_carrier", {}).get("iata_code") or \
                        seg.get("marketing_carrier", {}).get("iata_code", "")
                    if c and c not in airlines_list:
                        airlines_list.append(c)
                best = FlightResult(
                    price=total,
                    currency=offer.get("total_currency", currency),
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
