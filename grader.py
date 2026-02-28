#!/usr/bin/env python3
"""
grader.py — Grade Todoist recurring tasks based on completion rates.

Algorithm
---------
For each recurring task over the past N days:
  - completion_count = number of days the task was marked complete
  - snooze_count     = number of "updated" activity-log events that changed
                       the due date on a day that had no same-day completion
  - rate             = completion_count / (completion_count + snooze_count)
                       (0.0 when no events exist)

Grade thresholds (configurable):
  A  if rate >= thresholds.A   (default 0.85)
  B  if rate >= thresholds.B   (default 0.65)
  C  otherwise

Labels grade:A / grade:B / grade:C are created in Todoist if absent, then
each recurring task has any existing grade label replaced with the new one.

Requirements
------------
  Python  3.11+  (tomllib is stdlib)
  pip install todoist-api-python requests rich

Usage
-----
  python grader.py [--config PATH] [--dry-run] [--summary]
"""

import argparse
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table
from todoist_api_python.api import TodoistAPI

SYNC_BASE = "https://api.todoist.com/sync/v9"

# The three label names this script manages
GRADE_LABEL_NAMES: tuple[str, ...] = ("grade:A", "grade:B", "grade:C")

# Todoist API colour names (valid values for the labels/add endpoint)
LABEL_COLOURS = {
    "grade:A": "green",
    "grade:B": "yellow",
    "grade:C": "red",
}


# ---------------------------------------------------------------------------
# Config & CLI
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Config file not found: {path}")
    with open(p, "rb") as f:
        return tomllib.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grade Todoist recurring tasks by completion rate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.toml", metavar="PATH",
        help="Path to config.toml (default: config.toml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print planned changes without writing to Todoist",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print a rich summary table after grading",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Todoist Sync API helpers (completed tasks + activity log live here,
# not in the REST v2 SDK)
# ---------------------------------------------------------------------------

def _sync_get(token: str, endpoint: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{SYNC_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_completed_tasks(token: str, since: datetime) -> list[dict]:
    """
    Return all completed-item records since *since* via the Sync API.
    Paginates automatically.

    Each record has:
      task_id        — ID of the original recurring task
      completed_at   — ISO-8601 UTC timestamp of the completion
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    results: list[dict] = []
    offset, limit = 0, 200

    while True:
        data = _sync_get(token, "items/completed/get_all", {
            "since":  since_str,
            "limit":  limit,
            "offset": offset,
        })
        chunk: list[dict] = data.get("items", [])
        results.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit

    return results


def fetch_activity_events(token: str, since: datetime) -> list[dict]:
    """
    Return all item-level 'updated' activity events since *since*.
    Requires Todoist Pro/Business; raises HTTPError(402/403) otherwise.
    Paginates automatically.

    Relevant fields per event:
      object_id   — task ID
      event_date  — ISO-8601 UTC timestamp of the event
      extra_data  — dict; contains 'last_due_date' if due date was changed
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    results: list[dict] = []
    offset, limit = 0, 100

    while True:
        data = _sync_get(token, "activity/get", {
            "object_type": "item",
            "event_type":  "updated",
            "since":       since_str,
            "limit":       limit,
            "offset":      offset,
        })
        chunk: list[dict] = data.get("events", [])
        results.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit

    return results


# ---------------------------------------------------------------------------
# Grading logic
# ---------------------------------------------------------------------------

def completion_dates_for(task_id: str, completed_items: list[dict]) -> set[str]:
    """
    Return the set of YYYY-MM-DD dates on which *task_id* was completed.
    For recurring tasks Todoist creates a new completed-item record per
    occurrence; the 'task_id' field links each back to the source task.
    """
    dates: set[str] = set()
    for item in completed_items:
        if str(item.get("task_id", "")) == task_id:
            ts = item.get("completed_at") or item.get("date_completed", "")
            if ts:
                dates.add(ts[:10])  # YYYY-MM-DD
    return dates


def count_snoozes(
    task_id: str,
    activity_events: list[dict],
    completion_dates: set[str],
) -> int:
    """
    Count 'snooze' events for a task.

    A snooze is an activity-log 'updated' event where:
      1. The due date was changed  (extra_data contains 'last_due_date'), AND
      2. The task was NOT completed on that same calendar day.

    Rationale: if you reschedule a recurring task to tomorrow instead of
    completing it today, that is a snooze regardless of the new due date.
    """
    count = 0
    for event in activity_events:
        if str(event.get("object_id", "")) != task_id:
            continue
        extra = event.get("extra_data") or {}
        if "last_due_date" not in extra:
            continue                          # due date was not touched
        event_day = (event.get("event_date") or "")[:10]
        if event_day not in completion_dates:
            count += 1
    return count


def assign_grade(rate: float, thresholds: dict) -> str:
    if rate >= float(thresholds.get("A", 0.85)):
        return "A"
    if rate >= float(thresholds.get("B", 0.65)):
        return "B"
    return "C"


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------

def ensure_grade_labels(api: TodoistAPI, dry_run: bool) -> None:
    """Create grade:A / grade:B / grade:C labels in Todoist if they are missing."""
    existing = {lbl.name for lbl in api.get_labels()}
    for name in GRADE_LABEL_NAMES:
        if name in existing:
            continue
        if dry_run:
            print(f"  [dry-run] would create label '{name}'")
        else:
            api.add_label(name=name, color=LABEL_COLOURS[name])
            print(f"  Created label '{name}'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args   = parse_args()
    cfg    = load_config(args.config)

    # ── Config values ──────────────────────────────────────────────────────
    try:
        token = cfg["todoist"]["api_token"]
    except KeyError:
        sys.exit("config.toml must have [todoist] api_token")

    grading_cfg = cfg.get("grading", {})
    days        = int(grading_cfg.get("days", 30))
    thresholds  = grading_cfg.get("thresholds", {"A": 0.85, "B": 0.65})
    write_delay = float(cfg.get("rate_limit", {}).get("write_delay_seconds", 0.5))

    since = datetime.now(timezone.utc) - timedelta(days=days)
    api   = TodoistAPI(token)

    # ── Fetch ──────────────────────────────────────────────────────────────
    print("Fetching tasks…")
    all_tasks = api.get_tasks()
    recurring = [t for t in all_tasks if t.due and t.due.is_recurring]
    print(f"  {len(recurring)} recurring  /  {len(all_tasks)} total")

    print(f"Fetching completions for the past {days} days…")
    completed_items = fetch_completed_tasks(token, since)
    print(f"  {len(completed_items)} completed instances")

    print("Fetching activity log (due-date changes)…")
    try:
        activity_events = fetch_activity_events(token, since)
        print(f"  {len(activity_events)} updated events")
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code in (402, 403):
            print(
                "  Activity log unavailable (Todoist Pro/Business required).\n"
                "  Snooze counts will be 0; completion rate reflects completions only."
            )
            activity_events = []
        else:
            raise

    # ── Ensure labels exist ────────────────────────────────────────────────
    print("Checking grade labels…")
    ensure_grade_labels(api, args.dry_run)

    # ── Calculate grades ───────────────────────────────────────────────────
    results: list[dict] = []
    for task in recurring:
        tid        = str(task.id)
        comp_dates = completion_dates_for(tid, completed_items)
        snoozes    = count_snoozes(tid, activity_events, comp_dates)
        comps      = len(comp_dates)
        total      = comps + snoozes
        rate       = comps / total if total else 0.0
        grade      = assign_grade(rate, thresholds)
        results.append({
            "task":    task,
            "comps":   comps,
            "snoozes": snoozes,
            "rate":    rate,
            "grade":   grade,
        })

    # ── Apply labels ───────────────────────────────────────────────────────
    print("\nApplying grade labels…")
    grade_label_set = set(GRADE_LABEL_NAMES)
    changed = 0

    for r in results:
        task      = r["task"]
        new_label = f"grade:{r['grade']}"
        old       = list(task.labels or [])
        # Strip any existing grade labels, then append the new one
        filtered  = [lbl for lbl in old if lbl not in grade_label_set]
        new       = filtered + [new_label]

        if set(old) == set(new):
            continue  # already correct, skip

        changed += 1
        line = (
            f"  {task.content!r:50s}  "
            f"rate={r['rate']:5.1%}  →  grade:{r['grade']}"
        )
        if args.dry_run:
            print(f"[dry-run]{line}")
        else:
            api.update_task(task_id=task.id, labels=new)
            print(line)
            time.sleep(write_delay)   # respect Todoist rate limits

    if changed == 0:
        print("  All tasks already have the correct grade label.")
    elif not args.dry_run:
        print(f"\n  {changed} task(s) updated.")

    # ── Summary table ──────────────────────────────────────────────────────
    if args.summary:
        grade_style = {"A": "bold green", "B": "bold yellow", "C": "bold red"}
        console     = Console()

        table = Table(
            title=f"Recurring Task Grades  (past {days} days)",
            show_header=True,
            header_style="bold",
            border_style="dim",
            show_lines=False,
        )
        table.add_column("Task",        style="cyan", no_wrap=False, max_width=55)
        table.add_column("Completions", justify="right")
        table.add_column("Snoozes",     justify="right")
        table.add_column("Rate",        justify="right")
        table.add_column("Grade",       justify="center")

        for r in sorted(results, key=lambda x: -x["rate"]):
            g   = r["grade"]
            sty = grade_style[g]
            table.add_row(
                r["task"].content,
                str(r["comps"]),
                str(r["snoozes"]),
                f"{r['rate']:.1%}",
                f"[{sty}]{g}[/{sty}]",
            )

        console.print(table)


if __name__ == "__main__":
    main()
