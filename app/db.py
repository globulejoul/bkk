"""SQLite persistence for price checks, state, and alerts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

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
    # Pour ajouter une migration future :
    # (2, "ALTER TABLE checks ADD COLUMN new_col TEXT;"),
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


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        cur = _current_version(c)

        # Bootstrap: DB existante créée avant le système de migrations
        if cur == 0 and _detect_pre_migration_db(c):
            c.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)
            from datetime import datetime
            c.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, datetime.now().isoformat()),
            )
            cur = 1
            print("DB migration: existing database tagged as v1")

        # Ensure schema_version table exists
        if cur == 0:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

        # Apply pending migrations
        for version, sql in MIGRATIONS:
            if version <= cur:
                continue
            c.executescript(sql)
            from datetime import datetime
            c.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now().isoformat()),
            )
            print(f"DB migration: applied v{version}")


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
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
    "last_check_at",
})


def upsert_state(c: sqlite3.Connection, trip: str, **kwargs) -> None:
    if "rolling" in kwargs:
        kwargs["rolling_json"] = json.dumps(kwargs.pop("rolling"))
    bad = set(kwargs.keys()) - _VALID_STATE_COLS
    if bad:
        raise ValueError(f"Invalid state columns: {bad}")
    current = c.execute("SELECT 1 FROM state WHERE trip_name=?", (trip,)).fetchone()
    if current:
        sets = ", ".join(f"{k}=:{k}" for k in kwargs)
        c.execute(f"UPDATE state SET {sets} WHERE trip_name=:trip",
                  {**kwargs, "trip": trip})
    else:
        cols = ["trip_name"] + list(kwargs.keys())
        vals = ["?"] * len(cols)
        c.execute(
            f"INSERT INTO state ({','.join(cols)}) VALUES ({','.join(vals)})",
            (trip, *kwargs.values()),
        )


def log_alert(c: sqlite3.Connection, trip: str, kind: str,
              price_eur: float, payload: dict) -> None:
    from datetime import datetime
    c.execute(
        """INSERT INTO alerts (sent_at, trip_name, kind, price_eur, payload_json)
           VALUES (?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(), trip, kind, price_eur, json.dumps(payload)),
    )


def start_run(c: sqlite3.Connection) -> int:
    from datetime import datetime
    cur = c.execute(
        "INSERT INTO run_log (started_at, status) VALUES (?, 'running')",
        (datetime.now().isoformat(),),
    )
    return cur.lastrowid


def finish_run(c: sqlite3.Connection, run_id: int, status: str,
               trips_checked: int, alerts: int, error: str | None) -> None:
    from datetime import datetime
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
          AND source NOT LIKE '%_th'
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


# ── Queries for the API ──────────────────────────────────────────

def trips_summary(c: sqlite3.Connection) -> list[dict]:
    """Per-trip overview: best ever, current, last check."""
    rows = c.execute("""
        SELECT
            s.trip_name,
            s.lowest_price_eur AS all_time_low,
            s.lowest_seen_date,
            s.lowest_origin,
            s.lowest_destination,
            s.lowest_booking_url,
            s.last_check_at,
            (
                SELECT MIN(price_eur) FROM checks
                WHERE trip_name = s.trip_name
                AND check_date = (SELECT MAX(check_date) FROM checks
                                  WHERE trip_name = s.trip_name)
            ) AS current_best,
            (
                SELECT AVG(price_eur) FROM checks
                WHERE trip_name = s.trip_name
                AND check_date >= date('now', '-30 days')
            ) AS avg_30d,
            (
                SELECT MAX(price_eur) FROM checks
                WHERE trip_name = s.trip_name
            ) AS all_time_high
        FROM state s
        ORDER BY s.trip_name
    """).fetchall()
    return [dict(r) for r in rows]


def trip_history(c: sqlite3.Connection, trip: str,
                 days: int = 60) -> list[dict]:
    """Daily best price for a trip, across all origin/dest combinations."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT check_date,
               MIN(price_eur) AS price_eur,
               GROUP_CONCAT(DISTINCT origin) AS origins,
               GROUP_CONCAT(DISTINCT destination) AS destinations
        FROM checks
        WHERE trip_name = ? AND check_date >= ? AND price_eur IS NOT NULL
        GROUP BY check_date
        ORDER BY check_date
    """, (trip, cutoff)).fetchall()
    return [dict(r) for r in rows]


def trip_breakdown(c: sqlite3.Connection, trip: str) -> list[dict]:
    """Best price per origin/destination, with source and dates."""
    rows = c.execute("""
        SELECT c.origin, c.destination, c.price_eur AS best_eur,
               c.check_date AS last_seen, c.airlines,
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
