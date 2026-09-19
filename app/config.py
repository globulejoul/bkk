"""Configuration loading and validation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

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

_IATA_RE = re.compile(r"^[A-Z]{3}$")

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
    # VPN proxy URL (set via env, not in config)
    vpn_proxy_url: str | None = None


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


def _as_date(value: Any) -> date | None:
    """Date exploitable, ou None si ce n'est pas une date ISO."""
    try:
        return date.fromisoformat(_as_date_str(value))
    except ValueError:
        return None


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(f"  ⚠ config: {msg}")


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
    return errs


# ── Lecture / écriture ───────────────────────────────────────


def _dump(data: dict) -> str:
    return yaml.dump(data, default_flow_style=False,
                     allow_unicode=True, sort_keys=False)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        print(f"  ⚠ config: {path} illisible ({e})")
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
        print(f"  ⚠ config: {CONFIG_PATH} inexploitable, "
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
            outbound_window=tuple(_as_date_str(d)
                                  for d in t["outbound_window"]),
            return_window=tuple(_as_date_str(d) for d in t["return_window"]),
            price_threshold=t.get("price_threshold"),
            min_nights=t.get("min_nights"),
            max_nights=t.get("max_nights"),
            enabled=t.get("enabled", True),
        )
        for t in data.get("trips", [])
    ]

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
        print(f"  ⚠ config: sauvegarde {BACKUP_PATH} impossible ({e})")


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
