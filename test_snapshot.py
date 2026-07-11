import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

import db
import db as db_module
import graph
from db import SnapshotRow
from snapshot import (
    compute_avg_age,
    fetch_filter_tasks,
    fetch_todoist_filters,
    main,
    resolve_filters,
)


# DB tests use a real in-memory SQLite connection — no mocking needed since sqlite3 has no I/O cost.
@pytest.fixture
def conn():
    c = db.init_db(":memory:")
    yield c
    c.close()


class TestInitDb:
    def test_creates_snapshots_table(self, conn):
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert ("snapshots",) in tables

    def test_creates_index(self, conn):
        indexes = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        assert ("idx_snapshots_filter",) in indexes

    def test_creates_avg_age_days_column(self, conn):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
        assert "avg_age_days" in cols

    def test_migration_adds_column_to_existing_db(self, tmp_path):

        db_file = str(tmp_path / "old.db")
        old_conn = sqlite3.connect(db_file)
        old_conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_on TEXT NOT NULL,
                filter_name TEXT NOT NULL,
                task_count INTEGER NOT NULL,
                UNIQUE (created_on, filter_name)
            )
        """)
        old_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_filter ON snapshots (filter_name, created_on)"
        )
        old_conn.commit()
        old_conn.close()
        conn2 = db.init_db(db_file)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(snapshots)")}
        conn2.close()
        assert "avg_age_days" in cols

    def test_migration_idempotent_when_column_already_exists(self, tmp_path):
        db_file = str(tmp_path / "migrated.db")
        conn1 = db.init_db(db_file)
        conn1.close()
        conn2 = db.init_db(db_file)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(snapshots)")}
        conn2.close()
        assert "avg_age_days" in cols


class TestWriteSnapshot:
    def test_inserts_row(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 42)
        row = conn.execute(
            "SELECT created_on, filter_name, task_count, avg_age_days FROM snapshots"
        ).fetchone()
        assert row == ("2026-06-01", "next 7 days", 42, None)

    def test_inserts_row_with_avg_age(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 42, avg_age_days=18.5)
        row = conn.execute("SELECT avg_age_days FROM snapshots").fetchone()
        assert row[0] == pytest.approx(18.5)

    def test_idempotent_overwrite(self, conn):
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 42)
        db.write_snapshot(conn, "2026-06-01", "next 7 days", 55, avg_age_days=10.0)
        rows = conn.execute(
            "SELECT task_count, avg_age_days FROM snapshots "
            "WHERE created_on='2026-06-01' AND filter_name='next 7 days'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 55
        assert rows[0][1] == pytest.approx(10.0)

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
        mock_post.return_value = _make_resp(
            {
                "filters": [
                    {"name": "Next 7 Days", "query": "7 days", "is_deleted": False},
                ]
            }
        )
        result = fetch_todoist_filters("tok")
        assert "next 7 days" in result
        assert result["next 7 days"] == ("Next 7 Days", "7 days")

    @patch("snapshot.requests.post")
    def test_excludes_deleted_filters(self, mock_post):
        mock_post.return_value = _make_resp(
            {
                "filters": [
                    {"name": "Old Filter", "query": "something", "is_deleted": True},
                ]
            }
        )
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
        "next 7 days": ("Next 7 Days", "7 days & !subtask"),
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


class TestFetchFilterTasks:
    @patch("snapshot.requests.get")
    def test_returns_task_list_single_page(self, mock_get):
        mock_get.return_value = _make_resp({"results": [{"id": "1"}, {"id": "2"}]})
        result = fetch_filter_tasks("tok", "today")
        assert result == [{"id": "1"}, {"id": "2"}]

    @patch("snapshot.requests.get")
    def test_paginates_and_returns_all(self, mock_get):
        mock_get.side_effect = [
            _make_resp({"results": [{"id": str(i)} for i in range(200)], "next_cursor": "c1"}),
            _make_resp({"results": [{"id": "200"}]}),
        ]
        result = fetch_filter_tasks("tok", "today")
        assert len(result) == 201
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert second_params["cursor"] == "c1"

    @patch("snapshot.requests.get")
    def test_partial_page_with_cursor_continues(self, mock_get):
        mock_get.side_effect = [
            _make_resp({"results": [{}] * 50, "next_cursor": "c1"}),
            _make_resp({"results": [{}] * 30}),
        ]
        assert len(fetch_filter_tasks("tok", "today")) == 80

    @patch("snapshot.requests.get")
    def test_sends_query_and_auth(self, mock_get):
        mock_get.return_value = _make_resp({"results": []})
        fetch_filter_tasks("tok", "next 7 days & !subtask")
        kwargs = mock_get.call_args.kwargs
        assert kwargs["params"]["query"] == "next 7 days & !subtask"
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    @patch("snapshot.requests.get")
    def test_raises_immediately_on_4xx(self, mock_get):
        mock_get.return_value = _make_resp({}, status_code=401)
        with pytest.raises(requests.HTTPError):
            fetch_filter_tasks("tok", "today")

    @patch("snapshot.requests.get")
    def test_retries_on_5xx_then_succeeds(self, mock_get):
        mock_get.side_effect = [
            _make_resp({}, status_code=503),
            _make_resp({"results": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}),
        ]
        result = fetch_filter_tasks("tok", "today", retries=3, backoff=0.0)
        assert len(result) == 3
        assert mock_get.call_count == 2

    @patch("snapshot.requests.get")
    def test_raises_after_max_retries(self, mock_get):
        mock_get.return_value = _make_resp({}, status_code=503)
        with pytest.raises(requests.HTTPError):
            fetch_filter_tasks("tok", "today", retries=2, backoff=0.0)
        assert mock_get.call_count == 3


class TestComputeAvgAge:
    TODAY = date(2026, 6, 7)

    def _task(self, task_id, added_at, is_recurring=False):
        return {
            "id": task_id,
            "added_at": f"{added_at}T10:00:00Z",
            "due": {"is_recurring": is_recurring} if is_recurring else None,
        }

    def test_returns_none_for_empty_task_list(self):
        assert compute_avg_age([], {}, self.TODAY) is None

    def test_non_recurring_uses_added_at(self):
        tasks = [self._task("1", "2026-05-01", is_recurring=False)]
        result = compute_avg_age(tasks, {}, self.TODAY)
        assert result == pytest.approx(37.0)  # June 7 - May 1 = 37 days

    def test_recurring_with_map_entry_uses_last_completion(self):
        tasks = [self._task("1", "2026-01-01", is_recurring=True)]
        completion_map = {"1": date(2026, 6, 1)}
        result = compute_avg_age(tasks, completion_map, self.TODAY)
        assert result == pytest.approx(6.0)  # June 7 - June 1 = 6 days

    def test_recurring_with_integer_id_matches_string_map_key(self):
        # Filter endpoint returns integer IDs; completion_map keys are str — must match
        tasks = [self._task(12345, "2026-01-01", is_recurring=True)]
        completion_map = {"12345": date(2026, 6, 1)}
        result = compute_avg_age(tasks, completion_map, self.TODAY)
        assert result == pytest.approx(6.0)

    def test_recurring_without_map_entry_falls_back_to_added_at(self):
        tasks = [self._task("1", "2026-05-01", is_recurring=True)]
        result = compute_avg_age(tasks, {}, self.TODAY)
        assert result == pytest.approx(37.0)

    def test_averages_across_multiple_tasks(self):
        tasks = [
            self._task("1", "2026-06-01"),  # 6 days old
            self._task("2", "2026-05-28"),  # 10 days old
        ]
        result = compute_avg_age(tasks, {}, self.TODAY)
        assert result == pytest.approx(8.0)

    def test_skips_task_with_no_added_at(self):
        tasks = [
            {"id": "1", "due": None},  # no added_at
            self._task("2", "2026-06-01"),
        ]
        result = compute_avg_age(tasks, {}, self.TODAY)
        assert result == pytest.approx(6.0)

    def test_returns_none_when_all_tasks_lack_added_at(self):
        tasks = [{"id": "1", "due": None}]
        assert compute_avg_age(tasks, {}, self.TODAY) is None


class TestMain:
    @pytest.fixture(autouse=True)
    def _close_conns(self):
        # main() closes the conn itself on happy paths; closing again is a no-op.
        # This catches tests where main() raises before ever calling db.init_db.
        self._conns: list = []
        yield
        for conn in self._conns:
            conn.close()

    def _patched_conn(self, mocker, counts: dict[str, int], prior_rows=None):
        """
        Returns an in-memory SQLite connection, pre-populated with prior_rows if given,
        and patches snapshot.db.init_db to return it.
        counts: {query_string: count} — number of fake tasks returned by fetch_filter_tasks.
        prior_rows: [(created_on, filter_name, task_count), ...]
        """
        conn = db_module.init_db(":memory:")
        self._conns.append(conn)
        if prior_rows:
            for row in prior_rows:
                db_module.write_snapshot(conn, *row)

        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {"filters": ["next 7 days"], "db_path": "irrelevant"},
            },
        )
        mocker.patch(
            "snapshot.fetch_todoist_filters",
            return_value={
                "next 7 days": ("Next 7 Days", "7 days"),
            },
        )
        mocker.patch(
            "snapshot.fetch_filter_tasks",
            side_effect=lambda tok, q, **kw: [{}] * counts.get(q, 0),
        )
        mocker.patch("snapshot.build_last_completion_map", return_value={})
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        return conn

    def _patch_date(self, mocker, iso: str):
        from datetime import date as real_date

        mock_date = MagicMock()
        real_today = real_date.fromisoformat(iso)
        mock_date.today.return_value = real_today
        mock_date.fromisoformat.side_effect = real_date.fromisoformat
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
        assert "0" in out  # delta column shows "0", not em dash or blank
        assert "+0" not in out  # zero delta must not be formatted as "+0"

    def test_completion_map_5xx_stores_none_avg_ages(self, mocker, capsys):
        self._patched_conn(mocker, counts={"7 days": 3})
        write_spy = mocker.patch("snapshot.db.write_snapshot")
        resp = MagicMock()
        resp.status_code = 503
        mocker.patch(
            "snapshot.build_last_completion_map",
            side_effect=requests.HTTPError(response=resp),
        )
        self._patch_date(mocker, "2026-06-05")
        main()  # must not raise
        err = capsys.readouterr().err
        assert "Warning" in err
        assert "503" in err
        _, kwargs = write_spy.call_args
        assert kwargs.get("avg_age_days") is None

    def test_completion_map_connection_error_raises(self, mocker):
        self._patched_conn(mocker, counts={"7 days": 3})
        mocker.patch(
            "snapshot.build_last_completion_map",
            side_effect=requests.exceptions.ConnectionError("timeout"),
        )
        self._patch_date(mocker, "2026-06-05")
        with pytest.raises(requests.exceptions.ConnectionError):
            main()

    def test_completion_map_4xx_raises(self, mocker):
        self._patched_conn(mocker, counts={"7 days": 3})
        resp = MagicMock()
        resp.status_code = 401
        mocker.patch(
            "snapshot.build_last_completion_map",
            side_effect=requests.HTTPError(response=resp),
        )
        self._patch_date(mocker, "2026-06-05")
        with pytest.raises(requests.HTTPError):
            main()

    def test_exits_when_snapshots_section_missing(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
            },
        )
        with pytest.raises(SystemExit):
            main()

    def test_exits_when_filters_empty(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {"filters": [], "db_path": "irrelevant"},
            },
        )
        with pytest.raises(SystemExit):
            main()

    def test_exits_when_no_filters_matched(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {"filters": ["bogus filter"], "db_path": "irrelevant"},
            },
        )
        mocker.patch("snapshot.fetch_todoist_filters", return_value={})
        with pytest.raises(SystemExit):
            main()

    def test_calls_write_graph(self, mocker, capsys):
        self._patched_conn(mocker, counts={"7 days": 42})
        mock_write = mocker.patch("snapshot.graph.write_graph")
        self._patch_date(mocker, "2026-06-05")
        main()
        assert mock_write.called
        assert mock_write.call_args.args[1] == "snapshots_graph.html"

    def test_opens_graph_in_browser(self, mocker):
        self._patched_conn(mocker, counts={"7 days": 42})
        mock_run = mocker.patch("snapshot.subprocess.run")
        self._patch_date(mocker, "2026-06-05")
        main()
        assert mock_run.called
        assert mock_run.call_args.args[0] == ["open", "snapshots_graph.html"]

    def test_no_solo_filters_calls_render_page_with_one_chart(self, mocker):
        self._patched_conn(mocker, counts={"7 days": 42})
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-05")
        main()
        assert mock_render.called
        groups, _ = mock_render.call_args.args
        assert len(groups) == 1

    def test_solo_filter_calls_render_page_with_two_charts(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {
                    "filters": ["next 7 days", "next 30 days"],
                    "db_path": "irrelevant",
                    "solo_filters": ["next 30 days"],
                },
            },
        )
        mocker.patch(
            "snapshot.fetch_todoist_filters",
            return_value={
                "next 7 days": ("Next 7 Days", "7 days"),
                "next 30 days": ("Next 30 Days", "30 days"),
            },
        )
        mocker.patch(
            "snapshot.fetch_filter_tasks",
            side_effect=lambda tok, q, **kw: [{}] * {"7 days": 10, "30 days": 100}.get(q, 0),
        )
        import db as db_module

        conn = db_module.init_db(":memory:")
        mocker.patch("snapshot.build_last_completion_map", return_value={})
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-05")
        main()
        groups, _ = mock_render.call_args.args
        assert len(groups) == 2
        subtitles = [sub for group in groups for _, sub in group]
        assert "" in subtitles
        assert "Next 30 Days" in subtitles

    def test_avg_age_column_header_shown(self, mocker, capsys):
        self._patched_conn(mocker, counts={"7 days": 5})
        self._patch_date(mocker, "2026-06-07")
        main()
        assert "Avg Age" in capsys.readouterr().out

    def test_age_chart_group_added_to_render_page(self, mocker):
        self._patched_conn(mocker, counts={"7 days": 5})
        mocker.patch("snapshot.compute_avg_age", return_value=10.0)
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-07")
        main()
        groups, _ = mock_render.call_args.args
        # Count and age for the main filters stay together in one group/row.
        main_group_subtitles = [sub for _, sub in groups[0]]
        assert "" in main_group_subtitles
        assert "Avg Task Age (days)" in main_group_subtitles

    def test_solo_filter_gets_separate_age_chart(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {
                    "filters": ["next 7 days"],
                    "solo_filters": ["next 30 days"],
                    "db_path": "irrelevant",
                },
            },
        )
        mocker.patch(
            "snapshot.fetch_todoist_filters",
            return_value={
                "next 7 days": ("Next 7 Days", "7 days"),
                "next 30 days": ("Next 30 Days", "30 days"),
            },
        )
        mocker.patch("snapshot.fetch_filter_tasks", side_effect=lambda tok, q, **kw: [{}] * 5)
        mocker.patch("snapshot.build_last_completion_map", return_value={})
        mocker.patch("snapshot.compute_avg_age", return_value=10.0)
        import db as db_module

        conn = db_module.init_db(":memory:")
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-07")
        main()
        groups, _ = mock_render.call_args.args
        assert len(groups) == 2  # main group + separate solo group
        subtitles = [sub for group in groups for _, sub in group]
        assert "Avg Task Age (days)" in subtitles  # main age chart
        assert "Next 30 Days — Avg Task Age (days)" in subtitles  # solo age chart

    def test_solo_filter_matching_is_case_insensitive(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {
                    "filters": ["next 7 days"],
                    "db_path": "irrelevant",
                    "solo_filters": ["NEXT 7 DAYS"],
                },
            },
        )
        mocker.patch(
            "snapshot.fetch_todoist_filters",
            return_value={
                "next 7 days": ("Next 7 Days", "7 days"),
            },
        )
        mocker.patch(
            "snapshot.fetch_filter_tasks",
            side_effect=lambda tok, q, **kw: [{}] * 42,
        )
        import db as db_module

        conn = db_module.init_db(":memory:")
        mocker.patch("snapshot.build_last_completion_map", return_value={})
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-05")
        main()
        groups, _ = mock_render.call_args.args
        subtitles = [sub for group in groups for _, sub in group]
        assert "Next 7 Days" in subtitles

    def test_solo_filter_not_in_filters_is_still_fetched(self, mocker):
        mocker.patch(
            "snapshot.load_config",
            return_value={
                "todoist": {"api_token": "tok"},
                "snapshots": {
                    "filters": ["next 7 days"],
                    "db_path": "irrelevant",
                    "solo_filters": ["next year"],  # not in filters
                },
            },
        )
        mocker.patch(
            "snapshot.fetch_todoist_filters",
            return_value={
                "next 7 days": ("Next 7 Days", "7 days"),
                "next year": ("Next Year", "next year"),
            },
        )
        mocker.patch(
            "snapshot.fetch_filter_tasks",
            side_effect=lambda tok, q, **kw: [{}] * {"7 days": 10, "next year": 100}.get(q, 0),
        )
        mocker.patch("snapshot.build_last_completion_map", return_value={})
        import db as db_module

        conn = db_module.init_db(":memory:")
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-05")
        main()
        groups, _ = mock_render.call_args.args
        assert len(groups) == 2
        subtitles = [sub for group in groups for _, sub in group]
        assert "" in subtitles  # main chart
        assert "Next Year" in subtitles  # solo chart


class TestReadLastNDays:
    def test_returns_last_7_days_and_excludes_older(self, conn):
        for d, count in [
            ("2026-05-28", 10),
            ("2026-05-29", 11),
            ("2026-05-30", 12),
            ("2026-05-31", 13),
            ("2026-06-01", 14),
            ("2026-06-02", 15),
            ("2026-06-03", 16),
            ("2026-06-04", 17),
            ("2026-06-05", 18),
            ("2026-06-06", 19),
        ]:
            db.write_snapshot(conn, d, "Next 7 Days", count)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert len(result["Next 7 Days"]) == 7
        first = result["Next 7 Days"][0]
        assert first.date == "2026-05-31"
        assert first.count == 13
        assert first.avg_age_days is None
        last = result["Next 7 Days"][-1]
        assert last.date == "2026-06-06"
        assert last.count == 19

    def test_missing_days_produce_gap_not_zero(self, conn):
        db.write_snapshot(conn, "2026-06-04", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 12)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        rows = result["Next 7 Days"]
        assert rows[0] == SnapshotRow("2026-06-04", 10, None)
        assert rows[1] == SnapshotRow("2026-06-06", 12, None)

    def test_avg_age_days_returned_when_present(self, conn):
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 42, avg_age_days=18.5)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert result["Next 7 Days"][0].avg_age_days == pytest.approx(18.5)

    def test_multiple_filters_returned_separately(self, conn):
        db.write_snapshot(conn, "2026-06-05", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-05", "Next 30 Days", 20)
        result = db.read_last_n_days(conn, ["Next 7 Days", "Next 30 Days"], as_of="2026-06-05")
        assert result["Next 7 Days"][0].count == 10
        assert result["Next 30 Days"][0].count == 20

    def test_n_equals_1_returns_only_as_of_date(self, conn):
        db.write_snapshot(conn, "2026-06-05", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 20)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=1, as_of="2026-06-06")
        assert len(result["Next 7 Days"]) == 1
        assert result["Next 7 Days"][0].count == 20

    def test_returns_empty_list_for_filter_with_no_data(self, conn):
        result = db.read_last_n_days(conn, ["missing filter"], as_of="2026-06-06")
        assert result["missing filter"] == []

    def test_returns_empty_dict_for_empty_filter_names(self, conn):
        assert db.read_last_n_days(conn, []) == {}

    def test_excludes_rows_after_as_of(self, conn):
        db.write_snapshot(conn, "2026-06-07", "Next 7 Days", 99)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert result["Next 7 Days"] == []

    def test_default_window_is_30_days(self, conn):
        db.write_snapshot(conn, "2026-05-31", "Next 7 Days", 1)  # 31 days back: excluded
        db.write_snapshot(conn, "2026-06-01", "Next 7 Days", 2)  # 30 days back: included
        db.write_snapshot(conn, "2026-06-30", "Next 7 Days", 3)
        result = db.read_last_n_days(conn, ["Next 7 Days"], as_of="2026-06-30")
        rows = result["Next 7 Days"]
        assert [(r.date, r.count) for r in rows] == [("2026-06-01", 2), ("2026-06-30", 3)]


class TestBuildDataset:
    def test_labels_span_every_date_in_window(self):
        rows = {
            "Next 7 Days": [("2026-06-04", 10), ("2026-06-06", 12)],
        }
        result = graph.build_dataset(rows, "2026-06-03", "2026-06-06")
        assert result["labels"] == ["2026-06-03", "2026-06-04", "2026-06-05", "2026-06-06"]

    def test_missing_date_produces_none_in_series(self):
        rows = {
            "Next 7 Days": [("2026-06-04", 10), ("2026-06-05", 11), ("2026-06-06", 12)],
            "Next 30 Days": [("2026-06-05", 20), ("2026-06-06", 21)],
        }
        result = graph.build_dataset(rows, "2026-06-04", "2026-06-06")
        next_30 = next(d for d in result["datasets"] if d["label"] == "Next 30 Days")
        assert next_30["data"] == [None, 20, 21]

    def test_gap_day_in_window_gets_none(self):
        rows = {"Next 7 Days": [("2026-06-04", 10), ("2026-06-06", 12)]}
        result = graph.build_dataset(rows, "2026-06-04", "2026-06-06")
        assert result["datasets"][0]["data"] == [10, None, 12]

    def test_window_pads_days_before_first_snapshot(self):
        rows = {"Next 7 Days": [("2026-06-06", 42)]}
        result = graph.build_dataset(rows, "2026-06-04", "2026-06-06")
        assert result["labels"] == ["2026-06-04", "2026-06-05", "2026-06-06"]
        assert result["datasets"][0]["data"] == [None, None, 42]

    def test_single_filter_single_day(self):
        rows = {"Next 7 Days": [("2026-06-06", 42)]}
        result = graph.build_dataset(rows, "2026-06-06", "2026-06-06")
        assert result["labels"] == ["2026-06-06"]
        assert len(result["datasets"]) == 1
        assert result["datasets"][0]["label"] == "Next 7 Days"
        assert result["datasets"][0]["data"] == [42]

    def test_empty_filter_produces_all_none_series(self):
        rows = {
            "Next 7 Days": [("2026-06-06", 42)],
            "Next 30 Days": [],
        }
        result = graph.build_dataset(rows, "2026-06-06", "2026-06-06")
        next_30 = next(d for d in result["datasets"] if d["label"] == "Next 30 Days")
        assert next_30["data"] == [None]


class TestRenderChart:
    def test_canvas_id_contains_index(self):
        fragment = graph.render_chart({"labels": [], "datasets": []}, "", 3)
        assert 'id="chart-3"' in fragment

    def test_subtitle_renders_h2_when_non_empty(self):
        fragment = graph.render_chart({"labels": [], "datasets": []}, "My Filter", 0)
        assert "<h2>My Filter</h2>" in fragment

    def test_no_h2_when_subtitle_empty(self):
        fragment = graph.render_chart({"labels": [], "datasets": []}, "", 0)
        assert "<h2>" not in fragment

    def test_embeds_dataset_json(self):
        dataset = {"labels": ["2026-06-06"], "datasets": [{"label": "A", "data": [42]}]}
        fragment = graph.render_chart(dataset, "", 0)
        assert '"A"' in fragment
        assert "42" in fragment

    def test_escapes_script_injection(self):
        dataset = {
            "labels": ["2026-06-06"],
            "datasets": [{"label": "</script>xss", "data": [1]}],
        }
        fragment = graph.render_chart(dataset, "", 0)
        assert "</script>xss" not in fragment
        assert r"<\/script>xss" in fragment

    def test_subtitle_html_special_chars_escaped(self):
        fragment = graph.render_chart({"labels": [], "datasets": []}, "<bad & title>", 0)
        assert "<bad & title>" not in fragment
        assert "&lt;bad &amp; title&gt;" in fragment

    def test_index_zero_produces_chart_0(self):
        fragment = graph.render_chart({"labels": [], "datasets": []}, "", 0)
        assert 'id="chart-0"' in fragment

    def test_wrapped_in_chart_cell(self):
        fragment = graph.render_chart({"labels": [], "datasets": []}, "", 0)
        assert 'class="chart-cell"' in fragment


class TestRenderPage:
    def test_is_valid_html_document(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "")]], "Test")
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_contains_chartjs_cdn(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "")]], "Test")
        assert "cdn.jsdelivr.net/npm/chart.js" in html

    def test_page_title_in_title_tag_and_h1(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "")]], "My Page")
        assert "<title>My Page</title>" in html
        assert "<h1>My Page</h1>" in html

    def test_title_html_escaped(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "")]], "<Page & Title>")
        assert "<Page & Title>" not in html
        assert "&lt;Page &amp; Title&gt;" in html

    def test_contains_prefers_color_scheme(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "")]], "Test")
        assert "prefers-color-scheme" in html

    def test_single_chart_has_one_canvas(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "")]], "Test")
        assert html.count("<canvas") == 1

    def test_two_charts_have_two_canvases(self):
        pair = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([[pair, pair]], "Test")
        assert html.count("<canvas") == 2

    def test_chart_ids_are_unique(self):
        pair = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([[pair, pair]], "Test")
        assert 'id="chart-0"' in html
        assert 'id="chart-1"' in html

    def test_subtitle_appears_when_non_empty(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "View All")]], "Test")
        assert "View All" in html

    def test_embeds_dataset_json(self):
        dataset = {"labels": ["2026-06-06"], "datasets": [{"label": "Filter A", "data": [42]}]}
        html = graph.render_page([[(dataset, "")]], "Test")
        assert '"Filter A"' in html
        assert "42" in html

    def test_script_tag_in_filter_name_does_not_break_output(self):
        dataset = {
            "labels": ["2026-06-06"],
            "datasets": [{"label": "</script><script>alert(1)</script>", "data": [1]}],
        }
        html = graph.render_page([[(dataset, "")]], "Test")
        assert "</script><script>" not in html
        assert r"<\/script>" in html

    def test_one_section_per_group(self):
        chart = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([[chart, chart], [chart, chart]], "Test")
        assert html.count('class="chart-group"') == 2

    def test_group_charts_stay_in_same_section(self):
        chart = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([[chart, chart]], "Test")
        assert html.count('class="chart-group"') == 1
        assert html.count("<canvas") == 2

    def test_single_chart_group_renders_intact(self):
        html = graph.render_page([[({"labels": [], "datasets": []}, "Count only")]], "Test")
        assert html.count('class="chart-group"') == 1
        assert html.count("<canvas") == 1
        assert "Count only" in html

    def test_canvas_ids_unique_across_groups(self):
        chart = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([[chart, chart], [chart]], "Test")
        assert 'id="chart-0"' in html
        assert 'id="chart-1"' in html
        assert 'id="chart-2"' in html
