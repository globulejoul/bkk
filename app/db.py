"""SQLite persistence for price checks, state, and alerts."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

DB_PATH = Path("/app/data/prices.db")

# ── Migrations ──────────────────────────────────────────────────
# Each entry: (version_number, sql).  Applied once, in order.
# To evolve the schema: append a new tuple — never edit previous ones.

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_date TEXT NOT NULL,
            trip_name TEXT NOT NULL,
            source TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            price_local REAL NOT NULL,
            currency TEXT NOT NULL,
            price_eur REAL,
            outbound_date TEXT,
            return_date TEXT,
            out_h REAL,
            ret_h REAL,
            out_stops INTEGER,
            ret_stops INTEGER,
            airlines TEXT,
            booking_url TEXT,
            captured_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_checks_trip ON checks(trip_name);
        CREATE INDEX IF NOT EXISTS idx_checks_date ON checks(check_date);
        CREATE INDEX IF NOT EXISTS idx_checks_origin ON checks(origin);
        CREATE INDEX IF NOT EXISTS idx_checks_dest ON checks(destination);

        CREATE TABLE IF NOT EXISTS state (
            trip_name TEXT PRIMARY KEY,
            lowest_price_eur REAL,
            lowest_seen_date TEXT,
            lowest_origin TEXT,
            lowest_destination TEXT,
            lowest_booking_url TEXT,
            rolling_json TEXT,
            last_check_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            trip_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            price_eur REAL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at);

        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT,
            trips_checked INTEGER,
            alerts_generated INTEGER,
            error TEXT
        );
    """),
    (2, "ALTER TABLE state ADD COLUMN flash_until TEXT;"),
    (3, """
        CREATE TABLE IF NOT EXISTS hotel_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_date TEXT NOT NULL,
            trip_name TEXT NOT NULL,
            hotel_name TEXT NOT NULL,
            source TEXT NOT NULL,
            price_local REAL NOT NULL,
            currency TEXT NOT NULL,
            price_eur REAL,
            checkin_date TEXT NOT NULL,
            checkout_date TEXT NOT NULL,
            nights INTEGER,
            booking_url TEXT,
            captured_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hc_trip ON hotel_checks(trip_name);
        CREATE INDEX IF NOT EXISTS idx_hc_date ON hotel_checks(check_date);
        CREATE INDEX IF NOT EXISTS idx_hc_hotel ON hotel_checks(hotel_name);

        CREATE TABLE IF NOT EXISTS hotel_state (
            hotel_name TEXT NOT NULL,
            trip_name TEXT NOT NULL,
            lowest_price_eur REAL,
            lowest_seen_date TEXT,
            lowest_source TEXT,
            rolling_json TEXT,
            last_check_at TEXT,
            PRIMARY KEY (hotel_name, trip_name)
        );
    """),
    (4, """
        ALTER TABLE hotel_state ADD COLUMN last_error TEXT;
        ALTER TABLE hotel_state ADD COLUMN consecutive_failures INTEGER DEFAULT 0;
        ALTER TABLE hotel_state ADD COLUMN last_alert_at TEXT;
    """),
    # Les requêtes du dashboard filtrent toutes sur (trip_name, check_date)
    # puis agrègent price_eur : index couvrant pour éviter le balayage.
    (5, """
        CREATE INDEX IF NOT EXISTS idx_checks_trip_date
            ON checks(trip_name, check_date, price_eur);
    """),
    # Empreinte des paramètres de recherche : quand ils changent, le
    # « plus bas » enregistré ne porte plus sur le même voyage et doit
    # être réinitialisé. L'historique des relevés, lui, est conservé.
    (6, """
        ALTER TABLE state ADD COLUMN config_hash TEXT;
        ALTER TABLE hotel_state ADD COLUMN config_hash TEXT;
    """),
    # Sondes par compagnie. Table SÉPARÉE, sur le précédent des hôtels
    # (v3) plutôt qu'un discriminant `source` dans `checks` : une sonde
    # mesure UNE compagnie sur UNE route, là où fli mesure le marché.
    # Mélangées dans `checks`, ses lignes décalaient le 10e percentile
    # (prix distincts, non pondérés → fausse alerte basse), polluaient
    # trip_history et imposaient un NOT LIKE sur le chemin de lecture le
    # plus chaud. Séparées, aucune de ces requêtes ne change d'une ligne.
    (7, """
        CREATE TABLE IF NOT EXISTS probe_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_date TEXT NOT NULL,
            trip_name TEXT NOT NULL,
            probe TEXT NOT NULL,
            carrier TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            price_local REAL NOT NULL,
            currency TEXT NOT NULL,
            price_eur REAL,
            outbound_date TEXT,
            return_date TEXT,
            out_h REAL,
            ret_h REAL,
            out_stops INTEGER,
            ret_stops INTEGER,
            airlines TEXT,
            fare_family TEXT,
            booking_url TEXT,
            captured_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pc_trip_date
            ON probe_checks(trip_name, check_date, price_eur);
        CREATE INDEX IF NOT EXISTS idx_pc_cell
            ON probe_checks(trip_name, probe, outbound_date, return_date);

        CREATE TABLE IF NOT EXISTS probe_quota (
            bucket TEXT NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bucket, day)
        );

        CREATE TABLE IF NOT EXISTS probe_cursor (
            probe TEXT NOT NULL,
            trip_name TEXT NOT NULL,
            last_out TEXT,
            last_ret TEXT,
            grid_hash TEXT,
            last_error TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (probe, trip_name)
        );
    """),
    # `blocked_until` : notre clé de jour est Europe/Paris, celle d'Air
    # France est inconnue. Un 403 « Over Rate » reçu juste après minuit
    # peut donc appartenir à SA journée de la veille — fermer la nôtre
    # d'office y condamnait 24 h de relevés. On met le seau en pause.
    #
    # Le reste : état d'alerte par (sonde, période). Une sonde ne peut
    # pas alerter sur l'état du marché — elle ne voit qu'une compagnie —
    # mais elle peut alerter sur SON propre plus bas, ce qui demande une
    # référence à elle, et une empreinte du produit mesuré pour la
    # remettre à zéro quand la compagnie ou la cabine change.
    (8, """
        ALTER TABLE probe_quota  ADD COLUMN blocked_until TEXT;
        ALTER TABLE probe_cursor ADD COLUMN lowest_price_eur REAL;
        ALTER TABLE probe_cursor ADD COLUMN lowest_out TEXT;
        ALTER TABLE probe_cursor ADD COLUMN lowest_ret TEXT;
        ALTER TABLE probe_cursor ADD COLUMN lowest_seen_at TEXT;
        ALTER TABLE probe_cursor ADD COLUMN last_alert_at TEXT;
        ALTER TABLE probe_cursor ADD COLUMN product_hash TEXT;
    """),
]


def _current_version(c: sqlite3.Connection) -> int:
    """Return current schema version, 0 if fresh database."""
    # Check if schema_version table exists
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not row:
        return 0
    row = c.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def _detect_pre_migration_db(c: sqlite3.Connection) -> bool:
    """Check if the DB was created before the migration system existed."""
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='checks'"
    ).fetchone()
    return row is not None


def _split_statements(sql: str) -> list[str]:
    """Découpe un script en instructions : executescript committe
    implicitement, il ne peut donc pas servir dans une transaction."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if buf.strip() and sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+(?:COLUMN\s+)?(\w+)", re.IGNORECASE
)


def _add_column_already_applied(c: sqlite3.Connection, stmt: str) -> bool:
    """Rend les ALTER TABLE ADD COLUMN rejouables : une migration
    interrompue autrefois a pu en appliquer une partie sans se taguer."""
    m = _ADD_COLUMN_RE.match(stmt)
    if not m:
        return False
    table, column = m.group(1), m.group(2)
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _apply_migration(c: sqlite3.Connection, version: int, sql: str) -> None:
    """DDL et tag de version dans la même transaction : sinon un crash
    entre les deux fait rejouer la migration au démarrage suivant."""
    c.execute("BEGIN IMMEDIATE")
    try:
        for stmt in _split_statements(sql):
            if _add_column_already_applied(c, stmt):
                continue
            c.execute(stmt)
        c.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now().isoformat()),
        )
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    log.info(f"DB migration: applied v{version}")


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        # WAL est persistant dans le fichier : posé une fois ici plutôt
        # que rejoué à chaque connexion.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        cur = _current_version(c)

        # Bootstrap: DB existante créée avant le système de migrations
        if cur == 0 and _detect_pre_migration_db(c):
            c.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (?, ?)",
                (1, datetime.now().isoformat()),
            )
            cur = 1
            log.info("DB migration: existing database tagged as v1")

        # Apply pending migrations
        for version, sql in MIGRATIONS:
            if version <= cur:
                continue
            _apply_migration(c, version, sql)

        # Le seau de quota est une ligne par jour et par credential :
        # purge opportuniste plutôt qu'une tâche planifiée de plus.
        try:
            probe_quota_purge(c)
        except sqlite3.Error as e:      # jamais bloquant au démarrage
            log.warning(f"  ⚠ purge probe_quota: {e}")


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    # Deux runs peuvent écrire en même temps (cron + /api/run-now) :
    # on attend le verrou au lieu d'échouer sur « database is locked ».
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
    finally:
        c.close()


def insert_check(c: sqlite3.Connection, row: dict[str, Any]) -> None:
    c.execute(
        """INSERT INTO checks (
            check_date, trip_name, source, origin, destination,
            price_local, currency, price_eur,
            outbound_date, return_date, out_h, ret_h,
            out_stops, ret_stops, airlines, booking_url, captured_at
        ) VALUES (
            :check_date, :trip_name, :source, :origin, :destination,
            :price_local, :currency, :price_eur,
            :outbound_date, :return_date, :out_h, :ret_h,
            :out_stops, :ret_stops, :airlines, :booking_url, :captured_at
        )""",
        row,
    )


def get_state(c: sqlite3.Connection, trip: str) -> dict[str, Any] | None:
    row = c.execute("SELECT * FROM state WHERE trip_name=?", (trip,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("rolling_json"):
        d["rolling"] = json.loads(d["rolling_json"])
    return d


_VALID_STATE_COLS = frozenset({
    "lowest_price_eur", "lowest_seen_date", "lowest_origin",
    "lowest_destination", "lowest_booking_url", "rolling_json",
    "last_check_at", "flash_until", "config_hash",
})


def upsert_state(c: sqlite3.Connection, trip: str, **kwargs) -> None:
    if "rolling" in kwargs:
        kwargs["rolling_json"] = json.dumps(kwargs.pop("rolling"))
    bad = set(kwargs.keys()) - _VALID_STATE_COLS
    if bad:
        raise ValueError(f"Invalid state columns: {bad}")
    if not kwargs:
        c.execute("INSERT OR IGNORE INTO state (trip_name) VALUES (?)", (trip,))
        return
    # UPSERT en une seule instruction : le SELECT puis UPDATE/INSERT
    # précédent pouvait perdre l'écriture d'un run concurrent.
    cols = ["trip_name"] + list(kwargs.keys())
    placeholders = ", ".join(f":{k}" for k in cols)
    sets = ", ".join(f"{k}=excluded.{k}" for k in kwargs)
    c.execute(
        f"INSERT INTO state ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(trip_name) DO UPDATE SET {sets}",
        {**kwargs, "trip_name": trip},
    )


def log_alert(c: sqlite3.Connection, trip: str, kind: str,
              price_eur: float, payload: dict) -> None:
    c.execute(
        """INSERT INTO alerts (sent_at, trip_name, kind, price_eur, payload_json)
           VALUES (?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(), trip, kind, price_eur, json.dumps(payload)),
    )


def start_run(c: sqlite3.Connection) -> int:
    cur = c.execute(
        "INSERT INTO run_log (started_at, status) VALUES (?, 'running')",
        (datetime.now().isoformat(),),
    )
    return cur.lastrowid


def finish_run(c: sqlite3.Connection, run_id: int, status: str,
               trips_checked: int, alerts: int, error: str | None) -> None:
    c.execute(
        """UPDATE run_log SET finished_at=?, status=?,
           trips_checked=?, alerts_generated=?, error=? WHERE id=?""",
        (datetime.now().isoformat(), status, trips_checked, alerts, error, run_id),
    )


# ── Analytics ────────────────────────────────────────────────────

def percentile_rank(c: sqlite3.Connection, trip: str,
                    price_eur: float) -> float | None:
    """Return the percentile rank (0-100) of price_eur in the trip's history.
    0 = cheapest ever, 100 = most expensive ever.
    Returns None if fewer than 5 historical data points."""
    rows = c.execute("""
        SELECT DISTINCT price_eur FROM checks
        WHERE trip_name = ? AND price_eur IS NOT NULL
          AND source NOT LIKE '%\\_th' ESCAPE '\\'
        ORDER BY price_eur
    """, (trip,)).fetchall()
    prices = [r[0] for r in rows]
    if len(prices) < 5:
        return None
    below = sum(1 for p in prices if p < price_eur)
    equal = sum(1 for p in prices if p == price_eur)
    # Percentile rank formula: (below + 0.5 * equal) / total * 100
    rank = (below + 0.5 * equal) / len(prices) * 100
    return round(rank, 1)


# ── Heatmap / stats / trend ──────────────────────────────────────

def heatmap_data(c: sqlite3.Connection, trip: str,
                 days: int = 14) -> list[dict]:
    """Best price per (outbound_date, return_date) combo, relevés récents."""
    # Sans borne temporelle, une combinaison vue une seule fois à bas prix
    # restait affichée comme la moins chère indéfiniment.
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT outbound_date, return_date, MIN(price_eur) AS best_eur,
               MAX(check_date) AS last_seen,
               GROUP_CONCAT(DISTINCT airlines) AS airlines
        FROM checks
        WHERE trip_name = ? AND price_eur IS NOT NULL
          AND source NOT LIKE '%\\_ow' ESCAPE '\\'
          AND check_date >= ?
        GROUP BY outbound_date, return_date
        ORDER BY outbound_date, return_date
    """, (trip, cutoff)).fetchall()
    return [dict(r) for r in rows]


def price_trend(c: sqlite3.Connection, trip: str,
                days: int = 7) -> list[tuple[str, float]]:
    """Returns list of (date, min_price) for last N days for trend calculation."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT check_date, MIN(price_eur) AS min_price
        FROM checks
        WHERE trip_name = ? AND price_eur IS NOT NULL
          AND source NOT LIKE '%\\_ow' ESCAPE '\\' AND source NOT LIKE '%\\_th' ESCAPE '\\'
          AND check_date >= ?
        GROUP BY check_date
        ORDER BY check_date
    """, (trip, cutoff)).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── Queries for the API ──────────────────────────────────────────

def trips_summary(c: sqlite3.Connection) -> list[dict]:
    """Per-trip overview: best ever, current, last check.

    Les agrégats portent sur les MEILLEURS prix par relevé et non sur
    toutes les paires : « moy 30j » et « haut » incluaient des liaisons
    hors sujet, ce qui faisait paraître n'importe quel prix avantageux.
    Un seul regroupement en CTE, puis jointure : en sous-requêtes
    corrélées, chacune rebalayait `checks` pour chaque période.
    Le `_` de LIKE est un joker : il est échappé, sinon le filtre exclut
    tout ce qui finit par « ow » ou « th ».
    """
    rows = c.execute("""
        WITH runs AS (
            SELECT trip_name,
                   captured_at,
                   MAX(check_date) AS check_date,
                   MIN(price_eur)  AS m
            FROM checks
            WHERE price_eur IS NOT NULL
              AND source NOT LIKE '%\\_ow' ESCAPE '\\'
              AND source NOT LIKE '%\\_th' ESCAPE '\\'
            GROUP BY trip_name, captured_at
        ),
        agg AS (
            SELECT trip_name,
                   MAX(captured_at) AS last_captured_at,
                   MAX(m)           AS all_time_high,
                   AVG(CASE WHEN check_date >= date('now', '-30 days')
                            THEN m END) AS avg_30d
            FROM runs
            GROUP BY trip_name
        )
        SELECT
            s.trip_name,
            s.lowest_price_eur AS all_time_low,
            s.lowest_seen_date,
            s.lowest_origin,
            s.lowest_destination,
            s.lowest_booking_url,
            s.last_check_at,
            a.last_captured_at,
            a.avg_30d,
            a.all_time_high,
            -- Prix du DERNIER relevé, pas le minimum de la journée :
            -- sinon un prix vu à 6 h puis disparu restait affiché jusqu'à
            -- minuit alors que le graphique et la notification
            -- « Hausse » annonçaient autre chose.
            (SELECT r.m FROM runs r
              WHERE r.trip_name = s.trip_name
                AND r.captured_at = a.last_captured_at) AS current_best
        FROM state s
        LEFT JOIN agg a ON a.trip_name = s.trip_name
        ORDER BY s.trip_name
    """).fetchall()
    return [dict(r) for r in rows]

def trip_history(c: sqlite3.Connection, trip: str,
                 days: int = 60) -> list[dict]:
    """Best price per run for a trip, across all origin/dest combinations."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT captured_at,
               MIN(price_eur) AS price_eur,
               GROUP_CONCAT(DISTINCT origin) AS origins,
               GROUP_CONCAT(DISTINCT destination) AS destinations
        FROM checks
        WHERE trip_name = ? AND check_date >= ? AND price_eur IS NOT NULL
        GROUP BY captured_at
        ORDER BY captured_at
    """, (trip, cutoff)).fetchall()
    return [dict(r) for r in rows]


def trip_history_by_route(c: sqlite3.Connection, trip: str,
                          days: int = 60) -> list[dict]:
    """Best price per route per run (captured_at) for a trip."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT captured_at, origin, destination,
               MIN(price_eur) AS price_eur
        FROM checks
        WHERE trip_name = ? AND check_date >= ? AND price_eur IS NOT NULL
        GROUP BY captured_at, origin, destination
        ORDER BY captured_at, origin, destination
    """, (trip, cutoff)).fetchall()
    return [dict(r) for r in rows]


def trip_breakdown(c: sqlite3.Connection, trip: str) -> list[dict]:
    """Best price per origin/destination, with source and dates."""
    # MAX(c.check_date) : sans agrégat, SQLite prenait les colonnes nues
    # (dates, compagnies, « vu le ») sur une ligne arbitraire du groupe.
    rows = c.execute("""
        SELECT c.origin, c.destination, c.price_eur AS best_eur,
               MAX(c.check_date) AS last_seen, c.airlines,
               c.outbound_date, c.return_date, c.booking_url, c.source
        FROM checks c
        INNER JOIN (
            SELECT origin, destination, MIN(price_eur) AS min_price
            FROM checks
            WHERE trip_name = ? AND price_eur IS NOT NULL
              AND check_date >= date('now', '-30 days')
            GROUP BY origin, destination
        ) best ON c.origin = best.origin
             AND c.destination = best.destination
             AND c.price_eur = best.min_price
        WHERE c.trip_name = ? AND c.price_eur IS NOT NULL
          AND c.check_date >= date('now', '-30 days')
        GROUP BY c.origin, c.destination
        ORDER BY best_eur ASC
    """, (trip, trip)).fetchall()
    return [dict(r) for r in rows]


def recent_alerts(c: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = c.execute(
        "SELECT * FROM alerts ORDER BY sent_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        out.append(d)
    return out


def last_runs(c: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = c.execute(
        "SELECT * FROM run_log ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Hotel queries ──────────────────────────────────────────────

def insert_hotel_check(c: sqlite3.Connection, row: dict[str, Any]) -> None:
    c.execute(
        """INSERT INTO hotel_checks (
            check_date, trip_name, hotel_name, source,
            price_local, currency, price_eur,
            checkin_date, checkout_date, nights,
            booking_url, captured_at
        ) VALUES (
            :check_date, :trip_name, :hotel_name, :source,
            :price_local, :currency, :price_eur,
            :checkin_date, :checkout_date, :nights,
            :booking_url, :captured_at
        )""",
        row,
    )


def get_hotel_state(c: sqlite3.Connection, hotel: str,
                    trip: str) -> dict[str, Any] | None:
    row = c.execute(
        "SELECT * FROM hotel_state WHERE hotel_name=? AND trip_name=?",
        (hotel, trip),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("rolling_json"):
        d["rolling"] = json.loads(d["rolling_json"])
    return d


_VALID_HOTEL_STATE_COLS = frozenset({
    "lowest_price_eur", "lowest_seen_date", "lowest_source",
    "rolling_json", "last_check_at", "last_error",
    "consecutive_failures", "last_alert_at", "config_hash",
})


def upsert_hotel_state(c: sqlite3.Connection, hotel: str,
                       trip: str, **kwargs) -> None:
    if "rolling" in kwargs:
        kwargs["rolling_json"] = json.dumps(kwargs.pop("rolling"))
    bad = set(kwargs.keys()) - _VALID_HOTEL_STATE_COLS
    if bad:
        raise ValueError(f"Invalid hotel_state columns: {bad}")
    current = c.execute(
        "SELECT 1 FROM hotel_state WHERE hotel_name=? AND trip_name=?",
        (hotel, trip),
    ).fetchone()
    if current:
        sets = ", ".join(f"{k}=:{k}" for k in kwargs)
        c.execute(
            f"UPDATE hotel_state SET {sets} "
            "WHERE hotel_name=:hotel AND trip_name=:trip",
            {**kwargs, "hotel": hotel, "trip": trip},
        )
    else:
        cols = ["hotel_name", "trip_name"] + list(kwargs.keys())
        vals = ["?"] * len(cols)
        c.execute(
            f"INSERT INTO hotel_state ({','.join(cols)}) VALUES ({','.join(vals)})",
            (hotel, trip, *kwargs.values()),
        )


def hotel_summary(c: sqlite3.Connection) -> list[dict]:
    """Summary of all monitored hotels per trip."""
    rows = c.execute("""
        SELECT hs.hotel_name, hs.trip_name,
               hs.lowest_price_eur, hs.lowest_seen_date, hs.lowest_source,
               hs.last_check_at, hs.last_error, hs.consecutive_failures,
               -- Prix du DERNIER relevé, pas le minimum de la journée :
               -- sinon un prix vu à 6h et disparu reste affiché jusqu'à
               -- minuit alors que le graphique montre autre chose.
               (SELECT MIN(price_eur) FROM hotel_checks
                 WHERE hotel_name = hs.hotel_name
                   AND trip_name = hs.trip_name
                   AND price_eur IS NOT NULL
                   AND captured_at = (SELECT MAX(captured_at)
                                        FROM hotel_checks
                                       WHERE hotel_name = hs.hotel_name
                                         AND trip_name = hs.trip_name)
               ) AS current_best,
               (SELECT MAX(captured_at) FROM hotel_checks
                 WHERE hotel_name = hs.hotel_name
                   AND trip_name = hs.trip_name
               ) AS last_captured_at,
               -- Moyenne des MEILLEURS prix par relevé, pour être
               -- homogène avec « bas » et « prix actuel ».
               (SELECT AVG(r.m) FROM (
                    SELECT hotel_name, trip_name, captured_at,
                           MIN(price_eur) AS m
                      FROM hotel_checks
                     WHERE price_eur IS NOT NULL
                       AND check_date >= date('now', '-30 days')
                     GROUP BY hotel_name, trip_name, captured_at
                 ) r
                 WHERE r.hotel_name = hs.hotel_name
                   AND r.trip_name = hs.trip_name
               ) AS avg_30d
        FROM hotel_state hs
        ORDER BY hs.trip_name
    """).fetchall()
    return [dict(r) for r in rows]


def hotel_history(c: sqlite3.Connection, hotel: str, trip: str,
                  days: int = 60) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT check_date, MIN(price_eur) AS price_eur,
               GROUP_CONCAT(DISTINCT source) AS sources
        FROM hotel_checks
        WHERE hotel_name = ? AND trip_name = ? AND check_date >= ?
          AND price_eur IS NOT NULL
        GROUP BY check_date
        ORDER BY check_date
    """, (hotel, trip, cutoff)).fetchall()
    return [dict(r) for r in rows]


def hotel_breakdown(c: sqlite3.Connection, hotel: str,
                    trip: str) -> list[dict]:
    """Best price per provider for a hotel/trip."""
    # Deux agrégats (MIN + MAX) dans un même SELECT rendent les colonnes
    # nues arbitraires en SQLite : on isole le minimum puis on rejoint.
    rows = c.execute("""
        SELECT b.source, b.best_eur, b.last_seen,
               h.price_local, h.currency, h.booking_url
        FROM (
            SELECT source, MIN(price_eur) AS best_eur,
                   MAX(check_date) AS last_seen
            FROM hotel_checks
            WHERE hotel_name = :hotel AND trip_name = :trip
              AND price_eur IS NOT NULL
              AND check_date >= date('now', '-30 days')
            GROUP BY source
        ) b
        JOIN hotel_checks h
          ON h.hotel_name = :hotel AND h.trip_name = :trip
         AND h.source = b.source AND h.price_eur = b.best_eur
        GROUP BY b.source
        ORDER BY b.best_eur ASC
    """, {"hotel": hotel, "trip": trip}).fetchall()
    return [dict(r) for r in rows]


# ── Probe queries ──────────────────────────────────────────────
#
# Les sondes vivent dans leurs propres tables : aucune des requêtes
# ci-dessus ne les voit, donc ni l'état, ni les alertes, ni le 10e
# percentile, ni les séries du dashboard ne peuvent être faussés par un
# relevé mono-compagnie.


def insert_probe_check(c: sqlite3.Connection, row: dict[str, Any]) -> None:
    c.execute(
        """INSERT INTO probe_checks (
            check_date, trip_name, probe, carrier, origin, destination,
            price_local, currency, price_eur,
            outbound_date, return_date, out_h, ret_h,
            out_stops, ret_stops, airlines, fare_family,
            booking_url, captured_at
        ) VALUES (
            :check_date, :trip_name, :probe, :carrier, :origin, :destination,
            :price_local, :currency, :price_eur,
            :outbound_date, :return_date, :out_h, :ret_h,
            :out_stops, :ret_stops, :airlines, :fare_family,
            :booking_url, :captured_at
        )""",
        row,
    )


def probe_quota_take(c: sqlite3.Connection, bucket: str, limit: int,
                     *, day: str | None = None,
                     now: str | None = None) -> bool:
    """Prend un jeton dans le seau du jour. False si le plafond est atteint.

    Le seau appartient au CREDENTIAL, pas à la sonde : vérifié en
    production, les hosts AF et KL d'une même clé Air France partagent
    un unique quota de 100 requêtes/jour.

    UPSERT en une instruction plutôt que SELECT puis UPDATE : deux runs
    peuvent écrire en même temps (cron + /api/run-now, cf. conn()), et
    la version en deux temps accordait deux fois le dernier jeton.
    """
    if limit <= 0:
        return False
    d = day or date.today().isoformat()
    n = now or datetime.now().isoformat()
    cur = c.execute(
        "INSERT INTO probe_quota (bucket, day, used) VALUES (:b, :d, 1) "
        "ON CONFLICT(bucket, day) DO UPDATE SET used = used + 1 "
        "WHERE used < :q "
        "  AND (blocked_until IS NULL OR blocked_until <= :n)",
        {"b": bucket, "d": d, "q": limit, "n": n},
    )
    return bool(cur.rowcount)


def probe_quota_used(c: sqlite3.Connection, bucket: str,
                     *, day: str | None = None) -> int:
    d = day or date.today().isoformat()
    row = c.execute(
        "SELECT used FROM probe_quota WHERE bucket=? AND day=?", (bucket, d)
    ).fetchone()
    return int(row["used"]) if row else 0


def probe_quota_purge(c: sqlite3.Connection, keep_days: int = 30) -> None:
    """Le seau est une ligne par jour : sans purge, la table grossit à vie."""
    c.execute("DELETE FROM probe_quota WHERE day < ?",
              ((date.today() - timedelta(days=keep_days)).isoformat(),))


def probe_cursor_get(c: sqlite3.Connection, probe: str,
                     trip: str) -> dict[str, Any] | None:
    row = c.execute(
        "SELECT * FROM probe_cursor WHERE probe=? AND trip_name=?",
        (probe, trip),
    ).fetchone()
    return dict(row) if row else None


def probe_cursor_set(c: sqlite3.Connection, probe: str, trip: str,
                     *, last_out: str | None, last_ret: str | None,
                     grid_hash: str, now: str,
                     error: str | None = None) -> None:
    """Mémorise la DERNIÈRE CELLULE visitée, pas un index.

    Un index devenait faux dès qu'une date échue disparaissait de la
    grille : la liste se décalait et la rotation repartait du début
    chaque jour, si bien que les retours tardifs n'étaient jamais
    interrogés. Une cellule (aller, retour) se retrouve par bissection
    dans la liste du jour, même si des cellules ont disparu autour.
    """
    fail_sql = ("consecutive_failures = consecutive_failures + 1"
                if error else "consecutive_failures = 0")
    c.execute(
        f"""INSERT INTO probe_cursor (
                probe, trip_name, last_out, last_ret, grid_hash,
                last_error, consecutive_failures, updated_at)
            VALUES (:probe, :trip, :last_out, :last_ret, :grid_hash,
                    :error, :seed, :now)
            ON CONFLICT(probe, trip_name) DO UPDATE SET
                last_out = :last_out, last_ret = :last_ret,
                grid_hash = :grid_hash, last_error = :error,
                {fail_sql}, updated_at = :now""",
        {"probe": probe, "trip": trip, "last_out": last_out,
         "last_ret": last_ret, "grid_hash": grid_hash, "error": error,
         "seed": 1 if error else 0, "now": now},
    )


def probe_quota_fill(c: sqlite3.Connection, bucket: str, limit: int,
                     *, day: str | None = None) -> None:
    """Ferme le seau pour la journée.

    Appelé quand le FOURNISSEUR répond « quota dépassé » : notre compteur
    est alors en retard sur le sien (une requête perdue, un décalage de
    fuseau sur l'heure de remise à zéro). Sans ça, chaque période et
    chaque run suivant du jour repartait émettre une requête vouée au 403.
    """
    d = day or date.today().isoformat()
    c.execute(
        "INSERT INTO probe_quota (bucket, day, used) VALUES (?, ?, ?) "
        "ON CONFLICT(bucket, day) DO UPDATE SET used = MAX(used, ?)",
        (bucket, d, limit, limit),
    )


def probe_quota_block(c: sqlite3.Connection, bucket: str, limit: int,
                      *, until: str, day: str | None = None) -> None:
    """Met le seau en PAUSE parce que le fournisseur a refusé.

    Pourquoi une pause et non la fermeture de la journée : notre clé de
    jour est Europe/Paris, celle d'Air France est inconnue. Un 403 reçu
    peu après minuit peut donc appartenir encore à SA journée de la
    veille, et fermer la nôtre d'office y condamnerait 24 h de relevés
    pour rien. La pause borne les dégâts à quelques heures et se répare
    seule.

    Exception : si notre propre compteur confirme qu'on a déjà consommé
    la moitié du plafond, c'est un vrai épuisement — on ferme la journée.
    """
    d = day or date.today().isoformat()
    used = probe_quota_used(c, bucket, day=d)
    c.execute(
        "INSERT INTO probe_quota (bucket, day, used, blocked_until) "
        "VALUES (:b, :d, :u, :until) "
        "ON CONFLICT(bucket, day) DO UPDATE SET "
        "  blocked_until = :until, used = MAX(used, :u)",
        {"b": bucket, "d": d, "until": until,
         "u": limit if used >= limit // 2 else used},
    )


_VALID_PROBE_STATE_COLS = frozenset({
    "lowest_price_eur", "lowest_out", "lowest_ret", "lowest_seen_at",
    "last_alert_at", "product_hash",
})


def probe_state_set(c: sqlite3.Connection, probe: str, trip: str,
                    **kwargs: Any) -> None:
    """État d'alerte d'une sonde. Même table que le curseur : même clé."""
    bad = set(kwargs) - _VALID_PROBE_STATE_COLS
    if bad:
        raise ValueError(f"Colonnes d'état de sonde invalides : {bad}")
    if not kwargs:
        return
    cols = ["probe", "trip_name"] + list(kwargs)
    placeholders = ", ".join(f":{k}" for k in cols)
    sets = ", ".join(f"{k}=excluded.{k}" for k in kwargs)
    c.execute(
        f"INSERT INTO probe_cursor ({', '.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(probe, trip_name) DO UPDATE SET {sets}",
        {**kwargs, "probe": probe, "trip_name": trip},
    )


def probe_cursors_all(c: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Tous les curseurs en une requête, indexés par (sonde, période)."""
    rows = c.execute("SELECT * FROM probe_cursor").fetchall()
    return {(r["probe"], r["trip_name"]): dict(r) for r in rows}


def probe_heatmap(c: sqlite3.Connection, trip: str, probe: str,
                  carrier: str | None = None,
                  days: int = 30) -> list[dict]:
    """Grille aller × retour d'UNE sonde, source homogène.

    Une seule sonde par matrice, et jamais mélangée aux relevés fli :
    fli est multi-compagnies et trié au moins cher, donc ≤ une compagnie
    seule par construction. Mélangés, le MIN aurait peint en « meilleure
    date » la seule cellule que fli couvre — un artefact de couverture
    lu comme un conseil de dates.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    # Filtre sur la compagnie en plus du nom : changer AF en KL sur une
    # sonde existante ne renomme pas la sonde, et la grille aurait alors
    # mélangé deux produits différents dans le même MIN.
    rows = c.execute(f"""
        SELECT outbound_date, return_date, MIN(price_eur) AS best_eur,
               MAX(check_date) AS last_seen,
               GROUP_CONCAT(DISTINCT airlines) AS airlines
        FROM probe_checks
        WHERE trip_name = :trip AND probe = :probe AND price_eur IS NOT NULL
          AND check_date >= :cutoff
          {"AND carrier = :carrier" if carrier else ""}
        GROUP BY outbound_date, return_date
        ORDER BY outbound_date, return_date
    """, {"trip": trip, "probe": probe, "cutoff": cutoff,
          "carrier": carrier} if carrier else
        {"trip": trip, "probe": probe, "cutoff": cutoff}).fetchall()
    return [dict(r) for r in rows]


def probe_summary(c: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Couverture et meilleur prix par (sonde, période)."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    # Le regroupement doit correspondre EXACTEMENT à la clé sur laquelle
    # l'API indexe le résultat, (probe, trip_name) : avec `carrier` dans
    # le GROUP BY, une sonde passée d'AF à KL produisait deux lignes dont
    # une écrasait silencieusement l'autre côté appelant.
    rows = c.execute("""
        SELECT p.probe, p.trip_name,
               GROUP_CONCAT(DISTINCT p.carrier) AS carriers,
               COUNT(DISTINCT p.outbound_date || '>' || p.return_date) AS cells,
               MIN(p.price_eur) AS best_eur,
               MAX(p.captured_at) AS last_capture
        FROM probe_checks p
        WHERE p.price_eur IS NOT NULL AND p.check_date >= ?
        GROUP BY p.probe, p.trip_name
        ORDER BY p.probe, p.trip_name
    """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]
