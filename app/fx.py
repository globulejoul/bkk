"""Currency conversion via free ECB feeds."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

FRANKFURTER = "https://api.frankfurter.app/latest"
ER_API = "https://open.er-api.com/v6/latest"


# Les taux de change bougent de quelques dixièmes de pour cent par jour :
# les rafraîchir plus souvent n'apporte rien et, avec le flash mode qui
# tourne toutes les 5 minutes, cela ferait ~290 appels par jour vers
# frankfurter depuis l'IP du VPS.
_CACHE_TTL_S = 6 * 3600
_cache: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, float]]] = {}


def fetch_rates(base: str, targets: list[str]) -> dict[str, float]:
    """Return {currency: rate} (rate = how many <currency> per 1 <base>)."""
    targets = [t for t in targets if t != base]
    if not targets:
        return {base: 1.0}

    key = (base, tuple(sorted(targets)))
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_S:
        return dict(hit[1])
    # Try Frankfurter
    try:
        r = requests.get(FRANKFURTER, params={
            "from": base, "to": ",".join(targets)}, timeout=10)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        if rates:
            rates[base] = 1.0
            _cache[key] = (time.monotonic(), dict(rates))
            return rates
    except Exception:
        pass
    # Fallback: open.er-api.com
    try:
        r = requests.get(f"{ER_API}/{base}", timeout=10)
        r.raise_for_status()
        all_rates = r.json().get("rates", {})
        rates = {t: all_rates[t] for t in targets if t in all_rates}
        rates[base] = 1.0
        _cache[key] = (time.monotonic(), dict(rates))
        return rates
    except Exception:
        pass
    # Échec des deux sources : un taux périmé vaut mieux que « 1 EUR = 1 THB »,
    # qui faisait silencieusement passer des prix THB pour des euros.
    if hit:
        log.warning("  FX: sources indisponibles, taux en cache réutilisé")
        return dict(hit[1])
    log.error("  FX: aucun taux disponible, conversions non-EUR abandonnées")
    return {base: 1.0}


def to_eur(amount: float, currency: str,
           rates_from_eur: dict[str, float]) -> float | None:
    """Convert amount in given currency to EUR.
    rates_from_eur: 1 EUR = X <currency>."""
    if currency == "EUR":
        return amount
    rate = rates_from_eur.get(currency)
    if not rate or rate == 1.0:
        return None
    return amount / rate
