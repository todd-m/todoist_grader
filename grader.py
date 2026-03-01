#!/usr/bin/env python3
"""
grader.py — Grade Todoist recurring tasks based on completion rates.

Algorithm
---------
For each recurring task over the past N days:
  - completion_count = number of days the task was marked complete
  - snooze_count     = number of "updated" activity events that changed
                       the due date on a day that had no same-day completion
  - rate             = completion_count / (completion_count + snooze_count)
                       (0.0 when no events exist)

Grade thresholds (configurable):
  A  if rate >= thresholds.A   (default 0.85)
  B  if rate >= thresholds.B   (default 0.65)
  C  otherwise

Data source
-----------
The REST API v1 /api/v1/activities endpoint (object_type=item) is the only
source that records recurring task completions. The /tasks/completed endpoints
do NOT log recurring task completions — only the activity log does.

  completed events: event_type="completed", extra_data.is_recurring=True
  snooze events:    event_type="updated",   extra_data.last_due_date present

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

ACTIVITIES_URL = "https://api.todoist.com/api/v1/activities"

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
    parser.add_argument(
        "--today", action="store_true",
        help="Filter reports to tasks due today (recurring and non-recurring)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# SDK helper
# ---------------------------------------------------------------------------

def _all_pages(paginator) -> list:
    """Flatten a v3 SDK ResultsPaginator into a single list."""
    results = []
    for page in paginator:
        results.extend(page)
    return results


# ---------------------------------------------------------------------------
# Activity log (REST API v1) — sole source of recurring task history
# ---------------------------------------------------------------------------

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
    since_date = since.strftime("%Y-%m-%d")   # YYYY-MM-DD for cheap string compare
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

        resp = requests.get(
            ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        chunk: list[dict] = data.get("results", [])

        # Events arrive newest-first.  Stop as soon as any event predates since.
        in_window = [e for e in chunk if (e.get("event_date") or "")[:10] >= since_date]
        results.extend(in_window)
        if len(in_window) < len(chunk):
            break           # hit the date boundary — no need to page further

        cursor = data.get("next_cursor")
        if not cursor or len(chunk) < 100:
            break

    return results


# ---------------------------------------------------------------------------
# Grading logic
# ---------------------------------------------------------------------------

def completion_dates_for(task_id: str, completed_events: list[dict]) -> set[str]:
    """
    Return the set of YYYY-MM-DD dates on which *task_id* was completed.

    Uses activity events with event_type="completed" and
    extra_data.is_recurring=True, which is the correct source for recurring
    task completions (they do not appear in /tasks/completed endpoints).
    """
    dates: set[str] = set()
    for event in completed_events:
        if str(event.get("object_id", "")) != task_id:
            continue
        if not (event.get("extra_data") or {}).get("is_recurring"):
            continue
        ts = event.get("event_date", "")
        if ts:
            dates.add(ts[:10])  # YYYY-MM-DD
    return dates


def count_snoozes(
    task_id: str,
    updated_events: list[dict],
    completion_dates: set[str],
) -> int:
    """
    Count snooze events for a task.

    A snooze is an activity "updated" event where:
      1. The due date was changed (extra_data contains 'last_due_date'), AND
      2. The task was NOT completed on that same calendar day.

    Rationale: rescheduling a recurring task to tomorrow instead of
    completing it is a snooze regardless of the new due date.
    """
    count = 0
    for event in updated_events:
        if str(event.get("object_id", "")) != task_id:
            continue
        extra = event.get("extra_data") or {}
        if "last_due_date" not in extra:
            continue                     # due date was not touched
        event_day = (event.get("event_date") or "")[:10]
        if event_day not in completion_dates:
            count += 1
    return count


def nonrecurring_snooze_report(
    all_tasks: list,
    updated_events: list[dict],
) -> list[dict]:
    """
    Return snooze counts for non-recurring tasks that were snoozed at least once.

    A snooze = an 'updated' activity event with extra_data.last_due_date present.
    No completion-date exclusion (non-recurring tasks are not graded on completion).
    Sorted by snooze count descending.
    """
    recurring_ids = {str(t.id) for t in all_tasks if t.due and t.due.is_recurring}
    task_map = {str(t.id): t for t in all_tasks}

    snooze_counts: dict[str, int] = {}
    for event in updated_events:
        tid = str(event.get("object_id", ""))
        if tid in recurring_ids:
            continue
        extra = event.get("extra_data") or {}
        if "last_due_date" not in extra:
            continue
        snooze_counts[tid] = snooze_counts.get(tid, 0) + 1

    rows = [
        {"task": task_map[tid], "snoozes": n}
        for tid, n in snooze_counts.items()
        if tid in task_map
    ]
    return sorted(rows, key=lambda r: -r["snoozes"])


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
    existing = {lbl.name for lbl in _all_pages(api.get_labels())}
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
    args = parse_args()
    cfg  = load_config(args.config)

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
    all_tasks = _all_pages(api.get_tasks())
    recurring = [t for t in all_tasks if t.due and t.due.is_recurring]
    print(f"  {len(recurring)} recurring  /  {len(all_tasks)} total")

    print(f"Fetching activity log for the past {days} days…")
    completed_events = fetch_item_activities(token, since, "completed")
    updated_events   = fetch_item_activities(token, since, "updated")
    print(f"  {len(completed_events)} completed events, {len(updated_events)} updated events")

    # ── Ensure labels exist ────────────────────────────────────────────────
    print("Checking grade labels…")
    ensure_grade_labels(api, args.dry_run)

    # ── Calculate grades ───────────────────────────────────────────────────
    results: list[dict] = []
    for task in recurring:
        tid        = str(task.id)
        comp_dates = completion_dates_for(tid, completed_events)
        snoozes    = count_snoozes(tid, updated_events, comp_dates)
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

    # ── Build report view (optionally filtered to today) ───────────────────
    nr_snoozed = nonrecurring_snooze_report(all_tasks, updated_events)
    if args.today:
        today_str      = datetime.now().strftime("%Y-%m-%d")
        def _due_today(r): return r["task"].due and str(r["task"].due.date)[:10] == today_str
        report_results  = [r for r in results   if _due_today(r)]
        nr_snoozed      = [r for r in nr_snoozed if _due_today(r)]
        recurring_title = f"Recurring Tasks Due Today  (grades over past {days} days)"
        nr_title        = "Non-Recurring Tasks Due Today: Snooze Counts"
        nr_header       = "Non-recurring tasks due today that have been snoozed"
    else:
        today_str       = None
        report_results  = results
        recurring_title = f"Recurring Task Grades  (past {days} days)"
        nr_title        = f"Non-Recurring Tasks: Snooze Counts  (past {days} days)"
        nr_header       = f"Non-recurring tasks snoozed in the past {days} days"

    # ── Apply labels ───────────────────────────────────────────────────────
    print("\nApplying grade labels…")
    grade_label_set = set(GRADE_LABEL_NAMES)

    # Collect pending changes first so we can preview before writing
    pending = []
    for r in results:
        task      = r["task"]
        new_label = f"grade:{r['grade']}"
        old       = list(task.labels or [])
        new       = [lbl for lbl in old if lbl not in grade_label_set] + [new_label]
        if set(old) != set(new):
            pending.append({
                "task":       task,
                "new_labels": new,
                "line":       (f"  {task.content!r:50s}  "
                               f"rate={r['rate']:5.1%}  →  grade:{r['grade']}"),
            })

    if not pending:
        print("  All tasks already have the correct grade label.")
    elif args.dry_run:
        for p in pending:
            if not args.today or (p["task"].due and str(p["task"].due.date)[:10] == today_str):
                print(f"[dry-run]{p['line']}")
    else:
        print(f"  {len(pending)} task(s) will be updated:")
        for p in pending:
            print(p["line"])
        answer = input("\n  Apply these changes? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit("  Aborted.")
        for p in pending:
            api.update_task(task_id=p["task"].id, labels=p["new_labels"])
            print(f"  Updated: {p['task'].content!r}")
            time.sleep(write_delay)
        print(f"\n  {len(pending)} task(s) updated.")

    if nr_snoozed:
        print(f"\n{nr_header}:")
        for r in nr_snoozed:
            print(f"  {r['snoozes']:3d}x  {r['task'].content!r}")

    # ── Summary table ──────────────────────────────────────────────────────
    if args.summary:
        grade_style = {"A": "bold green", "B": "bold yellow", "C": "bold red"}
        console     = Console()

        table = Table(
            title=recurring_title,
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

        for r in sorted(report_results, key=lambda x: -x["rate"]):
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

        if nr_snoozed:
            nr_table = Table(
                title=nr_title,
                show_header=True,
                header_style="bold",
                border_style="dim",
                show_lines=False,
            )
            nr_table.add_column("Task",    style="cyan", no_wrap=False, max_width=55)
            nr_table.add_column("Snoozes", justify="right")
            for r in nr_snoozed:
                nr_table.add_row(r["task"].content, str(r["snoozes"]))
            console.print(nr_table)


if __name__ == "__main__":
    main()
