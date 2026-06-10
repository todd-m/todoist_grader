import sys
import time

import requests
from datetime import date, datetime, timedelta, timezone

ACTIVITIES_URL = "https://api.todoist.com/api/v1/activities"


def get_with_retry(
    url: str,
    *,
    headers: dict,
    params: dict,
    timeout: int,
    retries: int = 3,
    backoff: float = 2.0,
    label: str,
) -> requests.Response:
    attempt = 0
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        try:
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code < 500:
                raise
            attempt += 1
            if attempt > retries:
                print(f"[{label}] HTTP {resp.status_code} on attempt {attempt}/{retries} — giving up", file=sys.stderr)
                raise
            delay = backoff ** attempt
            print(f"[{label}] HTTP {resp.status_code} on attempt {attempt}/{retries} — retrying in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)


def fetch_item_activities(token: str, since: datetime, event_type: str) -> list[dict]:
    """
    Return all item-level activity events of *event_type* since *since*.

    Uses GET /api/v1/activities with object_type=item — the only endpoint
    that records recurring task completions and due-date changes.

    Relevant fields per event:
      object_id        — task ID (matches the active recurring task's ID)
      event_date       — ISO-8601 UTC timestamp
      event_type       — "completed" or "updated"
      extra_data       — dict with:
          is_recurring     (completed events) True when the task recurs
          last_due_date    (updated events)   previous due date before change

    Note: the API's 'since' query param is ignored server-side; events are
    returned newest-first, so we stop paginating as soon as we see an event
    older than *since* and filter client-side.
    """
    since_date = since.strftime("%Y-%m-%d")
    results: list[dict] = []
    cursor: str | None = None

    while True:
        params: dict = {
            "object_type": "item",
            "event_type":  event_type,
            "limit":       100,
        }
        if cursor:
            params["cursor"] = cursor

        resp = get_with_retry(
            ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
            label="fetch_item_activities",
        )
        data = resp.json()
        chunk: list[dict] = data.get("results", [])

        in_window = [e for e in chunk if (e.get("event_date") or "")[:10] >= since_date]
        results.extend(in_window)
        if len(in_window) < len(chunk):
            break

        cursor = data.get("next_cursor")
        if not cursor or len(chunk) < 100:
            break

    return results


def build_last_completion_map(token: str, lookback_days: int = 365) -> dict[str, date]:
    """Return {task_id: most_recent_completion_date} for recurring tasks only."""
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    events = fetch_item_activities(token, since, "completed")
    result: dict[str, date] = {}
    for event in events:
        if not event.get("extra_data", {}).get("is_recurring"):
            continue
        task_id = str(event["object_id"])
        if task_id not in result:  # events are newest-first; first seen = most recent
            result[task_id] = date.fromisoformat(event["event_date"][:10])
    return result
