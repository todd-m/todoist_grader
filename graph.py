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


def build_dataset(rows: dict[str, list]) -> dict:
    all_dates = sorted({row[0] for series in rows.values() for row in series})
    datasets = []
    for i, (filter_name, series) in enumerate(rows.items()):
        by_date = {row[0]: row[1] for row in series}
        color = COLORS[i % len(COLORS)]
        datasets.append(
            {
                "label": filter_name,
                "data": [by_date.get(d) for d in all_dates],
                "borderColor": color,
                "backgroundColor": color,
                "tension": 0.1,
                "pointRadius": 4,
                "spanGaps": False,
            }
        )
    return {"labels": all_dates, "datasets": datasets}


def render_chart(dataset: dict, subtitle: str, index: int) -> str:
    data_json = json.dumps(dataset).replace("</", r"<\/")
    canvas_id = f"chart-{int(index)}"
    heading = f"    <h2>{_html.escape(subtitle)}</h2>\n" if subtitle else ""
    return f"""  <div class="chart-cell">
{heading}    <div class="chart-container">
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
    </script>
  </div>"""


def render_page(groups: list[list[tuple[dict, str]]], title: str) -> str:
    escaped_title = _html.escape(title)
    sections = []
    index = 0
    for group in groups:
        cells = []
        for ds, sub in group:
            cells.append(render_chart(ds, sub, index))
            index += 1
        sections.append('  <section class="chart-group">\n' + "\n".join(cells) + "\n  </section>")
    body_fragments = "\n".join(sections)
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
      margin: 0 0 12px;
      color: var(--fg);
    }}
    .chart-group {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
      gap: 24px 32px;
      max-width: 1400px;
      margin-bottom: 40px;
    }}
    .chart-cell {{
      min-width: 0;
      max-width: 700px;
    }}
    .chart-container {{
      position: relative;
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


def write_graph(html: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
