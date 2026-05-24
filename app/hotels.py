"""Google Hotels scraper for specific hotel price monitoring."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
    rating: float | None = None
    scraped_at: str = ""


# Regex pour extraire les prix (supporte EUR, THB, USD, etc.)
_PRICE_RE = re.compile(
    r'(?:€|EUR)\s*([\d\s,.]+)|'           # €123 ou EUR 123
    r'([\d\s,.]+)\s*(?:€|EUR)|'           # 123€ ou 123 EUR
    r'(?:฿|THB)\s*([\d\s,.]+)|'           # ฿3,500 ou THB 3500
    r'([\d\s,.]+)\s*(?:฿|THB)|'           # 3500฿
    r'(?:\$|USD)\s*([\d\s,.]+)|'          # $123
    r'([\d\s,.]+)\s*(?:\$|USD)'           # 123$
)

# Providers connus sur Google Hotels
_KNOWN_PROVIDERS = [
    "Booking.com", "Agoda", "Hotels.com", "Expedia",
    "Trip.com", "Traveloka", "Prestigia", "ZenHotels",
    "Priceline", "Orbitz", "eDreams", "Kayak",
    "Official Site", "Site officiel",
]


def _parse_price(text: str) -> tuple[float, str] | None:
    """Extrait un montant et une devise d'un texte."""
    text = text.strip()
    # EUR
    for pattern in [r'€\s*([\d\s.,]+)', r'([\d\s.,]+)\s*€',
                    r'EUR\s*([\d\s.,]+)', r'([\d\s.,]+)\s*EUR']:
        m = re.search(pattern, text)
        if m:
            return _clean_amount(m.group(1)), "EUR"
    # THB
    for pattern in [r'฿\s*([\d\s.,]+)', r'([\d\s.,]+)\s*฿',
                    r'THB\s*([\d\s.,]+)', r'([\d\s.,]+)\s*THB']:
        m = re.search(pattern, text)
        if m:
            return _clean_amount(m.group(1)), "THB"
    # USD
    for pattern in [r'\$\s*([\d\s.,]+)', r'([\d\s.,]+)\s*\$',
                    r'USD\s*([\d\s.,]+)', r'([\d\s.,]+)\s*USD']:
        m = re.search(pattern, text)
        if m:
            return _clean_amount(m.group(1)), "USD"
    return None


def _clean_amount(s: str) -> float:
    """Nettoie un montant : '3 500,00' ou '3,500.00' → 3500.0."""
    s = s.strip().replace('\u202f', '').replace('\xa0', '').replace(' ', '')
    # Déterminer le séparateur décimal
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


def search_hotel(
    entity_id: str,
    checkin: str,
    checkout: str,
    adults: int = 1,
    children: list[int] | None = None,
    currency: str = "EUR",
) -> HotelResult | None:
    """Scrape Google Hotels pour un hôtel spécifique.

    Args:
        entity_id: Google Hotels entity ID (ex: CgsI4tWK2cf7nNfzARAB)
        checkin: Date check-in YYYY-MM-DD
        checkout: Date check-out YYYY-MM-DD
        adults: Nombre d'adultes
        children: Liste d'âges des enfants
        currency: Devise souhaitée

    Returns:
        HotelResult avec les prix par provider, ou None si échec.
    """
    from datetime import datetime

    checkin_dt = datetime.strptime(checkin, "%Y-%m-%d")
    checkout_dt = datetime.strptime(checkout, "%Y-%m-%d")
    nights = (checkout_dt - checkin_dt).days

    url = (
        f"https://www.google.com/travel/hotels/entity/{entity_id}/prices"
        f"?checkin={checkin}&checkout={checkout}"
        f"&guests={adults}"
        f"&currency={currency}&hl=fr"
    )

    print(f"  Hotels: scraping {url}")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                locale="fr-FR",
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")

            # Gérer le consentement Google cookies
            _handle_consent(page)

            # Attendre que les prix se chargent
            page.wait_for_timeout(5000)

            # Extraire les données
            result = _extract_prices(page, entity_id, checkin, checkout,
                                     nights, currency)

            browser.close()
            return result

    except Exception as e:
        print(f"  Hotels error: {e}")
        return None


def _handle_consent(page) -> None:
    """Accepte la popup de consentement Google si présente."""
    try:
        # Bouton "Tout accepter" ou "Accept all"
        for selector in [
            'button:has-text("Tout accepter")',
            'button:has-text("Accept all")',
            'button:has-text("Accepter tout")',
            'form[action*="consent"] button',
        ]:
            btn = page.query_selector(selector)
            if btn:
                btn.click()
                page.wait_for_timeout(2000)
                return
    except Exception:
        pass


def _extract_prices(page, entity_id: str, checkin: str, checkout: str,
                    nights: int, currency: str) -> HotelResult:
    """Extrait les prix depuis la page Google Hotels."""
    result = HotelResult(
        hotel_name="",
        checkin=checkin,
        checkout=checkout,
        nights=nights,
        scraped_at=datetime.now().isoformat(),
    )

    # Nom de l'hôtel - chercher dans les headings
    for sel in ['h1', 'h2', '[data-hotel-name]', '[class*="hotel"] h1']:
        el = page.query_selector(sel)
        if el:
            name = el.text_content().strip()
            if name and len(name) > 3:
                result.hotel_name = name
                break

    # Rating
    for sel in ['[class*="rating"]', '[aria-label*="étoile"]',
                '[aria-label*="star"]']:
        el = page.query_selector(sel)
        if el:
            text = el.get_attribute("aria-label") or el.text_content() or ""
            m = re.search(r'([\d.]+)', text)
            if m:
                val = float(m.group(1))
                if 0 < val <= 5:
                    result.rating = val
                    break

    # Extraire tous les blocs de prix
    # Google Hotels affiche une liste de providers avec prix
    # Stratégie : récupérer le texte visible et parser
    content = page.content()

    # Chercher les liens vers les providers (contiennent le prix et le nom)
    links = page.query_selector_all('a[href*="booking.com"], a[href*="agoda"], '
                                     'a[href*="hotels.com"], a[href*="expedia"], '
                                     'a[href*="trip.com"], a[href*="traveloka"]')
    for link in links:
        try:
            text = link.text_content() or ""
            href = link.get_attribute("href") or ""
            provider = _identify_provider(href, text)
            parsed = _parse_price(text)
            if provider and parsed:
                price_val, cur = parsed
                result.prices.append(HotelPrice(
                    source=provider,
                    price=price_val,
                    currency=cur,
                    url=href,
                ))
        except Exception:
            continue

    # Fallback : chercher dans tous les éléments contenant un prix
    if not result.prices:
        all_elements = page.query_selector_all(
            '[class*="price"], [class*="rate"], [data-price]'
        )
        for el in all_elements:
            try:
                text = el.text_content() or ""
                parsed = _parse_price(text)
                if parsed:
                    price_val, cur = parsed
                    # Trouver le provider le plus proche
                    parent_text = ""
                    parent = el.query_selector("xpath=..")
                    if parent:
                        parent_text = parent.text_content() or ""
                    provider = _identify_provider_from_text(parent_text)
                    result.prices.append(HotelPrice(
                        source=provider or "Google Hotels",
                        price=price_val,
                        currency=cur,
                    ))
            except Exception:
                continue

    # Fallback ultime : regex sur tout le texte de la page
    if not result.prices:
        page_text = page.inner_text("body")
        result.prices = _extract_prices_from_text(page_text, currency)

    # Dédupliquer par provider (garder le moins cher)
    seen: dict[str, HotelPrice] = {}
    for p in result.prices:
        if p.source not in seen or p.price < seen[p.source].price:
            seen[p.source] = p
    result.prices = list(seen.values())

    # Best price
    if result.prices:
        best = min(result.prices, key=lambda p: p.price)
        result.best_price = best.price
        result.best_currency = best.currency
        result.best_source = best.source

    print(f"  Hotels: {len(result.prices)} providers trouvés, "
          f"best={result.best_price} {result.best_currency} ({result.best_source})")

    return result


def _identify_provider(href: str, text: str) -> str | None:
    """Identifie le provider depuis l'URL ou le texte."""
    href_lower = href.lower()
    if "booking.com" in href_lower:
        return "Booking.com"
    if "agoda" in href_lower:
        return "Agoda"
    if "hotels.com" in href_lower:
        return "Hotels.com"
    if "expedia" in href_lower:
        return "Expedia"
    if "trip.com" in href_lower:
        return "Trip.com"
    if "traveloka" in href_lower:
        return "Traveloka"
    if "priceline" in href_lower:
        return "Priceline"
    return _identify_provider_from_text(text)


def _identify_provider_from_text(text: str) -> str | None:
    """Identifie le provider depuis un texte."""
    text_lower = text.lower()
    for provider in _KNOWN_PROVIDERS:
        if provider.lower() in text_lower:
            return provider
    return None


def _extract_prices_from_text(text: str, default_currency: str) -> list[HotelPrice]:
    """Extraction fallback : parse le texte brut pour des prix."""
    prices = []
    lines = text.split('\n')
    for i, line in enumerate(lines):
        parsed = _parse_price(line)
        if not parsed:
            continue
        price_val, cur = parsed
        if price_val <= 0 or price_val > 100000:
            continue
        # Chercher le provider dans les lignes voisines
        context = ' '.join(lines[max(0, i-2):i+3])
        provider = _identify_provider_from_text(context) or "Google Hotels"
        prices.append(HotelPrice(
            source=provider,
            price=price_val,
            currency=cur,
        ))
    return prices
