"""
Unit tests for grader.py

Run with:  pytest test_grader.py -v
"""

import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from grader import (
    GRADE_LABEL_NAMES,
    assign_grade,
    completion_dates_for,
    count_snoozes,
    ensure_grade_labels,
    fetch_item_activities,
    load_config,
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
# completion_dates_for  (activity events: object_id, event_date, extra_data.is_recurring)
# ---------------------------------------------------------------------------

class TestCompletionDatesFor:
    EVENTS = [
        {"object_id": "1", "event_date": "2024-03-01T09:00:00Z", "extra_data": {"is_recurring": True}},
        {"object_id": "1", "event_date": "2024-03-05T18:30:00Z", "extra_data": {"is_recurring": True}},
        {"object_id": "2", "event_date": "2024-03-01T10:00:00Z", "extra_data": {"is_recurring": True}},
        {"object_id": "1", "event_date": "2024-03-05T20:00:00Z", "extra_data": {"is_recurring": True}},  # same day → deduped
    ]

    def test_returns_dates_for_matching_task(self):
        dates = completion_dates_for("1", self.EVENTS)
        assert dates == {"2024-03-01", "2024-03-05"}

    def test_deduplicates_same_day_completions(self):
        dates = completion_dates_for("1", self.EVENTS)
        assert len(dates) == 2  # two unique days despite three records

    def test_returns_empty_for_unknown_task(self):
        assert completion_dates_for("99", self.EVENTS) == set()

    def test_returns_empty_for_empty_list(self):
        assert completion_dates_for("1", []) == set()

    def test_skips_non_recurring_events(self):
        events = [{"object_id": "1", "event_date": "2024-04-10T08:00:00Z",
                   "extra_data": {"is_recurring": False}}]
        assert completion_dates_for("1", events) == set()

    def test_skips_events_with_no_is_recurring(self):
        events = [{"object_id": "1", "event_date": "2024-04-10T08:00:00Z", "extra_data": {}}]
        assert completion_dates_for("1", events) == set()

    def test_skips_events_with_no_timestamp(self):
        events = [{"object_id": "1", "event_date": "", "extra_data": {"is_recurring": True}}]
        assert completion_dates_for("1", events) == set()

    def test_object_id_coerced_to_string(self):
        events = [{"object_id": 42, "event_date": "2024-05-01T00:00:00Z",
                   "extra_data": {"is_recurring": True}}]
        assert completion_dates_for("42", events) == {"2024-05-01"}

    def test_handles_missing_extra_data(self):
        events = [{"object_id": "1", "event_date": "2024-04-10T08:00:00Z"}]
        assert completion_dates_for("1", events) == set()

    def test_handles_none_extra_data(self):
        events = [{"object_id": "1", "event_date": "2024-04-10T08:00:00Z", "extra_data": None}]
        assert completion_dates_for("1", events) == set()


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
# fetch_item_activities
# ---------------------------------------------------------------------------

class TestFetchItemActivities:
    SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)

    @patch("grader.requests.get")
    def test_returns_results_from_single_page(self, mock_get):
        mock_get.return_value = make_response({"results": [{"object_id": "1"}, {"object_id": "2"}]})
        result = fetch_item_activities("tok", self.SINCE, "completed")
        assert len(result) == 2

    @patch("grader.requests.get")
    def test_paginates_via_next_cursor(self, mock_get):
        full_page = make_response({
            "results": [{"object_id": str(i)} for i in range(100)],
            "next_cursor": "cursor_abc",
        })
        last_page = make_response({"results": [{"object_id": "x"}]})
        mock_get.side_effect = [full_page, last_page]
        result = fetch_item_activities("tok", self.SINCE, "completed")
        assert len(result) == 101
        assert mock_get.call_count == 2

    @patch("grader.requests.get")
    def test_stops_when_no_cursor(self, mock_get):
        mock_get.return_value = make_response({"results": [{"object_id": "1"}]})
        fetch_item_activities("tok", self.SINCE, "completed")
        assert mock_get.call_count == 1

    @patch("grader.requests.get")
    def test_returns_empty_list_when_no_results(self, mock_get):
        mock_get.return_value = make_response({"results": []})
        assert fetch_item_activities("tok", self.SINCE, "completed") == []

    @patch("grader.requests.get")
    def test_sends_auth_header(self, mock_get):
        mock_get.return_value = make_response({"results": []})
        fetch_item_activities("mytoken", self.SINCE, "completed")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer mytoken"

    @patch("grader.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value = make_response({}, status_code=500)
        with pytest.raises(requests.HTTPError):
            fetch_item_activities("tok", self.SINCE, "completed")

    @patch("grader.requests.get")
    def test_sends_correct_params_for_completed(self, mock_get):
        mock_get.return_value = make_response({"results": []})
        fetch_item_activities("tok", self.SINCE, "completed")
        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        assert params["object_type"] == "item"
        assert params["event_type"] == "completed"
        assert params["since"] == "2024-01-01T00:00:00"
        assert params["limit"] == 100

    @patch("grader.requests.get")
    def test_sends_correct_event_type_for_updated(self, mock_get):
        mock_get.return_value = make_response({"results": []})
        fetch_item_activities("tok", self.SINCE, "updated")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["event_type"] == "updated"

    @patch("grader.requests.get")
    def test_passes_cursor_on_subsequent_requests(self, mock_get):
        full_page = make_response({
            "results": [{"object_id": str(i)} for i in range(100)],
            "next_cursor": "cursor_xyz",
        })
        last_page = make_response({"results": []})
        mock_get.side_effect = [full_page, last_page]
        fetch_item_activities("tok", self.SINCE, "completed")
        second_call_params = mock_get.call_args_list[1][1]["params"]
        assert second_call_params["cursor"] == "cursor_xyz"


# ---------------------------------------------------------------------------
# ensure_grade_labels
# ---------------------------------------------------------------------------

class TestEnsureGradeLabels:
    def _make_api(self, existing_names):
        api = MagicMock()
        # _all_pages iterates over the paginator; each element is a page (list of labels)
        api.get_labels.return_value = [[make_label(n) for n in existing_names]]
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

    def _comp_event(self, object_id, date_str):
        return {
            "object_id": object_id,
            "event_date": f"{date_str}T09:00:00Z",
            "extra_data": {"is_recurring": True},
        }

    def _snooze_event(self, object_id, date_str):
        return {
            "object_id": object_id,
            "event_date": f"{date_str}T12:00:00Z",
            "extra_data": {"last_due_date": "2024-01-01"},
        }

    def _run(self, task_id, completed_events, snooze_events, thresholds=None):
        thresholds = thresholds or {"A": 0.85, "B": 0.65}
        comp_dates = completion_dates_for(task_id, completed_events)
        snoozes    = count_snoozes(task_id, snooze_events, comp_dates)
        comps      = len(comp_dates)
        total      = comps + snoozes
        rate       = comps / total if total else 0.0
        grade      = assign_grade(rate, thresholds)
        return {"comps": comps, "snoozes": snoozes, "rate": rate, "grade": grade}

    def test_perfect_record_gets_A(self):
        events = [self._comp_event("1", f"2024-03-{d:02d}") for d in range(1, 11)]
        result = self._run("1", events, [])
        assert result["grade"] == "A"
        assert result["rate"] == pytest.approx(1.0)

    def test_no_history_gets_C(self):
        result = self._run("1", [], [])
        assert result["grade"] == "C"
        assert result["rate"] == pytest.approx(0.0)

    def test_snoozes_lower_the_grade(self):
        # 6 completions, 4 snoozes → 60% → C
        comp_events   = [self._comp_event("1", f"2024-03-{d:02d}") for d in range(1, 7)]
        snooze_events = [self._snooze_event("1", f"2024-03-{d:02d}") for d in range(10, 14)]
        result = self._run("1", comp_events, snooze_events)
        assert result["comps"] == 6
        assert result["snoozes"] == 4
        assert result["rate"] == pytest.approx(0.6)
        assert result["grade"] == "C"

    def test_snooze_on_completion_day_not_counted(self):
        # Completed on day 1, snooze event also on day 1 → snooze not counted
        comp_events   = [self._comp_event("1", "2024-03-01")]
        snooze_events = [self._snooze_event("1", "2024-03-01")]
        result = self._run("1", comp_events, snooze_events)
        assert result["snoozes"] == 0
        assert result["comps"] == 1
        assert result["grade"] == "A"

    def test_grade_b_boundary(self):
        # 13 completions, 7 snoozes → 65% → exactly B threshold
        comp_events   = [self._comp_event("1", f"2024-03-{d:02d}") for d in range(1, 14)]
        snooze_events = [self._snooze_event("1", f"2024-03-{d:02d}") for d in range(20, 27)]
        result = self._run("1", comp_events, snooze_events)
        assert result["rate"] == pytest.approx(0.65)
        assert result["grade"] == "B"
