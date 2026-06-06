import sqlite3
from datetime import date, timedelta

_DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_on  TEXT NOT NULL,
    filter_name TEXT NOT NULL,
    task_count  INTEGER NOT NULL,
    UNIQUE (created_on, filter_name)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_filter
    ON snapshots (filter_name, created_on);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(_DDL)
    conn.commit()
    return conn


def write_snapshot(
    conn: sqlite3.Connection, created_on: str, filter_name: str, count: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (created_on, filter_name, task_count)"
        " VALUES (?, ?, ?)",
        (created_on, filter_name, count),
    )
    conn.commit()


def read_latest_before(conn: sqlite3.Connection, before_date: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT filter_name, task_count
        FROM snapshots
        WHERE created_on = (
            SELECT MAX(s2.created_on)
            FROM snapshots s2
            WHERE s2.filter_name = snapshots.filter_name
              AND s2.created_on < ?
        )
        """,
        (before_date,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def read_last_n_days(
    conn: sqlite3.Connection,
    filter_names: list[str],
    n: int = 7,
    as_of: str | None = None,
) -> dict[str, list[tuple[str, int]]]:
    if not filter_names:
        return {}
    if as_of is None:
        as_of = date.today().isoformat()
    start = (date.fromisoformat(as_of) - timedelta(days=n - 1)).isoformat()
    placeholders = ",".join("?" * len(filter_names))
    rows = conn.execute(
        f"SELECT filter_name, created_on, task_count FROM snapshots "
        f"WHERE filter_name IN ({placeholders}) AND created_on >= ? AND created_on <= ? "
        f"ORDER BY filter_name, created_on",
        (*filter_names, start, as_of),
    ).fetchall()
    result: dict[str, list[tuple[str, int]]] = {name: [] for name in filter_names}
    for filter_name, created_on, task_count in rows:
        result[filter_name].append((created_on, task_count))
    return result
