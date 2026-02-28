"""
Unit tests for grader.py

Run with:  pytest test_grader.py -v
"""

import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from grader import (
    GRADE_LABEL_NAMES,
    assign_grade,
    completion_dates_for,
    count_snoozes,
    ensure_grade_labels,
    fetch_activity_events,
    fetch_completed_tasks,
    load_config,
    _sync_get,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_label(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=f"id_{name}")


def make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        http_err = requests.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_valid_toml(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[todoist]\napi_token = "tok123"\n[grading]\ndays = 14\n'
        )
        cfg = load_config(str(cfg_file))
        assert cfg["todoist"]["api_token"] == "tok123"
        assert cfg["grading"]["days"] == 14

    def test_exits_when_file_missing(self, tmp_path):
        with pytest.raises(SystemExit):
            load_config(str(tmp_path / "nonexistent.toml"))

    def test_loads_thresholds(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[todoist]\napi_token = "x"\n[grading.thresholds]\nA = 0.9\nB = 0.7\n'
        )
        cfg = load_config(str(cfg_file))
        assert cfg["grading"]["thresholds"]["A"] == pytest.approx(0.9)
        assert cfg["grading"]["thresholds"]["B"] == pytest.approx(0.7)

    def test_exits_on_invalid_toml(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_bytes(b"[not valid toml ===")
        with pytest.raises((SystemExit, tomllib.TOMLDecodeError)):
            load_config(str(cfg_file))


# ---------------------------------------------------------------------------
# completion_dates_for
# ---------------------------------------------------------------------------

class TestCompletionDatesFor:
    ITEMS = [
        {"task_id": "1", "completed_at": "2024-03-01T09:00:00Z"},
        {"task_id": "1", "completed_at": "2024-03-05T18:30:00Z"},
        {"task_id": "2", "completed_at": "2024-03-01T10:00:00Z"},
        {"task_id": "1", "completed_at": "2024-03-05T20:00:00Z"},  # same day as above → deduped
    ]

    def test_returns_dates_for_matching_task(self):
        dates = completion_dates_for("1", self.ITEMS)
        assert dates == {"2024-03-01", "2024-03-05"}

    def test_deduplicates_same_day_completions(self):
        dates = completion_dates_for("1", self.ITEMS)
        assert len(dates) == 2  # two unique days despite three records

    def test_returns_empty_for_unknown_task(self):
        assert completion_dates_for("99", self.ITEMS) == set()

    def test_returns_empty_for_empty_list(self):
        assert completion_dates_for("1", []) == set()

    def test_falls_back_to_date_completed_field(self):
        items = [{"task_id": "1", "date_completed": "2024-04-10T08:00:00Z"}]
        assert completion_dates_for("1", items) == {"2024-04-10"}

    def test_skips_items_with_no_timestamp(self):
        items = [{"task_id": "1", "completed_at": ""}]
        assert completion_dates_for("1", items) == set()

    def test_task_id_coerced_to_string(self):
        items = [{"task_id": 42, "completed_at": "2024-05-01T00:00:00Z"}]
        assert completion_dates_for("42", items) == {"2024-05-01"}


# ---------------------------------------------------------------------------
# count_snoozes
# ---------------------------------------------------------------------------

class TestCountSnoozes:
    def _event(self, object_id, event_date, has_due_date_change=True):
        return {
            "object_id": object_id,
            "event_date": event_date + "T12:00:00Z",
            "extra_data": {"last_due_date": "2024-03-01"} if has_due_date_change else {},
        }

    def test_counts_snooze_without_same_day_completion(self):
        events = [self._event("1", "2024-03-02")]
        assert count_snoozes("1", events, set()) == 1

    def test_does_not_count_snooze_if_completed_same_day(self):
        events = [self._event("1", "2024-03-02")]
        assert count_snoozes("1", events, {"2024-03-02"}) == 0

    def test_ignores_events_for_other_tasks(self):
        events = [self._event("99", "2024-03-02")]
        assert count_snoozes("1", events, set()) == 0

    def test_ignores_updates_without_due_date_change(self):
        events = [self._event("1", "2024-03-02", has_due_date_change=False)]
        assert count_snoozes("1", events, set()) == 0

    def test_multiple_snoozes(self):
        events = [
            self._event("1", "2024-03-01"),
            self._event("1", "2024-03-03"),
            self._event("1", "2024-03-05"),
        ]
        assert count_snoozes("1", events, set()) == 3

    def test_mixed_snooze_and_completion_days(self):
        events = [
            self._event("1", "2024-03-01"),  # snooze (no completion)
            self._event("1", "2024-03-03"),  # completed same day → not a snooze
            self._event("1", "2024-03-05"),  # snooze
        ]
        assert count_snoozes("1", events, {"2024-03-03"}) == 2

    def test_returns_zero_for_empty_events(self):
        assert count_snoozes("1", [], set()) == 0

    def test_handles_missing_extra_data(self):
        event = {"object_id": "1", "event_date": "2024-03-02T00:00:00Z"}
        assert count_snoozes("1", [event], set()) == 0

    def test_handles_none_extra_data(self):
        event = {"object_id": "1", "event_date": "2024-03-02T00:00:00Z", "extra_data": None}
        assert count_snoozes("1", [event], set()) == 0

    def test_object_id_coerced_to_string(self):
        events = [self._event(1, "2024-03-02")]  # int object_id
        assert count_snoozes("1", events, set()) == 1


# ---------------------------------------------------------------------------
# assign_grade
# ---------------------------------------------------------------------------

class TestAssignGrade:
    THRESHOLDS = {"A": 0.85, "B": 0.65}

    def test_grade_a_at_threshold(self):
        assert assign_grade(0.85, self.THRESHOLDS) == "A"

    def test_grade_a_above_threshold(self):
        assert assign_grade(1.0, self.THRESHOLDS) == "A"

    def test_grade_b_at_threshold(self):
        assert assign_grade(0.65, self.THRESHOLDS) == "B"

    def test_grade_b_below_a(self):
        assert assign_grade(0.84, self.THRESHOLDS) == "B"

    def test_grade_c_below_b(self):
        assert assign_grade(0.64, self.THRESHOLDS) == "C"

    def test_grade_c_at_zero(self):
        assert assign_grade(0.0, self.THRESHOLDS) == "C"

    def test_uses_defaults_when_thresholds_missing(self):
        assert assign_grade(0.85, {}) == "A"
        assert assign_grade(0.65, {}) == "B"
        assert assign_grade(0.0,  {}) == "C"

    def test_custom_thresholds(self):
        t = {"A": 0.95, "B": 0.80}
        assert assign_grade(0.94, t) == "B"
        assert assign_grade(0.95, t) == "A"
        assert assign_grade(0.79, t) == "C"

    def test_thresholds_as_strings(self):
        # config values might be float or string depending on toml parsing
        assert assign_grade(0.85, {"A": "0.85", "B": "0.65"}) == "A"

    @pytest.mark.parametrize("rate,expected", [
        (1.00, "A"),
        (0.90, "A"),
        (0.85, "A"),
        (0.84, "B"),
        (0.70, "B"),
        (0.65, "B"),
        (0.64, "C"),
        (0.50, "C"),
        (0.00, "C"),
    ])
    def test_parametrized_boundary_values(self, rate, expected):
        assert assign_grade(rate, self.THRESHOLDS) == expected


# ---------------------------------------------------------------------------
# _sync_get
# ---------------------------------------------------------------------------

class TestSyncGet:
    @patch("grader.requests.get")
    def test_returns_json_on_success(self, mock_get):
        mock_get.return_value = make_response({"items": [1, 2, 3]})
        result = _sync_get("tok", "items/completed/get_all", {"limit": 10})
        assert result == {"items": [1, 2, 3]}

    @patch("grader.requests.get")
    def test_sends_auth_header(self, mock_get):
        mock_get.return_value = make_response({})
        _sync_get("mytoken", "activity/get")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer mytoken"

    @patch("grader.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value = make_response({}, status_code=500)
        with pytest.raises(requests.HTTPError):
            _sync_get("tok", "activity/get")

    @patch("grader.requests.get")
    def test_passes_params(self, mock_get):
        mock_get.return_value = make_response({})
        _sync_get("tok", "items/completed/get_all", {"since": "2024-01-01", "limit": 200})
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["since"] == "2024-01-01"
        assert kwargs["params"]["limit"] == 200


# ---------------------------------------------------------------------------
# fetch_completed_tasks
# ---------------------------------------------------------------------------

class TestFetchCompletedTasks:
    SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)

    @patch("grader._sync_get")
    def test_returns_all_items(self, mock_sync):
        mock_sync.return_value = {"items": [{"task_id": "1"}, {"task_id": "2"}]}
        result = fetch_completed_tasks("tok", self.SINCE)
        assert len(result) == 2

    @patch("grader._sync_get")
    def test_paginates_until_partial_page(self, mock_sync):
        full_page  = {"items": [{"task_id": str(i)} for i in range(200)]}
        last_page  = {"items": [{"task_id": "x"}]}
        mock_sync.side_effect = [full_page, last_page]
        result = fetch_completed_tasks("tok", self.SINCE)
        assert len(result) == 201
        assert mock_sync.call_count == 2

    @patch("grader._sync_get")
    def test_returns_empty_list_when_no_items(self, mock_sync):
        mock_sync.return_value = {"items": []}
        assert fetch_completed_tasks("tok", self.SINCE) == []

    @patch("grader._sync_get")
    def test_formats_since_correctly(self, mock_sync):
        mock_sync.return_value = {"items": []}
        fetch_completed_tasks("tok", self.SINCE)
        params = mock_sync.call_args[0][2]
        assert params["since"] == "2024-01-01T00:00:00"


# ---------------------------------------------------------------------------
# fetch_activity_events
# ---------------------------------------------------------------------------

class TestFetchActivityEvents:
    SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)

    @patch("grader._sync_get")
    def test_returns_all_events(self, mock_sync):
        mock_sync.return_value = {"events": [{"object_id": "1"}, {"object_id": "2"}]}
        result = fetch_activity_events("tok", self.SINCE)
        assert len(result) == 2

    @patch("grader._sync_get")
    def test_paginates_until_partial_page(self, mock_sync):
        full_page = {"events": [{"object_id": str(i)} for i in range(100)]}
        last_page = {"events": [{"object_id": "x"}]}
        mock_sync.side_effect = [full_page, last_page]
        result = fetch_activity_events("tok", self.SINCE)
        assert len(result) == 101
        assert mock_sync.call_count == 2

    @patch("grader._sync_get")
    def test_returns_empty_list_when_no_events(self, mock_sync):
        mock_sync.return_value = {"events": []}
        assert fetch_activity_events("tok", self.SINCE) == []

    @patch("grader._sync_get")
    def test_requests_updated_events_only(self, mock_sync):
        mock_sync.return_value = {"events": []}
        fetch_activity_events("tok", self.SINCE)
        params = mock_sync.call_args[0][2]
        assert params["event_type"] == "updated"
        assert params["object_type"] == "item"


# ---------------------------------------------------------------------------
# ensure_grade_labels
# ---------------------------------------------------------------------------

class TestEnsureGradeLabels:
    def _make_api(self, existing_names):
        api = MagicMock()
        api.get_labels.return_value = [make_label(n) for n in existing_names]
        return api

    def test_creates_missing_labels(self):
        api = self._make_api([])
        ensure_grade_labels(api, dry_run=False)
        assert api.add_label.call_count == 3
        created = {c.kwargs["name"] for c in api.add_label.call_args_list}
        assert created == set(GRADE_LABEL_NAMES)

    def test_skips_existing_labels(self):
        api = self._make_api(["grade:A", "grade:B", "grade:C"])
        ensure_grade_labels(api, dry_run=False)
        api.add_label.assert_not_called()

    def test_creates_only_missing_labels(self):
        api = self._make_api(["grade:A"])
        ensure_grade_labels(api, dry_run=False)
        assert api.add_label.call_count == 2
        created = {c.kwargs["name"] for c in api.add_label.call_args_list}
        assert created == {"grade:B", "grade:C"}

    def test_dry_run_does_not_call_add_label(self, capsys):
        api = self._make_api([])
        ensure_grade_labels(api, dry_run=True)
        api.add_label.assert_not_called()
        out = capsys.readouterr().out
        assert "dry-run" in out

    def test_dry_run_prints_missing_labels(self, capsys):
        api = self._make_api(["grade:A"])
        ensure_grade_labels(api, dry_run=True)
        out = capsys.readouterr().out
        assert "grade:B" in out
        assert "grade:C" in out

    def test_created_labels_have_correct_colours(self):
        api = self._make_api([])
        ensure_grade_labels(api, dry_run=False)
        colour_map = {c.kwargs["name"]: c.kwargs["color"] for c in api.add_label.call_args_list}
        assert colour_map["grade:A"] == "green"
        assert colour_map["grade:B"] == "yellow"
        assert colour_map["grade:C"] == "red"


# ---------------------------------------------------------------------------
# Integration: grading pipeline (completion_dates_for + count_snoozes + assign_grade)
# ---------------------------------------------------------------------------

class TestGradingPipeline:
    """End-to-end tests through the pure computation layer."""

    def _run(self, task_id, completed_items, activity_events, thresholds=None):
        thresholds = thresholds or {"A": 0.85, "B": 0.65}
        comp_dates = completion_dates_for(task_id, completed_items)
        snoozes    = count_snoozes(task_id, activity_events, comp_dates)
        comps      = len(comp_dates)
        total      = comps + snoozes
        rate       = comps / total if total else 0.0
        grade      = assign_grade(rate, thresholds)
        return {"comps": comps, "snoozes": snoozes, "rate": rate, "grade": grade}

    def test_perfect_record_gets_A(self):
        items  = [{"task_id": "1", "completed_at": f"2024-03-{d:02d}T09:00:00Z"} for d in range(1, 11)]
        result = self._run("1", items, [])
        assert result["grade"] == "A"
        assert result["rate"] == pytest.approx(1.0)

    def test_no_history_gets_C(self):
        result = self._run("1", [], [])
        assert result["grade"] == "C"
        assert result["rate"] == pytest.approx(0.0)

    def test_snoozes_lower_the_grade(self):
        # 6 completions, 4 snoozes → 60% → C
        items = [{"task_id": "1", "completed_at": f"2024-03-{d:02d}T09:00:00Z"} for d in range(1, 7)]
        events = [
            {"object_id": "1", "event_date": f"2024-03-{d:02d}T12:00:00Z",
             "extra_data": {"last_due_date": "2024-03-01"}}
            for d in range(10, 14)
        ]
        result = self._run("1", items, events)
        assert result["comps"] == 6
        assert result["snoozes"] == 4
        assert result["rate"] == pytest.approx(0.6)
        assert result["grade"] == "C"

    def test_snooze_on_completion_day_not_counted(self):
        # Completed on day 1, snooze event also on day 1 → snooze not counted
        items  = [{"task_id": "1", "completed_at": "2024-03-01T09:00:00Z"}]
        events = [{"object_id": "1", "event_date": "2024-03-01T12:00:00Z",
                   "extra_data": {"last_due_date": "2024-03-01"}}]
        result = self._run("1", items, events)
        assert result["snoozes"] == 0
        assert result["comps"] == 1
        assert result["grade"] == "A"

    def test_grade_b_boundary(self):
        # exactly 65% → B
        items  = [{"task_id": "1", "completed_at": f"2024-03-{d:02d}T09:00:00Z"} for d in range(1, 14)]  # 13 completions
        events = [{"object_id": "1", "event_date": f"2024-03-{d:02d}T12:00:00Z",
                   "extra_data": {"last_due_date": "2024-01-01"}} for d in range(20, 27)]  # 7 snoozes
        result = self._run("1", items, events)
        # 13 / 20 = 0.65 → exactly B threshold
        assert result["rate"] == pytest.approx(0.65)
        assert result["grade"] == "B"
