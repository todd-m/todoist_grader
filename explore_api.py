"""
explore_api.py — Scratch script for investigating the Todoist API v1 responses.

Run individual sections by commenting/uncommenting the calls at the bottom,
or run the whole thing to get a full picture of what data is available.

Usage: .venv/bin/python explore_api.py
"""

import json
import tomllib
from datetime import datetime, timedelta, timezone

import requests
from todoist_api_python.api import TodoistAPI

cfg = tomllib.load(open("config.toml", "rb"))
TOKEN = cfg["todoist"]["api_token"]
api = TodoistAPI(TOKEN)

HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SINCE = datetime.now(timezone.utc) - timedelta(days=30)
UNTIL = datetime.now(timezone.utc)


def pp(data, limit=2000):
    print(json.dumps(data, indent=2, default=str)[:limit])


# ---------------------------------------------------------------------------

def show_recurring_tasks(n=5):
    print(f"\n=== Active recurring tasks (first {n}) ===")
    all_tasks = []
    for page in api.get_tasks():
        all_tasks.extend(page)
    recurring = [t for t in all_tasks if t.due and t.due.is_recurring]
    print(f"Total active: {len(all_tasks)}, recurring: {len(recurring)}")
    for t in recurring[:n]:
        print(f"  id={t.id}  due={t.due.date}  content={t.content[:60]}")
    return recurring


def show_completed_by_completion_date(n=10):
    print(f"\n=== Completed tasks/by_completion_date (last 30 days, first {n}) ===")
    all_items = []
    cursor = None
    while True:
        params = {"since": SINCE.isoformat(), "until": UNTIL.isoformat(), "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            "https://api.todoist.com/api/v1/tasks/completed/by_completion_date",
            headers=HEADERS, params=params, timeout=10,
        )
        data = resp.json()
        items = data.get("items", data.get("results", []))
        all_items.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    print(f"Total: {len(all_items)}")
    for item in all_items[:n]:
        is_rec = item.get("due", {}).get("is_recurring", "?") if item.get("due") else "no due"
        print(f"  {item['completed_at'][:10]}  recurring={is_rec}  {item['content'][:60]}")
    return all_items


def show_completed_by_due_date(n=10):
    print(f"\n=== Completed tasks/by_due_date (last 30 days, first {n}) ===")
    params = {
        "since": SINCE.date().isoformat(),
        "until": UNTIL.date().isoformat(),
        "limit": n,
    }
    resp = requests.get(
        "https://api.todoist.com/api/v1/tasks/completed/by_due_date",
        headers=HEADERS, params=params, timeout=10,
    )
    data = resp.json()
    items = data.get("items", data.get("results", []))
    print(f"Returned: {len(items)}, next_cursor: {bool(data.get('next_cursor'))}")
    for item in items[:n]:
        is_rec = item.get("due", {}).get("is_recurring", "?") if item.get("due") else "no due"
        print(f"  due={item.get('due',{}).get('date','?')[:10]}  recurring={is_rec}  {item['content'][:60]}")
    return items


def show_task_activity(task_id, task_name=""):
    print(f"\n=== tasks/activity for '{task_name}' (id={task_id}) ===")
    resp = requests.get(
        "https://api.todoist.com/api/v1/tasks/activity",
        headers=HEADERS, params={"task_id": task_id, "limit": 10}, timeout=10,
    )
    print(f"Status: {resp.status_code}")
    pp(resp.json())
    return resp.json()


def show_raw_api_endpoints():
    """Probe endpoints that might expose recurring completion history."""
    print("\n=== Probing undocumented/alt endpoints ===")
    candidates = [
        "tasks/history",
        "tasks/log",
        "tasks/events",
        "items/completed",
        "activity",
        "karma",
        "stats",
    ]
    for path in candidates:
        resp = requests.get(
            f"https://api.todoist.com/api/v1/{path}",
            headers=HEADERS, timeout=10,
        )
        print(f"  GET /api/v1/{path}  →  {resp.status_code}")


def show_sync_completed_items():
    """
    Try the main Sync API v9 full-sync endpoint.
    Even though items/completed/get_all is 410, the core sync endpoint
    may still return completed items in its 'items' resource.
    """
    print("\n=== Sync API v9 full sync (completed_info resource) ===")
    resp = requests.post(
        "https://api.todoist.com/sync/v9/sync",
        headers=HEADERS,
        json={
            "sync_token": "*",
            "resource_types": ["items", "completed_info"],
        },
        timeout=30,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return None
    data = resp.json()
    print("Top-level keys:", list(data.keys()))
    items = data.get("items", [])
    completed_info = data.get("completed_info", [])
    checked = [i for i in items if i.get("checked")]
    print(f"  items total: {len(items)}, checked: {len(checked)}")
    print(f"  completed_info entries: {len(completed_info)}")
    if completed_info:
        print("  Sample completed_info entry:")
        pp(completed_info[:2])
    return data


def match_recurring_vs_completed(recurring, completed):
    """Check overlap between active recurring tasks and completed tasks."""
    print("\n=== Match: recurring ↔ completed ===")
    rec_by_content = {t.content: t.id for t in recurring}
    rec_by_id = {t.id for t in recurring}

    by_id = [c for c in completed if c["id"] in rec_by_id]
    by_content = [c for c in completed if c["content"] in rec_by_content]

    print(f"  Matches by ID:      {len(by_id)}")
    print(f"  Matches by content: {len(by_content)}")
    for c in by_content[:5]:
        print(f"    {c['completed_at'][:10]}  {c['content'][:60]}")


# ---------------------------------------------------------------------------

def show_activity_log_for_task(task_id, task_name="", limit=20):
    """
    Probe the /api/v1/activity endpoint for a specific task.
    MCP confirms this returns completed + updated events with isRecurring flag.
    We're testing what the raw REST endpoint looks like.
    """
    print(f"\n=== /api/v1/activity for '{task_name}' (id={task_id}) ===")
    candidates = [
        ("object_id", {"object_type": "task", "object_id": task_id, "limit": limit}),
        ("task_id",   {"task_id": task_id, "limit": limit}),
        ("item_id",   {"object_type": "item", "object_id": task_id, "limit": limit}),
    ]
    for label, params in candidates:
        resp = requests.get(
            "https://api.todoist.com/api/v1/activity",
            headers=HEADERS, params=params, timeout=10,
        )
        print(f"  params={label}  →  status={resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            events = data.get("events", data.get("results", []))
            print(f"    keys={list(data.keys())}  events={len(events)}")
            for e in events[:3]:
                print(f"    {e.get('event_date','')[:10]}  type={e.get('event_type')}  extra={e.get('extra_data',{})}")
            return data
        else:
            print(f"    {resp.text[:200]}")
    return None


def show_activities_endpoint(task_id, task_name="", limit=10):
    """
    Explore /api/v1/activities — the correct activity log endpoint.
    Try various filter params to find recurring completions.
    """
    print(f"\n=== /api/v1/activities — probing filter params ===")
    param_sets = [
        ("no filter",           {"limit": limit}),
        ("object_type=task",    {"object_type": "task", "limit": limit}),
        ("event_type=completed",{"object_type": "task", "event_type": "completed", "limit": limit}),
        ("event_type=updated",  {"object_type": "task", "event_type": "updated", "limit": limit}),
        ("object_id=task_id",   {"object_type": "task", "object_id": task_id, "limit": limit}),
        ("item event_type=completed", {"object_type": "item", "event_type": "completed", "limit": limit}),
    ]
    for label, params in param_sets:
        resp = requests.get(
            "https://api.todoist.com/api/v1/activities",
            headers=HEADERS, params=params, timeout=10,
        )
        if resp.status_code != 200:
            print(f"  [{label}]  status={resp.status_code}  {resp.text[:100]}")
            continue
        data = resp.json()
        results = data.get("results", [])
        recurring_completions = [
            r for r in results
            if r.get("event_type") == "completed"
            and r.get("extra_data", {}).get("is_recurring")
        ]
        print(f"  [{label}]  total={len(results)}  recurring_completions={len(recurring_completions)}")
        if results:
            r0 = results[0]
            print(f"    sample keys: {list(r0.keys())}")
            print(f"    sample: date={r0.get('event_date','')[:10]}  type={r0.get('event_type')}  extra={r0.get('extra_data',{})}")


def show_activities_deep_dive(task_id=None):
    """
    Confirm the correct field names, pagination, and filtering for
    /api/v1/activities with object_type=item.
    """
    print("\n=== /api/v1/activities deep dive ===")

    # 1. Completed recurring events — full field inspection
    resp = requests.get(
        "https://api.todoist.com/api/v1/activities",
        headers=HEADERS,
        params={"object_type": "item", "event_type": "completed", "limit": 5,
                "since": SINCE.strftime("%Y-%m-%dT%H:%M:%S")},
        timeout=10,
    )
    data = resp.json()
    results = data.get("results", [])
    print(f"\n1. completed events (last 30d): {len(results)}, has_more={bool(data.get('next_cursor'))}")
    for r in results[:3]:
        extra = r.get("extra_data", {})
        print(f"   {r['event_date'][:10]}  object_id={r['object_id']}  "
              f"is_recurring={extra.get('is_recurring')}  content={extra.get('content','')[:40]}")

    # 2. Updated events — check field names for snooze detection
    resp2 = requests.get(
        "https://api.todoist.com/api/v1/activities",
        headers=HEADERS,
        params={"object_type": "item", "event_type": "updated", "limit": 5,
                "since": SINCE.strftime("%Y-%m-%dT%H:%M:%S")},
        timeout=10,
    )
    data2 = resp2.json()
    results2 = data2.get("results", [])
    print(f"\n2. updated events (last 30d): {len(results2)}")
    for r in results2[:3]:
        extra = r.get("extra_data", {})
        print(f"   {r['event_date'][:10]}  object_id={r['object_id']}  "
              f"last_due_date={extra.get('last_due_date')}  due_date={extra.get('due_date')}")

    # 3. Filter by specific task object_id (if supported)
    if task_id:
        resp3 = requests.get(
            "https://api.todoist.com/api/v1/activities",
            headers=HEADERS,
            params={"object_type": "item", "object_id": task_id, "limit": 5},
            timeout=10,
        )
        print(f"\n3. filter by object_id={task_id}: status={resp3.status_code}")
        if resp3.status_code == 200:
            r3 = resp3.json().get("results", [])
            print(f"   events returned: {len(r3)}")
            for r in r3[:3]:
                print(f"   {r['event_date'][:10]}  type={r['event_type']}  "
                      f"extra={r.get('extra_data',{})}")
        else:
            print(f"   {resp3.text[:200]}")

    # 4. Pagination test
    resp4 = requests.get(
        "https://api.todoist.com/api/v1/activities",
        headers=HEADERS,
        params={"object_type": "item", "event_type": "completed", "limit": 200,
                "since": SINCE.strftime("%Y-%m-%dT%H:%M:%S")},
        timeout=10,
    )
    data4 = resp4.json()
    all_completed = data4.get("results", [])
    next_cursor = data4.get("next_cursor")
    print(f"\n4. pagination: first page={len(all_completed)}, next_cursor={bool(next_cursor)}")
    if next_cursor:
        resp5 = requests.get(
            "https://api.todoist.com/api/v1/activities",
            headers=HEADERS,
            params={"object_type": "item", "event_type": "completed", "limit": 200,
                    "cursor": next_cursor},
            timeout=10,
        )
        page2 = resp5.json().get("results", [])
        print(f"   second page={len(page2)}, next_cursor={bool(resp5.json().get('next_cursor'))}")

    # 5. Count recurring vs non-recurring in completed events
    rec = [r for r in all_completed if r.get("extra_data", {}).get("is_recurring")]
    print(f"\n5. of {len(all_completed)} completed events: {len(rec)} are recurring")


def probe_activity_url_variants(task_id=None):
    """
    Systematically try every plausible URL shape for the activity log.
    The MCP find-activity works (OAuth), but /api/v1/activity 404s with token auth.
    """
    print("\n=== Probing activity URL variants ===")
    urls = [
        "https://api.todoist.com/api/v1/activity",
        "https://api.todoist.com/api/v1/activities",
        "https://api.todoist.com/api/v1/tasks/activity",
        "https://api.todoist.com/api/v1/events",
        "https://api.todoist.com/api/v1/log",
        "https://api.todoist.com/api/v1/audit",
    ]
    if task_id:
        urls += [
            f"https://api.todoist.com/api/v1/tasks/{task_id}/activity",
            f"https://api.todoist.com/api/v1/tasks/{task_id}/events",
            f"https://api.todoist.com/api/v1/tasks/{task_id}/history",
        ]
    for url in urls:
        resp = requests.get(url, headers=HEADERS, params={"limit": 1}, timeout=10)
        print(f"  {resp.status_code}  {url}")
        if resp.status_code == 200:
            print(f"    keys: {list(resp.json().keys())[:8]}")


def show_activity_log_global(limit=20):
    """
    Try the /api/v1/activity endpoint without filtering to a specific task.
    Look for completed events with isRecurring=true.
    """
    print(f"\n=== /api/v1/activity (global, last {limit} events) ===")
    resp = requests.get(
        "https://api.todoist.com/api/v1/activity",
        headers=HEADERS,
        params={"object_type": "task", "event_type": "completed", "limit": limit},
        timeout=10,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:300])
        return None
    data = resp.json()
    events = data.get("events", data.get("results", []))
    print(f"Keys: {list(data.keys())}  events: {len(events)}")
    recurring_completions = [e for e in events if e.get("extra_data", {}).get("isRecurring")]
    print(f"Recurring completions in sample: {len(recurring_completions)}")
    for e in events[:5]:
        extra = e.get("extra_data", {})
        print(f"  {e.get('event_date','')[:10]}  id={e.get('object_id')}  isRecurring={extra.get('isRecurring')}  content={extra.get('content','')[:50]}")
    return data


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    recurring = show_recurring_tasks()
    completed = show_completed_by_completion_date()
    show_completed_by_due_date()
    match_recurring_vs_completed(recurring, completed)

    if recurring:
        show_task_activity(recurring[0].id, recurring[0].content)

    show_raw_api_endpoints()
    show_sync_completed_items()

    # New: probe the activity endpoint directly
    if recurring:
        show_activity_log_for_task(recurring[0].id, recurring[0].content)
    show_activity_log_global()
    probe_activity_url_variants(recurring[0].id if recurring else None)
    if recurring:
        show_activities_endpoint(recurring[0].id, recurring[0].content)
    show_activities_deep_dive(recurring[0].id if recurring else None)
