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
    raise NotImplementedError


if __name__ == "__main__":
    main()
