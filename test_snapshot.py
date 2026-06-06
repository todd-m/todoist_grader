import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

import db
import db as db_module
import graph
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
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
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
        charts, _ = mock_render.call_args.args
        assert len(charts) == 1

    def test_solo_filter_calls_render_page_with_two_charts(self, mocker):
        mocker.patch("snapshot.load_config", return_value={
            "todoist": {"api_token": "tok"},
            "snapshots": {
                "filters": ["next 7 days", "next 30 days"],
                "db_path": "irrelevant",
                "solo_filters": ["next 30 days"],
            },
        })
        mocker.patch("snapshot.fetch_todoist_filters", return_value={
            "next 7 days":  ("Next 7 Days",  "7 days"),
            "next 30 days": ("Next 30 Days", "30 days"),
        })
        mocker.patch(
            "snapshot.count_filter_tasks",
            side_effect=lambda tok, q, **kw: {"7 days": 10, "30 days": 100}.get(q, 0),
        )
        import db as db_module
        conn = db_module.init_db(":memory:")
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-05")
        main()
        charts, _ = mock_render.call_args.args
        assert len(charts) == 2
        subtitles = [sub for _, sub in charts]
        assert "" in subtitles
        assert "Next 30 Days" in subtitles

    def test_solo_filter_matching_is_case_insensitive(self, mocker):
        mocker.patch("snapshot.load_config", return_value={
            "todoist": {"api_token": "tok"},
            "snapshots": {
                "filters": ["next 7 days"],
                "db_path": "irrelevant",
                "solo_filters": ["NEXT 7 DAYS"],
            },
        })
        mocker.patch("snapshot.fetch_todoist_filters", return_value={
            "next 7 days": ("Next 7 Days", "7 days"),
        })
        mocker.patch(
            "snapshot.count_filter_tasks",
            side_effect=lambda tok, q, **kw: 42,
        )
        import db as db_module
        conn = db_module.init_db(":memory:")
        mocker.patch("snapshot.db.init_db", return_value=conn)
        mocker.patch("snapshot.graph.write_graph")
        mocker.patch("snapshot.subprocess.run")
        mock_render = mocker.patch("snapshot.graph.render_page", return_value="<html/>")
        self._patch_date(mocker, "2026-06-05")
        main()
        charts, _ = mock_render.call_args.args
        subtitles = [sub for _, sub in charts]
        assert "Next 7 Days" in subtitles


class TestReadLastNDays:
    def test_returns_last_7_days_and_excludes_older(self, conn):
        for d, count in [
            ("2026-05-28", 10), ("2026-05-29", 11), ("2026-05-30", 12),
            ("2026-05-31", 13), ("2026-06-01", 14), ("2026-06-02", 15),
            ("2026-06-03", 16), ("2026-06-04", 17), ("2026-06-05", 18),
            ("2026-06-06", 19),
        ]:
            db.write_snapshot(conn, d, "Next 7 Days", count)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert len(result["Next 7 Days"]) == 7
        assert result["Next 7 Days"][0] == ("2026-05-31", 13)
        assert result["Next 7 Days"][-1] == ("2026-06-06", 19)

    def test_missing_days_produce_gap_not_zero(self, conn):
        db.write_snapshot(conn, "2026-06-04", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 12)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert result["Next 7 Days"] == [("2026-06-04", 10), ("2026-06-06", 12)]

    def test_multiple_filters_returned_separately(self, conn):
        db.write_snapshot(conn, "2026-06-05", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-05", "Next 30 Days", 20)
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 11)
        db.write_snapshot(conn, "2026-06-06", "Next 30 Days", 21)
        result = db.read_last_n_days(
            conn, ["Next 7 Days", "Next 30 Days"], n=7, as_of="2026-06-06"
        )
        assert result["Next 7 Days"]  == [("2026-06-05", 10), ("2026-06-06", 11)]
        assert result["Next 30 Days"] == [("2026-06-05", 20), ("2026-06-06", 21)]

    def test_n_equals_1_returns_only_as_of_date(self, conn):
        db.write_snapshot(conn, "2026-06-05", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 11)
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=1, as_of="2026-06-06")
        assert result["Next 7 Days"] == [("2026-06-06", 11)]

    def test_returns_empty_list_for_filter_with_no_data(self, conn):
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert result == {"Next 7 Days": []}

    def test_returns_empty_dict_for_empty_filter_names(self, conn):
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 42)
        result = db.read_last_n_days(conn, [], n=7, as_of="2026-06-06")
        assert result == {}

    def test_excludes_rows_after_as_of(self, conn):
        db.write_snapshot(conn, "2026-06-05", "Next 7 Days", 10)
        db.write_snapshot(conn, "2026-06-06", "Next 7 Days", 11)
        db.write_snapshot(conn, "2026-06-07", "Next 7 Days", 12)  # future row
        result = db.read_last_n_days(conn, ["Next 7 Days"], n=7, as_of="2026-06-06")
        assert result["Next 7 Days"] == [("2026-06-05", 10), ("2026-06-06", 11)]


class TestBuildDataset:
    def test_labels_are_sorted_union_of_all_dates(self):
        rows = {
            "Next 7 Days":  [("2026-06-04", 10), ("2026-06-05", 11), ("2026-06-06", 12)],
            "Next 30 Days": [("2026-06-05", 20), ("2026-06-06", 21)],
        }
        result = graph.build_dataset(rows)
        assert result["labels"] == ["2026-06-04", "2026-06-05", "2026-06-06"]

    def test_missing_date_produces_none_in_series(self):
        rows = {
            "Next 7 Days":  [("2026-06-04", 10), ("2026-06-05", 11), ("2026-06-06", 12)],
            "Next 30 Days": [("2026-06-05", 20), ("2026-06-06", 21)],
        }
        result = graph.build_dataset(rows)
        next_30 = next(d for d in result["datasets"] if d["label"] == "Next 30 Days")
        assert next_30["data"] == [None, 20, 21]

    def test_single_filter_single_day(self):
        rows = {"Next 7 Days": [("2026-06-06", 42)]}
        result = graph.build_dataset(rows)
        assert result["labels"] == ["2026-06-06"]
        assert len(result["datasets"]) == 1
        assert result["datasets"][0]["label"] == "Next 7 Days"
        assert result["datasets"][0]["data"] == [42]

    def test_empty_filter_produces_all_none_series(self):
        rows = {
            "Next 7 Days":  [("2026-06-06", 42)],
            "Next 30 Days": [],
        }
        result = graph.build_dataset(rows)
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


class TestRenderHtml:
    def test_contains_chartjs_cdn(self):
        html = graph.render_html({"labels": [], "datasets": []}, "Test")
        assert "cdn.jsdelivr.net/npm/chart.js" in html

    def test_embeds_dataset_json(self):
        dataset = {"labels": ["2026-06-06"], "datasets": [{"label": "Filter A", "data": [42]}]}
        html = graph.render_html(dataset, "Test")
        assert '"Filter A"' in html
        assert "42" in html

    def test_contains_prefers_color_scheme(self):
        html = graph.render_html({"labels": [], "datasets": []}, "Test")
        assert "prefers-color-scheme" in html

    def test_title_appears_in_output(self):
        html = graph.render_html({"labels": [], "datasets": []}, "My Custom Title")
        assert "My Custom Title" in html

    def test_html_special_chars_in_title_are_escaped(self):
        html = graph.render_html({"labels": [], "datasets": []}, "<My & Title>")
        assert "<My & Title>" not in html
        assert "&lt;My &amp; Title&gt;" in html

    def test_script_tag_in_filter_name_does_not_break_output(self):
        dataset = {
            "labels": ["2026-06-06"],
            "datasets": [{"label": "</script><script>alert(1)</script>", "data": [1]}],
        }
        html = graph.render_html(dataset, "Test")
        assert "</script><script>" not in html
        assert r"<\/script>" in html


class TestRenderPage:
    def test_is_valid_html_document(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "")], "Test")
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_contains_chartjs_cdn(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "")], "Test")
        assert "cdn.jsdelivr.net/npm/chart.js" in html

    def test_page_title_in_title_tag_and_h1(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "")], "My Page")
        assert "<title>My Page</title>" in html
        assert "<h1>My Page</h1>" in html

    def test_title_html_escaped(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "")], "<Page & Title>")
        assert "<Page & Title>" not in html
        assert "&lt;Page &amp; Title&gt;" in html

    def test_contains_prefers_color_scheme(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "")], "Test")
        assert "prefers-color-scheme" in html

    def test_single_chart_has_one_canvas(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "")], "Test")
        assert html.count("<canvas") == 1

    def test_two_charts_have_two_canvases(self):
        pair = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([pair, pair], "Test")
        assert html.count("<canvas") == 2

    def test_chart_ids_are_unique(self):
        pair = ({"labels": [], "datasets": []}, "")
        html = graph.render_page([pair, pair], "Test")
        assert 'id="chart-0"' in html
        assert 'id="chart-1"' in html

    def test_subtitle_appears_when_non_empty(self):
        html = graph.render_page([({"labels": [], "datasets": []}, "View All")], "Test")
        assert "View All" in html

    def test_embeds_dataset_json(self):
        dataset = {"labels": ["2026-06-06"], "datasets": [{"label": "Filter A", "data": [42]}]}
        html = graph.render_page([(dataset, "")], "Test")
        assert '"Filter A"' in html
        assert "42" in html

    def test_script_tag_in_filter_name_does_not_break_output(self):
        dataset = {
            "labels": ["2026-06-06"],
            "datasets": [{"label": "</script><script>alert(1)</script>", "data": [1]}],
        }
        html = graph.render_page([(dataset, "")], "Test")
        assert "</script><script>" not in html
        assert r"<\/script>" in html
