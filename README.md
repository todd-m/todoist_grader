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
pip install todoist-api-python requests rich
```

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
python grader.py --dry-run --summary

# Apply grades and print a table
python grader.py --summary

# Use a different config file
python grader.py --config ~/my-config.toml
```

### Flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Print planned changes without writing to Todoist |
| `--summary` | Print a rich table of all recurring tasks with their rates and grades |
| `--config PATH` | Path to config file (default: `config.toml`) |

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
