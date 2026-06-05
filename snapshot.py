# snapshot.py
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

import db

SYNC_URL   = "https://api.todoist.com/api/v1/sync"
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


def count_filter_tasks(token: str, query: str, retries: int = 3, backoff: float = 2.0) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    count = 0
    cursor: str | None = None

    while True:
        params: dict = {"query": query, "limit": 200}
        if cursor:
            params["cursor"] = cursor

        attempt = 0
        while True:
            resp = requests.get(FILTER_URL, headers=headers, params=params, timeout=15)
            try:
                resp.raise_for_status()
                break
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code < 500:
                    raise
                attempt += 1
                if attempt > retries:
                    raise
                time.sleep(backoff ** attempt)

        data = resp.json()
        results = data.get("results", [])
        count += len(results)
        cursor = data.get("next_cursor")
        if not cursor or len(results) < 200:
            break

    return count


def main() -> None:
    cfg = load_config()

    try:
        token = cfg["todoist"]["api_token"]
    except KeyError:
        sys.exit("config.toml must have [todoist] api_token")

    snap_cfg = cfg.get("snapshots")
    if not snap_cfg:
        sys.exit("config.toml must have a [snapshots] section")

    config_names: list[str] = snap_cfg.get("filters", [])
    if not config_names:
        sys.exit("[snapshots] filters must not be empty")

    db_path: str = snap_cfg.get("db_path", "snapshots.db")

    print("Fetching Todoist filters…")
    todoist_filters = fetch_todoist_filters(token)

    resolved = resolve_filters(config_names, todoist_filters)
    if not resolved:
        sys.exit("No configured filters matched any Todoist filter. Aborting.")

    today = date.today().isoformat()

    print("Counting tasks…")
    counts: dict[str, int] = {}
    for _config_name, display_name, query in resolved:
        n = count_filter_tasks(token, query)
        counts[display_name] = n
        print(f"  {display_name}: {n}")

    conn = db.init_db(db_path)
    for display_name, n in counts.items():
        db.write_snapshot(conn, today, display_name, n)

    prior = db.read_latest_before(conn, today)
    conn.close()

    console = Console()
    table = Table(
        title=f"Task Snapshots — {today}",
        show_header=True,
        header_style="bold",
        border_style="dim",
    )
    table.add_column("Filter",  style="cyan")
    table.add_column("Count",   justify="right")
    table.add_column("Δ prior", justify="right")

    for _config_name, display_name, _query in resolved:
        n = counts[display_name]
        prior_n = prior.get(display_name)
        if prior_n is None:
            delta_str = "—"
        else:
            delta = n - prior_n
            delta_str = f"+{delta}" if delta > 0 else str(delta)
        table.add_row(display_name, str(n), delta_str)

    console.print(table)


if __name__ == "__main__":
    main()
