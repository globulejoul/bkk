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
    max_fly_duration_hours: int = 18
    schedule_cron: str = "0 7,19 * * *"  # 2x/day
    rolling_window_days: int = 14
    rise_threshold_pct: float = 0.10
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    trips: list[Trip] = field(default_factory=list)
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
        max_fly_duration_hours=data.get("max_fly_duration_hours", 18),
        schedule_cron=data.get("schedule_cron", "0 7,19 * * *"),
        rolling_window_days=data.get("rolling_window_days", 14),
        rise_threshold_pct=data.get("rise_threshold_pct", 0.10),
        ntfy=NtfyConfig(
            server=ntfy_data.get("server", "https://ntfy.sh"),
            topic=ntfy_data.get("topic"),
        ),
        trips=trips,
    )
