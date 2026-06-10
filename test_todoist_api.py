from datetime import date, datetime

import pytest

from todoist_api import build_last_completion_map, fetch_item_activities, get_with_retry


def _event(object_id, event_date, is_recurring=True):
    return {
        "object_id": object_id,
        "event_date": f"{event_date}T10:00:00Z",
        "extra_data": {"is_recurring": is_recurring},
    }


class TestBuildLastCompletionMap:
    def test_returns_empty_dict_for_no_events(self, mocker):
        mocker.patch("todoist_api.fetch_item_activities", return_value=[])
        assert build_last_completion_map("tok") == {}

    def test_filters_out_non_recurring_events(self, mocker):
        mocker.patch("todoist_api.fetch_item_activities", return_value=[
            _event("1", "2026-05-01", is_recurring=False),
        ])
        assert build_last_completion_map("tok") == {}

    def test_returns_date_for_recurring_task(self, mocker):
        mocker.patch("todoist_api.fetch_item_activities", return_value=[
            _event("1", "2026-06-01"),
        ])
        assert build_last_completion_map("tok") == {"1": date(2026, 6, 1)}

    def test_picks_first_event_per_task_events_newest_first(self, mocker):
        # events arrive newest-first; first seen = most recent
        mocker.patch("todoist_api.fetch_item_activities", return_value=[
            _event("1", "2026-06-01"),
            _event("1", "2026-05-01"),
        ])
        assert build_last_completion_map("tok") == {"1": date(2026, 6, 1)}

    def test_handles_multiple_distinct_tasks(self, mocker):
        mocker.patch("todoist_api.fetch_item_activities", return_value=[
            _event("1", "2026-06-01"),
            _event("2", "2026-05-15"),
        ])
        assert build_last_completion_map("tok") == {
            "1": date(2026, 6, 1),
            "2": date(2026, 5, 15),
        }

    def test_passes_completed_event_type(self, mocker):
        mock_fetch = mocker.patch("todoist_api.fetch_item_activities", return_value=[])
        build_last_completion_map("tok")
        assert mock_fetch.call_args.args[2] == "completed"

    def test_passes_token(self, mocker):
        mock_fetch = mocker.patch("todoist_api.fetch_item_activities", return_value=[])
        build_last_completion_map("mytoken")
        assert mock_fetch.call_args.args[0] == "mytoken"


# ---------------------------------------------------------------------------
# fetch_item_activities (moved from test_grader.py)
# ---------------------------------------------------------------------------

from datetime import timezone
from unittest.mock import MagicMock, patch as std_patch


def _make_resp(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        import requests as _req
        resp.raise_for_status.side_effect = _req.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestGetWithRetry:
    URL = "https://example.com/api"
    HEADERS = {"Authorization": "Bearer tok"}
    PARAMS = {"key": "val"}

    def _call(self, **kwargs):
        defaults = dict(headers=self.HEADERS, params=self.PARAMS, timeout=10, label="test")
        defaults.update(kwargs)
        return get_with_retry(self.URL, **defaults)

    @std_patch("todoist_api.requests.get")
    def test_returns_response_on_success(self, mock_get):
        mock_get.return_value = _make_resp({})
        resp = self._call()
        assert resp.status_code == 200
        assert mock_get.call_count == 1

    @std_patch("todoist_api.requests.get")
    def test_passes_url_headers_params_timeout(self, mock_get):
        mock_get.return_value = _make_resp({})
        self._call(timeout=42)
        assert mock_get.call_args.args[0] == self.URL
        kw = mock_get.call_args.kwargs
        assert kw["headers"] == self.HEADERS
        assert kw["params"] == self.PARAMS
        assert kw["timeout"] == 42

    @std_patch("todoist_api.time.sleep")
    @std_patch("todoist_api.requests.get")
    def test_retries_on_5xx_then_succeeds(self, mock_get, mock_sleep):
        mock_get.side_effect = [_make_resp({}, 503), _make_resp({})]
        resp = self._call()
        assert mock_get.call_count == 2
        assert resp.status_code == 200

    @std_patch("todoist_api.time.sleep")
    @std_patch("todoist_api.requests.get")
    def test_exhausts_retries_and_raises(self, mock_get, mock_sleep):
        import requests as _req
        mock_get.return_value = _make_resp({}, 503)
        with pytest.raises(_req.HTTPError):
            self._call(retries=3)
        assert mock_get.call_count == 4  # 1 initial + 3 retries

    @std_patch("todoist_api.time.sleep")
    @std_patch("todoist_api.requests.get")
    def test_4xx_raises_immediately_without_retry(self, mock_get, mock_sleep):
        import requests as _req
        mock_get.return_value = _make_resp({}, 401)
        with pytest.raises(_req.HTTPError):
            self._call()
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @std_patch("todoist_api.time.sleep")
    @std_patch("todoist_api.requests.get")
    def test_sleep_uses_exponential_backoff(self, mock_get, mock_sleep):
        import requests as _req
        mock_get.return_value = _make_resp({}, 503)
        with pytest.raises(_req.HTTPError):
            self._call(retries=3, backoff=2.0)
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [2.0, 4.0, 8.0]


class TestFetchItemActivities:
    SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)

    @std_patch("todoist_api.requests.get")
    def test_returns_results_from_single_page(self, mock_get):
        mock_get.return_value = _make_resp({"results": [
            {"object_id": "1", "event_date": "2024-03-01T09:00:00Z"},
            {"object_id": "2", "event_date": "2024-03-02T09:00:00Z"},
        ]})
        result = fetch_item_activities("tok", self.SINCE, "completed")
        assert len(result) == 2

    @std_patch("todoist_api.requests.get")
    def test_paginates_via_next_cursor(self, mock_get):
        full_page = _make_resp({
            "results": [{"object_id": str(i), "event_date": "2024-03-01T09:00:00Z"} for i in range(100)],
            "next_cursor": "cursor_abc",
        })
        last_page = _make_resp({"results": [{"object_id": "x", "event_date": "2024-03-02T09:00:00Z"}]})
        mock_get.side_effect = [full_page, last_page]
        result = fetch_item_activities("tok", self.SINCE, "completed")
        assert len(result) == 101
        assert mock_get.call_count == 2

    @std_patch("todoist_api.requests.get")
    def test_stops_when_no_cursor(self, mock_get):
        mock_get.return_value = _make_resp({"results": [{"object_id": "1"}]})
        fetch_item_activities("tok", self.SINCE, "completed")
        assert mock_get.call_count == 1

    @std_patch("todoist_api.requests.get")
    def test_returns_empty_list_when_no_results(self, mock_get):
        mock_get.return_value = _make_resp({"results": []})
        assert fetch_item_activities("tok", self.SINCE, "completed") == []

    @std_patch("todoist_api.requests.get")
    def test_sends_auth_header(self, mock_get):
        mock_get.return_value = _make_resp({"results": []})
        fetch_item_activities("mytoken", self.SINCE, "completed")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer mytoken"

    @std_patch("todoist_api.requests.get")
    def test_raises_on_http_error(self, mock_get):
        import requests as _req
        mock_get.return_value = _make_resp({}, status_code=500)
        with pytest.raises(_req.HTTPError):
            fetch_item_activities("tok", self.SINCE, "completed")

    @std_patch("todoist_api.requests.get")
    def test_sends_correct_params_for_completed(self, mock_get):
        mock_get.return_value = _make_resp({"results": []})
        fetch_item_activities("tok", self.SINCE, "completed")
        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        assert params["object_type"] == "item"
        assert params["event_type"] == "completed"
        assert params["limit"] == 100
        assert "since" not in params

    @std_patch("todoist_api.requests.get")
    def test_sends_correct_event_type_for_updated(self, mock_get):
        mock_get.return_value = _make_resp({"results": []})
        fetch_item_activities("tok", self.SINCE, "updated")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["event_type"] == "updated"

    @std_patch("todoist_api.requests.get")
    def test_passes_cursor_on_subsequent_requests(self, mock_get):
        full_page = _make_resp({
            "results": [{"object_id": str(i), "event_date": "2024-03-01T09:00:00Z"} for i in range(100)],
            "next_cursor": "cursor_xyz",
        })
        last_page = _make_resp({"results": []})
        mock_get.side_effect = [full_page, last_page]
        fetch_item_activities("tok", self.SINCE, "completed")
        second_call_params = mock_get.call_args_list[1][1]["params"]
        assert second_call_params["cursor"] == "cursor_xyz"

    @std_patch("todoist_api.requests.get")
    def test_stops_early_when_events_predate_since(self, mock_get):
        in_window = [{"object_id": str(i), "event_date": "2024-03-01T09:00:00Z"} for i in range(98)]
        too_old   = [{"object_id": "old", "event_date": "2023-12-01T09:00:00Z"}] * 2
        mock_get.return_value = _make_resp({
            "results": in_window + too_old,
            "next_cursor": "cursor_would_not_be_used",
        })
        result = fetch_item_activities("tok", self.SINCE, "completed")
        assert len(result) == 98
        assert mock_get.call_count == 1
