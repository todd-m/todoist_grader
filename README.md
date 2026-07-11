# todoist-grader

Grades your Todoist recurring tasks based on how consistently you complete
them. Assigns `grade:A`, `grade:B`, or `grade:C` labels directly in Todoist.

## How it works

For each recurring task over a configurable look-back window:

```
completion_rate = completions / (completions + snoozes)
```

A **completion** is any day the task was marked done.
A **snooze** is any day you pushed the due date forward without completing it.

| Rate | Grade |
|------|-------|
| ≥ 85% | A |
| ≥ 65% | B |
| < 65% | C |

Thresholds are configurable.

## Requirements

- Python 3.11+
- Todoist Pro/Business (activity log required for snooze detection; free accounts get completion-only rates)

## Installation

```bash
make install
```

Creates a `.venv` virtual environment and installs pinned dependencies from
`requirements.txt`.

## Configuration

Copy and edit `config.toml`:

```toml
[todoist]
api_token = "your_token_here"  # https://app.todoist.com/app/settings/integrations/developer

[grading]
days = 30  # look-back window

[grading.thresholds]
A = 0.85
B = 0.65

[rate_limit]
write_delay_seconds = 0.5
```

## Usage

```bash
# Preview changes without writing anything
make dry-run

# Apply grades and print a table
make summary

# Or invoke directly, e.g. with a different config file
.venv/bin/python grader.py --config ~/my-config.toml
```

Run `make help` for the full list of targets (`run`, `dry-run`, `summary`,
`today`, `completed`, `snapshot`, `graph`, `test`, `lint`, `audit`, `ci`).

### Flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Print planned changes without writing to Todoist |
| `--summary` | Print a rich table of all recurring tasks with their rates and grades |
| `--today` | Filter report to tasks due today |
| `--completed` | Print a completion report for a project (requires `--project`) |
| `--project NAME` | Project name for `--completed`; via Make: `make completed PROJECT="Name"` |
| `--config PATH` | Path to config file (default: `config.toml`) |

---

## Daily filter snapshots (`snapshot.py`)

Captures the task count for a set of named Todoist filters once per day, stores them in a local SQLite database, and prints a table showing today's count alongside the delta versus the most recent prior snapshot.

```bash
make snapshot
```

```
Fetching Todoist filters…
Counting tasks…
  Next 7 Days: 42
  Next 30 Days: 118
                  Task Snapshots — 2026-06-06
┌──────────────┬───────┬─────────┬─────────┐
│ Filter       │ Count │ Δ prior │ Avg Age │
├──────────────┼───────┼─────────┼─────────┤
│ Next 7 Days  │    42 │      +3 │     14d │
│ Next 30 Days │   118 │      -1 │     21d │
└──────────────┴───────┴─────────┴─────────┘
```

The Δ column shows `—` on the first run and `0` / `+N` / `-N` thereafter. Re-running on the same day overwrites today's row (idempotent).

**Avg Age** is the average age of tasks in each filter. For non-recurring tasks this is days since the task was created. For recurring tasks it is days since the task was last completed (using a 365-day lookback window), so it reflects how stale your recurring work feels rather than how old the task definition is.

### Config

Add to `config.toml`:

```toml
[snapshots]
db_path = "snapshots.db"          # SQLite file path (gitignored)
graph_path = "snapshots_graph.html"  # optional; this is the default

filters = [
  "next 7 days",
  "next 30 days",
]

solo_filters = [                   # optional; each gets its own chart below the main one
  "view all",
]
```

Filter names can be defined in `filters` and/or `solo_filters`. Any name with no match in Todoist is warned and skipped.

After printing the table, `make snapshot` generates `snapshots_graph.html` and opens it in your browser. The page shows the last 30 days of data, one row of charts per filter group:

- **Main row** — a task-count chart (one series per filter in `filters`) alongside an average-task-age chart.
- **Solo rows** — each filter in `solo_filters` gets its own row with its count and age charts side by side.

The page respects the system dark/light appearance preference.

To re-render the graph from existing data without fetching new snapshots:

```bash
make graph
```

---

## Example output

```
Applying grade labels…
  "Exercise"                                         rate=91.7%  →  grade:A
  "Read for 20 minutes"                              rate=72.3%  →  grade:B
  "Weekly review"                                    rate=45.0%  →  grade:C

┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━┓
┃ Task                    ┃ Completions ┃ Snoozes ┃  Rate ┃ Grade ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━┩
│ Exercise                │          22 │       2 │ 91.7% │   A   │
│ Read for 20 minutes     │          21 │       8 │ 72.4% │   B   │
│ Weekly review           │           9 │      11 │ 45.0% │   C   │
└─────────────────────────┴─────────────┴─────────┴───────┴───────┘
```
