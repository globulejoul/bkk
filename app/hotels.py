"""Google Hotels scraper pour le suivi d'un hôtel précis.

Les dates de séjour voyagent dans le paramètre `ts` (protobuf base64) :
Google ignore `checkin`/`checkout` en clair et répondrait avec ses dates
par défaut. Un contrôle vérifie ensuite que les liens providers portent
bien les dates demandées. L'extraction se fait en UN aller-retour DOM,
puis les montants aberrants sont écartés par comparaison au médian.
"""
from __future__ import annotations

import base64
import re
import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import quote_plus


class HotelScrapeError(RuntimeError):
    """Échec identifiable du scrape (blocage Google, hôtel introuvable)."""


# Conversion locale → EUR. Renvoie None si la devise est inconnue.
ToEur = Callable[[float, str], float | None]


@dataclass
class HotelPrice:
    """Prix d'un provider pour un hôtel."""
    source: str       # "Booking.com", "Agoda", "Hotels.com", etc.
    price: float
    currency: str     # "EUR", "THB", etc.
    url: str = ""
    price_eur: float | None = None


@dataclass
class HotelResult:
    """Résultat agrégé de Google Hotels pour un hôtel."""
    hotel_name: str
    checkin: str
    checkout: str
    nights: int
    prices: list[HotelPrice] = field(default_factory=list)
    best_price: float | None = None
    best_currency: str = "EUR"
    best_source: str = ""
    best_price_eur: float | None = None
    scraped_at: str = ""
    entry_url: str = ""       # URL réellement utilisée (traçabilité)
    matched_text: str = ""    # libellé du lien cliqué, si repli recherche
    dates_confirmed: bool = False  # un lien provider porte bien les dates
    rejected: list[str] = field(default_factory=list)  # montants écartés


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

# Un séjour plausible, en EUR, après conversion. Hors bornes = parsing raté.
_MIN_EUR = 10.0
_MAX_EUR = 20000.0
# Un prix provider s'écartant trop du médian des autres providers est
# presque toujours une erreur d'extraction (prix par nuit, prix barré,
# montant d'un bloc voisin). Observé en production : 49 € contre un
# médian de ~120 €, qui a figé le plus-bas historique.
_OUTLIER_LOW = 0.55
_OUTLIER_HIGH = 1.9


def search_hotel(
    hotel_name: str,
    checkin: str,
    checkout: str,
    adults: int = 1,
    children: list[int] | None = None,
    currency: str = "EUR",
    to_eur: ToEur | None = None,
    timeout: int = 60,
) -> HotelResult | None:
    """Scrape Google Hotels pour un hôtel spécifique.

    *to_eur* convertit un montant local en EUR ; injecté par l'appelant
    pour garder ce module indépendant de `fx`. Sans lui, seuls les
    montants déjà en EUR sont exploitables.

    Lève `HotelScrapeError` sur un échec identifiable (blocage Google,
    hôtel introuvable, timeout) afin que l'appelant puisse le tracer.
    """
    checkin_dt = datetime.strptime(checkin, "%Y-%m-%d")
    checkout_dt = datetime.strptime(checkout, "%Y-%m-%d")
    nights = (checkout_dt - checkin_dt).days
    if nights <= 0:
        raise HotelScrapeError(
            f"dates incohérentes ({checkin} → {checkout})")
    total_guests = adults + (len(children) if children else 0)

    print(f"  Hotels: {hotel_name} {checkin}→{checkout} "
          f"({nights}n, {total_guests} guests)")

    return _scrape_with_timeout(
        hotel_name=hotel_name, checkin=checkin, checkout=checkout,
        nights=nights, guests=total_guests, adults=adults,
        currency=currency, to_eur=to_eur, timeout=timeout,
    )


def _scrape_with_timeout(
    *, hotel_name: str, checkin: str, checkout: str, nights: int,
    guests: int, adults: int, currency: str,
    to_eur: ToEur | None, timeout: int = 60,
) -> HotelResult | None:
    """Lance le scrape Playwright avec un garde-fou de durée.

    Les timeouts natifs Playwright (10 s par action, 20 s par navigation)
    bornent déjà le thread ; ce wrapper n'est qu'une seconde barrière.
    """
    result_holder: list[HotelResult | None] = [None]
    error_holder: list[BaseException | None] = [None]

    def _do_scrape() -> None:
        try:
            result_holder[0] = _scrape_hotel(
                hotel_name=hotel_name, checkin=checkin, checkout=checkout,
                nights=nights, guests=guests, adults=adults,
                currency=currency, to_eur=to_eur,
            )
        except BaseException as e:  # noqa: BLE001 - propagé au thread appelant
            error_holder[0] = e

    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        raise HotelScrapeError(f"timeout après {timeout}s")
    if error_holder[0] is not None:
        raise error_holder[0]
    return result_holder[0]


# ── URLs ──────────────────────────────────────────────────────────

# Google Travel ignore les paramètres `checkin`/`checkout` de l'URL : les
# dates ne sont lues que dans le paramètre `ts`, un protobuf encodé en
# base64url. Structure relevée sur une URL produite par l'interface :
#   1: 1
#   3 { 1 { 3 {} }  2 { 2 { 1{a,m,j} 2{a,m,j} 3:1 }  6 { 1: adultes } } }
#   5 { 1 { 7: "EUR" }  3 {} }
# Sans ce paramètre, Google répond avec SES dates par défaut (1 nuit à
# ~2 mois), ce qui a fait enregistrer 4 mois de prix hors sujet.

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        chunk = n & 0x7F
        n >>= 7
        if n:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _pb_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _pb_msg(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _pb_date(value: str) -> bytes:
    d = datetime.strptime(value, "%Y-%m-%d").date()
    return (_pb_varint(1, d.year) + _pb_varint(2, d.month)
            + _pb_varint(3, d.day))


def build_ts(checkin: str, checkout: str, adults: int = 1,
             currency: str = "EUR") -> str:
    """Encode les dates de séjour dans le paramètre `ts` de Google Travel."""
    stay = (_pb_msg(1, _pb_date(checkin))
            + _pb_msg(2, _pb_date(checkout))
            + _pb_varint(3, 1))
    occupancy = _pb_msg(2, stay) + _pb_msg(6, _pb_varint(1, max(1, adults)))
    block = _pb_msg(1, _pb_msg(3, b"")) + _pb_msg(2, occupancy)
    payload = (
        _pb_varint(1, 1)
        + _pb_msg(3, block)
        + _pb_msg(5, _pb_msg(1, _pb_msg(7, currency.encode()))
                  + _pb_msg(3, b""))
    )
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _search_url(hotel_name: str, checkin: str, checkout: str,
                adults: int, currency: str) -> str:
    return (
        f"https://www.google.com/travel/search"
        f"?q={quote_plus(hotel_name)}"
        f"&ts={build_ts(checkin, checkout, adults, currency)}"
        f"&hl=fr&gl=fr&curr={quote_plus(currency)}"
    )


def _compact(value: str) -> str:
    return value.replace("-", "")


def _dates_look_applied(anchors: list[dict], checkin: str,
                        checkout: str) -> bool | None:
    """Les liens providers portent-ils bien les dates demandées ?

    True  : au moins un lien confirme la date d'arrivée.
    False : des liens portent des dates, mais aucune ne correspond.
    None  : aucun lien daté, indécidable.
    """
    wanted = {checkin, _compact(checkin), checkout, _compact(checkout)}
    dated = False
    for a in anchors:
        href = a.get("href") or ""
        if not _identify_provider(href):
            continue
        found = set(re.findall(r"\d{4}-\d{2}-\d{2}", href))
        found |= set(re.findall(r"20\d{6}", href))
        if not found:
            continue
        dated = True
        if found & wanted:
            return True
    return False if dated else None


# ── Scrape ────────────────────────────────────────────────────────

# Un seul aller-retour DOM : toutes les ancres avec leur texte et le
# texte de leur conteneur. Évite les centaines d'appels CDP unitaires.
_JS_COLLECT_ANCHORS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => {
  const p = a.closest('div, li, tr');
  return {
    href: a.getAttribute('href') || '',
    text: (a.textContent || '').trim().slice(0, 160),
    parent: p ? (p.textContent || '').trim().slice(0, 400) : ''
  };
})
"""

_JS_COLLECT_LINK_TEXTS = """
() => Array.from(document.querySelectorAll('a')).map(
  a => (a.textContent || '').trim().slice(0, 160))
"""


def _scrape_hotel(
    *, hotel_name: str, checkin: str, checkout: str, nights: int,
    guests: int, adults: int, currency: str,
    to_eur: ToEur | None,
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
        try:
            ctx = browser.new_context(
                locale="fr-FR",
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.set_default_timeout(10000)
            page.set_default_navigation_timeout(20000)

            # La recherche par nom exact atterrit directement sur la fiche
            # de l'hôtel : inutile de cliquer quand les providers sont
            # déjà présents. Les dates voyagent dans `ts`.
            url = _search_url(hotel_name, checkin, checkout, adults,
                              currency)
            result.entry_url = url
            page.goto(url, wait_until="domcontentloaded")
            _handle_consent(page)
            _assert_not_blocked(page)
            page.wait_for_timeout(6000)
            anchors = page.evaluate(_JS_COLLECT_ANCHORS)

            # Repli : page de résultats multiples, il faut ouvrir la fiche.
            if not _has_provider(anchors):
                texts = page.evaluate(_JS_COLLECT_LINK_TEXTS)
                idx = _best_link_index(hotel_name, texts)
                if idx is None:
                    raise HotelScrapeError(
                        f"'{hotel_name}' introuvable dans les résultats")
                handles = page.query_selector_all("a")
                if idx >= len(handles):
                    raise HotelScrapeError("DOM modifié pendant la sélection")
                result.matched_text = texts[idx]
                handles[idx].click()
                page.wait_for_timeout(5000)
                _assert_not_blocked(page)
                anchors = page.evaluate(_JS_COLLECT_ANCHORS)
        finally:
            browser.close()

    # Garde-fou : ne jamais enregistrer un prix pour d'autres dates que
    # celles demandées. C'est ce contrôle qui manquait jusqu'ici.
    applied = _dates_look_applied(anchors, checkin, checkout)
    if applied is False:
        raise HotelScrapeError(
            f"dates non appliquées par Google ({checkin} → {checkout})")
    if applied is None:
        print("  Hotels: ⚠ aucune date dans les liens, dates non vérifiables")
    result.dates_confirmed = bool(applied)

    result.prices = _extract_prices(anchors)
    _consolidate(result, to_eur)

    print(f"  Hotels: {len(result.prices)} providers, "
          f"best={result.best_price} {result.best_currency} "
          f"({result.best_source})")
    if result.rejected:
        print(f"  Hotels: écartés → {', '.join(result.rejected)}")

    return result


def _assert_not_blocked(page) -> None:
    """Détecte un blocage Google plutôt que de renvoyer « aucun prix »."""
    url = (page.url or "").lower()
    if "/sorry/" in url or "consent.google.com" in url:
        raise HotelScrapeError(f"bloqué par Google ({page.url[:80]})")
    try:
        head = (page.content() or "")[:4000].lower()
    except Exception:
        return
    for marker in ("unusual traffic", "trafic inhabituel",
                   "systèmes ont détecté", "not a robot"):
        if marker in head:
            raise HotelScrapeError(f"blocage détecté ({marker})")


def _handle_consent(page) -> None:
    """Accepte la popup de consentement Google si présente."""
    for selector in (
        'button:has-text("Tout accepter")',
        'button:has-text("Accept all")',
        'button:has-text("Accepter tout")',
        'form[action*="consent"] button',
    ):
        try:
            btn = page.query_selector(selector)
        except Exception:
            continue
        if btn:
            try:
                btn.click()
                page.wait_for_timeout(2000)
            except Exception:
                pass
            return


def _has_provider(anchors: list[dict]) -> bool:
    return any(_identify_provider(a.get("href", "")) for a in anchors)


def _best_link_index(hotel_name: str, texts: list[str]) -> int | None:
    """Index du lien correspondant le mieux au nom de l'hôtel.

    Exige TOUS les mots significatifs du nom : l'ancien seuil « la moitié
    des mots » acceptait « Chatrium Grand Bangkok » pour « Chatrium
    Riverside Bangkok ».
    """
    words = [w for w in re.findall(r"\w+", hotel_name.lower()) if len(w) > 2]
    if not words:
        return None
    for i, raw in enumerate(texts):
        text = (raw or "").lower()
        if not text:
            continue
        if all(w in text for w in words):
            return i
    return None


def _extract_prices(anchors: list[dict]) -> list[HotelPrice]:
    """Construit la liste des prix providers à partir des ancres."""
    prices: list[HotelPrice] = []
    for a in anchors:
        href = a.get("href") or ""
        provider = _identify_provider(href)
        if not provider:
            continue
        parsed = _parse_price(a.get("parent") or "")
        if not parsed:
            continue
        amount, cur = parsed
        prices.append(HotelPrice(source=provider, price=amount,
                                 currency=cur, url=href))
    return prices


def _consolidate(result: HotelResult, to_eur: ToEur | None) -> None:
    """Déduplique par provider, convertit en EUR, écarte les aberrants.

    Fonction pure (hors `to_eur`) : testable sans navigateur.
    """
    # 1) Un prix par provider : le moins cher.
    seen: dict[str, HotelPrice] = {}
    for hp in result.prices:
        if hp.source not in seen or hp.price < seen[hp.source].price:
            seen[hp.source] = hp

    # 2) Conversion en EUR AVANT toute comparaison : comparer 1 200 THB
    #    et 120 EUR par ordre numérique n'a aucun sens.
    kept: list[HotelPrice] = []
    for hp in seen.values():
        if hp.currency == "EUR":
            hp.price_eur = hp.price
        elif to_eur is not None:
            hp.price_eur = to_eur(hp.price, hp.currency)
        if hp.price_eur is None:
            result.rejected.append(
                f"{hp.source} {hp.price:.0f} {hp.currency} (devise inconnue)")
            continue
        if not (_MIN_EUR <= hp.price_eur <= _MAX_EUR):
            result.rejected.append(
                f"{hp.source} {hp.price_eur:.0f}€ (hors bornes)")
            continue
        kept.append(hp)

    # 3) Rejet des valeurs aberrantes par rapport au médian des providers.
    if len(kept) >= 3:
        median = statistics.median(hp.price_eur for hp in kept)
        plausible = [
            hp for hp in kept
            if _OUTLIER_LOW * median <= hp.price_eur <= _OUTLIER_HIGH * median
        ]
        for hp in kept:
            if hp not in plausible:
                result.rejected.append(
                    f"{hp.source} {hp.price_eur:.0f}€ "
                    f"(médian {median:.0f}€)")
        kept = plausible

    result.prices = sorted(kept, key=lambda hp: hp.price_eur or 0.0)

    if result.prices:
        best = result.prices[0]
        result.best_price = best.price
        result.best_currency = best.currency
        result.best_source = best.source
        result.best_price_eur = best.price_eur
    else:
        result.best_price = None
        result.best_price_eur = None
        result.best_source = ""


def _identify_provider(href: str) -> str | None:
    """Identifie le provider depuis l'URL du lien."""
    href_lower = href.lower()
    for domain, name in _PROVIDERS.items():
        if domain in href_lower:
            return name
    return None


_NUM = r'(\d[\d\s \xa0.,]*)'
_CUR_PATTERNS: list[tuple[str, str]] = [
    ("EUR", _NUM + r'\s*€'),
    ("EUR", r'€\s*' + _NUM),
    ("THB", _NUM + r'\s*฿'),
    ("THB", r'฿\s*' + _NUM),
    ("USD", r'\$\s*' + _NUM),
    ("USD", _NUM + r'\s*\$'),
    ("GBP", r'£\s*' + _NUM),
    ("GBP", _NUM + r'\s*£'),
]


def _parse_price(text: str) -> tuple[float, str] | None:
    """Extrait le premier montant et sa devise depuis un texte."""
    if not text:
        return None
    for currency, pattern in _CUR_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                return _clean_amount(m.group(1)), currency
            except ValueError:
                continue
    return None


def _clean_amount(s: str) -> float:
    """'3 500,00' → 3500.0 ; '3,500.00' → 3500.0 ; '1.234' → 1234.0"""
    s = s.strip().replace(' ', '').replace('\xa0', '').replace(' ', '')
    s = s.rstrip('.,')
    if not s:
        raise ValueError("montant vide")
    if ',' in s and '.' in s:
        # Le dernier séparateur rencontré est le séparateur décimal.
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) == 2 and len(parts[-1]) <= 2:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    elif '.' in s:
        parts = s.split('.')
        # '1.234' et '1.234.567' sont des milliers, pas des décimales :
        # un prix n'a jamais exactement trois décimales.
        if len(parts) > 2 or len(parts[-1]) == 3:
            s = s.replace('.', '')
    return float(s)
