# snapshot.py
import argparse
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

import db
import graph
from todoist_api import build_last_completion_map, get_with_retry

SYNC_URL = "https://api.todoist.com/api/v1/sync"
FILTER_URL = "https://api.todoist.com/api/v1/tasks/filter"


def load_config(path: str = "config.toml") -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Config file not found: {path}")
    with open(p, "rb") as f:
        return tomllib.load(f)


def fetch_todoist_filters(token: str) -> dict[str, tuple[str, str]]:
    resp = requests.post(
        SYNC_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"sync_token": "*", "resource_types": ["filters"]},
        timeout=15,
    )
    resp.raise_for_status()
    result: dict[str, tuple[str, str]] = {}
    for f in resp.json().get("filters", []):
        if f.get("is_deleted"):
            continue
        display_name = f["name"]
        result[display_name.lower()] = (display_name, f["query"])
    return result


def resolve_filters(
    config_names: list[str],
    todoist_filters: dict[str, tuple[str, str]],
) -> list[tuple[str, str, str]]:
    resolved = []
    for name in config_names:
        match = todoist_filters.get(name.lower())
        if match is None:
            print(f"Warning: filter {name!r} not found in Todoist — skipping", file=sys.stderr)
            continue
        display_name, query = match
        resolved.append((name, display_name, query))
    return resolved


def fetch_filter_tasks(
    token: str, query: str, retries: int = 3, backoff: float = 2.0
) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    tasks: list[dict] = []
    cursor: str | None = None

    while True:
        params: dict = {"query": query, "limit": 200}
        if cursor:
            params["cursor"] = cursor

        resp = get_with_retry(
            FILTER_URL,
            headers=headers,
            params=params,
            timeout=15,
            retries=retries,
            backoff=backoff,
            label="fetch_filter_tasks",
        )

        data = resp.json()
        results = data.get("results", [])
        tasks.extend(results)
        cursor = data.get("next_cursor")
        if not cursor:
            break

    return tasks


def compute_avg_age(
    tasks: list[dict],
    completion_map: dict[str, date],
    today: date,
) -> float | None:
    ages = []
    for task in tasks:
        created_str = task.get("added_at", "")
        if not created_str:
            continue
        created_date = date.fromisoformat(created_str[:10])
        due = task.get("due") or {}
        is_recurring = due.get("is_recurring", False)
        if is_recurring and str(task.get("id", "")) in completion_map:
            ref_date = completion_map[str(task["id"])]
        else:
            ref_date = created_date
        ages.append((today - ref_date).days)
    return sum(ages) / len(ages) if ages else None


def _render_graph(snap_cfg: dict, history: dict) -> None:
    solo_filter_names = {n.lower() for n in snap_cfg.get("solo_filters", [])}

    # Count series: extract (date, count) from SnapshotRows
    main_count_rows = {
        k: [(row.date, row.count) for row in v]
        for k, v in history.items()
        if k.lower() not in solo_filter_names
    }
    solo_count_rows = {
        k: [(row.date, row.count) for row in v]
        for k, v in history.items()
        if k.lower() in solo_filter_names
    }

    # Age series split by solo_filters, mirroring the count chart pattern
    main_age_rows = {
        k: [(row.date, row.avg_age_days) for row in v]
        for k, v in history.items()
        if k.lower() not in solo_filter_names
    }
    main_age_rows = {k: v for k, v in main_age_rows.items() if any(a is not None for _, a in v)}

    solo_age_rows = {
        k: [(row.date, row.avg_age_days) for row in v]
        for k, v in history.items()
        if k.lower() in solo_filter_names
    }
    solo_age_rows = {k: v for k, v in solo_age_rows.items() if any(a is not None for _, a in v)}

    # Each group keeps one filter's stats together (count, age, …) on its own row.
    groups: list[list[tuple[dict, str]]] = []

    main_group: list[tuple[dict, str]] = []
    if main_count_rows:
        main_group.append((graph.build_dataset(main_count_rows), ""))
    if main_age_rows:
        main_group.append((graph.build_dataset(main_age_rows), "Avg Task Age (days)"))
    if main_group:
        groups.append(main_group)

    for name, series in solo_count_rows.items():
        solo_group: list[tuple[dict, str]] = [(graph.build_dataset({name: series}), name)]
        if name in solo_age_rows:
            solo_group.append(
                (graph.build_dataset({name: solo_age_rows[name]}), f"{name} — Avg Task Age (days)")
            )
        groups.append(solo_group)

    graph_path = snap_cfg.get("graph_path", "snapshots_graph.html")
    html = graph.render_page(groups, "Task Snapshots — Last 30 Days")
    graph.write_graph(html, graph_path)
    try:
        subprocess.run(["open", graph_path])
    except FileNotFoundError:
        print(f"Graph written to {graph_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--graph-only",
        action="store_true",
        help="Re-render graph from existing DB data without fetching new snapshots",
    )
    args, _ = parser.parse_known_args()

    cfg = load_config()

    snap_cfg = cfg.get("snapshots")
    if not snap_cfg:
        sys.exit("config.toml must have a [snapshots] section")

    if args.graph_only:
        db_path: str = snap_cfg.get("db_path", "snapshots.db")
        conn = db.init_db(db_path)
        try:
            filter_names = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT filter_name FROM snapshots ORDER BY filter_name"
                ).fetchall()
            ]
            if not filter_names:
                sys.exit("No snapshot data found. Run `make snapshot` first.")
            history = db.read_last_n_days(conn, filter_names)
        finally:
            conn.close()
        _render_graph(snap_cfg, history)
        return

    try:
        token = cfg["todoist"]["api_token"]
    except KeyError:
        sys.exit("config.toml must have [todoist] api_token")

    config_names: list[str] = snap_cfg.get("filters", [])
    solo_names: list[str] = snap_cfg.get("solo_filters", [])
    seen = {n.lower() for n in config_names}
    all_names = list(config_names) + [n for n in solo_names if n.lower() not in seen]
    if not all_names:
        sys.exit("[snapshots] filters must not be empty")

    db_path = snap_cfg.get("db_path", "snapshots.db")

    print("Fetching Todoist filters…")
    todoist_filters = fetch_todoist_filters(token)

    resolved = resolve_filters(all_names, todoist_filters)
    if not resolved:
        sys.exit("No configured filters matched any Todoist filter. Aborting.")

    today_date = date.today()
    today = today_date.isoformat()

    print("Counting tasks…")
    all_tasks: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for _config_name, display_name, query in resolved:
        tasks = fetch_filter_tasks(token, query)
        all_tasks[display_name] = tasks
        counts[display_name] = len(tasks)
        print(f"  {display_name}: {len(tasks)}")

    try:
        completion_map = build_last_completion_map(token, lookback_days=365)
        avg_ages: dict[str, float | None] = {
            display_name: compute_avg_age(all_tasks[display_name], completion_map, today_date)
            for _, display_name, _ in resolved
        }
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code < 500:
            raise
        print(
            f"Warning: activities endpoint returned {exc.response.status_code} — "
            f"avg age skipped for this run",
            file=sys.stderr,
        )
        avg_ages = {display_name: None for _, display_name, _ in resolved}

    conn = db.init_db(db_path)
    try:
        for display_name, n in counts.items():
            db.write_snapshot(conn, today, display_name, n, avg_ages.get(display_name))
        prior = db.read_latest_before(conn, today)
        history = db.read_last_n_days(conn, [dn for _, dn, _ in resolved], as_of=today)
    finally:
        conn.close()

    console = Console()
    table = Table(
        title=f"Task Snapshots — {today}",
        show_header=True,
        header_style="bold",
        border_style="dim",
    )
    table.add_column("Filter", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Δ prior", justify="right")
    table.add_column("Avg Age", justify="right")

    for _config_name, display_name, _query in resolved:
        n = counts[display_name]
        prior_n = prior.get(display_name)
        if prior_n is None:
            delta_str = "—"
        else:
            delta = n - prior_n
            delta_str = f"+{delta}" if delta > 0 else str(delta)
        avg = avg_ages.get(display_name)
        avg_str = f"{avg:.0f}d" if avg is not None else "—"
        table.add_row(display_name, str(n), delta_str, avg_str)

    console.print(table)
    _render_graph(snap_cfg, history)


if __name__ == "__main__":
    main()
