import sqlite3
import pytest
import db


# DB tests use a real in-memory SQLite connection — no mocking needed since sqlite3 has no I/O cost.
@pytest.fixture
def conn():
    c = db.init_db(":memory:")
    yield c
    c.close()


class TestInitDb:
    def test_creates_snapshots_table(self, conn):
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert ("snapshots",) in tables

    def test_creates_index(self, conn):
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        assert ("idx_snapshots_filter",) in indexes


class TestWriteSnapshot:
    def test_inserts_row(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 42)
        row = conn.execute(
            "SELECT created_on, filter_name, task_count FROM snapshots"
        ).fetchone()
        assert row == ("2026-06-01", "next 7 days", 42)

    def test_idempotent_overwrite(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 42)
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 55)
        rows = conn.execute(
            "SELECT task_count FROM snapshots "
            "WHERE created_on='2026-06-01' AND filter_name='next 7 days'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 55

    def test_different_filters_same_day_stored_separately(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 10)
        db.write_snapshot(conn, "2026-06-01", "next 30 days", 20)
        count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count == 2


class TestReadLatestBefore:
    def test_returns_empty_when_no_rows(self, conn):
        assert db.read_latest_before(conn, "2026-06-05") == {}

    def test_returns_most_recent_count_for_filter(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 40)
        db.write_snapshot(conn, "2026-06-03", "next 7 days", 42)
        result = db.read_latest_before(conn, "2026-06-05")
        assert result == {"next 7 days": 42}

    def test_ignores_same_day_row(self, conn):
        db.write_snapshot(conn, "2026-06-05", "next 7 days", 99)
        assert db.read_latest_before(conn, "2026-06-05") == {}

    def test_handles_multiple_filters(self, conn):
        db.write_snapshot(conn, "2026-06-04", "next 7 days", 42)
        db.write_snapshot(conn, "2026-06-04", "next 30 days", 118)
        result = db.read_latest_before(conn, "2026-06-05")
        assert result == {"next 7 days": 42, "next 30 days": 118}

    def test_returns_most_recent_not_oldest(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 10)
        db.write_snapshot(conn, "2026-06-03", "next 7 days", 20)
        db.write_snapshot(conn, "2026-06-04", "next 7 days", 30)
        result = db.read_latest_before(conn, "2026-06-05")
        assert result["next 7 days"] == 30
