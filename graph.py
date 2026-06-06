import html as _html
import json

COLORS = [
    "#388bfd",
    "#a371f7",
    "#3fb950",
    "#e3b341",
    "#f78166",
    "#79c0ff",
]


def build_dataset(rows: dict[str, list[tuple[str, int]]]) -> dict:
    all_dates = sorted({d for series in rows.values() for d, _ in series})
    datasets = []
    for i, (filter_name, series) in enumerate(rows.items()):
        by_date = {d: c for d, c in series}
        color = COLORS[i % len(COLORS)]
        datasets.append({
            "label": filter_name,
            "data": [by_date.get(d) for d in all_dates],
            "borderColor": color,
            "backgroundColor": color,
            "tension": 0.1,
            "pointRadius": 4,
            "spanGaps": False,
        })
    return {"labels": all_dates, "datasets": datasets}


def render_chart(dataset: dict, subtitle: str, index: int) -> str:
    data_json = json.dumps(dataset).replace("</", r"<\/")
    canvas_id = f"chart-{int(index)}"
    heading = f"  <h2>{_html.escape(subtitle)}</h2>\n" if subtitle else ""
    return f"""{heading}  <div class="chart-container">
    <canvas id="{canvas_id}"></canvas>
  </div>
  <script>
    new Chart(document.getElementById('{canvas_id}'), {{
      type: 'line',
      data: {data_json},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ labels: {{ color: textColor }} }} }},
        scales: {{
          x: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }},
          y: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }}, beginAtZero: false }}
        }}
      }}
    }});
  </script>"""


def render_page(charts: list[tuple[dict, str]], title: str) -> str:
    escaped_title = _html.escape(title)
    body_fragments = "\n".join(
        render_chart(ds, sub, i) for i, (ds, sub) in enumerate(charts)
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escaped_title}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #ffffff;
      --fg: #24292f;
      --grid: #d0d7de;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0d1117;
        --fg: #c9d1d9;
        --grid: #21262d;
      }}
    }}
    body {{
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 24px;
    }}
    h1 {{
      font-size: 18px;
      font-weight: 600;
      margin: 0 0 24px;
    }}
    h2 {{
      font-size: 14px;
      font-weight: 600;
      margin: 32px 0 12px;
      color: var(--fg);
    }}
    .chart-container {{
      position: relative;
      max-width: 900px;
    }}
  </style>
  <script>
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const gridColor = isDark ? '#21262d' : '#d0d7de';
    const textColor = isDark ? '#c9d1d9' : '#24292f';
  </script>
</head>
<body>
  <h1>{escaped_title}</h1>
{body_fragments}
</body>
</html>"""


def render_html(dataset: dict, title: str) -> str:
    data_json = json.dumps(dataset).replace("</", r"<\/")
    title = _html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #ffffff;
      --fg: #24292f;
      --grid: #d0d7de;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0d1117;
        --fg: #c9d1d9;
        --grid: #21262d;
      }}
    }}
    body {{
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 24px;
    }}
    h1 {{
      font-size: 18px;
      font-weight: 600;
      margin: 0 0 24px;
    }}
    .chart-container {{
      position: relative;
      max-width: 900px;
    }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class="chart-container">
    <canvas id="chart"></canvas>
  </div>
  <script>
    const DATA = {data_json};
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const gridColor = isDark ? '#21262d' : '#d0d7de';
    const textColor = isDark ? '#c9d1d9' : '#24292f';
    new Chart(document.getElementById('chart'), {{
      type: 'line',
      data: DATA,
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ labels: {{ color: textColor }} }}
        }},
        scales: {{
          x: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }},
          y: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }}, beginAtZero: false }}
        }}
      }}
    }});
  </script>
</body>
</html>"""


def write_graph(html: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
