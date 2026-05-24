"""Configuration loading and validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path("/app/config.yml")


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


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text())

    trips = [
        Trip(
            name=t["name"],
            outbound_window=tuple(t["outbound_window"]),
            return_window=tuple(t["return_window"]),
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
                entity_id=h["entity_id"],
                checkin=h.get("checkin", ""),
                checkout=h.get("checkout", ""),
                price_threshold=h.get("price_threshold"),
                enabled=h.get("enabled", True),
            )
            for h in data.get("hotels", [])
        ],
    )


def load_raw() -> dict:
    """Load raw YAML data for editing."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text())


def save_raw(data: dict) -> None:
    """Write config back to YAML, preserving structure."""
    # Validate before writing
    if not data.get("origins") or not isinstance(data["origins"], list):
        raise ValueError("origins must be a non-empty list")
    if not data.get("destinations") or not isinstance(data["destinations"], list):
        raise ValueError("destinations must be a non-empty list")
    for t in data.get("trips", []):
        if not t.get("name"):
            raise ValueError("Each trip must have a name")
        if not t.get("outbound_window") or len(t["outbound_window"]) != 2:
            raise ValueError(f"Trip {t['name']}: outbound_window must be [start, end]")
        if not t.get("return_window") or len(t["return_window"]) != 2:
            raise ValueError(f"Trip {t['name']}: return_window must be [start, end]")

    CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False,
                                      allow_unicode=True, sort_keys=False))
