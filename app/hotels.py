"""Google Hotels scraper for specific hotel price monitoring.

Approche validée : recherche par nom → clic sur l'hôtel → extraction
des liens providers (booking.com, expedia, etc.) avec leurs prix.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class HotelPrice:
    """A single provider's price for a hotel."""
    source: str       # "Booking.com", "Agoda", "Hotels.com", etc.
    price: float
    currency: str     # "EUR", "THB", etc.
    url: str = ""


@dataclass
class HotelResult:
    """Aggregated result from Google Hotels for one hotel."""
    hotel_name: str
    checkin: str
    checkout: str
    nights: int
    prices: list[HotelPrice] = field(default_factory=list)
    best_price: float | None = None
    best_currency: str = "EUR"
    best_source: str = ""
    scraped_at: str = ""


_PROVIDERS = {
    "booking.com": "Booking.com",
    "agoda": "Agoda",
    "hotels.com": "Hotels.com",
    "expedia": "Expedia",
    "trip.com": "Trip.com",
    "traveloka": "Traveloka",
    "priceline": "Priceline",
    "orbitz": "Orbitz",
    "edreams": "eDreams",
}


def search_hotel(
    hotel_name: str,
    checkin: str,
    checkout: str,
    adults: int = 1,
    children: list[int] | None = None,
    currency: str = "EUR",
    entity_id: str = "",
) -> HotelResult | None:
    """Scrape Google Hotels pour un hôtel spécifique.

    Recherche par nom, clique sur le résultat, et extrait les prix
    par provider (Booking.com, Expedia, Trip.com, etc.).
    """
    checkin_dt = datetime.strptime(checkin, "%Y-%m-%d")
    checkout_dt = datetime.strptime(checkout, "%Y-%m-%d")
    nights = (checkout_dt - checkin_dt).days
    total_guests = adults + (len(children) if children else 0)

    print(f"  Hotels: {hotel_name} {checkin}→{checkout} "
          f"({nights}n, {total_guests} guests)")

    try:
        return _scrape_with_timeout(
            hotel_name, checkin, checkout, nights, total_guests,
            currency, timeout=60)
    except Exception as e:
        print(f"  Hotels error: {e}")
        return None


def _scrape_with_timeout(
    hotel_name: str, checkin: str, checkout: str,
    nights: int, guests: int, currency: str,
    timeout: int = 60,
) -> HotelResult | None:
    """Lance le scrape Playwright avec un timeout strict."""
    result_holder: list[HotelResult | None] = [None]
    error_holder: list[Exception | None] = [None]

    def _do_scrape():
        try:
            result_holder[0] = _scrape_hotel(
                hotel_name, checkin, checkout, nights, guests, currency)
        except Exception as e:
            error_holder[0] = e

    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        print(f"  Hotels: timeout après {timeout}s, abandon")
        return None
    if error_holder[0]:
        raise error_holder[0]
    return result_holder[0]


def _scrape_hotel(
    hotel_name: str, checkin: str, checkout: str,
    nights: int, guests: int, currency: str,
) -> HotelResult:
    """Scrape effectif via Playwright."""
    from playwright.sync_api import sync_playwright

    result = HotelResult(
        hotel_name=hotel_name,
        checkin=checkin,
        checkout=checkout,
        nights=nights,
        scraped_at=datetime.now().isoformat(),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            locale="fr-FR",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # 1) Recherche Google Hotels
        search_q = hotel_name.replace(" ", "+")
        url = (
            f"https://www.google.com/travel/search"
            f"?q={search_q}"
            f"&checkin={checkin}&checkout={checkout}"
            f"&guests={guests}&hl=fr"
        )
        page.goto(url, timeout=25000, wait_until="domcontentloaded")

        # Consentement cookies
        _handle_consent(page)
        page.wait_for_timeout(3000)

        # 2) Cliquer sur l'hôtel dans les résultats
        name_lower = hotel_name.lower()
        clicked = False
        for link in page.query_selector_all("a"):
            text = (link.text_content() or "").lower()
            # Matcher sur les mots-clés du nom
            words = name_lower.split()
            if sum(1 for w in words if w in text) >= len(words) // 2 + 1:
                link.click()
                clicked = True
                break

        if not clicked:
            print(f"  Hotels: '{hotel_name}' non trouvé dans les résultats")
            browser.close()
            return result

        page.wait_for_timeout(5000)

        # 3) Extraire les prix par provider depuis les liens
        for a in page.query_selector_all("a[href]"):
            try:
                href = a.get_attribute("href") or ""
                provider = _identify_provider(href)
                if not provider:
                    continue

                # Remonter au parent pour trouver le prix à côté du lien
                parent = a.evaluate_handle("el => el.closest('div, li, tr')")
                parent_text = parent.evaluate(
                    "el => el ? el.textContent : ''") if parent else ""
                parsed = _parse_price(parent_text)
                if parsed:
                    price_val, cur = parsed
                    if 1 < price_val < 50000:
                        result.prices.append(HotelPrice(
                            source=provider,
                            price=price_val,
                            currency=cur,
                            url=href,
                        ))
            except Exception:
                continue

        browser.close()

    # Dédupliquer par provider (garder le moins cher)
    seen: dict[str, HotelPrice] = {}
    for hp in result.prices:
        if hp.source not in seen or hp.price < seen[hp.source].price:
            seen[hp.source] = hp
    result.prices = list(seen.values())

    # Best price
    if result.prices:
        best = min(result.prices, key=lambda hp: hp.price)
        result.best_price = best.price
        result.best_currency = best.currency
        result.best_source = best.source

    print(f"  Hotels: {len(result.prices)} providers, "
          f"best={result.best_price} {result.best_currency} ({result.best_source})")

    return result


def _handle_consent(page) -> None:
    """Accepte la popup de consentement Google si présente."""
    try:
        for selector in [
            'button:has-text("Tout accepter")',
            'button:has-text("Accept all")',
            'button:has-text("Accepter tout")',
        ]:
            btn = page.query_selector(selector)
            if btn:
                btn.click()
                page.wait_for_timeout(2000)
                return
    except Exception:
        pass


def _identify_provider(href: str) -> str | None:
    """Identifie le provider depuis l'URL du lien."""
    href_lower = href.lower()
    for domain, name in _PROVIDERS.items():
        if domain in href_lower:
            return name
    return None


def _parse_price(text: str) -> tuple[float, str] | None:
    """Extrait un montant et une devise d'un texte."""
    if not text:
        return None
    # EUR patterns
    for pattern in [r'(\d[\d\s.,]*)\s*€', r'€\s*(\d[\d\s.,]*)']:
        m = re.search(pattern, text)
        if m:
            return _clean_amount(m.group(1)), "EUR"
    # THB
    for pattern in [r'(\d[\d\s.,]*)\s*฿', r'฿\s*(\d[\d\s.,]*)']:
        m = re.search(pattern, text)
        if m:
            return _clean_amount(m.group(1)), "THB"
    # USD
    for pattern in [r'\$\s*(\d[\d\s.,]*)', r'(\d[\d\s.,]*)\s*\$']:
        m = re.search(pattern, text)
        if m:
            return _clean_amount(m.group(1)), "USD"
    return None


def _clean_amount(s: str) -> float:
    """'3 500,00' ou '3,500.00' → 3500.0"""
    s = s.strip().replace('\u202f', '').replace('\xa0', '').replace(' ', '')
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        parts = s.split(',')
        if len(parts[-1]) <= 2:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    return float(s)
