from datetime import date

import pytest

from todoist_api import build_last_completion_map


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
