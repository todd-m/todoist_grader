import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

import db
import db as db_module
from snapshot import fetch_todoist_filters, resolve_filters, count_filter_tasks, main


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


def _make_resp(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestFetchTodoistFilters:
    @patch("snapshot.requests.post")
    def test_returns_lowercase_keyed_dict(self, mock_post):
        mock_post.return_value = _make_resp({"filters": [
            {"name": "Next 7 Days", "query": "7 days", "is_deleted": False},
        ]})
        result = fetch_todoist_filters("tok")
        assert "next 7 days" in result
        assert result["next 7 days"] == ("Next 7 Days", "7 days")

    @patch("snapshot.requests.post")
    def test_excludes_deleted_filters(self, mock_post):
        mock_post.return_value = _make_resp({"filters": [
            {"name": "Old Filter", "query": "something", "is_deleted": True},
        ]})
        assert fetch_todoist_filters("tok") == {}

    @patch("snapshot.requests.post")
    def test_sends_correct_payload(self, mock_post):
        mock_post.return_value = _make_resp({"filters": []})
        fetch_todoist_filters("mytoken")
        kwargs = mock_post.call_args.kwargs
        assert kwargs["json"]["sync_token"] == "*"
        assert kwargs["json"]["resource_types"] == ["filters"]
        assert kwargs["headers"]["Authorization"] == "Bearer mytoken"

    @patch("snapshot.requests.post")
    def test_returns_empty_when_no_filters(self, mock_post):
        mock_post.return_value = _make_resp({"filters": []})
        assert fetch_todoist_filters("tok") == {}


class TestResolveFilters:
    TODOIST: dict[str, tuple[str, str]] = {
        "next 7 days":  ("Next 7 Days",  "7 days & !subtask"),
        "next 30 days": ("Next 30 Days", "30 days & !subtask"),
    }

    def test_matches_exact_lowercase(self):
        result = resolve_filters(["next 7 days"], self.TODOIST)
        assert result == [("next 7 days", "Next 7 Days", "7 days & !subtask")]

    def test_matches_case_insensitively(self):
        result = resolve_filters(["NEXT 7 DAYS"], self.TODOIST)
        assert len(result) == 1
        config_name, display_name, query = result[0]
        assert config_name == "NEXT 7 DAYS"
        assert display_name == "Next 7 Days"
        assert query == "7 days & !subtask"

    def test_warns_for_missing_name(self, capsys):
        resolve_filters(["nonexistent"], self.TODOIST)
        assert "nonexistent" in capsys.readouterr().err

    def test_skips_unmatched_names(self):
        assert resolve_filters(["nonexistent"], self.TODOIST) == []

    def test_returns_all_matched(self):
        result = resolve_filters(["next 7 days", "next 30 days"], self.TODOIST)
        assert len(result) == 2

    def test_partial_match_skips_unmatched(self, capsys):
        result = resolve_filters(["next 7 days", "bogus"], self.TODOIST)
        assert len(result) == 1
        assert result[0][1] == "Next 7 Days"
        assert "bogus" in capsys.readouterr().err


class TestCountFilterTasks:
    @patch("snapshot.requests.get")
    def test_counts_single_page(self, mock_get):
        mock_get.return_value = _make_resp({"results": [{}] * 5})
        assert count_filter_tasks("tok", "today") == 5

    @patch("snapshot.requests.get")
    def test_paginates_to_count_all(self, mock_get):
        mock_get.side_effect = [
            _make_resp({"results": [{}] * 200, "next_cursor": "c1"}),
            _make_resp({"results": [{}] * 10}),
        ]
        assert count_filter_tasks("tok", "today") == 210
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert second_params["cursor"] == "c1"

    @patch("snapshot.requests.get")
    def test_partial_page_with_cursor_continues_paginating(self, mock_get):
        mock_get.side_effect = [
            _make_resp({"results": [{}] * 50, "next_cursor": "c1"}),
            _make_resp({"results": [{}] * 30}),
        ]
        assert count_filter_tasks("tok", "today") == 80

    @patch("snapshot.requests.get")
    def test_sends_query_and_auth(self, mock_get):
        mock_get.return_value = _make_resp({"results": []})
        count_filter_tasks("tok", "next 7 days & !subtask")
        kwargs = mock_get.call_args.kwargs
        assert kwargs["params"]["query"] == "next 7 days & !subtask"
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    @patch("snapshot.requests.get")
    def test_raises_immediately_on_4xx(self, mock_get):
        mock_get.return_value = _make_resp({}, status_code=401)
        with pytest.raises(requests.HTTPError):
            count_filter_tasks("tok", "today")

    @patch("snapshot.requests.get")
    def test_retries_on_5xx_then_succeeds(self, mock_get):
        mock_get.side_effect = [
            _make_resp({}, status_code=503),
            _make_resp({"results": [{}] * 3}),
        ]
        assert count_filter_tasks("tok", "today", retries=3, backoff=0.0) == 3
        assert mock_get.call_count == 2

    @patch("snapshot.requests.get")
    def test_raises_after_max_retries(self, mock_get):
        mock_get.return_value = _make_resp({}, status_code=503)
        with pytest.raises(requests.HTTPError):
            count_filter_tasks("tok", "today", retries=2, backoff=0.0)
        assert mock_get.call_count == 3  # 1 initial + 2 retries


class TestMain:
    def _patched_conn(self, mocker, counts: dict[str, int], prior_rows=None):
        """
        Returns an in-memory SQLite connection, pre-populated with prior_rows if given,
        and patches snapshot.db.init_db to return it.
        counts: {query_string: count} — used to stub count_filter_tasks.
        prior_rows: [(created_on, filter_name, task_count), ...]
        """
        conn = db_module.init_db(":memory:")
        if prior_rows:
            for row in prior_rows:
                db_module.write_snapshot(conn, *row)

        mocker.patch("snapshot.load_config", return_value={
            "todoist": {"api_token": "tok"},
            "snapshots": {"filters": ["next 7 days"], "db_path": "irrelevant"},
        })
        mocker.patch("snapshot.fetch_todoist_filters", return_value={
            "next 7 days": ("Next 7 Days", "7 days"),
        })
        mocker.patch(
            "snapshot.count_filter_tasks",
            side_effect=lambda tok, q, **kw: counts.get(q, 0),
        )
        mocker.patch("snapshot.db.init_db", return_value=conn)
        return conn

    def _patch_date(self, mocker, iso: str):
        mock_date = MagicMock()
        mock_date.today.return_value.isoformat.return_value = iso
        mocker.patch("snapshot.date", mock_date)

    def test_prints_filter_name_and_count(self, mocker, capsys):
        self._patched_conn(mocker, counts={"7 days": 42})
        self._patch_date(mocker, "2026-06-05")
        main()
        out = capsys.readouterr().out
        assert "Next 7 Days" in out
        assert "42" in out

    def test_shows_dash_when_no_prior(self, mocker, capsys):
        self._patched_conn(mocker, counts={"7 days": 42})
        self._patch_date(mocker, "2026-06-05")
        main()
        assert "—" in capsys.readouterr().out  # em dash

    def test_shows_positive_delta(self, mocker, capsys):
        self._patched_conn(
            mocker,
            counts={"7 days": 42},
            prior_rows=[("2026-06-04", "Next 7 Days", 39)],
        )
        self._patch_date(mocker, "2026-06-05")
        main()
        assert "+3" in capsys.readouterr().out

    def test_shows_negative_delta(self, mocker, capsys):
        self._patched_conn(
            mocker,
            counts={"7 days": 40},
            prior_rows=[("2026-06-04", "Next 7 Days", 45)],
        )
        self._patch_date(mocker, "2026-06-05")
        main()
        assert "-5" in capsys.readouterr().out

    def test_shows_zero_delta(self, mocker, capsys):
        self._patched_conn(
            mocker,
            counts={"7 days": 42},
            prior_rows=[("2026-06-04", "Next 7 Days", 42)],
        )
        self._patch_date(mocker, "2026-06-05")
        main()
        out = capsys.readouterr().out
        assert "42" in out
        assert "0" in out       # delta column shows "0", not em dash or blank
        assert "+0" not in out  # zero delta must not be formatted as "+0"

    def test_exits_when_snapshots_section_missing(self, mocker):
        mocker.patch("snapshot.load_config", return_value={
            "todoist": {"api_token": "tok"},
        })
        with pytest.raises(SystemExit):
            main()

    def test_exits_when_filters_empty(self, mocker):
        mocker.patch("snapshot.load_config", return_value={
            "todoist": {"api_token": "tok"},
            "snapshots": {"filters": [], "db_path": "irrelevant"},
        })
        with pytest.raises(SystemExit):
            main()

    def test_exits_when_no_filters_matched(self, mocker):
        mocker.patch("snapshot.load_config", return_value={
            "todoist": {"api_token": "tok"},
            "snapshots": {"filters": ["bogus filter"], "db_path": "irrelevant"},
        })
        mocker.patch("snapshot.fetch_todoist_filters", return_value={})
        with pytest.raises(SystemExit):
            main()
