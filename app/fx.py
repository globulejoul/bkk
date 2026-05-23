"""Currency conversion via free ECB feeds."""
from __future__ import annotations

import requests

FRANKFURTER = "https://api.frankfurter.app/latest"
ER_API = "https://open.er-api.com/v6/latest"


def fetch_rates(base: str, targets: list[str]) -> dict[str, float]:
    """Return {currency: rate} (rate = how many <currency> per 1 <base>)."""
    targets = [t for t in targets if t != base]
    if not targets:
        return {base: 1.0}
    # Try Frankfurter
    try:
        r = requests.get(FRANKFURTER, params={
            "from": base, "to": ",".join(targets)}, timeout=10)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        if rates:
            rates[base] = 1.0
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
        return rates
    except Exception:
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
