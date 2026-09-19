"""Flight data sources: fli (Google Flights) + Duffel API.

fli is the primary source — reverse-engineered Google Flights API (no scraping).
Duffel provides direct airline pricing (AF, Emirates, QR, EY...).
"""
from __future__ import annotations

import inspect
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests


# ─────────────────────────── Normalized result ────────────────────

# slots : un run Duffel peut instancier des dizaines de milliers de résultats,
# autant ne pas payer un __dict__ par objet (aucun attribut n'est ajouté
# dynamiquement dans le projet).
@dataclass(slots=True)
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


GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"


def _google_flights_url(origin: str, destination: str,
                        out_date: str, ret_date: str = "") -> str:
    """Lien de recherche Google Flights pré-rempli.

    Aucune source ne renvoie d'URL de réservation réutilisable (Duffel exige
    la création d'une commande) : faute de mieux on stocke un point d'entrée
    direct, sinon le bouton « Réserver » et l'en-tête Click ntfy restent vides.
    """
    q = f"Flights from {origin} to {destination} on {out_date}"
    if ret_date:
        q += f" through {ret_date}"
    return f"{GOOGLE_FLIGHTS_URL}?q={quote(q)}"


# ─────────────────────────── fli (Google Flights) ─────────────────

# Marché forcé : sans ces paramètres Google choisit devise et point de vente
# selon l'IP du serveur, et un prix en GBP/USD est soit écarté, soit comparé
# à tort à des euros.
_FLI_LOCALE = {"currency": "EUR", "language": "fr", "country": "FR"}

_fli_client: Any = None


def _get_fli_client(cls):
    """Client fli réutilisé d'une recherche à l'autre (évite de reconstruire
    la session HTTP à chaque appel)."""
    global _fli_client
    if _fli_client is None:
        _fli_client = cls()
    return _fli_client


def _fli_locale_kwargs(search_fn) -> dict[str, str]:
    """N'envoie que les paramètres de marché réellement acceptés par la
    version de fli installée (ils n'existent pas dans toutes les versions)."""
    try:
        params = inspect.signature(search_fn).parameters
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in _FLI_LOCALE.items() if k in params}


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

        search = _get_fli_client(SearchFlights)
        results = search.search(filters, **_fli_locale_kwargs(search.search))

        # fli concatène les sections « best » et « other » sans les retrier :
        # results[0] n'est pas forcément le moins cher, et son prix peut être
        # absent (ce qui faisait perdre toute la paire).
        priced = [r for r in (results or [])
                  if getattr(r, "price", None) is not None]
        if not priced:
            return _empty_result(origin, destination, outbound_date,
                                 return_date or "", "google_flights", label)

        best = min(priced, key=lambda r: r.price)
        price = best.price
        currency = getattr(best, "currency", None) or _FLI_LOCALE["currency"]
        if currency != _FLI_LOCALE["currency"]:
            print(f"  fli ({label}): prix en {currency} alors que "
                  f"{_FLI_LOCALE['currency']} était demandé")
        duration = getattr(best, "duration", 0) or 0  # minutes
        stops = getattr(best, "stops", 0) or 0

        # Airlines from legs
        airlines_list: list[str] = []
        legs = getattr(best, "legs", []) or []
        for leg in legs:
            airline = getattr(leg, "airline", None)
            if airline:
                # airline may be an Airline enum, a string, or other
                if hasattr(airline, "value"):
                    name = str(airline.value)
                elif hasattr(airline, "name"):
                    name = str(airline.name)
                else:
                    name = str(airline)
                if name and name not in airlines_list:
                    airlines_list.append(name)

        # Duration: fli returns total minutes for the trip
        dur_h = duration / 60 if isinstance(duration, (int, float)) else 0
        # If duration is 0 but we have legs, try to extract from legs
        if dur_h == 0 and legs:
            for leg in legs:
                leg_dur = getattr(leg, "duration", 0) or 0
                if isinstance(leg_dur, (int, float)) and leg_dur > 0:
                    dur_h = leg_dur / 60
                    break

        return FlightResult(
            price=float(price),
            currency=currency, origin=origin, destination=destination,
            outbound_date=outbound_date, return_date=return_date or "",
            out_h=round(dur_h, 2), ret_h=round(dur_h, 2),
            out_stops=stops, ret_stops=stops,
            airlines="+".join(airlines_list),
            booking_url=_google_flights_url(origin, destination,
                                            outbound_date, return_date or ""),
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
# Duffel : 60 req/60s (120 en live sur la recherche).
DUFFEL_DEFAULT_LIMIT = 60
DUFFEL_MAX_ATTEMPTS = 3
# Temps d'attente cumulé maximal sur les 429 d'un même run, bien en deçà
# du RUN_TIMEOUT de 900 s au-delà duquel le watchdog relâche le verrou.
DUFFEL_WAIT_BUDGET_S = 180
# supplier_timeout était à 20000 : la réponse attendait la compagnie la plus
# lente alors que les offres utiles arrivent bien avant (minimum Duffel 2000).
DUFFEL_PARAMS = {"return_offers": "true", "supplier_timeout": "8000"}

_duffel_session: requests.Session | None = None


class _DuffelAuthError(Exception):
    """Clé Duffel refusée : insister sur les combos suivantes est inutile."""


def _duffel_token() -> str | None:
    return os.environ.get("DUFFEL_API_KEY") or os.environ.get("DUFFEL")


def _get_duffel_session(token: str) -> requests.Session:
    """Session réutilisée : sans elle, chaque combo repayait un handshake
    TLS vers api.duffel.com (des dizaines de minutes sur un run complet)."""
    global _duffel_session
    if _duffel_session is None:
        _duffel_session = requests.Session()
    _duffel_session.headers.update({
        "Authorization": f"Bearer {token}",
        "Duffel-Version": "v2",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    return _duffel_session


def _duffel_reset_wait(rl_reset: str | None) -> float:
    """Secondes à attendre avant la remise à zéro du quota."""
    if rl_reset:
        try:
            from email.utils import parsedate_to_datetime
            reset_dt = parsedate_to_datetime(rl_reset)
            delta = (reset_dt - datetime.now(tz=reset_dt.tzinfo)).total_seconds()
            # Borné : un en-tête aberrant ne doit pas geler le run.
            return min(120.0, max(1.0, delta))
        except Exception:
            pass
    return 60.0


def _duffel_post(body: dict, *, tag: str, quota: dict[str, int]) -> dict | None:
    """POST /air/offer_requests. Renvoie le bloc `data`, ou None si échec.

    Retente la même combo sur 429/5xx : auparavant elle était simplement
    perdue pour le run, ce qui creusait des trous silencieux dans la heatmap
    et pouvait déclencher de faux plus-bas. Journalise tout statut inattendu
    (une clé invalide enchaînait des milliers de `continue` sans un message).
    """
    token = _duffel_token()
    if not token:
        return None
    session = _get_duffel_session(token)
    url = f"{DUFFEL_BASE}/air/offer_requests"

    for attempt in range(1, DUFFEL_MAX_ATTEMPTS + 1):
        try:
            r = session.post(url, json=body, params=DUFFEL_PARAMS, timeout=30)
        except requests.RequestException as e:
            print(f"  Duffel {tag}: réseau — {e} "
                  f"({attempt}/{DUFFEL_MAX_ATTEMPTS})")
            time.sleep(2.0 * attempt)
            continue

        for header, key in (("ratelimit-remaining", "remaining"),
                            ("ratelimit-limit", "limit")):
            raw = r.headers.get(header)
            if raw is not None:
                try:
                    quota[key] = int(raw)
                except ValueError:
                    pass

        if r.status_code in (200, 201):
            try:
                payload = r.json()
            except Exception:
                print(f"  Duffel {tag}: réponse JSON illisible")
                return None
            data = payload.get("data") if isinstance(payload, dict) else None
            return data if isinstance(data, dict) else {}

        if r.status_code in (401, 403):
            raise _DuffelAuthError(f"HTTP {r.status_code} — {r.text[:200]}")

        if r.status_code == 429:
            # Budget d'attente global au run : sans lui, un rate-limiting
            # soutenu remplaçait les trous de la heatmap par un run entier
            # marqué « timeout » par le watchdog (RUN_TIMEOUT = 900 s).
            if quota.get("waited", 0) >= DUFFEL_WAIT_BUDGET_S:
                print(f"  Duffel {tag}: budget d'attente épuisé, abandon")
                return None
            wait = _duffel_reset_wait(r.headers.get("ratelimit-reset"))
            quota["waited"] = quota.get("waited", 0) + int(wait)
            print(f"  Duffel {tag}: 429 — pause {wait:.0f}s "
                  f"({attempt}/{DUFFEL_MAX_ATTEMPTS})")
            time.sleep(wait)
            continue

        print(f"  Duffel {tag}: HTTP {r.status_code} — {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(2.0 * attempt)
            continue
        return None  # 422 & co : retenter à l'identique ne changerait rien

    print(f"  Duffel {tag}: abandon après {DUFFEL_MAX_ATTEMPTS} tentatives")
    return None


def _duffel_throttle(quota: dict[str, int]) -> None:
    """Cadence dérivée de ratelimit-limit plutôt que de seuils codés en dur
    pour une limite de 60 (identique à l'ancien comportement si limit=60)."""
    limit = quota.get("limit") or DUFFEL_DEFAULT_LIMIT
    remaining = quota.get("remaining", limit)
    if remaining <= max(2, limit // 12):
        time.sleep(5.0)
    elif remaining <= max(5, limit // 4):
        time.sleep(2.0)
    else:
        time.sleep(1.0)


def search_duffel(*, origins: list[str], destinations: list[str],
                  outbound_dates: list[str], return_dates: list[str],
                  adults: int = 1, currency: str = "EUR",
                  max_fly_h: int = 18) -> list[FlightResult]:
    """Search flights via Duffel API across all date combos.
    Iterates (origin, dest, outbound_date, return_date) combinations.

    `currency` n'est pas transmis à l'API (offer_requests ne l'accepte pas :
    Duffel facture dans la devise de l'organisation) ; il sert uniquement à
    signaler les offres arrivant dans une autre devise, que le watcher ne
    saura pas convertir.
    """
    if not _duffel_token():
        return []

    # Seul le minimum par combo est exploité en aval (best par paire) :
    # conserver toutes les offres faisait vivre des centaines de milliers
    # d'objets pendant des heures pour rien.
    best_by_combo: dict[tuple[str, str, str, str], FlightResult] = {}
    total = len(origins) * len(destinations) * len(outbound_dates) * len(return_dates)
    done = 0
    quota = {"remaining": DUFFEL_DEFAULT_LIMIT, "limit": DUFFEL_DEFAULT_LIMIT}
    other_currencies: set[str] = set()
    test_mode_warned = False

    try:
        for out_d in outbound_dates:
            for ret_d in return_dates:
                for orig in origins:
                    for dest in destinations:
                        done += 1
                        tag = f"{orig}→{dest} {out_d}/{ret_d}"
                        body = {
                            "data": {
                                "slices": [
                                    {"origin": orig, "destination": dest,
                                     "departure_date": out_d},
                                    {"origin": dest, "destination": orig,
                                     "departure_date": ret_d},
                                ],
                                "passengers": [{"type": "adult"} for _ in range(adults)],
                                "cabin_class": "economy",
                            }
                        }
                        data = _duffel_post(body, tag=tag, quota=quota)

                        # live_mode=false ⇒ token de test : des prix
                        # synthétiques n'ont rien à faire en base.
                        if data is not None and data.get("live_mode") is False:
                            if not test_mode_warned:
                                print("  ⚠ Duffel: token TEST (live_mode=false) — "
                                      "offres synthétiques ignorées")
                                test_mode_warned = True
                            data = None

                        for offer in (data or {}).get("offers") or []:
                            parsed = _parse_duffel_offer(offer, orig, dest,
                                                         out_d, ret_d, max_fly_h)
                            if parsed is None:
                                continue
                            if parsed.currency != currency:
                                other_currencies.add(parsed.currency)
                            key = (orig, dest, out_d, ret_d)
                            known = best_by_combo.get(key)
                            if known is None or (parsed.price or 1e9) < (known.price or 1e9):
                                best_by_combo[key] = parsed

                        _duffel_throttle(quota)

                if done % 30 == 0:
                    print(f"  Duffel: {done}/{total} "
                          f"({len(best_by_combo)} combos avec prix, "
                          f"quota restant: {quota.get('remaining')})")
    except _DuffelAuthError as e:
        print(f"  ⚠ Duffel: clé refusée ({e}) — source abandonnée pour ce run")

    if other_currencies:
        print(f"  ⚠ Duffel: offres en {', '.join(sorted(other_currencies))} "
              f"et non en {currency} — non converties si le taux n'est pas chargé")

    results = sorted(best_by_combo.values(), key=lambda r: r.price or 1e9)
    return results


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

        # Durée illisible : on écarte l'offre plutôt que de lui attribuer 0 h,
        # ce qui la faisait passer sous la limite et s'afficher « 0.0h ».
        if out_dur is None or ret_dur is None:
            return None
        if out_dur > max_fly_h or ret_dur > max_fly_h:
            return None

        # Airlines (full names from Duffel)
        airlines_set: list[str] = []
        for seg in out_segments + ret_segments:
            op = seg.get("operating_carrier", {})
            mk = seg.get("marketing_carrier", {})
            name = op.get("name") or mk.get("name") or op.get("iata_code") or mk.get("iata_code", "")
            if name and name not in airlines_set:
                airlines_set.append(name)

        return FlightResult(
            price=total, currency=currency,
            origin=out_origin, destination=out_dest,
            outbound_date=out_dep, return_date=ret_dep,
            out_h=round(out_dur, 2), ret_h=round(ret_dur, 2),
            out_stops=out_stops, ret_stops=ret_stops,
            airlines="+".join(airlines_set),
            booking_url=_google_flights_url(out_origin, out_dest,
                                            out_dep, ret_dep),
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
    if not _duffel_token():
        return None

    body = {
        "data": {
            "slices": [{"origin": origin, "destination": destination,
                        "departure_date": dep_date}],
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": "economy",
        }
    }
    tag = f"OW {origin}→{destination} {dep_date}"
    try:
        # Même client HTTP, même politique de retry/log que l'aller-retour.
        data = _duffel_post(body, tag=tag, quota={})
    except _DuffelAuthError as e:
        print(f"  ⚠ Duffel {tag}: clé refusée ({e})")
        return None
    if not data:
        return None
    if data.get("live_mode") is False:
        print(f"  ⚠ Duffel {tag}: token TEST — offres ignorées")
        return None

    try:
        offers = data.get("offers") or []
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
            if dur is None or dur > max_fly_h:
                continue
            if total < best_price:
                best_price = total
                segs = slices[0].get("segments", [])
                airlines_list = []
                for seg in segs:
                    op = seg.get("operating_carrier", {})
                    mk = seg.get("marketing_carrier", {})
                    name = op.get("name") or mk.get("name") or op.get("iata_code") or mk.get("iata_code", "")
                    if name and name not in airlines_list:
                        airlines_list.append(name)
                best = FlightResult(
                    price=total, currency=offer.get("total_currency", currency),
                    origin=origin, destination=destination,
                    outbound_date=dep_date, return_date="",
                    out_h=round(dur, 2), ret_h=0,
                    out_stops=max(0, len(segs) - 1), ret_stops=0,
                    airlines="+".join(airlines_list),
                    booking_url=_google_flights_url(origin, destination,
                                                    dep_date),
                    source="duffel_ow",
                )
        return best
    except Exception as e:
        print(f"  Duffel {tag} parse error: {e}")
        return None


def _parse_iso_duration(s: str) -> float | None:
    """Parse ISO 8601 duration like 'PT14H30M' or 'P1DT2H30M' to hours.

    Renvoie None si la chaîne est vide ou illisible : le fallback à 0.0
    faisait passer l'itinéraire pour conforme à la durée maximale.
    """
    if not s:
        return None
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", s)
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = m.groups()
    return (int(days or 0) * 24 + int(hours or 0)
            + int(minutes or 0) / 60 + float(seconds or 0) / 3600)


# ─────────────────────────── Date helpers ─────────────────────────

def mid_date(window: tuple[str, str]) -> str:
    """Return the middle date of a window (YYYY-MM-DD)."""
    d1 = datetime.strptime(window[0], "%Y-%m-%d")
    d2 = datetime.strptime(window[1], "%Y-%m-%d")
    mid = d1 + (d2 - d1) / 2
    return mid.strftime("%Y-%m-%d")


def date_range(window: tuple[str, str]) -> list[str]:
    """Return all dates in a window (inclusive), as YYYY-MM-DD strings."""
    from datetime import timedelta
    d1 = datetime.strptime(window[0], "%Y-%m-%d")
    d2 = datetime.strptime(window[1], "%Y-%m-%d")
    dates = []
    cur = d1
    while cur <= d2:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates
