"""Графіки для сайту.

Три функції з контракту, кожна повертає plotly.graph_objects.Figure і нічого не показує:
price_histogram(), top_winners(), purchases_over_time().
На сайті фігуру виводить st.plotly_chart(fig).
На порожніх даних повертається фігура з написом «Немає даних», а не виняток.
Модуль не читає файли і не залежить від app.py.
"""

import math
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# --- Спільний стиль ----------------------------------------------------------

TEMPLATE = "plotly_white"
FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif'
FONT_SIZE = 13
TITLE_FONT_SIZE = 17

MAIN_COLOR = "#2a78d6"       # стовпці
SOFT_COLOR = "#86b6ef"       # стовпці другого плану (світліший крок того самого синього)
ACCENT_COLOR = "#eb6834"     # вибраний тендер
MEDIAN_COLOR = "#0b0b0b"     # лінія медіани
SURFACE_COLOR = "#fcfcfb"    # тло графіка
TEXT_COLOR = "#0b0b0b"       # заголовок і підписи значень
AXIS_TEXT_COLOR = "#52514e"  # підзаголовок і підписи осей
GRID_COLOR = "#e1e0d9"
AXIS_LINE_COLOR = "#c3c2b7"

SEPARATORS = ", "            # десяткова кома, тисячі через пробіл: 1 234,5
DEFAULT_HEIGHT = 400
EMPTY_HEIGHT = 220
NO_DATA_TEXT = "Немає даних"
UAH = "UAH"
AMOUNT_UNITS = ((1e9, "млрд грн"), (1e6, "млн грн"), (1e3, "тис. грн"), (1, "грн"))

# --- Гістограма цін ------------------------------------------------------------

# Якщо найбільша ціна в групі перевищує найменшу у стільки разів, вісь логарифмічна:
# на лінійній осі всі стовпці злиплися б біля нуля.
LOG_AXIS_MIN_SPREAD = 20
MIN_BINS = 8
MAX_BINS = 30
SAME_PRICE_BINS = 9          # непарне число: єдина ціна потрапляє в середину стовпця
MEDIAN_LABEL = "Медіана"
HIGHLIGHT_LABEL = "Вибраний тендер"
POSITION_FORMS = ("позиція", "позиції", "позицій")

# --- Постачальники -----------------------------------------------------------------

TOP_WINNERS_TITLE = "Хто найчастіше перемагає в цього замовника"
WINNER_NAME_MAX_LEN = 40
BAR_ROW_HEIGHT = 34          # висота рядка на одного постачальника, px
TENDER_FORMS = ("тендер", "тендери", "тендерів")
WINNER_FORMS = ("постачальник", "постачальники", "постачальників")
# Організаційну форму скорочуємо, інакше всі назви починаються однаково.
LEGAL_FORMS = (
    ("ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ", "ТОВ"),
    ("ПРИВАТНЕ АКЦІОНЕРНЕ ТОВАРИСТВО", "ПрАТ"),
    ("ПУБЛІЧНЕ АКЦІОНЕРНЕ ТОВАРИСТВО", "ПАТ"),
    ("АКЦІОНЕРНЕ ТОВАРИСТВО", "АТ"),
    ("ФІЗИЧНА ОСОБА-ПІДПРИЄМЕЦЬ", "ФОП"),
    ("ФІЗИЧНА ОСОБА - ПІДПРИЄМЕЦЬ", "ФОП"),
    ("КОМУНАЛЬНЕ НЕКОМЕРЦІЙНЕ ПІДПРИЄМСТВО", "КНП"),
    ("КОМУНАЛЬНЕ ПІДПРИЄМСТВО", "КП"),
    ("ДЕРЖАВНЕ ПІДПРИЄМСТВО", "ДП"),
    ("ПРИВАТНЕ ПІДПРИЄМСТВО", "ПП"),
)

# --- Закупівлі в часі ---------------------------------------------------------------

PURCHASES_TITLE = "Закупівлі замовника в часі"
PURCHASES_HEIGHT = 500
COUNT_PANEL = (0.57, 1.0)    # частка висоти під верхній графік (кількість)
AMOUNT_PANEL = (0.0, 0.43)   # і під нижній (суми)
BAR_WIDTH_SHARE = 0.8        # частка періоду, яку займає стовпець
FEW_PERIODS = 3
MAX_DATE_TICKS = 8
SHORT_RANGE_DAYS = 62        # для коротшого проміжку на осі дні, а не місяці
# pandas рахує періоди за «M», а в нових версіях місяць ще пишуть як «ME» чи «MS».
PERIOD_ALIASES = {"ME": "M", "MS": "M", "QE": "Q", "QS": "Q", "YE": "Y", "YS": "Y"}
PERIOD_NAMES = {"D": "по днях", "W": "по тижнях", "M": "по місяцях",
                "Q": "по кварталах", "Y": "по роках"}
# Заголовок підказки: дата початку періоду.
PERIOD_HOVER_FORMATS = {"D": "%d.%m.%Y", "W": "Тиждень з %d.%m.%Y", "M": "Місяць %m.%Y"}
DEFAULT_HOVER_FORMAT = "Період з %d.%m.%Y"
MONTHS = ("січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру")


# --- Допоміжні функції -----------------------------------------------------------

def format_number(value, digits=0):
    """1234567.8 -> '1 234 568', з digits=2 -> '1 234 567,80'."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def format_price(value):
    """Ціна з копійками: 12.5 -> '12,50', 0.085 -> '0,085'."""
    return format_number(value, 2 if value >= 1 else 3)


def format_amount(value):
    """Сума з одиницею: 2 500 000 -> '2,5 млн грн', 800 -> '800 грн'."""
    for divisor, unit in AMOUNT_UNITS:
        if value >= divisor:
            return f"{format_number(value / divisor, 0 if divisor == 1 else 1)} {unit}"
    return f"{format_number(value)} грн"


def plural(count, forms):
    """1 тендер, 2 тендери, 5 тендерів: forms = (один, кілька, багато)."""
    count = int(count)
    if count % 10 == 1 and count % 100 != 11:
        form = forms[0]
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        form = forms[1]
    else:
        form = forms[2]
    return f"{format_number(count)} {form}"


def positive_number(value):
    """Число більше нуля або None (для None, NaN і тексту)."""
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) and number > 0 else None


def uah_amounts(tenders):
    """Очікувана вартість тендерів у гривні; для іншої валюти й пропусків 0."""
    amounts = pd.to_numeric(tenders["expected_value"], errors="coerce")
    if "currency" in tenders.columns:
        amounts = amounts.where(tenders["currency"] == UAH)
    return amounts.fillna(0)


def count_axis(largest):
    """Налаштування осі з кількістю: лише цілі поділки, без 0,5 тендера."""
    return dict(rangemode="tozero", tickformat=",d", dtick=1 if largest <= 6 else None)


def apply_style(fig, title="", subtitle="", height=DEFAULT_HEIGHT):
    """Єдиний вигляд для всіх графіків: шрифт, кольори, сітка, формат чисел."""
    top_margin = 28 + (34 if title else 0) + (24 if subtitle else 0)
    fig.update_layout(
        template=TEMPLATE,
        title=dict(text=title, x=0, xanchor="left", yref="container", y=1, yanchor="top",
                   pad=dict(l=16, t=18),
                   font=dict(size=TITLE_FONT_SIZE, color=TEXT_COLOR),
                   subtitle=dict(text=subtitle,
                                 font=dict(size=FONT_SIZE, color=AXIS_TEXT_COLOR))),
        font=dict(family=FONT_FAMILY, size=FONT_SIZE, color=AXIS_TEXT_COLOR),
        paper_bgcolor=SURFACE_COLOR,
        plot_bgcolor=SURFACE_COLOR,
        separators=SEPARATORS,
        showlegend=False,
        height=height,
        margin=dict(l=16, r=32, t=top_margin, b=16),
        hoverlabel=dict(bgcolor="white", bordercolor=GRID_COLOR,
                        font=dict(family=FONT_FAMILY, size=FONT_SIZE, color=TEXT_COLOR)),
    )
    # Кольори підписів задаємо явно: інакше в темній темі Streamlit вони світлі на світлому тлі.
    axis_style = dict(gridcolor=GRID_COLOR, linecolor=AXIS_LINE_COLOR, zeroline=False,
                      automargin=True, exponentformat="none",
                      tickfont=dict(color=AXIS_TEXT_COLOR),
                      title_font=dict(color=AXIS_TEXT_COLOR))
    fig.update_xaxes(**axis_style)
    fig.update_yaxes(**axis_style)
    return fig


def empty_figure(title=""):
    """Фігура з написом «Немає даних» замість графіка."""
    fig = go.Figure()
    fig.add_annotation(text=NO_DATA_TEXT, x=0.5, y=0.5, xref="paper", yref="paper",
                       showarrow=False, font=dict(size=16, color=AXIS_TEXT_COLOR))
    apply_style(fig, title, height=EMPTY_HEIGHT)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


# --- Гістограма цін ------------------------------------------------------------

def price_histogram(prices, median, highlight=None, title=""):
    """Розподіл цін за одиницю в групі однакових товарів.

    prices: ціни позицій групи. median: медіана групи, вертикальна лінія.
    highlight: ціна у вибраному тендері, окрема лінія іншого кольору з підписом,
    у скільки разів вона вища за медіану.
    Коли ціни в групі відрізняються у десятки разів, вісь цін логарифмічна.
    """
    values = pd.to_numeric(pd.Series(prices), errors="coerce").to_numpy(dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if len(values) == 0:
        return empty_figure(title)

    median, highlight = positive_number(median), positive_number(highlight)
    marks = []
    if median:
        marks.append((f"{MEDIAN_LABEL}: {format_price(median)} грн", median, MEDIAN_COLOR))
    if highlight:
        text = f"{HIGHLIGHT_LABEL}: {format_price(highlight)} грн"
        if median:
            text += f" (×{format_number(highlight / median, 1)} від медіани)"
        marks.append((text, highlight, ACCENT_COLOR))

    lowest = min([values.min()] + [mark[1] for mark in marks])
    highest = max([values.max()] + [mark[1] for mark in marks])
    use_log = highest / lowest >= LOG_AXIS_MIN_SPREAD

    def to_axis(price):
        return np.log10(price) if use_log else price

    def from_axis(position):
        return 10 ** position if use_log else position

    left, right = to_axis(lowest), to_axis(highest)
    if right - left < 1e-9:   # усі ціни однакові: даємо стовпцю місце з обох боків
        padding = 0.5 if use_log else lowest * 0.5
        left, right = left - padding, right + padding
        bins = SAME_PRICE_BINS
    else:
        bins = int(np.clip(np.sqrt(len(values)) * 1.5, MIN_BINS, MAX_BINS))
    counts, edges = np.histogram(to_axis(values), bins=bins, range=(left, right))

    fig = go.Figure(go.Bar(
        x=(edges[:-1] + edges[1:]) / 2,
        y=counts,
        width=np.diff(edges),
        marker=dict(color=MAIN_COLOR, cornerradius=3,
                    line=dict(color=SURFACE_COLOR, width=1.5)),
        customdata=[[format_price(from_axis(a)), format_price(from_axis(b))]
                    for a, b in zip(edges[:-1], edges[1:])],
        hovertemplate="%{customdata[0]} – %{customdata[1]} грн<br>Позицій: %{y}<extra></extra>",
    ))
    if values.min() == values.max():
        price_range = f"усі по {format_price(values.min())} грн за одиницю"
    else:
        price_range = (f"від {format_price(values.min())} до {format_price(values.max())} "
                       "грн за одиницю")
    subtitle = f"{plural(len(values), POSITION_FORMS)} · {price_range}"
    apply_style(fig, title, subtitle)
    fig.update_layout(margin_t=fig.layout.margin.t + 22 * len(marks))
    fig.update_yaxes(title_text="Кількість позицій", **count_axis(counts.max()))
    padding = (right - left) * 0.03
    fig.update_xaxes(title_text="Ціна за одиницю, грн" + (" (логарифмічна шкала)" if use_log else ""),
                     showgrid=False, range=[left - padding, right + padding])
    if use_log:
        ticks = _log_ticks(left, right)
        fig.update_xaxes(tickvals=np.log10(ticks), ticktext=[_tick_label(t) for t in ticks])

    for row, (text, price, color) in enumerate(marks):
        position = to_axis(price)
        on_left_half = position < (left + right) / 2
        fig.add_shape(type="line", x0=position, x1=position, y0=0, y1=1, yref="paper",
                      line=dict(color=color, width=2))
        fig.add_annotation(
            x=position, y=1, yref="paper", yanchor="bottom", yshift=4 + 20 * row,
            xanchor="left" if on_left_half else "right", showarrow=False,
            text=f"<span style='color:{color}'>▍</span>{text}",
            font=dict(color=TEXT_COLOR))
    return fig


def _log_ticks(left, right):
    """Поділки 1, 2, 5, 10, 20, 50… у межах логарифмічної осі."""
    steps = (1, 2, 5) if right - left <= 3 else (1,)
    ticks = [step * 10.0 ** power
             for power in range(int(np.floor(left)), int(np.ceil(right)) + 1)
             for step in steps]
    return [tick for tick in ticks if left - 1e-9 <= np.log10(tick) <= right + 1e-9]


def _tick_label(tick):
    """0.05 -> '0,05', 5 -> '5', 2000 -> '2 000'."""
    return format_number(tick, max(0, -int(np.floor(np.log10(tick)))))


# --- Постачальники -----------------------------------------------------------------

def short_name(name, max_len=WINNER_NAME_MAX_LEN):
    """'ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "СМ-КАСКАД"' -> 'ТОВ "СМ-КАСКАД"'."""
    text = re.sub(r"\s+", " ", str(name).replace("­", "")).strip()
    for full, short in LEGAL_FORMS:
        if text.upper().startswith(full):
            text = short + text[len(full):]
            break
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _most_common(names):
    """Найчастіша назва постачальника: один ЄДРПОУ буває записаний по-різному."""
    names = names.dropna()
    return names.mode().iat[0] if len(names) else ""


def top_winners(buyer_tenders, n=10):
    """Постачальники, які найчастіше перемагають у замовника.

    buyer_tenders: рядки tenders одного замовника. Постачальників рахуємо за
    winner_edrpou, тендери без переможця відкидаємо. Лідер (або кілька лідерів
    з однаковим результатом) виділений насиченим кольором.
    """
    if not {"winner_edrpou", "winner_name"} <= set(buyer_tenders.columns):
        return empty_figure(TOP_WINNERS_TITLE)
    won = buyer_tenders.dropna(subset=["winner_edrpou"])
    if won.empty:
        return empty_figure(TOP_WINNERS_TITLE)

    by_winner = won.groupby("winner_edrpou")
    winners = pd.DataFrame({"wins": by_winner.size(),
                            "name": by_winner["winner_name"].agg(_most_common)})
    if "expected_value" in won.columns:
        winners["amount"] = uah_amounts(won).groupby(won["winner_edrpou"]).sum()
    else:
        winners["amount"] = 0.0
    winners = winners.reset_index()
    winners["name"] = winners["name"].where(winners["name"] != "", winners["winner_edrpou"])
    top = winners.sort_values(["wins", "name"], ascending=[False, True]).head(n)

    rows = list(range(len(top)))   # номери рядків замість назв: назви можуть збігатися
    is_leader = top["wins"] == top["wins"].max()
    fig = go.Figure(go.Bar(
        x=top["wins"],
        y=rows,
        orientation="h",
        width=0.62,
        marker=dict(color=np.where(is_leader, MAIN_COLOR, SOFT_COLOR), cornerradius=4),
        text=[f"<b>{format_number(wins)}</b>" for wins in top["wins"]],
        textposition="outside",
        textfont=dict(color=TEXT_COLOR, size=FONT_SIZE),
        cliponaxis=False,
        customdata=np.column_stack([
            top["name"], top["winner_edrpou"],
            [plural(wins, TENDER_FORMS) for wins in top["wins"]],
            [format_amount(amount) for amount in top["amount"]]]),
        hovertemplate=("<b>%{customdata[0]}</b><br>ЄДРПОУ %{customdata[1]}<br>"
                       "Виграно: %{customdata[2]} на %{customdata[3]}<extra></extra>"),
    ))
    subtitle = (f"{plural(len(won), TENDER_FORMS)} із переможцем · "
                f"{plural(len(winners), WINNER_FORMS)}")
    apply_style(fig, TOP_WINNERS_TITLE, subtitle, height=120 + BAR_ROW_HEIGHT * len(top))
    # Кожен стовпець підписано числом, тому вісь і сітка зайві.
    fig.update_xaxes(visible=False, range=[0, top["wins"].max() * 1.1])
    fig.update_yaxes(tickvals=rows, ticktext=[short_name(name) for name in top["name"]],
                     autorange="reversed", showgrid=False, showline=True,
                     tickfont=dict(color=TEXT_COLOR))
    return fig


# --- Закупівлі в часі ---------------------------------------------------------------

def _amount_unit(largest):
    """Одиниця для осі сум: (дільник, підпис). 2 500 000 -> (1e6, 'млн грн')."""
    for divisor, label in AMOUNT_UNITS:
        if largest >= divisor:
            return divisor, label
    return AMOUNT_UNITS[-1]


def _label_peak(fig, middles, heights, labels, yref):
    """Підписує лише найвищий стовпець: числа над кожним зливаються."""
    peak = int(np.argmax(heights))
    if heights[peak] > 0:
        fig.add_annotation(text=f"<b>{labels[peak]}</b>", x=middles[peak], y=heights[peak],
                           yref=yref, yanchor="bottom", yshift=2, showarrow=False,
                           font=dict(color=TEXT_COLOR, size=FONT_SIZE - 1))


def _month_ticks(first_day, last_day):
    """Поділки осі часу на початку місяців з українськими назвами: (дати, підписи)."""
    months = pd.period_range(first_day, last_day, freq="M")
    months = months[:: math.ceil(len(months) / MAX_DATE_TICKS)]
    several_years = first_day.year != last_day.year
    labels = [MONTHS[month.month - 1] + (f"<br>{month.year}" if several_years else "")
              for month in months]
    return months.start_time, labels


def purchases_over_time(buyer_tenders, freq="W"):
    """Кількість тендерів замовника та їхня очікувана вартість за періодами.

    buyer_tenders: рядки tenders одного замовника. Період рахуємо за date_created
    (у ній немає пропусків). freq: «W» тиждень, «M» місяць, «D» день.
    Дві величини мають різний масштаб, тому це два графіки один під одним
    зі спільною віссю часу і спільною підказкою. У суму входять лише тендери у гривні.
    """
    if not {"date_created", "expected_value"} <= set(buyer_tenders.columns):
        return empty_figure(PURCHASES_TITLE)
    dates = pd.to_datetime(buyer_tenders["date_created"], utc=True, errors="coerce")
    known = dates.notna()
    if not known.any():
        return empty_figure(PURCHASES_TITLE)

    freq = PERIOD_ALIASES.get(freq, freq)
    days = dates[known].dt.tz_localize(None)
    periods = days.dt.to_period(freq)
    by_period = uah_amounts(buyer_tenders)[known].groupby(periods).agg(["size", "sum"])
    # Періоди без закупівель теж показуємо, з нулем.
    by_period = by_period.reindex(
        pd.period_range(periods.min(), periods.max(), freq=freq), fill_value=0)

    starts = by_period.index.start_time
    ends = by_period.index.end_time
    length_ms = np.asarray((ends - starts).total_seconds()) * 1000
    counts = by_period["size"].to_numpy()
    amounts = by_period["sum"].to_numpy()
    divisor, unit = _amount_unit(amounts.max())

    # x — початок періоду (його показує підказка), стовпець зсунуто в середину періоду.
    bar = dict(x=starts, width=length_ms * BAR_WIDTH_SHARE,
               offset=length_ms * (1 - BAR_WIDTH_SHARE) / 2,
               marker=dict(color=MAIN_COLOR, cornerradius=3))
    fig = go.Figure([
        go.Bar(y=counts, yaxis="y",
               customdata=[plural(count, TENDER_FORMS) for count in counts],
               hovertemplate="%{customdata}<extra></extra>", **bar),
        go.Bar(y=amounts / divisor, yaxis="y2",
               customdata=[format_number(amount) for amount in amounts],
               hovertemplate="Очікувана вартість: %{customdata} грн<extra></extra>", **bar),
    ])

    middles = starts + pd.to_timedelta(length_ms / 2, unit="ms")
    _label_peak(fig, middles, counts, [format_number(count) for count in counts], "y")
    _label_peak(fig, middles, amounts / divisor,
                [format_amount(amount) for amount in amounts], "y2")

    subtitle = " · ".join(part for part in (
        f"{plural(counts.sum(), TENDER_FORMS)} на {format_amount(amounts.sum())}",
        " – ".join(dict.fromkeys(f"{day:%d.%m.%Y}" for day in (days.min(), days.max()))),
        PERIOD_NAMES.get(str(freq)[:1].upper(), ""),
    ) if part)
    apply_style(fig, PURCHASES_TITLE, subtitle, height=PURCHASES_HEIGHT)
    # Одна підказка одразу для обох графіків.
    fig.update_layout(hovermode="x unified", hoversubplots="axis",
                      margin_t=fig.layout.margin.t + 26)

    baseline = dict(zeroline=True, zerolinecolor=AXIS_LINE_COLOR, zerolinewidth=1)
    fig.update_layout(
        yaxis=dict(domain=COUNT_PANEL, range=[0, max(counts.max(), 1) * 1.2],
                   **count_axis(counts.max()), **baseline),
        yaxis2=dict(domain=AMOUNT_PANEL, anchor="x",
                    range=[0, max(amounts.max() / divisor, 1e-9) * 1.2], **baseline),
    )
    fig.update_yaxes(gridcolor=GRID_COLOR, automargin=True, exponentformat="none",
                     tickfont=dict(color=AXIS_TEXT_COLOR), nticks=5)
    for label, panel in (("Кількість тендерів", COUNT_PANEL),
                         (f"Очікувана вартість, {unit}", AMOUNT_PANEL)):
        fig.add_annotation(text=label, x=0, xref="paper", xanchor="left",
                           y=panel[1], yref="paper", yanchor="bottom", yshift=8,
                           showarrow=False, font=dict(color=TEXT_COLOR))

    # Відступ з боків; коли періодів мало, він більший, щоб стовпець не був на весь графік.
    padding = (pd.to_timedelta(length_ms.max(), unit="ms")
               * (0.5 if len(by_period) > FEW_PERIODS else 1.5))
    fig.update_xaxes(anchor="y2", showgrid=False, showline=False,
                     range=[starts[0] - padding, ends[-1] + padding],
                     hoverformat=PERIOD_HOVER_FORMATS.get(str(freq)[:1].upper(),
                                                          DEFAULT_HOVER_FORMAT))
    if (ends[-1] - starts[0]).days < SHORT_RANGE_DAYS:
        fig.update_xaxes(tickformat="%d.%m", nticks=MAX_DATE_TICKS)
    else:
        tickvals, ticktext = _month_ticks(starts[0], ends[-1])
        fig.update_xaxes(tickvals=tickvals, ticktext=ticktext)
    return fig
