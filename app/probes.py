"""Sondes par compagnie : API officielle d'un transporteur, périmètre étroit.

Une sonde ne remplace aucune source. fli couvre tout le marché mais ne
sonde que la date médiane ; la sonde balaie progressivement la grille de
dates d'UNE route pour reconstruire la heatmap, perdue depuis la coupure
de Duffel.

Ses relevés vivent dans `probe_checks` et ne participent ni à l'état, ni
à la fenêtre glissante, ni aux alertes : une seule compagnie ne définit
pas le prix de marché. Mélanger ces lignes à `checks` décalait le 10e
percentile — qui travaille sur des prix distincts, non pondérés — et
suffisait à déclencher de fausses alertes basses.

Quota : Air France Open Data accorde 100 requêtes/jour à la clé entière
— mesuré en production, les hosts AF et KL la partagent — et ne renvoie
AUCUN en-tête de quota. Le comptage est donc intégralement à notre
charge. Il compte l'INTENTION : une requête partie puis perdue a
probablement été décomptée côté AF, et même une 400 de validation
consomme un jeton.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

import requests

from app import db
from app.config import Config, Probe, Trip

log = logging.getLogger(__name__)

ToEur = Callable[[float, str], float | None]

AFKL_URL = ("https://api.airfranceklm.com/opendata/offers/v3/"
            "available-offers")
AFKL_TIMEOUT = 40
# Un 5xx mérite une seconde chance, pas une 400 : le corps est construit
# par nous, une 400 est un bug de notre côté et consommerait un jeton de
# plus pour le même échec.
AFKL_MAX_ATTEMPTS = 2
# Budget mural accordé aux sondes pour TOUT le run, pas par période :
# instancié par période, le temps total croissait linéairement avec le
# nombre de périodes actives et le plafond ne plafonnait plus rien.
# Les sondes tournent DANS la section critique du run, protégée par
# RUN_TIMEOUT = 900 s : une API tierce lente ne doit pas pouvoir faire
# expirer un run de marché dont elle n'est qu'un supplément. Le curseur
# est justement conçu pour reprendre au run suivant.
PROBE_BUDGET_S = 150.0
# Pause du seau quand le FOURNISSEUR refuse alors que notre compteur ne
# l'était pas : assez long pour franchir une remise à zéro dont on
# ignore l'heure, assez court pour ne pas condamner la journée.
PROBE_BLOCK_H = 3

_session: requests.Session | None = None


class ProbeExhausted(Exception):
    """Notre propre seau est vide : on s'arrête, rien d'anormal."""


class ProviderExhausted(ProbeExhausted):
    """Le FOURNISSEUR refuse alors que notre compteur ne l'était pas.

    Distinct du cas précédent : c'est le seul qui justifie de mettre le
    seau en pause, parce qu'il révèle un décalage entre son compteur et
    le nôtre (requête perdue, ou remise à zéro à une heure inconnue).
    """


class ProbeAuthError(Exception):
    """Clé refusée : toutes les cellules suivantes échoueraient pareil."""


@dataclass(slots=True)
class ProbeOffer:
    price: float
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
    fare_family: str


# ── Transport ────────────────────────────────────────────────────


def _get_session() -> requests.Session:
    """Session réutilisée : sans elle chaque cellule repaie un handshake
    TLS, soit plusieurs minutes gaspillées sur un balayage de grille."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _headers(probe: Probe, key: str) -> dict[str, str]:
    return {
        "API-Key": key,
        "Accept": "application/hal+json",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        # Le host choisit la compagnie interrogée avec la MÊME clé.
        "AFKL-TRAVEL-Host": probe.travel_host,
        "AFKL-TRAVEL-Country": "FR",
    }


def _passengers(cfg: Config, probe: Probe) -> list[dict[str, Any]]:
    """Liste de passagers au format AFKL.

    `adults` par défaut : c'est le seul périmètre comparable aux autres
    sources, qui n'envoient jamais les enfants. `family` donne le prix
    réellement payé, mais ces relevés ne sont alors comparables qu'entre
    eux — d'où la séparation stricte des tables.
    """
    pax: list[dict[str, Any]] = [
        {"id": i + 1, "type": "ADT"} for i in range(max(1, cfg.adults))
    ]
    if probe.passengers == "family":
        for age in cfg.children:
            pax.append({"id": len(pax) + 1,
                        "type": "INF" if int(age) < 2 else "CHD"})
    return pax


def _body(cfg: Config, probe: Probe, origin: str, destination: str,
          out_date: str, ret_date: str) -> dict[str, Any]:
    # `commercialCabins` au pluriel et en tableau : le singulier est
    # rejeté par une 400, qui coûterait un jeton de quota.
    return {
        "commercialCabins": [probe.cabin],
        "bookingFlow": "LEISURE",
        "passengers": _passengers(cfg, probe),
        "requestedConnections": [
            {"departureDate": out_date,
             "origin": {"type": "AIRPORT", "code": origin},
             "destination": {"type": "AIRPORT", "code": destination}},
            {"departureDate": ret_date,
             "origin": {"type": "AIRPORT", "code": destination},
             "destination": {"type": "AIRPORT", "code": origin}},
        ],
    }


def _post(probe: Probe, key: str, body: dict[str, Any],
          *, tag: str, take: Callable[[], bool]) -> dict[str, Any] | None:
    """POST vers l'API compagnie. None si la cellule échoue sans gravité.

    `take` prend un jeton de quota et doit être appelé avant CHAQUE
    tentative HTTP, pas une fois par cellule : un rejeu après 429 ou 5xx
    est une requête de plus côté fournisseur, et la compter pour zéro
    faisait dériver notre compteur jusqu'à un facteur deux — sur une API
    qui ne renvoie aucun en-tête de quota, c'est notre seule mesure.

    Deux 403 bien distincts, et les confondre coûterait cher : « Developer
    Over Rate » signifie que le seau est vide (on arrête tout), « Developer
    Inactive » que la clé est absente ou révoquée (on arrête aussi, mais
    ce n'est pas la même cause et le message doit le dire).
    """
    session = _get_session()
    for attempt in range(1, AFKL_MAX_ATTEMPTS + 1):
        if not take():
            raise ProbeExhausted(f"{probe.bucket}: plafond local atteint")
        try:
            r = session.post(AFKL_URL, json=body,
                             headers=_headers(probe, key),
                             timeout=AFKL_TIMEOUT)
        except requests.RequestException as e:
            log.warning(f"    ⚠ {tag}: réseau ({e})")
            if attempt >= AFKL_MAX_ATTEMPTS:
                return None
            time.sleep(2.0)
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                log.warning(f"    ⚠ {tag}: réponse illisible")
                return None

        if r.status_code == 403:
            blob = (r.text or "")[:200]
            if "Over Rate" in blob or "Over Limit" in blob:
                raise ProviderExhausted(f"{probe.bucket}: quota épuisé "
                                        "côté fournisseur")
            raise ProbeAuthError(f"clé {probe.key_env} refusée ({blob})")

        if r.status_code == 429:
            # La cadence est déjà tenue par min_interval_s : un 429 est
            # une surprise, on laisse passer l'orage une fois.
            if attempt >= AFKL_MAX_ATTEMPTS:
                return None
            time.sleep(5.0)
            continue

        if 500 <= r.status_code < 600 and attempt < AFKL_MAX_ATTEMPTS:
            time.sleep(2.0)
            continue

        log.warning(f"    ⚠ {tag}: HTTP {r.status_code} {(r.text or '')[:160]}")
        return None
    return None


# ── Lecture de la réponse ────────────────────────────────────────


def _leg_index(leg: Any) -> dict[Any, dict]:
    """Les trajets sont normalisés à part et référencés par connectionId.

    L'API renvoie un objet indexé « 0 », « 1 »… et non un tableau : on
    accepte les deux écritures plutôt que de dépendre de la forme.
    """
    items = leg.values() if isinstance(leg, dict) else (leg or [])
    return {c.get("id"): c for c in items if isinstance(c, dict)}


def _conn_stats(conn: dict) -> tuple[float, int, list[str]] | None:
    segments = conn.get("segments") or []
    if not segments:
        return None
    duration = conn.get("duration")
    if duration is None:
        return None
    carriers: list[str] = []
    for s in segments:
        code = (((s.get("marketingFlight") or {}).get("carrier")
                 or {}).get("code"))
        if code and code not in carriers:
            carriers.append(code)
    return float(duration) / 60.0, len(segments) - 1, carriers


def _parse(payload: dict[str, Any], *, origin: str, destination: str,
           out_date: str, ret_date: str,
           max_fly_h: float) -> list[ProbeOffer]:
    legs = payload.get("connections") or []
    if len(legs) < 2:
        return []
    out_idx, ret_idx = _leg_index(legs[0]), _leg_index(legs[1])

    offers: list[ProbeOffer] = []
    for rec in payload.get("recommendations") or []:
        for fp in rec.get("flightProducts") or []:
            price = fp.get("price") or {}
            total = price.get("totalPrice")
            conns = fp.get("connections") or []
            if total is None or len(conns) < 2:
                continue
            out_c = out_idx.get(conns[0].get("connectionId"))
            ret_c = ret_idx.get(conns[1].get("connectionId"))
            if not out_c or not ret_c:
                continue
            out_stats, ret_stats = _conn_stats(out_c), _conn_stats(ret_c)
            if not out_stats or not ret_stats:
                continue
            out_h, out_stops, out_car = out_stats
            ret_h, ret_stops, ret_car = ret_stats
            # Même filtre que les autres sources : sans lui la sonde
            # remonte des trajets de 30 h que le reste du projet écarte.
            if max_fly_h and (out_h > max_fly_h or ret_h > max_fly_h):
                continue
            families = [str((c.get("fareFamily") or {}).get("code") or "")
                        for c in conns]
            offers.append(ProbeOffer(
                price=float(total),
                currency=str(price.get("currency") or "EUR"),
                origin=origin, destination=destination,
                outbound_date=out_date, return_date=ret_date,
                out_h=round(out_h, 2), ret_h=round(ret_h, 2),
                out_stops=out_stops, ret_stops=ret_stops,
                airlines="+".join(dict.fromkeys(out_car + ret_car)),
                fare_family="+".join(f for f in families if f),
            ))
    return offers


# ── Choix des cellules ───────────────────────────────────────────


def cells_of(combos: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Couples (aller, retour) valides, dans un ordre canonique stable."""
    return sorted((o, r) for o, rs in combos.items() for r in rs)


def grid_hash(trip: Trip, probe: Probe) -> str:
    """Empreinte de la FORME configurée de la grille.

    Surtout pas la liste des cellules encore réservables : celle-ci perd
    une entrée par jour à l'approche du départ, l'empreinte changeait donc
    quotidiennement et le curseur repartait de zéro à chaque run. Les
    retours tardifs n'étaient alors jamais interrogés, précisément dans
    les semaines où la grille sert le plus.
    """
    payload = json.dumps([
        list(trip.outbound_window), list(trip.return_window),
        trip.min_nights, trip.max_nights,
        sorted(probe.origins), sorted(probe.destinations),
    ], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _mid(window: tuple[str, str]) -> str | None:
    try:
        a, b = date.fromisoformat(window[0]), date.fromisoformat(window[1])
    except (ValueError, IndexError, TypeError):
        return None
    return (a + (b - a) / 2).isoformat()


def stable_anchor(trip: Trip,
                  cells: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Cellule de référence, calculée sur les fenêtres CONFIGURÉES.

    mid_combo() dérive des combos filtrés par la date du jour : l'ancre
    se déplaçait donc d'un jour sur l'autre et la série temporelle qu'elle
    est censée produire — un prix comparable d'un run à l'autre sur un
    produit identique — n'existait pas. On vise ici le milieu des fenêtres
    telles qu'elles sont écrites dans config.yml, avec repli sur la
    cellule encore réservable la plus proche.
    """
    if not cells:
        return None
    t_out, t_ret = _mid(trip.outbound_window), _mid(trip.return_window)
    if not t_out or not t_ret:
        return cells[len(cells) // 2]

    def gap(cell: tuple[str, str]) -> int:
        try:
            return (abs((date.fromisoformat(cell[0])
                         - date.fromisoformat(t_out)).days)
                    + abs((date.fromisoformat(cell[1])
                           - date.fromisoformat(t_ret)).days))
        except ValueError:
            return 10 ** 6

    return min(cells, key=gap)


def select_cells(probe: Probe, cells: list[tuple[str, str]],
                 cursor: dict[str, Any] | None, grid: str,
                 anchor: tuple[str, str] | None
                 ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Renvoie (ancre éventuelle, cellules en rotation).

    Le curseur mémorise la dernière CELLULE visitée, pas un indice : on la
    retrouve par bissection dans la liste du jour, ce qui absorbe la
    disparition des dates échues sans perdre la position.
    """
    if not cells:
        return [], []
    if probe.date_mode == "median":
        return ([anchor] if anchor else []), []

    # À cells_per_run = 1, l'ancre consommait le budget entier et la
    # rotation restait vide : la grille n'était jamais balayée, en
    # silence. Une seule cellule par run = une cellule qui tourne.
    head = [anchor] if (anchor and probe.cells_per_run > 1) else []

    start = 0
    if cursor and cursor.get("grid_hash") == grid and cursor.get("last_out"):
        last = (str(cursor["last_out"]), str(cursor.get("last_ret") or ""))
        start = bisect.bisect_right(cells, last) % len(cells)

    want = max(0, probe.cells_per_run - len(head))
    rotation: list[tuple[str, str]] = []
    for i in range(len(cells)):
        if len(rotation) >= want:
            break
        cell = cells[(start + i) % len(cells)]
        # L'ancre n'est écartée de la rotation que si elle est DÉJÀ
        # interrogée en tête de run. Sans la garde `not head`, à
        # cells_per_run = 1 (où head est vide) la cellule médiane était
        # exclue à vie : un trou permanent et silencieux, exactement au
        # milieu de la grille, l'endroit le plus regardé.
        if (not head or cell != anchor) and cell not in rotation:
            rotation.append(cell)
    return head, rotation


# ── Exécution ────────────────────────────────────────────────────


def run_probe(cfg: Config, probe: Probe, trip: Trip,
              combos: dict[str, list[str]], *, now: str,
              to_eur: ToEur, deadline: float | None = None) -> dict[str, Any]:
    """Interroge une sonde sur une période. Ne lève jamais.

    Renvoie un compte-rendu : lignes persistées, requêtes émises, cellules
    ratées, motif d'arrêt. La sonde ne doit jamais faire échouer une
    période — les autres sources sont la vraie surveillance.
    """
    # `error` = panne qui demande une intervention (clé refusée, cellules
    # sans résultat). `stopped` = arrêt NORMAL (seau vide, budget de
    # temps) : les confondre faisait monter consecutive_failures et
    # afficher une panne à l'admin alors que la sonde avait bien travaillé.
    out: dict[str, Any] = {"rows": 0, "calls": 0, "failed": 0,
                           "exhausted": False, "error": None,
                           "stopped": None, "best": None}
    # Court-circuit en première ligne, comme search_duffel sans clé :
    # une sonde sans credential n'est pas une panne, c'est une sonde
    # désactivée.
    key = os.environ.get(probe.key_env)
    if not key:
        return out

    cells = cells_of(combos)
    if not cells:
        return out

    grid = grid_hash(trip, probe)
    with db.conn() as c:
        cursor = db.probe_cursor_get(c, probe.name, trip.name)
    anchor = stable_anchor(trip, cells)
    head, rotation = select_cells(probe, cells, cursor, grid, anchor)

    limit = cfg.bucket_quota(probe.bucket)
    today = date.today().isoformat()
    # Budget mural : les sondes tournent DANS la section critique du run
    # de marché, protégée par RUN_TIMEOUT. Une API tierce lente ne doit
    # pas pouvoir faire expirer un run dont elle n'est pas la raison
    # d'être — le curseur est justement conçu pour reprendre plus tard.
    if deadline is None:
        deadline = time.monotonic() + PROBE_BUDGET_S

    def take() -> bool:
        with db.conn() as c:
            ok = db.probe_quota_take(c, probe.bucket, limit)
        if ok:
            out["calls"] += 1
        return ok

    routes = [(o, d) for o in probe.origins for d in probe.destinations]
    last_rot: tuple[str, str] | None = None
    stop = False

    # Ordre CELLULE-MAJEURE : toutes les routes d'une cellule, puis la
    # cellule suivante. En ordre route-majeur, un épuisement du seau en
    # cours de run privait systématiquement les dernières routes — donc
    # à chaque run, donc tous les jours, sans que rien ne le signale.
    for cell in head + rotation:
        if stop:
            break
        out_date, ret_date = cell
        complete = True
        for origin, destination in routes:
            # `complete = False` et non `complete = stop = True` : la
            # forme chaînée laissait la variable toujours vraie, donc la
            # garde plus bas était une tautologie et le curseur dépassait
            # une cellule dont seule une partie des routes avait été
            # interrogée — les dernières routes n'étaient alors reprises
            # qu'au tour de grille suivant, soit des jours plus tard.
            if time.monotonic() > deadline:
                out["stopped"] = out["stopped"] or "budget de temps dépassé"
                complete = False
                stop = True
                break
            tag = f"{probe.name} {origin}→{destination} {out_date}/{ret_date}"
            try:
                payload = _post(probe, key,
                                _body(cfg, probe, origin, destination,
                                      out_date, ret_date),
                                tag=tag, take=take)
            except ProviderExhausted as e:
                out["exhausted"] = True
                out["provider_exhausted"] = True
                out["stopped"] = str(e)
                complete = False
                stop = True
                break
            except ProbeExhausted as e:
                out["exhausted"] = True
                out["stopped"] = str(e)
                complete = False
                stop = True
                break
            except ProbeAuthError as e:
                out["error"] = str(e)
                log.error(f"  ❌ {probe.name}: {e}")
                complete = False
                stop = True
                break

            rows_before = out["rows"]
            if payload is not None:
                _persist_best(payload, cfg, probe, trip, origin, destination,
                              out_date, ret_date, today=today, now=now,
                              to_eur=to_eur, out=out)
            if out["rows"] == rows_before:
                # Transport en échec, réponse illisible, ou aucune offre
                # convertible : la cellule est perdue et doit se voir.
                out["failed"] += 1
            # Cadence imposée par le fournisseur (Air France : 1 req/s).
            time.sleep(probe.min_interval_s)

        # Le curseur n'avance que sur une cellule ENTIÈREMENT traitée :
        # une coupure en milieu de cellule ne fait donc perdre aucune route.
        if complete and cell in rotation:
            last_rot = cell

    # Seul le refus du FOURNISSEUR met le seau en pause : notre propre
    # plafond se gère tout seul, et fermer la journée sur un 403 reçu
    # après minuit — peut-être encore dans SA journée de la veille —
    # condamnerait 24 h de relevés pour rien.
    if out.get("provider_exhausted"):
        until = (datetime.now() + timedelta(hours=PROBE_BLOCK_H)).isoformat()
        with db.conn() as c:
            db.probe_quota_block(c, probe.bucket, limit, until=until)

    # L'échec dominant n'est pas l'exception, c'est la requête qui revient
    # vide : sans ça consecutive_failures restait à zéro et last_error
    # était effacé alors que la sonde brûlait son quota pour rien.
    # `failed` compte des REQUÊTES (une par route et par cellule), pas
    # des cellules — le message doit dire la même chose que le compteur.
    error = out["error"]
    if error is None and out["calls"] and out["rows"] == 0:
        error = f"{out['failed']} requête(s) sans résultat exploitable"
    elif error is None and out["failed"] > out["rows"]:
        # Dégradation partielle : on la rend visible sans la compter
        # comme une panne (consecutive_failures repart de zéro).
        error = None
        log.warning(f"  ⚠ {probe.name}: {out['failed']} requête(s) perdues "
                    f"pour {out['rows']} relevé(s)")

    if out["calls"]:
        with db.conn() as c:
            db.probe_cursor_set(
                c, probe.name, trip.name,
                last_out=last_rot[0] if last_rot else (
                    cursor.get("last_out") if cursor else None),
                last_ret=last_rot[1] if last_rot else (
                    cursor.get("last_ret") if cursor else None),
                grid_hash=grid, now=now, error=error)
    return out


def _persist_best(payload: dict[str, Any], cfg: Config, probe: Probe,
                  trip: Trip, origin: str, destination: str,
                  out_date: str, ret_date: str, *, today: str, now: str,
                  to_eur: ToEur, out: dict[str, Any]) -> None:
    """Retient la meilleure offre convertible de la cellule, et elle seule.

    Une requête rend une dizaine d'offres ; en persister dix par cellule
    gonflerait la table sans rien apprendre, la grille ne lisant que le
    minimum par couple de dates.
    """
    offers = _parse(payload, origin=origin, destination=destination,
                    out_date=out_date, ret_date=ret_date,
                    max_fly_h=cfg.max_fly_duration_hours)
    best: ProbeOffer | None = None
    best_eur: float | None = None
    for offer in offers:
        eur = to_eur(offer.price, offer.currency)
        if eur is None:
            continue
        if best_eur is None or eur < best_eur:
            best, best_eur = offer, eur
    if best is None:
        return
    with db.conn() as c:
        db.insert_probe_check(c, {
            "check_date": today,
            "trip_name": trip.name,
            "probe": probe.name,
            "carrier": probe.travel_host,
            "origin": best.origin,
            "destination": best.destination,
            "price_local": best.price,
            "currency": best.currency,
            "price_eur": best_eur,
            "outbound_date": best.outbound_date,
            "return_date": best.return_date,
            "out_h": best.out_h,
            "ret_h": best.ret_h,
            "out_stops": best.out_stops,
            "ret_stops": best.ret_stops,
            "airlines": best.airlines,
            "fare_family": best.fare_family,
            "booking_url": "",
            "captured_at": now,
        })
    out["rows"] += 1
    # Meilleur prix du run, toutes cellules confondues : c'est la seule
    # base d'alerte légitime pour une sonde, qui ne voit qu'une compagnie
    # et ne peut donc rien dire du marché.
    current = out.get("best")
    if current is None or best_eur < current["price_eur"]:
        out["best"] = {
            "price_eur": best_eur,
            "price_local": best.price,
            "currency": best.currency,
            "origin": best.origin,
            "destination": best.destination,
            "outbound_date": best.outbound_date,
            "return_date": best.return_date,
            "airlines": best.airlines,
            "fare_family": best.fare_family,
            "out_stops": best.out_stops,
            "ret_stops": best.ret_stops,
        }


def product_hash(cfg: Config, probe: Probe) -> str:
    """Empreinte du PRODUIT mesuré par la sonde.

    Changer de compagnie, de cabine ou de nombre de passagers ne renomme
    pas la sonde : sans cette empreinte, son « plus bas » resterait celui
    d'un produit qu'elle ne mesure plus, et l'alerte basse ne se
    redéclencherait jamais. Même principe que trip_config_hash côté
    marché. Les fenêtres de dates en sont volontairement absentes : elles
    sont couvertes par grid_hash, qui pilote la rotation.
    """
    payload = json.dumps([
        probe.travel_host, probe.cabin, probe.passengers,
        sorted(probe.origins), sorted(probe.destinations),
        cfg.adults, sorted(cfg.children) if probe.passengers == "family" else [],
    ], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def probes_for(cfg: Config, trip: Trip) -> list[Probe]:
    """Sondes actives qui couvrent cette période."""
    return [p for p in cfg.probes
            if p.enabled and (not p.trips or trip.name in p.trips)]
