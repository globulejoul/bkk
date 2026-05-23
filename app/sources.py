"""Flight data sources: Kiwi Tequila + fast-flights (Google Flights).

fast-flights is invoked optionally through an HTTP proxy (Gluetun) to
simulate browsing from a different country (e.g. Thailand).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

TEQUILA_API = "https://api.tequila.kiwi.com/v2/search"


# ─────────────────────────── Tequila ────────────────────────────

def _fmt_kiwi(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")


def search_tequila(*, origins: list[str], destinations: list[str],
                   outbound_window: tuple[str, str],
                   return_window: tuple[str, str],
                   currency: str = "EUR", adults: int = 1,
                   max_fly_duration_h: int = 18,
                   min_nights: int | None = None,
                   max_nights: int | None = None) -> list[dict]:
    """Single call: Tequila handles date windows natively."""
    key = os.environ.get("TEQUILA_API_KEY")
    if not key:
        return []
    params = {
        "fly_from": ",".join(origins),
        "fly_to": ",".join(destinations),
        "dateFrom": _fmt_kiwi(outbound_window[0]),
        "dateTo": _fmt_kiwi(outbound_window[1]),
        "returnFrom": _fmt_kiwi(return_window[0]),
        "returnTo": _fmt_kiwi(return_window[1]),
        "curr": currency,
        "adults": adults,
        "max_fly_duration": max_fly_duration_h,
        "sort": "price",
        "limit": 50,
        "vehicle_type": "aircraft",
    }
    if min_nights is not None:
        params["nights_in_dst_from"] = min_nights
    if max_nights is not None:
        params["nights_in_dst_to"] = max_nights

    r = requests.get(TEQUILA_API, headers={"apikey": key},
                     params=params, timeout=30)
    if r.status_code != 200:
        print(f"Tequila ({currency}) HTTP {r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("data", [])


def tequila_parse(flight: dict) -> dict:
    """Normalize a Tequila flight dict to our schema."""
    d = flight.get("duration", {})
    out_h = d.get("departure", 0) / 3600
    ret_h = d.get("return", 0) / 3600

    out_count = sum(1 for s in flight.get("route", []) if s.get("return") == 0)
    ret_count = sum(1 for s in flight.get("route", []) if s.get("return") == 1)
    out_stops = max(0, out_count - 1)
    ret_stops = max(0, ret_count - 1)

    airlines: list[str] = []
    for seg in flight.get("route", []):
        a = seg.get("airline")
        if a and a not in airlines:
            airlines.append(a)

    return_date = ""
    for seg in flight.get("route", []):
        if seg.get("return") == 1:
            return_date = seg.get("local_departure", "")[:10]
            break

    return {
        "origin": flight.get("cityCodeFrom") or flight.get("flyFrom"),
        "destination": flight.get("cityCodeTo") or flight.get("flyTo"),
        "price": flight.get("price"),
        "outbound_date": flight.get("local_departure", "")[:10],
        "return_date": return_date,
        "out_h": round(out_h, 2),
        "ret_h": round(ret_h, 2),
        "out_stops": out_stops,
        "ret_stops": ret_stops,
        "airlines": "+".join(airlines),
        "booking_url": flight.get("deep_link", ""),
    }


# ─────────────────────────── fast-flights (Google) ──────────────────

@dataclass
class FastFlightResult:
    price: float | None
    currency: str
    airlines: str
    market_label: str
    raw: Any = None


def search_fast_flights(*, origin: str, destination: str,
                        outbound_date: str, return_date: str,
                        adults: int = 1,
                        via_vpn: bool = False,
                        market_label: str = "GF FR") -> FastFlightResult:
    """Single point-in-time Google Flights search.

    When via_vpn=True, the request is routed through the Gluetun HTTP proxy
    (set via VPN_HTTP_PROXY env). Google then returns prices and currency
    matching the VPN exit country.

    Note: fast-flights' default currency is locale-dependent. We capture
    whatever it returns and let the caller normalize via fx.py.
    """
    try:
        from fast_flights import FlightData, Passengers, get_flights
    except ImportError:
        print("fast-flights not installed")
        return FastFlightResult(None, "EUR", "", market_label)

    # Configure proxy via environment if VPN routing requested
    old_proxies = {}
    if via_vpn:
        proxy = os.environ.get("VPN_HTTP_PROXY")
        if not proxy:
            print(f"  VPN requested for {market_label} but VPN_HTTP_PROXY not set")
            return FastFlightResult(None, "EUR", "", market_label)
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
        result = get_flights(
            flight_data=[
                FlightData(date=outbound_date,
                           from_airport=origin, to_airport=destination),
                FlightData(date=return_date,
                           from_airport=destination, to_airport=origin),
            ],
            trip="round-trip",
            seat="economy",
            passengers=Passengers(adults=adults, children=0,
                                  infants_in_seat=0, infants_on_lap=0),
            fetch_mode="fallback",
        )
        flights = getattr(result, "flights", []) or []
        if not flights:
            return FastFlightResult(None, "EUR", "", market_label, raw=result)
        cheapest = min(flights, key=lambda f: _flight_price(f))
        price = _flight_price(cheapest)
        currency = _detect_currency(cheapest, via_vpn=via_vpn)
        airline = getattr(cheapest, "name", "") or ""
        return FastFlightResult(
            price=price, currency=currency,
            airlines=airline, market_label=market_label, raw=result,
        )
    except Exception as e:
        print(f"  fast-flights ({market_label}) error: {e}")
        return FastFlightResult(None, "EUR", "", market_label)
    finally:
        # Restore proxy env
        if via_vpn:
            for k, v in old_proxies.items():
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)


def _flight_price(flight: Any) -> float:
    """Extract price from fast-flights Flight object (may be int, str like
    '$612', or '€612')."""
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
    """Try to detect currency from the price string; fall back by VPN context."""
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
