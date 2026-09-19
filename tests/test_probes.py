"""Tests des sondes compagnies : quota, rotation, isolation des données.

Base SQLite temporaire, aucun accès réseau : on ne teste que les
fonctions pures et la persistance.

    python -m tests.test_probes
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from app import db, probes
from app.config import Config, Probe, Trip


def check(label: str, got, expected) -> None:
    assert got == expected, f"{label}: attendu {expected!r}, obtenu {got!r}"


class _Env:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.init()
        return self

    def __exit__(self, *exc):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()
        return False


def _trip(name: str = "Hiver 2027") -> Trip:
    return Trip(
        name=name,
        outbound_window=("2027-02-10", "2027-02-16"),
        return_window=("2027-02-26", "2027-03-04"),
        min_nights=10, max_nights=25,
    )


def _probe(**kw) -> Probe:
    base = dict(name="af-cdg-bkk", origins=["CDG"], destinations=["BKK"],
                cells_per_run=6)
    base.update(kw)
    return Probe(**base)


def _combos(trip: Trip) -> dict[str, list[str]]:
    """Grille complète, sans le filtre « dates futures » du watcher."""
    def days(w):
        a, b = date.fromisoformat(w[0]), date.fromisoformat(w[1])
        return [(a + timedelta(days=i)).isoformat()
                for i in range((b - a).days + 1)]
    out: dict[str, list[str]] = {}
    for o in days(trip.outbound_window):
        valid = [r for r in days(trip.return_window)
                 if trip.min_nights <= (date.fromisoformat(r)
                                        - date.fromisoformat(o)).days
                 <= trip.max_nights]
        if valid:
            out[o] = valid
    return out


# ── Quota ────────────────────────────────────────────────────────


def test_quota_bucket() -> None:
    """Le seau plafonne, et il est partagé par credential.

    Vérifié en production : les hosts AF et KL d'une même clé Air France
    puisent dans le même quota de 100 requêtes/jour.
    """
    with _Env():
        with db.conn() as c:
            for i in range(3):
                check(f"jeton {i + 1}",
                      db.probe_quota_take(c, "afklm", 3), True)
            check("4e jeton refusé",
                  db.probe_quota_take(c, "afklm", 3), False)
            check("compteur", db.probe_quota_used(c, "afklm"), 3)

            # Deux sondes (AF et KL) sur le même seau : la seconde ne
            # retrouve pas un quota neuf.
            check("seau partagé", db.probe_quota_take(c, "afklm", 3), False)
            # Un autre credential a bien son propre seau.
            check("autre seau", db.probe_quota_take(c, "autre", 1), True)

            # Un plafond nul ou négatif ne doit jamais accorder de jeton.
            check("plafond nul", db.probe_quota_take(c, "vide", 0), False)

    with _Env():
        with db.conn() as c:
            demain = (date.today() + timedelta(days=1)).isoformat()
            db.probe_quota_take(c, "afklm", 1)
            check("épuisé aujourd'hui",
                  db.probe_quota_take(c, "afklm", 1), False)
            check("remis à zéro demain",
                  db.probe_quota_take(c, "afklm", 1, day=demain), True)


# ── Rotation ─────────────────────────────────────────────────────


def test_grid_hash_ignores_expired_dates() -> None:
    """L'empreinte porte sur la forme CONFIGURÉE, pas sur les cellules
    encore réservables : sinon elle changeait chaque jour à l'approche du
    départ et remettait le curseur à zéro, si bien que les retours
    tardifs n'étaient jamais interrogés."""
    trip, probe = _trip(), _probe()
    h1 = probes.grid_hash(trip, probe)
    h2 = probes.grid_hash(trip, probe)
    check("empreinte stable", h1, h2)

    autre = _trip()
    autre.outbound_window = ("2027-02-10", "2027-02-17")
    assert probes.grid_hash(autre, probe) != h1, \
        "une fenêtre modifiée doit changer l'empreinte"

    probe2 = _probe(destinations=["HKT"])
    assert probes.grid_hash(trip, probe2) != h1, \
        "un périmètre modifié doit changer l'empreinte"


def test_anchor_is_stable_when_dates_expire() -> None:
    """L'ancre vise le milieu des fenêtres configurées.

    Calculée sur les combos filtrés par la date du jour, elle se
    déplaçait d'un jour à l'autre : la série temporelle « même produit
    d'un run à l'autre » qu'elle est censée fournir n'existait pas.
    """
    trip = _trip()
    cells = probes.cells_of(_combos(trip))
    ancre = probes.stable_anchor(trip, cells)

    # On simule l'expiration des deux premières dates aller.
    survivants = [c for c in cells if c[0] >= "2027-02-12"]
    ancre2 = probes.stable_anchor(trip, survivants)
    check("ancre inchangée malgré des dates échues", ancre2, ancre)


def test_rotation_advances_and_covers() -> None:
    """La rotation avance sans trou ni doublon, et reprend après une
    disparition de cellules."""
    trip, probe = _trip(), _probe(cells_per_run=4)
    cells = probes.cells_of(_combos(trip))
    grid = probes.grid_hash(trip, probe)
    ancre = probes.stable_anchor(trip, cells)

    vus: set[tuple[str, str]] = set()
    cursor = None
    for run in range(6):
        head, rot = probes.select_cells(probe, cells, cursor, grid, ancre)
        check(f"run {run}: ancre présente", head, [ancre])
        check(f"run {run}: pas de doublon", len(set(rot)), len(rot))
        assert ancre not in rot, "l'ancre ne doit pas repasser en rotation"
        check(f"run {run}: budget respecté",
              len(head) + len(rot), probe.cells_per_run)
        vus.update(rot)
        cursor = {"grid_hash": grid, "last_out": rot[-1][0],
                  "last_ret": rot[-1][1]}

    check("6 runs × 3 cellules neuves", len(vus), 18)

    # Des cellules disparaissent (dates échues) : la reprise se fait par
    # bissection, sans repartir du début.
    restants = [c for c in cells if c[0] >= "2027-02-12"]
    head, rot = probes.select_cells(probe, restants, cursor, grid, ancre)
    assert rot, "la rotation doit continuer après expiration de dates"
    assert all(c in restants for c in rot), "cellules hors grille"


def test_single_cell_per_run_still_sweeps() -> None:
    """À cells_per_run = 1 en mode grille, l'ancre consommait tout le
    budget et la rotation restait vide : la grille n'était jamais
    balayée, sans le moindre avertissement."""
    trip, probe = _trip(), _probe(cells_per_run=1)
    cells = probes.cells_of(_combos(trip))
    ancre = probes.stable_anchor(trip, cells)
    head, rot = probes.select_cells(probe, cells, None, "h", ancre)
    check("pas d'ancre à 1 cellule", head, [])
    check("une cellule qui tourne", len(rot), 1)


def test_single_cell_mode_eventually_covers_the_anchor() -> None:
    """À cells_per_run = 1 l'ancre n'est pas interrogée en tête de run :
    elle doit donc rester ÉLIGIBLE à la rotation. Exclue des deux, elle
    créait un trou permanent au milieu exact de la grille."""
    trip, probe = _trip(), _probe(cells_per_run=1)
    cells = probes.cells_of(_combos(trip))
    grid = probes.grid_hash(trip, probe)
    ancre = probes.stable_anchor(trip, cells)

    vus: set[tuple[str, str]] = set()
    cursor = None
    for _ in range(len(cells)):
        head, rot = probes.select_cells(probe, cells, cursor, grid, ancre)
        check("aucune ancre en tête", head, [])
        check("une cellule par run", len(rot), 1)
        vus.update(rot)
        cursor = {"grid_hash": grid, "last_out": rot[-1][0],
                  "last_ret": rot[-1][1]}
    assert ancre in vus, "l'ancre doit finir par être interrogée"
    check("grille entièrement couverte", len(vus), len(cells))


def test_cursor_does_not_skip_a_partial_cell() -> None:
    """Le curseur ne doit pas dépasser une cellule dont seule une partie
    des routes a été interrogée : sinon les dernières routes ne sont
    reprises qu'au tour de grille suivant, des jours plus tard."""
    import os
    with _Env():
        trip = _trip()
        probe = _probe(key_env="BKK_TEST_KEY", origins=["CDG", "LYS"],
                       destinations=["BKK"], cells_per_run=2,
                       min_interval_s=0.5)   # borne basse : test rapide
        cfg = Config(origins=["CDG"], destinations=["BKK"])
        os.environ["BKK_TEST_KEY"] = "factice"
        vrai_post = probes._post
        n = {"appels": 0}

        def faux_post(probe_, key, body, *, tag, take):
            take()                      # comme le vrai : un jeton par tentative
            n["appels"] += 1
            if n["appels"] >= 4:        # coupe la 2e route de la 2e cellule
                raise probes.ProbeExhausted("seau vide")
            return {"recommendations": [], "connections": []}

        probes._post = faux_post
        try:
            rep = probes.run_probe(cfg, probe, trip, _combos(trip),
                                   now=datetime.now().isoformat(),
                                   to_eur=lambda a, c: a)
        finally:
            probes._post = vrai_post
            os.environ.pop("BKK_TEST_KEY", None)

        check("arrêt sur seau vide", rep["exhausted"], True)
        with db.conn() as c:
            cur = db.probe_cursor_get(c, probe.name, trip.name)
        assert cur is not None, "le curseur doit être écrit"
        check("curseur non avancé sur cellule partielle", cur["last_out"], None)


def test_provider_exhaustion_closes_the_bucket() -> None:
    """Quand le FOURNISSEUR dit « quota dépassé », notre compteur est en
    retard sur le sien : on ferme le seau, sinon chaque période et chaque
    run suivant repart émettre une requête vouée au 403."""
    with _Env():
        with db.conn() as c:
            check("seau ouvert", db.probe_quota_take(c, "AFKL_API_KEY", 50),
                  True)
            db.probe_quota_fill(c, "AFKL_API_KEY", 50)
            check("seau fermé", db.probe_quota_take(c, "AFKL_API_KEY", 50),
                  False)
            check("compteur au plafond",
                  db.probe_quota_used(c, "AFKL_API_KEY"), 50)
            # Idempotent : un second épuisement ne dépasse pas le plafond.
            db.probe_quota_fill(c, "AFKL_API_KEY", 50)
            check("toujours au plafond",
                  db.probe_quota_used(c, "AFKL_API_KEY"), 50)


def test_bucket_is_the_credential() -> None:
    """Le seau est le nom de la variable d'environnement : deux sondes
    sur la même clé ne peuvent pas déclarer deux plafonds."""
    af = _probe(name="af", travel_host="AF")
    kl = _probe(name="kl", travel_host="KL")
    check("même seau", af.bucket, kl.bucket)
    check("seau = credential", af.bucket, "AFKL_API_KEY")


def test_median_mode_queries_only_the_anchor() -> None:
    trip, probe = _trip(), _probe(date_mode="median", cells_per_run=6)
    cells = probes.cells_of(_combos(trip))
    ancre = probes.stable_anchor(trip, cells)
    head, rot = probes.select_cells(probe, cells, None, "h", ancre)
    check("une seule cellule", head, [ancre])
    check("aucune rotation", rot, [])


# ── Isolation des données ────────────────────────────────────────


def test_probe_rows_never_reach_market_queries() -> None:
    """Le cœur de la correction : les relevés de sonde n'entrent dans
    AUCUNE requête de marché. Un tarif mono-compagnie plus cher décalait
    sinon le 10e percentile — qui travaille sur des prix distincts, non
    pondérés — et déclenchait de fausses alertes basses."""
    with _Env():
        now = datetime.now().isoformat()
        today = date.today().isoformat()
        with db.conn() as c:
            for prix in (600.0, 650.0, 700.0):
                db.insert_check(c, {
                    "check_date": today, "trip_name": "Hiver 2027",
                    "source": "google_flights", "origin": "CDG",
                    "destination": "BKK", "price_local": prix,
                    "currency": "EUR", "price_eur": prix,
                    "outbound_date": "2027-02-13",
                    "return_date": "2027-02-27", "out_h": 12.0,
                    "ret_h": 13.0, "out_stops": 0, "ret_stops": 0,
                    "airlines": "TK", "booking_url": "",
                    "captured_at": now,
                })
            # Sonde nettement plus chère, sur des dates que fli ne couvre pas.
            for cell, prix in ((("2027-02-14", "2027-02-28"), 1263.31),
                               (("2027-02-15", "2027-03-01"), 1148.59)):
                db.insert_probe_check(c, {
                    "check_date": today, "trip_name": "Hiver 2027",
                    "probe": "af-cdg-bkk", "carrier": "AF",
                    "origin": "CDG", "destination": "BKK",
                    "price_local": prix, "currency": "EUR",
                    "price_eur": prix, "outbound_date": cell[0],
                    "return_date": cell[1], "out_h": 11.3, "ret_h": 13.2,
                    "out_stops": 0, "ret_stops": 0, "airlines": "AF",
                    "fare_family": "LIGHTLH", "booking_url": "",
                    "captured_at": now,
                })

            # trips_summary part de la table `state` : sans cette ligne
            # elle renvoie [] et l'assertion suivante levait une
            # IndexError qui interrompait le test avant tout le reste.
            db.upsert_state(c, "Hiver 2027")

            trend = db.price_trend(c, "Hiver 2027", days=7)
            check("price_trend ignore la sonde",
                  [p for _, p in trend], [600.0])

            summary = [s for s in db.trips_summary(c)
                       if s["trip_name"] == "Hiver 2027"]
            check("trips_summary ignore la sonde",
                  summary[0]["current_best"], 600.0)

            hist = db.trip_history(c, "Hiver 2027", days=7)
            check("trip_history ignore la sonde",
                  [h["price_eur"] for h in hist], [600.0])

            routes = db.trip_history_by_route(c, "Hiver 2027", days=7)
            check("trip_history_by_route ignore la sonde",
                  [r["price_eur"] for r in routes], [600.0])

            # Le percentile (0-100) ne voit que des prix de marché : les
            # trois relevés fli, jamais les deux lignes de sonde. Sous
            # cinq prix distincts, la fonction renvoie None par dessein.
            check("percentile sans la sonde",
                  db.percentile_rank(c, "Hiver 2027", 600.0), None)
            for prix in (620.0, 640.0):
                db.insert_check(c, {
                    "check_date": today, "trip_name": "Hiver 2027",
                    "source": "google_flights", "origin": "CDG",
                    "destination": "BKK", "price_local": prix,
                    "currency": "EUR", "price_eur": prix,
                    "outbound_date": "2027-02-13",
                    "return_date": "2027-02-27", "out_h": 12.0,
                    "ret_h": 13.0, "out_stops": 0, "ret_stops": 0,
                    "airlines": "TK", "booking_url": "",
                    "captured_at": now,
                })
            # 600 est le moins cher des cinq : rang 10 = (0 + 0.5) / 5.
            check("600 € reste le plus bas du marché",
                  db.percentile_rank(c, "Hiver 2027", 600.0), 10.0)
            check("breakdown ignore la sonde",
                  len(db.trip_breakdown(c, "Hiver 2027")), 1)

            grille = db.heatmap_data(c, "Hiver 2027", days=30)
            check("heatmap marché : une seule cellule", len(grille), 1)

            # …et la grille de la sonde, elle, ne voit que la sonde.
            pg = db.probe_heatmap(c, "Hiver 2027", "af-cdg-bkk", days=30)
            check("heatmap sonde : ses deux cellules", len(pg), 2)
            check("meilleur prix sonde",
                  round(min(r["best_eur"] for r in pg), 2), 1148.59)


def test_cursor_roundtrip() -> None:
    with _Env():
        now = datetime.now().isoformat()
        with db.conn() as c:
            check("curseur vide",
                  db.probe_cursor_get(c, "af-cdg-bkk", "Hiver 2027"), None)
            db.probe_cursor_set(c, "af-cdg-bkk", "Hiver 2027",
                                last_out="2027-02-13", last_ret="2027-02-27",
                                grid_hash="abc", now=now)
            cur = db.probe_cursor_get(c, "af-cdg-bkk", "Hiver 2027")
            check("cellule mémorisée", cur["last_out"], "2027-02-13")
            check("aucun échec", cur["consecutive_failures"], 0)

            db.probe_cursor_set(c, "af-cdg-bkk", "Hiver 2027",
                                last_out="2027-02-14", last_ret="2027-02-28",
                                grid_hash="abc", now=now, error="boom")
            cur = db.probe_cursor_get(c, "af-cdg-bkk", "Hiver 2027")
            check("échec compté", cur["consecutive_failures"], 1)
            check("erreur retenue", cur["last_error"], "boom")

            db.probe_cursor_set(c, "af-cdg-bkk", "Hiver 2027",
                                last_out="2027-02-15", last_ret="2027-03-01",
                                grid_hash="abc", now=now)
            cur = db.probe_cursor_get(c, "af-cdg-bkk", "Hiver 2027")
            check("compteur d'échecs remis à zéro",
                  cur["consecutive_failures"], 0)


# ── Court-circuit sans clé ───────────────────────────────────────


def test_no_key_is_silent() -> None:
    """Sans variable d'environnement, la sonde ne fait rien et n'échoue
    pas — même comportement que search_duffel sans clé Duffel."""
    import os
    with _Env():
        os.environ.pop("BKK_TEST_ABSENT_KEY", None)
        cfg = Config(origins=["CDG"], destinations=["BKK"])
        trip = _trip()
        rep = probes.run_probe(
            cfg, _probe(key_env="BKK_TEST_ABSENT_KEY"), trip,
            _combos(trip), now=datetime.now().isoformat(),
            to_eur=lambda a, c: a)
        check("aucune requête", rep["calls"], 0)
        check("aucune ligne", rep["rows"], 0)
        check("aucune erreur", rep["error"], None)


def main() -> None:
    for fn in (test_quota_bucket,
               test_grid_hash_ignores_expired_dates,
               test_anchor_is_stable_when_dates_expire,
               test_rotation_advances_and_covers,
               test_single_cell_per_run_still_sweeps,
               test_single_cell_mode_eventually_covers_the_anchor,
               test_cursor_does_not_skip_a_partial_cell,
               test_provider_exhaustion_closes_the_bucket,
               test_bucket_is_the_credential,
               test_median_mode_queries_only_the_anchor,
               test_probe_rows_never_reach_market_queries,
               test_cursor_roundtrip,
               test_no_key_is_silent):
        fn()
        print(f"  ✓ {fn.__name__}")
    print("Tous les tests de sondes passent.")


if __name__ == "__main__":
    main()
