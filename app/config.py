"""Configuration loading and validation."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Chemins surchargeables par l'environnement : les défauts reproduisent
# le montage Docker (aucune régression de déploiement), la surcharge rend
# l'app lançable et testable hors conteneur.
CONFIG_PATH = Path(os.environ.get("BKK_CONFIG", "/app/config.yml"))
# config.yml est bind-monté comme fichier unique : impossible d'y faire un
# tmp+rename atomique. On garde donc une copie dans le dossier data (monté
# comme dossier) pour survivre à une coupure pendant l'écriture.
BACKUP_PATH = Path(os.environ.get(
    "BKK_CONFIG_BAK", str(CONFIG_PATH.parent / "data" / "config.yml.bak")))

# Bornes de cardinalité : le volume d'un run est multiplicatif
# (origines × destinations × dates aller × dates retour) et chaque hôtel
# lance un Chromium. Sans plafond, un seul PUT rend les runs interminables.
MAX_ORIGINS = 8
MAX_DESTINATIONS = 5
MAX_TRIPS = 20
MAX_HOTELS = 10
MAX_WINDOW_DAYS = 31

# Sondes : les plafonds viennent du quota de l'API interrogée, pas du
# temps de run. Air France Open Data donne 100 requêtes/jour pour toute
# la clé (mesuré : les hosts AF et KL partagent le même seau), et une
# requête ne couvre qu'un couple origine/destination sur une paire de
# dates. Un périmètre large vide le quota avant la fin du premier run.
MAX_PROBES = 4
MAX_PROBE_ORIGINS = 3
MAX_PROBE_DESTS = 3
MAX_PROBE_CELLS = 12
DEFAULT_BUCKET_QUOTA = 80          # 80 sur 100 : marge pour run-now et retries

PROBE_ADAPTERS = {"afklm"}
PROBE_HOSTS = {"AF", "KL"}
PROBE_CABINS = {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS"}
PROBE_DATE_MODES = {"grid", "median"}
PROBE_PAX = {"adults", "family"}

_IATA_RE = re.compile(r"^[A-Z]{3}$")
# Nom de variable d'environnement, jamais la clé elle-même. Une vraie clé
# Air France (minuscules + chiffres) échoue sur ce motif : c'est
# volontaire, config.yml est servi au navigateur par GET /api/admin/config.
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")

# load() est appelé à chaque requête HTTP : on ne répète pas les mêmes
# avertissements dans les logs.
_warned: set[str] = set()


@dataclass
class Trip:
    name: str
    outbound_window: tuple[str, str]
    return_window: tuple[str, str]
    price_threshold: float | None = None
    min_nights: int | None = None
    max_nights: int | None = None
    enabled: bool = True


@dataclass
class HotelWatch:
    name: str
    entity_id: str
    checkin: str = ""       # YYYY-MM-DD
    checkout: str = ""      # YYYY-MM-DD
    price_threshold: float | None = None
    enabled: bool = True


@dataclass
class Probe:
    """Interrogation directe de l'API d'une compagnie, périmètre restreint.

    Une sonde ne remplace aucune source : elle mesure UNE compagnie sur
    UNE route, pour reconstruire la grille de dates que fli ne donne pas
    (il ne sonde que la date médiane). Ses relevés vivent dans
    `probe_checks` et ne touchent ni l'état, ni les alertes.

    La clé d'API vit en variable d'environnement — `key_env` n'en porte
    que le NOM.
    """
    name: str
    adapter: str = "afklm"
    travel_host: str = "AF"          # AF | KL, en-tête AFKL-TRAVEL-Host
    key_env: str = "AFKL_API_KEY"
    origins: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    trips: list[str] = field(default_factory=list)   # vide = toutes
    date_mode: str = "grid"          # grid | median
    cells_per_run: int = 4
    min_interval_s: float = 1.2
    cabin: str = "ECONOMY"
    passengers: str = "adults"       # adults | family
    enabled: bool = True

    @property
    def bucket(self) -> str:
        """Le seau de quota EST le credential.

        Mesuré : les hosts AF et KL d'une même clé Air France puisent
        dans les mêmes 100 requêtes/jour. Laisser l'utilisateur nommer
        librement son seau permettait à deux sondes sur la même clé d'en
        déclarer deux, donc d'obtenir 160 requêtes pour un quota réel de
        100. Le nom de la variable d'environnement lève l'ambiguïté.
        """
        return self.key_env


@dataclass
class NtfyConfig:
    server: str = "https://ntfy.sh"
    topic: str | None = None


@dataclass
class Config:
    origins: list[str]
    destinations: list[str]
    currency: str = "EUR"
    currencies: list[str] = field(default_factory=lambda: ["EUR"])
    adults: int = 1
    children: list[int] = field(default_factory=list)  # ages
    max_fly_duration_hours: int = 18
    schedule_cron: str = "0 7,19 * * *"  # 2x/day
    rolling_window_days: int = 14
    rise_threshold_pct: float = 0.10
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    trips: list[Trip] = field(default_factory=list)
    hotels: list[HotelWatch] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    # Plafond journalier par seau. La clé du seau est le CREDENTIAL, pas
    # la sonde : deux sondes sur la même clé ne peuvent pas déclarer deux
    # plafonds, sinon celui réellement appliqué dépend de l'ordre d'appel.
    quota_buckets: dict[str, int] = field(default_factory=dict)
    # VPN proxy URL (set via env, not in config)
    vpn_proxy_url: str | None = None

    def bucket_quota(self, bucket: str) -> int:
        return int(self.quota_buckets.get(bucket, DEFAULT_BUCKET_QUOTA))


def _as_date_str(value: Any) -> str:
    """Normalise une date en 'YYYY-MM-DD'.

    PyYAML résout `checkin: 2027-02-13` (sans guillemets) en
    `datetime.date`, ce qui faisait ensuite échouer strptime et rendait
    l'hôtel invisible. On accepte les deux écritures.
    """
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value).strip()[:10]


def _as_int(value: Any, default: int) -> int:
    """Entier tolérant : load() est appelé à CHAQUE requête HTTP.

    Un `cells_per_run: abc` saisi à la main ferait lever int() et
    emporterait toute l'API, pas seulement la sonde fautive. Le reste du
    fichier est durci de la même façon ; _config_errors dit pourquoi.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_date(value: Any) -> date | None:
    """Date exploitable, ou None si ce n'est pas une date ISO."""
    try:
        return date.fromisoformat(_as_date_str(value))
    except ValueError:
        return None


def _window(t: dict, key: str) -> tuple[str, str]:
    """Fenêtre [début, fin] garantie en deux chaînes 'YYYY-MM-DD'.

    Une borne non citée dans config.yml arrive en `datetime.date`, et une
    fenêtre malformée (scalaire, un seul élément) faisait planter load()
    — donc toute l'API, pas seulement la période fautive. On renvoie une
    fenêtre vide, traitée partout comme échue : la période est ignorée et
    _config_errors a déjà dit pourquoi dans les logs.
    """
    w = t.get(key)
    bornes = ((_as_date_str(w[0]), _as_date_str(w[1]))
              if isinstance(w, (list, tuple)) and len(w) == 2
              else ("", ""))
    # Date passée plutôt que chaîne vide : seule la fenêtre ALLER est
    # testée pour l'expiration, donc un retour vide arrivait jusqu'à
    # date_range() qui levait sur strptime("") à chaque run.
    return tuple(b or "1970-01-01" for b in bornes)  # type: ignore[return-value]


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        log.warning(f"  ⚠ config: {msg}")


# ── Validation ───────────────────────────────────────────────


def _trip_errors(t: dict) -> list[str]:
    """Cohérence des quatre bornes d'une période.

    Une fenêtre inversée ne produit aucune date à parcourir (période
    muette) et un retour antérieur à l'aller fait partir des centaines de
    requêtes en 422 : les deux échouaient sans le moindre message.
    """
    name = t.get("name") or "?"
    errs: list[str] = []
    windows: dict[str, tuple[date, date]] = {}
    for key in ("outbound_window", "return_window"):
        w = t.get(key) or []
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            errs.append(f"Trip {name}: {key} doit être [début, fin]")
            continue
        start, end = _as_date(w[0]), _as_date(w[1])
        if start is None or end is None:
            errs.append(f"Trip {name}: {key} contient une date illisible "
                        "(attendu AAAA-MM-JJ)")
            continue
        if start > end:
            errs.append(f"Trip {name}: {key} est inversée "
                        f"({start} après {end})")
            continue
        span = (end - start).days + 1
        if span > MAX_WINDOW_DAYS:
            errs.append(f"Trip {name}: {key} couvre {span} jours, "
                        f"maximum {MAX_WINDOW_DAYS}")
            continue
        windows[key] = (start, end)

    out = windows.get("outbound_window")
    ret = windows.get("return_window")
    # Tolérant aux fenêtres qui se chevauchent (aller flexible + contrainte
    # min_nights) : on exige seulement qu'un retour reste possible.
    if out and ret and ret[1] <= out[0]:
        errs.append(f"Trip {name}: la fenêtre retour se termine ({ret[1]}) "
                    f"avant le premier aller ({out[0]})")

    # min > max ne produit aucune combinaison de dates : la période
    # devient muette run après run, sans le moindre message.
    mn, mx = t.get("min_nights"), t.get("max_nights")
    if isinstance(mn, int) and isinstance(mx, int) and mn > mx:
        errs.append(f"Trip {name}: min_nights ({mn}) dépasse "
                    f"max_nights ({mx}), aucun séjour possible")
    return errs


def _hotel_errors(h: dict) -> list[str]:
    name = h.get("name") or "?"
    errs: list[str] = []
    ci, co = _as_date(h.get("checkin")), _as_date(h.get("checkout"))
    for key, parsed in (("checkin", ci), ("checkout", co)):
        if h.get(key) and parsed is None:
            errs.append(f"Hotel {name}: {key} illisible (attendu AAAA-MM-JJ)")
    # Un checkout antérieur ou égal donnait un nombre de nuits ≤ 0, affiché
    # tel quel dans l'UI et dans la notification.
    if ci and co and ci >= co:
        errs.append(f"Hotel {name}: check-in doit précéder le check-out")
    return errs


def _probe_errors(p: dict) -> list[str]:
    """Défauts structurels d'une sonde. Bloquants : elle est éditable
    depuis l'admin, donc l'erreur doit revenir à l'écran, pas dans un log.

    Le budget de requêtes, lui, n'est PAS ici : un dépassement ne doit
    jamais refuser une sauvegarde admin (cf. _probe_budget_warnings).
    """
    name = str(p.get("name") or "?")
    errs: list[str] = []
    if not p.get("name") or len(name) > 40:
        errs.append(f"Sonde {name}: nom requis, 40 caractères maximum")

    if p.get("adapter", "afklm") not in PROBE_ADAPTERS:
        errs.append(f"Sonde {name}: adapter « {p.get('adapter')} » inconnu "
                    f"(disponible : {', '.join(sorted(PROBE_ADAPTERS))})")
    if p.get("travel_host", "AF") not in PROBE_HOSTS:
        errs.append(f"Sonde {name}: travel_host doit valoir "
                    f"{' ou '.join(sorted(PROBE_HOSTS))}")
    if p.get("cabin", "ECONOMY") not in PROBE_CABINS:
        errs.append(f"Sonde {name}: cabine « {p.get('cabin')} » inconnue")
    if p.get("date_mode", "grid") not in PROBE_DATE_MODES:
        errs.append(f"Sonde {name}: date_mode doit valoir grid ou median")
    if p.get("passengers", "adults") not in PROBE_PAX:
        errs.append(f"Sonde {name}: passengers doit valoir adults ou family")

    # Une clé d'API collée ici partirait au navigateur via
    # GET /api/admin/config : on n'accepte qu'un nom de variable d'env.
    # Absent = valeur par défaut du dataclass, ce n'est pas une erreur.
    if p.get("key_env") is not None and not _ENV_RE.match(str(p["key_env"])):
        errs.append(f"Sonde {name}: key_env doit être un NOM de variable "
                    "d'environnement en majuscules (ex. AFKL_API_KEY), "
                    "jamais la clé elle-même")

    # Tout champ qui ressemble à un secret est refusé, quel que soit son
    # nom : ce bloc est servi au navigateur et réécrit dans config.yml.
    for champ in p:
        if champ != "key_env" and re.search(
                r"key|secret|token|password|passwd", str(champ), re.I):
            errs.append(f"Sonde {name}: champ « {champ} » interdit — les "
                        "secrets vivent en variable d'environnement")

    for key, cap in (("origins", MAX_PROBE_ORIGINS),
                     ("destinations", MAX_PROBE_DESTS)):
        codes = p.get(key) or []
        if not codes:
            errs.append(f"Sonde {name}: {key} ne peut pas être vide")
        elif len(codes) > cap:
            errs.append(f"Sonde {name}: {len(codes)} {key}, maximum {cap} "
                        "(le quota de l'API est le facteur limitant)")
        for code in codes:
            if not _IATA_RE.match(str(code).strip().upper()):
                errs.append(f"Sonde {name}: « {code} » n'est pas un code IATA")

    cells = p.get("cells_per_run", 4)
    if not isinstance(cells, int) or not 1 <= cells <= MAX_PROBE_CELLS:
        errs.append(f"Sonde {name}: cells_per_run doit être entre 1 et "
                    f"{MAX_PROBE_CELLS}")
    interval = p.get("min_interval_s", 1.2)
    if not isinstance(interval, (int, float)) or not 0.5 <= interval <= 10:
        errs.append(f"Sonde {name}: min_interval_s doit être entre 0.5 et 10")

    # Une période inconnue N'EST PAS une erreur bloquante : elle rendrait
    # la sonde muette, ce qui équivaut à enabled: false et n'abîme rien.
    # En revanche save_raw refuse toute la config sur la moindre erreur —
    # renommer une période depuis l'admin aurait donc verrouillé la page
    # entière pour une sonde orpheline. Avertissement, voir
    # _probe_budget_warnings.
    return errs


def _config_errors(data: dict) -> list[str]:
    """Défauts qui rendent la surveillance muette, fausse ou explosive."""
    errs: list[str] = []
    for key, cap in (("origins", MAX_ORIGINS),
                     ("destinations", MAX_DESTINATIONS)):
        codes = data.get(key) or []
        if len(codes) > cap:
            errs.append(f"{key}: {len(codes)} entrées, maximum {cap}")
        for code in codes:
            if not _IATA_RE.match(str(code).strip().upper()):
                errs.append(f"{key}: « {code} » n'est pas un code IATA "
                            "à 3 lettres")

    trips = data.get("trips") or []
    if len(trips) > MAX_TRIPS:
        errs.append(f"trips: {len(trips)} périodes, maximum {MAX_TRIPS}")
    for t in trips:
        if isinstance(t, dict):
            errs += _trip_errors(t)

    hotels = data.get("hotels") or []
    if len(hotels) > MAX_HOTELS:
        errs.append(f"hotels: {len(hotels)} hôtels, maximum {MAX_HOTELS}")
    for h in hotels:
        if isinstance(h, dict):
            errs += _hotel_errors(h)

    probes = data.get("probes") or []
    if len(probes) > MAX_PROBES:
        errs.append(f"probes: {len(probes)} sondes, maximum {MAX_PROBES}")
    seen: set[str] = set()
    for p in probes:
        if not isinstance(p, dict):
            continue
        errs += _probe_errors(p)
        nm = str(p.get("name") or "")
        if nm and nm in seen:
            errs.append(f"probes: deux sondes portent le nom « {nm} »")
        seen.add(nm)
    return errs


def _probe_budget_warnings(probes: list[Probe], trips: list[Trip],
                           quota_buckets: dict[str, int],
                           schedule_cron: str) -> list[str]:
    """Le budget est un AVERTISSEMENT, jamais un refus d'écriture.

    save_raw lève sur la moindre erreur, toutes sections confondues :
    rendre le budget bloquant aurait suffi à refuser toute sauvegarde
    admin — y compris un simple changement de cron — et à ne plus rien
    rendre modifiable sans SSH. La vraie protection est le seau, qui
    arrête la sonde net quand il est vide.
    """
    runs = _runs_per_day(schedule_cron)
    known = {t.name for t in trips}
    warns: list[str] = []
    per_bucket: dict[str, int] = {}

    for p in probes:
        # Une période inconnue rend la sonde muette : jamais bloquant,
        # mais il faut le dire, sinon la sonde ne tourne simplement pas.
        for tn in p.trips:
            if tn not in known:
                warns.append(f"sonde « {p.name} » : période « {tn} » "
                             "inconnue, la sonde ne tournera pas")
        if not p.enabled:
            continue
        nb_trips = len([t for t in trips if t.enabled
                        and (not p.trips or t.name in p.trips)])
        if not nb_trips:
            continue
        cells = 1 if p.date_mode == "median" else p.cells_per_run
        cost = cells * len(p.origins) * len(p.destinations) * nb_trips * runs
        per_bucket[p.bucket] = per_bucket.get(p.bucket, 0) + cost

    # Un seau déclaré qui ne correspond au key_env d'aucune sonde ne
    # plafonne rien : le vrai seau retombe silencieusement sur le défaut.
    # Cause la plus probable : une faute de frappe sur le nom de variable.
    for bucket in quota_buckets:
        if bucket not in {p.key_env for p in probes}:
            warns.append(
                f"quota_buckets déclare « {bucket} », qui n'est le key_env "
                "d'aucune sonde : ce plafond ne s'applique à rien")

    for bucket, cost in per_bucket.items():
        limit = int(quota_buckets.get(bucket, DEFAULT_BUCKET_QUOTA))
        if cost > limit:
            names = ", ".join(p.name for p in probes
                              if p.enabled and p.bucket == bucket)
            warns.append(
                f"seau « {bucket} » : {names} demandent au MINIMUM {cost} "
                f"requêtes/jour (hors rejeux) pour un plafond de {limit}. "
                "Les cellules au-delà du plafond seront simplement "
                "abandonnées jusqu'au lendemain.")
    return warns


def _cron_field_count(field_txt: str, period: int) -> int:
    """Nombre de déclenchements d'un champ cron sur sa période.

    Gère « * », « */n », les listes « 7,19 » et les intervalles « 8-20 ».
    On ne réimplémente pas croniter : il s'agit seulement de savoir si le
    budget d'une sonde tient dans le quota de son API.
    """
    field_txt = (field_txt or "*").strip()
    total = 0
    for part in field_txt.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, _, step_txt = part.partition("/")
            step = _as_int(step_txt, 1) or 1
            part = base.strip() or "*"
        if part == "*":
            total += max(1, period // step)
            continue
        if "-" in part:
            lo_txt, _, hi_txt = part.partition("-")
            lo, hi = _as_int(lo_txt, 0), _as_int(hi_txt, 0)
            total += max(1, ((hi - lo) // step) + 1) if hi >= lo else 1
            continue
        total += 1
    return max(1, total)


def _runs_per_day(cron: str) -> int:
    """Déclenchements quotidiens, minutes ET heures.

    Ignorer le champ des minutes sous-estimait grossièrement : un cron
    « */30 * * * * » fait 48 runs par jour, pas 24 — et le budget d'une
    sonde en dépend directement.
    """
    parts = str(cron or "").split()
    if len(parts) < 2:
        return 4
    return _cron_field_count(parts[0], 60) * _cron_field_count(parts[1], 24)


# ── Lecture / écriture ───────────────────────────────────────


# PyYAML ne sait pas conserver les commentaires : au premier
# « Sauvegarder » depuis l'admin, config.yml perdait toute sa doc inline,
# y compris l'unité des champs qui ne sont PAS éditables depuis l'admin.
# On réécrit donc cet en-tête constant en tête du fichier à chaque
# sauvegarde. Il documente ces champs-là ; la référence complète et
# commentée reste config.example.yml.
_HEADER = """\
# ═══════════════════════════════════════════════════════════════
# Bangkok Watch — config.yml
#
# CE FICHIER EST RÉGÉNÉRÉ par la page Admin : tout commentaire ajouté à
# la main y sera effacé à la prochaine sauvegarde. Référence commentée :
# config.example.yml.
#
# Éditable depuis l'admin : origins, destinations, adults, children,
# max_fly_duration_hours, schedule_cron, trips, hotels, probes.
#
# Les sondes (probes) ne portent QUE le nom de la variable d'environnement
# qui contient la clé (key_env), jamais la clé : ce fichier est servi au
# navigateur par GET /api/admin/config.
#
# À éditer à la main uniquement (non exposés par l'admin) :
#   currency / currencies  devise de référence et devises comparées
#   rolling_window_days    fenêtre glissante du plus bas, en jours
#   rise_threshold_pct     FRACTION, pas un pourcentage : 0.10 = +10 %
#   ntfy.server / topic    le topic est un secret (lecture ET écriture)
#
# Les dates se mettent entre guillemets ("2026-10-14") : sans elles YAML
# les résout en objets date.
# ═══════════════════════════════════════════════════════════════

"""


def _dump(data: dict) -> str:
    return _HEADER + yaml.dump(data, default_flow_style=False,
                               allow_unicode=True, sort_keys=False)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        log.warning(f"  ⚠ config: {path} illisible ({e})")
        return None


def _usable(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("origins"))


def _read_config_data() -> dict:
    """Lit config.yml, avec repli sur la copie de secours.

    Une coupure pendant la réécriture en place laisse un YAML tronqué :
    safe_load renvoyait alors None et le service repartait en boucle de
    crash jusqu'à intervention SSH.
    """
    if not CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")

    data = _read_yaml(CONFIG_PATH) if CONFIG_PATH.exists() else None
    if _usable(data):
        return data

    backup = _read_yaml(BACKUP_PATH) if BACKUP_PATH.exists() else None
    if _usable(backup):
        log.error(f"  ⚠ config: {CONFIG_PATH} inexploitable, "
              f"repli sur {BACKUP_PATH}")
        return backup

    raise ValueError("Config inexploitable et sans sauvegarde valide : "
                     f"{CONFIG_PATH}")


def load() -> Config:
    data = _read_config_data()
    # Avertissement seulement : une config déjà en place doit continuer à
    # tourner. Le refus dur est du ressort de save_raw (422 côté admin).
    for err in _config_errors(data):
        _warn_once(err)

    trips = [
        Trip(
            name=t["name"],
            outbound_window=_window(t, "outbound_window"),
            return_window=_window(t, "return_window"),
            price_threshold=t.get("price_threshold"),
            min_nights=t.get("min_nights"),
            max_nights=t.get("max_nights"),
            enabled=t.get("enabled", True),
        )
        for t in data.get("trips", [])
    ]

    probes = [
        Probe(
            name=str(p["name"]),
            adapter=str(p.get("adapter") or "afklm"),
            travel_host=str(p.get("travel_host") or "AF").upper(),
            key_env=str(p.get("key_env") or "AFKL_API_KEY"),
            origins=[str(o).strip().upper() for o in p.get("origins") or []],
            destinations=[str(d).strip().upper()
                          for d in p.get("destinations") or []],
            trips=[str(t) for t in p.get("trips") or []],
            date_mode=str(p.get("date_mode") or "grid"),
            cells_per_run=_as_int(p.get("cells_per_run"), 4),
            min_interval_s=_as_float(p.get("min_interval_s"), 1.2),
            cabin=str(p.get("cabin") or "ECONOMY").upper(),
            passengers=str(p.get("passengers") or "adults"),
            enabled=p.get("enabled", True),
        )
        for p in data.get("probes", []) or []
        if isinstance(p, dict) and p.get("name")
    ]
    raw_buckets = data.get("quota_buckets")
    quota_buckets = ({str(k): _as_int(v, DEFAULT_BUCKET_QUOTA)
                      for k, v in raw_buckets.items()}
                     if isinstance(raw_buckets, dict) else {})
    for warn in _probe_budget_warnings(
            probes, trips, quota_buckets,
            data.get("schedule_cron", "0 7,19 * * *")):
        _warn_once(warn)

    ntfy_data = data.get("ntfy") or {}
    return Config(
        origins=data["origins"],
        destinations=data["destinations"],
        currency=data.get("currency", "EUR"),
        currencies=data.get("currencies", ["EUR"]),
        adults=data.get("adults", 1),
        children=data.get("children", []),
        max_fly_duration_hours=data.get("max_fly_duration_hours", 18),
        schedule_cron=data.get("schedule_cron", "0 7,19 * * *"),
        rolling_window_days=data.get("rolling_window_days", 14),
        rise_threshold_pct=data.get("rise_threshold_pct", 0.10),
        ntfy=NtfyConfig(
            server=ntfy_data.get("server", "https://ntfy.sh"),
            topic=ntfy_data.get("topic"),
        ),
        trips=trips,
        probes=probes,
        quota_buckets=quota_buckets,
        hotels=[
            HotelWatch(
                name=h["name"],
                entity_id=str(h.get("entity_id") or ""),
                checkin=_as_date_str(h.get("checkin")),
                checkout=_as_date_str(h.get("checkout")),
                price_threshold=h.get("price_threshold"),
                enabled=h.get("enabled", True),
            )
            for h in data.get("hotels", [])
        ],
    )


def load_raw() -> dict:
    """Load raw YAML data for editing."""
    return _read_config_data()


def _write_backup(content: str) -> None:
    """Copie de secours, écrite AVANT config.yml.

    Le dossier data est monté comme dossier : on peut y faire un
    tmp+rename atomique. Si l'écriture en place de config.yml est coupée,
    la config voulue reste récupérable et est rechargée automatiquement.
    """
    try:
        BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = BACKUP_PATH.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, BACKUP_PATH)
    except OSError as e:
        # Une copie ratée n'est pas une raison de refuser une config valide.
        log.warning(f"  ⚠ config: sauvegarde {BACKUP_PATH} impossible ({e})")


def save_raw(data: dict) -> None:
    """Write config back to YAML, preserving structure."""
    # Validate before writing
    if not data.get("origins") or not isinstance(data["origins"], list):
        raise ValueError("origins must be a non-empty list")
    if not data.get("destinations") or not isinstance(data["destinations"], list):
        raise ValueError("destinations must be a non-empty list")
    data["origins"] = [str(o).strip().upper() for o in data["origins"]]
    data["destinations"] = [str(d).strip().upper()
                            for d in data["destinations"]]

    for t in data.get("trips", []):
        if not t.get("name"):
            raise ValueError("Each trip must have a name")
        # Réécrit les bornes en chaînes, même raison que pour les hôtels.
        for key in ("outbound_window", "return_window"):
            w = t.get(key)
            if isinstance(w, (list, tuple)):
                t[key] = [_as_date_str(d) for d in w]

    for p in data.get("probes", []) or []:
        if not p.get("name"):
            raise ValueError("Chaque sonde doit avoir un nom")
        # Normalisé avant validation : « cdg » saisi en minuscules dans
        # l'admin échouerait sinon sur le motif IATA.
        for key in ("origins", "destinations"):
            p[key] = [str(v).strip().upper() for v in p.get(key) or []
                      if str(v).strip()]
        p["travel_host"] = str(p.get("travel_host") or "AF").strip().upper()
        p["cabin"] = str(p.get("cabin") or "ECONOMY").strip().upper()

    for h in data.get("hotels", []):
        if not h.get("name"):
            raise ValueError("Each hotel must have a name")
        # Réécrit les dates en chaînes : évite que yaml.dump les
        # ressorte sans guillemets et qu'elles reviennent en objets date.
        for key in ("checkin", "checkout"):
            h[key] = _as_date_str(h.get(key))

    errors = _config_errors(data)
    if errors:
        raise ValueError(" ; ".join(errors))

    content = _dump(data)
    _write_backup(content)
    CONFIG_PATH.write_text(content, encoding="utf-8")
