"""Фігури plotly для дашборда: ті самі функції з charts.py + тема Telegram.

charts.py розрахований на світле тло. Для темної теми кольори в JSON фігури
замінюємо за таблицею «константа charts.py -> темний відповідник». Таблиця
будується з імен констант, тож зміна палітри власником підхопиться сама.
Тло фігури прозоре в обох темах: графік лягає на картку з кольором теми Telegram.
"""

from __future__ import annotations

import json
import re

import plotly.graph_objects as go

import charts

TRANSPARENT = "rgba(0,0,0,0)"
SUBTITLE_WRAP = 44

# Ім'я константи charts.py -> колір у темній темі.
DARK_BY_NAME = {
    "SURFACE_COLOR": TRANSPARENT,
    "TEXT_COLOR": "#f1f1ef",
    "AXIS_TEXT_COLOR": "#a9a9a4",
    "GRID_COLOR": "#3a3a3d",
    "AXIS_LINE_COLOR": "#55555a",
    "MEDIAN_COLOR": "#f1f1ef",
}
LIGHT_BY_NAME = {"SURFACE_COLOR": TRANSPARENT}
# Колір, який charts задає не константою (підказка при наведенні).
EXTRA_DARK = {"white": "#2c2c2e"}
DARK_TEMPLATE = "plotly_dark"


def palette(theme: str) -> dict[str, str]:
    """{світлий колір з charts.py: колір для теми} — ключі беремо з самих констант."""
    names = DARK_BY_NAME if theme == "dark" else LIGHT_BY_NAME
    mapping = {getattr(charts, name).lower(): color for name, color in names.items()
               if hasattr(charts, name)}
    if theme == "dark":
        mapping.update(EXTRA_DARK)
    return mapping


def light_colors() -> set[str]:
    """Усі світлі кольори charts.py, які в темній темі лишатися не мають."""
    return {getattr(charts, name).lower() for name in DARK_BY_NAME if hasattr(charts, name)}


def _recolor(value, mapping, pattern):
    if isinstance(value, dict):
        return {key: _recolor(item, mapping, pattern) for key, item in value.items()}
    if isinstance(value, list):
        return [_recolor(item, mapping, pattern) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in mapping:
            return mapping[lowered]
        if pattern is not None and "#" in value:
            return pattern.sub(lambda m: mapping[m.group(0).lower()], value)
    return value


def themed(fig: go.Figure, theme: str = "light") -> dict:
    """Фігура -> словник JSON для Plotly.newPlot з кольорами під тему."""
    theme = "dark" if theme == "dark" else "light"
    if theme == "dark":
        fig = go.Figure(fig)
        fig.update_layout(template=DARK_TEMPLATE)
    data = json.loads(fig.to_json())
    mapping = palette(theme)
    hex_keys = [re.escape(key) for key in mapping if key.startswith("#")]
    pattern = re.compile("|".join(hex_keys), re.IGNORECASE) if hex_keys else None
    data = _recolor(data, mapping, pattern)
    layout = data.setdefault("layout", {})
    layout["autosize"] = True
    layout.pop("width", None)
    margin = layout.setdefault("margin", {})
    margin["l"], margin["r"] = 8, 12
    title = layout.get("title")
    if isinstance(title, dict):
        title.setdefault("pad", {})["l"] = 4
    return data


def compact(fig: go.Figure, height: int | None = None, keep_title: bool = False) -> go.Figure:
    """Мобільна версія: без заголовка (він є в картці), менша висота."""
    fig = go.Figure(fig)
    if not keep_title:
        top = fig.layout.margin.t or 0
        title = fig.layout.title
        removed = (34 if title and title.text else 0)
        fig.update_layout(title_text="", margin_t=max(16, top - removed))
    subtitle = fig.layout.title.subtitle.text if fig.layout.title and fig.layout.title.subtitle else None
    if subtitle and len(subtitle) > SUBTITLE_WRAP and " · " in subtitle:
        # На вузькому екрані довгий підзаголовок обрізається: переносимо частини.
        parts = subtitle.split(" · ")
        fig.update_layout(title_subtitle_text="<br>".join(parts),
                          margin_t=(fig.layout.margin.t or 0) + 18 * (len(parts) - 1))
    if height:
        fig.update_layout(height=height)
    fig.update_layout(dragmode=False)
    return fig


def score_distribution(scores, threshold: float) -> go.Figure:
    """Розподіл балів конкурентних тендерів (крок 5) з лінією порогу «високого балу»."""
    import numpy as np
    import pandas as pd

    values = pd.to_numeric(pd.Series(scores), errors="coerce").dropna().to_numpy()
    if len(values) == 0:
        return charts.empty_figure()
    edges = np.arange(0, 105, 5)
    counts, _ = np.histogram(values, bins=edges)
    colors = [charts.ACCENT_COLOR if left >= threshold else charts.MAIN_COLOR for left in edges[:-1]]
    fig = go.Figure(go.Bar(
        x=(edges[:-1] + edges[1:]) / 2, y=counts, width=4.2,
        marker=dict(color=colors, cornerradius=3),
        customdata=[[int(a), int(b)] for a, b in zip(edges[:-1], edges[1:])],
        hovertemplate="Бал %{customdata[0]}–%{customdata[1]}: %{y} тендерів<extra></extra>",
    ))
    charts.apply_style(fig, "", f"{charts.plural(len(values), charts.TENDER_FORMS)}", height=300)
    fig.update_yaxes(type="log", title_text="Тендерів (логарифмічна шкала)")
    fig.update_xaxes(title_text="Бал", range=[0, 100], dtick=10, showgrid=False)
    fig.add_shape(type="line", x0=threshold, x1=threshold, y0=0, y1=1, yref="paper",
                  line=dict(color=charts.MEDIAN_COLOR, width=1.5, dash="dot"))
    fig.add_annotation(x=threshold, y=1, yref="paper", yanchor="bottom", xanchor="left",
                       showarrow=False, text=f" високий бал від {threshold}",
                       font=dict(color=charts.TEXT_COLOR))
    return fig
