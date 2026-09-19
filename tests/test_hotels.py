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


# ── _consolidate ─────────────────────────────────────────────────

def _result(prices: list[tuple[str, float, str]]) -> hotels.HotelResult:
    r = hotels.HotelResult(hotel_name="H", checkin="2027-02-13",
                           checkout="2027-02-15", nights=2)
    r.prices = [hotels.HotelPrice(source=s, price=p, currency=c)
                for s, p, c in prices]
    return r


def test_consolidate_retient_le_total_sejour() -> None:
    # Les montants lus près des liens providers n'ont pas de sémantique
    # garantie : c'est ainsi qu'un 144 € ne correspondant à aucune offre
    # réelle avait été enregistré. Seul le total annoncé fait foi.
    r = _result([
        ("Trip.com", 144.0, "EUR"),
        ("Priceline", 170.0, "EUR"),
        ("Expedia", 183.0, "EUR"),
    ])
    r.stay_total_eur = 240.0
    hotels._consolidate(r, None)
    check("prix retenu", r.best_price_eur, 240.0)
    check("source", r.best_source, "Google")
    check("une seule ligne persistée", len(r.prices), 1)
    check("providers conservés pour information", r.providers_seen,
          ["Expedia", "Priceline", "Trip.com"])


def test_consolidate_sans_total() -> None:
    r = _result([("Trip.com", 144.0, "EUR")])
    r.stay_total_eur = None
    hotels._consolidate(r, None)
    check("aucun prix", r.best_price_eur, None)
    check("rien à persister", len(r.prices), 0)


def test_consolidate_total_hors_bornes() -> None:
    r = _result([])
    r.stay_total_eur = 3.0
    hotels._consolidate(r, None)
    check("montant absurde rejeté", r.best_price_eur, None)
    assert any("hors bornes" in x for x in r.rejected)


# ── extract_stay_total ───────────────────────────────────────────

def test_extract_stay_total() -> None:
    txt = "120 €Prix total de 240 €2 nuits (taxes et frais compris)"
    check("total lu", hotels.extract_stay_total(txt, 2), 240.0)
    # Google annonce un autre nombre de nuits : on refuse le montant
    # plutôt que d'enregistrer un prix qui ne correspond pas au séjour.
    check("nuits incohérentes", hotels.extract_stay_total(txt, 3), None)
    check("libellé absent", hotels.extract_stay_total("aucun prix ici", 2), None)
    check("texte vide", hotels.extract_stay_total("", 2), None)
    sans_nuits = "Prix total de 1.234 € taxes comprises"
    check("milliers", hotels.extract_stay_total(sans_nuits, 2), 1234.0)


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


def test_parse_price_aria() -> None:
    ci, co, nom = "2027-02-13", "2027-02-15", "Chatrium Riverside Bangkok"
    ok = "120 € pour les dates 13–15 févr. 2027, Chatrium Hotel Riverside Bangkok"
    check("prix lu", hotels.parse_price_aria(ok, ci, co, nom), 120.0)
    # Prix affiché pour d'autres dates : à refuser, c'est exactement le
    # défaut qui a fait enregistrer 4 mois de prix hors sujet.
    mauvaises = "101 € pour les dates 29–30 nov. 2026, Chatrium Hotel Riverside Bangkok"
    check("autres dates", hotels.parse_price_aria(mauvaises, ci, co, nom), None)
    # Prix d'un autre hôtel de la liste.
    autre = "591 € pour les dates 13–15 févr. 2027, Four Seasons Hotel Bangkok"
    check("autre hôtel", hotels.parse_price_aria(autre, ci, co, nom), None)
    check("libellé quelconque",
          hotels.parse_price_aria("Voir les prix", ci, co, nom), None)


def test_is_per_night() -> None:
    check("mode par nuit", hotels._is_per_night("Prix affiché Prix total par nuit"), True)
    check("mode total séjour",
          hotels._is_per_night("Prix total du séjour, taxes et frais compris"), False)


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



# ── Score d'achat ────────────────────────────────────────────────

def _trip(depart: str = '2027-02-11'):
    from app.config import Trip
    return Trip(name='T', outbound_window=(depart, depart),
                return_window=('2027-03-01', '2027-03-01'))


def test_buy_score_hausse_sur_prix_haut_ne_paie_pas() -> None:
    from app.watcher import _calc_buy_score
    from datetime import date as _date
    jour = _date(2027, 1, 1)  # ~41 jours avant le départ
    trip = _trip()
    haut = _calc_buy_score(2000.0, trip, 90.0, {'direction': 'rising'},
                           lowest_eur=1000.0, today=jour)
    bas = _calc_buy_score(1000.0, trip, 5.0, {'direction': 'rising'},
                          lowest_eur=1000.0, today=jour)
    assert bas > haut + 40, f'bas={bas} haut={haut}'
    # L'ancienne pondération donnait 25/100 à toute hausse : un prix au
    # 90e percentile ne doit pas être recommandé à l'achat.
    assert haut < 40, haut


def test_buy_score_baisse_invite_a_attendre() -> None:
    from app.watcher import _calc_buy_score
    from datetime import date as _date
    jour, trip = _date(2027, 1, 1), _trip()
    baisse = _calc_buy_score(1000.0, trip, 20.0, {'direction': 'falling'},
                             lowest_eur=1000.0, today=jour)
    stable = _calc_buy_score(1000.0, trip, 20.0, {'direction': 'stable'},
                             lowest_eur=1000.0, today=jour)
    assert baisse < stable, f'baisse={baisse} stable={stable}'


def test_buy_score_borne_et_independant_du_jour_de_run() -> None:
    from app.watcher import _calc_buy_score
    from datetime import date as _date
    trip = _trip()
    # Même prix, même délai avant départ, deux jours de semaine
    # différents : le score ne doit plus bouger (facteur supprimé).
    mardi = _calc_buy_score(1000.0, _trip('2027-02-15'), 10.0,
                            {'direction': 'stable'}, lowest_eur=1000.0,
                            today=_date(2027, 1, 5))
    jeudi = _calc_buy_score(1000.0, _trip('2027-02-17'), 10.0,
                            {'direction': 'stable'}, lowest_eur=1000.0,
                            today=_date(2027, 1, 7))
    check('jour de run sans effet', mardi, jeudi)
    for s in (mardi, jeudi):
        assert 0 <= s <= 100


def test_config_hash_reagit_aux_bons_champs() -> None:
    from app.watcher import trip_config_hash
    from app.config import Config, Trip
    cfg = Config(origins=['CDG'], destinations=['BKK'], adults=2)
    t1 = Trip(name='A', outbound_window=('2027-02-11', '2027-02-13'),
              return_window=('2027-03-01', '2027-03-02'), price_threshold=800)
    base = trip_config_hash(cfg, t1)
    # Le seuil et le nom ne changent pas le voyage surveillé.
    t2 = Trip(name='B', outbound_window=('2027-02-11', '2027-02-13'),
              return_window=('2027-03-01', '2027-03-02'), price_threshold=500)
    check('seuil et nom indifférents', trip_config_hash(cfg, t2), base)
    # Les dates, si.
    t3 = Trip(name='A', outbound_window=('2027-02-12', '2027-02-13'),
              return_window=('2027-03-01', '2027-03-02'))
    assert trip_config_hash(cfg, t3) != base
    # Le nombre de voyageurs aussi.
    cfg2 = Config(origins=['CDG'], destinations=['BKK'], adults=3)
    assert trip_config_hash(cfg2, t1) != base


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
