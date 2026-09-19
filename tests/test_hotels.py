"""Tests des fonctions pures du suivi hôtel.

Exécutable sans pytest ni navigateur :
    python -m tests.test_hotels
"""
from __future__ import annotations

from app import hotels
from app.watcher import _alert_cooldown_passed


def check(label: str, got, expected) -> None:
    assert got == expected, f"{label}: attendu {expected!r}, obtenu {got!r}"


# ── _clean_amount ────────────────────────────────────────────────

def test_clean_amount() -> None:
    check("espace insécable", hotels._clean_amount("3 500,00"), 3500.0)
    check("format anglo", hotels._clean_amount("3,500.00"), 3500.0)
    check("décimale virgule", hotels._clean_amount("12,50"), 12.5)
    check("entier simple", hotels._clean_amount("152"), 152.0)
    # Le bug corrigé : '1.234' était lu 1.234 € au lieu de 1234 €,
    # ce qui faisait passer un prix aberrant sous le seuil d'alerte.
    check("point = milliers", hotels._clean_amount("1.234"), 1234.0)
    check("milliers multiples", hotels._clean_amount("1.234.567"), 1234567.0)
    check("décimale point", hotels._clean_amount("1234.5"), 1234.5)
    check("milliers virgule", hotels._clean_amount("1,234"), 1234.0)


# ── _parse_price ─────────────────────────────────────────────────

def test_parse_price() -> None:
    check("euro suffixe", hotels._parse_price("Booking.com 152 €"),
          (152.0, "EUR"))
    check("euro préfixe", hotels._parse_price("€ 98"), (98.0, "EUR"))
    check("baht", hotels._parse_price("4 500 ฿"), (4500.0, "THB"))
    check("dollar", hotels._parse_price("$ 120"), (120.0, "USD"))
    check("aucun montant", hotels._parse_price("Voir l'offre"), None)
    check("texte vide", hotels._parse_price(""), None)


# ── _best_link_index ─────────────────────────────────────────────

def test_best_link_index() -> None:
    textes = [
        "Hôtels à Bangkok",
        "Chatrium Grand Bangkok",
        "Chatrium Riverside Bangkok",
    ]
    # L'ancien seuil « la moitié des mots » retenait Chatrium Grand.
    check("exige tous les mots",
          hotels._best_link_index("Chatrium Riverside Bangkok", textes), 2)
    check("aucune correspondance",
          hotels._best_link_index("Mandarin Oriental", textes), None)


# ── _consolidate ─────────────────────────────────────────────────

def _result(prices: list[tuple[str, float, str]]) -> hotels.HotelResult:
    r = hotels.HotelResult(hotel_name="H", checkin="2027-02-13",
                           checkout="2027-02-15", nights=2)
    r.prices = [hotels.HotelPrice(source=s, price=p, currency=c)
                for s, p, c in prices]
    return r


def test_consolidate_rejette_aberrant() -> None:
    # Cas réel : Priceline à 49 € contre un médian de ~120 € avait figé
    # lowest_price_eur, rendant toute alerte « nouveau bas » impossible.
    r = _result([
        ("Expedia", 152.0, "EUR"),
        ("Hotels.com", 153.0, "EUR"),
        ("Trip.com", 130.0, "EUR"),
        ("Agoda", 117.0, "EUR"),
        ("Priceline", 49.0, "EUR"),
    ])
    hotels._consolidate(r, None)
    sources = {p.source for p in r.prices}
    assert "Priceline" not in sources, "l'aberrant 49 € aurait dû être écarté"
    check("meilleur prix retenu", r.best_price_eur, 117.0)
    check("meilleure source", r.best_source, "Agoda")
    assert r.rejected and "Priceline" in r.rejected[0]


def test_consolidate_dedoublonne() -> None:
    r = _result([
        ("Agoda", 140.0, "EUR"),
        ("Agoda", 120.0, "EUR"),
        ("Expedia", 130.0, "EUR"),
        ("Trip.com", 125.0, "EUR"),
    ])
    hotels._consolidate(r, None)
    check("un prix par provider", len(r.prices), 3)
    check("le moins cher du provider", r.best_price_eur, 120.0)


def test_consolidate_convertit_avant_comparaison() -> None:
    # 4 000 THB ≈ 104 € : moins cher que 120 €, mais numériquement
    # supérieur. Sans conversion préalable, le best était faux.
    def to_eur(amount: float, currency: str) -> float | None:
        return amount / 38.5 if currency == "THB" else None

    r = _result([
        ("Agoda", 4000.0, "THB"),
        ("Expedia", 120.0, "EUR"),
        ("Trip.com", 125.0, "EUR"),
    ])
    hotels._consolidate(r, to_eur)
    check("best en THB converti", r.best_source, "Agoda")
    assert r.best_price_eur is not None and 103 < r.best_price_eur < 105


def test_consolidate_devise_inconnue_ecartee() -> None:
    r = _result([("Agoda", 4000.0, "THB"), ("Expedia", 120.0, "EUR")])
    hotels._consolidate(r, lambda a, c: None)
    check("seul l'EUR survit", [p.source for p in r.prices], ["Expedia"])
    assert any("devise inconnue" in x for x in r.rejected)


def test_consolidate_sans_prix() -> None:
    r = _result([])
    hotels._consolidate(r, None)
    check("aucun best", r.best_price_eur, None)
    check("source vide", r.best_source, "")


# ── URLs ─────────────────────────────────────────────────────────

def test_urls_encodees() -> None:
    url = hotels._search_url("Hôtel A&B", "2027-02-13", "2027-02-15", 2, "EUR")
    assert "A%26B" in url, "le & du nom doit être encodé"
    assert "curr=EUR" in url and "gl=fr" in url
    assert "&ts=" in url, "les dates passent par le paramètre ts"


def test_build_ts_reproduit_la_reference() -> None:
    # Référence capturée depuis une URL produite par l'interface Google
    # pour 29→30 nov. 2026, 1 adulte, EUR.
    attendu = "CAEaIAoCGgASGhIUCgcI6g8QCxgdEgcI6g8QCxgeGAEyAggBKgkKBToDRVVSGgA"
    check("ts de référence",
          hotels.build_ts("2026-11-29", "2026-11-30", 1, "EUR"), attendu)
    # Les dates demandées doivent réellement changer l'encodage.
    autre = hotels.build_ts("2027-02-13", "2027-02-15", 2, "EUR")
    assert autre != attendu


def test_dates_look_applied() -> None:
    ci, co = "2027-02-13", "2027-02-15"
    ok = [{"href": "https://www.expedia.fr/x?startDate=2027-02-13&endDate=2027-02-15"}]
    ko = [{"href": "https://www.expedia.fr/x?startDate=2026-11-29&endDate=2026-11-30"}]
    muet = [{"href": "https://www.agoda.com/partners/x?hid=1"}]
    check("dates correctes", hotels._dates_look_applied(ok, ci, co), True)
    check("dates erronées", hotels._dates_look_applied(ko, ci, co), False)
    check("aucune date", hotels._dates_look_applied(muet, ci, co), None)
    check("format compact",
          hotels._dates_look_applied(
              [{"href": "https://www.priceline.com/r/?checkin=20270213"}],
              ci, co), True)


# ── Cooldown d'alerte ────────────────────────────────────────────

def test_alert_cooldown() -> None:
    check("jamais alerté",
          _alert_cooldown_passed(None, "2026-09-19T12:00:00"), True)
    check("alerte récente bloquée",
          _alert_cooldown_passed("2026-09-19T06:00:00",
                                 "2026-09-19T12:00:00"), False)
    check("plus de 24 h",
          _alert_cooldown_passed("2026-09-17T06:00:00",
                                 "2026-09-19T12:00:00"), True)
    check("horodatage illisible",
          _alert_cooldown_passed("n'importe quoi",
                                 "2026-09-19T12:00:00"), True)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passés")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
