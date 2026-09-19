"""Tests du flash mode : persistance et anti-spam des alertes.

Utilise une base SQLite temporaire et neutralise le réseau.

    python -m tests.test_flash
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app import db, notify, sources, watcher
from app.config import Config, Trip


def check(label: str, got, expected) -> None:
    assert got == expected, f"{label}: attendu {expected!r}, obtenu {got!r}"


def _result(price: float, origin: str = "CDG",
            dest: str = "BKK") -> sources.FlightResult:
    return sources.FlightResult(
        price=price, currency="EUR", origin=origin, destination=dest,
        outbound_date="2027-02-12", return_date="2027-03-01",
        out_h=13.0, ret_h=13.0, out_stops=1, ret_stops=1,
        airlines="Thai", booking_url="", source="duffel",
    )


class _Env:
    """Base temporaire + réseau neutralisé, restaurés à la sortie."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        db.init()
        self.sent: list[dict] = []
        self.old_notify = notify.send_ntfy
        self.old_ow = watcher._compare_oneway
        self.old_oj = watcher._compare_openjaw
        notify.send_ntfy = lambda cfg, payload: (self.sent.append(payload), True)[1]
        watcher.notify.send_ntfy = notify.send_ntfy
        watcher._compare_oneway = lambda *a, **k: None
        watcher._compare_openjaw = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        notify.send_ntfy = self.old_notify
        watcher.notify.send_ntfy = self.old_notify
        watcher._compare_oneway = self.old_ow
        watcher._compare_openjaw = self.old_oj
        db.DB_PATH = self.old_path
        self.tmp.cleanup()
        return False


def _cfg() -> Config:
    return Config(origins=["CDG"], destinations=["BKK"], adults=2)


def _trip() -> Trip:
    # Seuil haut : atteint dès le premier relevé, comme en situation de flash.
    return Trip(name="Hiver", outbound_window=("2027-02-11", "2027-02-13"),
                return_window=("2027-03-01", "2027-03-02"),
                price_threshold=2000)


def test_flash_persiste_ses_releves() -> None:
    """Le défaut d'origine : le flash affichait ses prix sans les garder."""
    with _Env():
        cfg, trip = _cfg(), _trip()
        rows, _ = watcher.process_results(
            cfg, trip, [_result(1000.0)], {}, flash=True)
        check("lignes renvoyées", rows, 1)
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
            low = c.execute(
                "SELECT lowest_price_eur FROM state").fetchone()[0]
        check("ligne écrite en base", n, 1)
        check("plus bas mémorisé", low, 1000.0)


def test_flash_alerte_sur_nouveau_bas_seulement() -> None:
    """Le seuil est atteint en permanence pendant un flash : sans cette
    règle, chaque passage toutes les 5 min notifierait."""
    with _Env() as env:
        cfg, trip = _cfg(), _trip()
        # Run complet initial : pose la référence, alerte sur le seuil.
        watcher.process_results(cfg, trip, [_result(1000.0)], {})
        check("alerte du run complet", len(env.sent), 1)

        # Trois passages flash au même prix : aucun nouveau record.
        for _ in range(3):
            watcher.process_results(cfg, trip, [_result(1000.0)], {},
                                    flash=True)
        check("aucune alerte en plus", len(env.sent), 1)

        # Prix en baisse : cette fois on notifie.
        _, alerts = watcher.process_results(
            cfg, trip, [_result(900.0)], {}, flash=True)
        check("alerte sur le nouveau bas", alerts, 1)
        check("total des envois", len(env.sent), 2)
        check("payload marqué flash", env.sent[-1]["flash"], True)
        check("prix notifié", env.sent[-1]["price"], 900.0)


def test_flash_prolonge_la_fenetre_sur_nouveau_bas() -> None:
    with _Env():
        cfg, trip = _cfg(), _trip()
        watcher.process_results(cfg, trip, [_result(1000.0)], {})
        with db.conn() as c:
            avant = c.execute(
                "SELECT flash_until FROM state").fetchone()[0]
        assert avant, "le seuil atteint doit armer la fenêtre flash"
        watcher.process_results(cfg, trip, [_result(800.0)], {}, flash=True)
        with db.conn() as c:
            apres = c.execute(
                "SELECT flash_until FROM state").fetchone()[0]
        assert apres > avant, "un nouveau bas doit repousser la fin du flash"


def test_flash_sans_resultat_ne_casse_rien() -> None:
    with _Env() as env:
        cfg, trip = _cfg(), _trip()
        rows, alerts = watcher.process_results(cfg, trip, [], {}, flash=True)
        check("aucune ligne", rows, 0)
        check("aucune alerte", alerts, 0)
        check("aucun envoi", len(env.sent), 0)


def test_run_complet_garde_seuil_et_percentile() -> None:
    """Le mode normal ne doit pas hériter de la restriction du flash."""
    with _Env() as env:
        cfg, trip = _cfg(), _trip()
        watcher.process_results(cfg, trip, [_result(1000.0)], {})
        check("première alerte", len(env.sent), 1)
        # Même prix, run complet : le seuil reste atteint, on alerte encore.
        watcher.process_results(cfg, trip, [_result(1000.0)], {})
        check("seuil toujours notifié hors flash", len(env.sent), 2)


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
