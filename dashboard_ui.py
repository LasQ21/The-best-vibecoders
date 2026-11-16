"""Streamlit interface for the Prozorro procurement dashboard."""

import logging
from html import escape

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app import PROZORRO_TENDER_URL
import dashboard_data as data

PAGES = ("Огляд", "Топ тендерів", "Замовники", "Ціни на товар")
PAGE_ICONS = (":material/space_dashboard:", ":material/radar:", ":material/apartment:", ":material/monitoring:")
FILTERS = {"buyer_region": "Регіон", "procedure": "Процедура", "category": "Категорія"}
FLAGS = {
    "flag_price": "Висока ціна за одиницю",
    "flag_single_bid": "Один учасник",
    "flag_repeat_winner": "Постійний переможець",
    "flag_short_period": "Короткий строк подачі",
}
SIGNALS = {
    "all": "Усі",
    "flag_price": "Ціна",
    "flag_single_bid": "Один учасник",
    "flag_repeat_winner": "Той самий переможець",
    "flag_short_period": "Короткий строк",
}
PROCEDURE_COLORS = ("#1d6b55", "#9dcb54", "#f1bc69", "#6d91bd")
LOGGER = logging.getLogger(__name__)


@st.cache_data(show_spinner="Завантажуємо дані Prozorro…")
def snapshot():
    return data.load_dashboard()


def text_value(value, fallback="—"):
    return str(value) if pd.notna(value) and str(value).strip() else fallback


def number(value, decimals=0):
    if pd.isna(value):
        return "—"
    return f"{value:,.{decimals}f}".replace(",", "\u202f").replace(".", ",")


def percent(value):
    return "—" if pd.isna(value) else f"{number(value * 100, 1)}%"


def money(value, currency="UAH"):
    if pd.isna(value):
        return "—"
    unit = {"UAH": "грн", "EUR": "EUR", "USD": "USD"}.get(currency, currency)
    decimals = 2 if abs(value) < 1000 else 0
    return f"{number(value, decimals)} {unit}"


def compact_money(value):
    if pd.isna(value):
        return "—"
    if abs(value) >= 1_000_000_000:
        return f"{number(value / 1_000_000_000, 2)} млрд ₴"
    if abs(value) >= 1_000_000:
        return f"{number(value / 1_000_000, 2)} млн ₴"
    return money(value)


def tender_link(code):
    if pd.notna(code) and str(code).startswith("UA-"):
        return PROZORRO_TENDER_URL.format(tender_code=code)
    return None


def change_page(page, tender_id=None, signal="all"):
    st.session_state["page"] = page
    st.session_state["price_tender"] = tender_id if page == "Ціни на товар" else None
    if page == "Топ тендерів":
        st.session_state["top_focus"] = tender_id
        st.session_state["top_signal"] = signal
        st.session_state["tender_search"] = ""
        st.session_state["minimum_score"] = 0
        for field in FILTERS:
            st.session_state[f"filter_{field}"] = []


def heading(label, title, description):
    st.html(
        f'<div class="page-heading"><div class="section-label"><i></i>{escape(label)}</div>'
        f'<h1>{escape(title)}</h1><p>{escape(description)}</p></div>'
    )


def cards(values):
    for index, (slot, (label, value, note)) in enumerate(zip(st.columns(len(values)), values)):
        with slot, st.container(key=f"pulse_metric_{index}"):
            st.metric(label, value)
            st.caption(note)


def update_data():
    try:
        updater = data.optional_module("updater")
        if updater is None:
            st.info("Модуль оновлення ще не підключено. Відображаються збережені дані.")
            return
        fetch_updates = getattr(updater, "fetch_updates", None)
        if fetch_updates is None:
            st.info("Читання фіду Prozorro підключено. Запис оновлень до CSV ще в розробці.")
            return
        with st.spinner("Перевіряємо оновлення Prozorro…"):
            result = fetch_updates()
        if not result.ok:
            st.warning(result.message or "Prozorro недоступний. Відображаються збережені дані.")
            return
        status = f"Додано {number(result.added)}, оновлено {number(result.updated)}."
        notice = f"{status} {result.message}".strip()
    except Exception:
        LOGGER.exception("Prozorro data update failed")
        st.warning("Не вдалося оновити дані. Відображаються збережені закупівлі.")
        return
    snapshot.clear()
    st.session_state["update_notice"] = notice
    st.rerun()


def sidebar(dataset):
    st.sidebar.html(
        '<div class="brand"><span class="brand-mark" aria-hidden="true"></span>'
        '<div><strong>Пульс закупівель</strong><small>Моніторинг Prozorro</small></div></div>'
    )
    for page, icon in zip(PAGES, PAGE_ICONS):
        st.sidebar.button(
            page, key=f"nav_{page}", icon=icon, width="stretch",
            type="primary" if page == st.session_state["page"] else "secondary",
            on_click=change_page, args=(page,),
        )

    st.sidebar.divider()
    source_label = {
        "model": "Прапорці та ML",
        "rules": "Оцінки за прапорцями",
        "sample": "Демонстраційні оцінки",
        "raw": "Локальні дані Prozorro",
    }
    st.sidebar.html(
        f'<div class="source-status {dataset.score_source}"><i></i>'
        f'{source_label[dataset.score_source]}</div>'
    )
    with st.sidebar:
        if st.button("Оновити з Prozorro", key="update", icon=":material/sync:", width="stretch"):
            update_data()
        notice = st.session_state.pop("update_notice", None)
        if notice:
            st.success(notice)
        st.toggle("Анімації", value=True, key="motion_enabled")


def tender_table(tenders, limit=100, selectable=False, focus=None):
    if tenders.empty:
        st.info("За цими умовами закупівель немає.")
        return None
    shown = tenders.head(limit)
    table = shown.loc[
        :, ["title", "buyer_name", "total_score", "reasons", "number_of_bids", "tender_code"]
    ].copy()
    table["tender_code"] = table["tender_code"].map(tender_link)
    has_scores = table["total_score"].notna().any()
    if not has_scores:
        table = table.drop(columns=["total_score", "reasons"])
    columns = {
        "title": st.column_config.TextColumn("Закупівля", width="large"),
        "buyer_name": st.column_config.TextColumn("Замовник", width="medium"),
        "number_of_bids": st.column_config.NumberColumn("Пропозиції", format="%d", width="small"),
        "tender_code": st.column_config.LinkColumn("Джерело", display_text="Prozorro", width="small"),
    }
    if has_scores:
        columns["total_score"] = st.column_config.ProgressColumn("Ризик", min_value=0, max_value=100, format="%.1f")
        columns["reasons"] = st.column_config.TextColumn("Причини", width="large")
    selected = None
    if selectable:
        order = ["title", "total_score", "buyer_name", "number_of_bids", "reasons", "tender_code"]
        ids = shown["tender_id"].tolist()
        default = {"selection": {"rows": [ids.index(focus)]}} if focus in ids else None
        event = st.dataframe(
            table, width="stretch", height=460, hide_index=True, column_config=columns,
            column_order=[field for field in order if field in table],
            row_height=46, key=f"rank_{hash(tuple(ids))}_{focus}", on_select="rerun",
            selection_mode="single-row", selection_default=default,
        )
        selected_index = event.selection.rows[0] if event.selection.rows else None
        if selected_index is None or not 0 <= selected_index < len(shown):
            selected_index = ids.index(focus) if focus in ids else 0
        selected = shown.iloc[selected_index]
    else:
        st.dataframe(table, width="stretch", hide_index=True, column_config=columns)
    if len(tenders) > limit:
        st.caption(f"Показано {number(limit)} із {number(len(tenders))} закупівель. Звузь фільтри для точнішого пошуку.")
    return selected


def chart(module_function, args, records, spec):
    try:
        if module_function:
            build = getattr(data.optional_module("charts"), module_function, None)
            if build:
                st.plotly_chart(build(*args), width="stretch", config={"displayModeBar": False})
                return
    except Exception:
        LOGGER.exception("Team chart %s failed", module_function)

    style = {
        "background": "transparent",
        "view": {"stroke": None},
        "axis": {"gridColor": "#e9eef5", "domain": False, "labelColor": "#466179"},
        "font": "Source Sans 3, sans-serif",
    }
    st.vega_lite_chart(
        {"data": {"values": records}, "config": style, **spec},
        width="stretch", theme=None,
    )


def bars(counts, buyer_tenders=None):
    if counts.empty:
        st.info("Даних для графіка поки немає.")
        return
    chart("top_winners" if buyer_tenders is not None else None, (buyer_tenders,), counts.to_dict("records"), {
        "height": max(128, len(counts) * 33),
        "mark": {"type": "bar", "cornerRadiusEnd": 5, "color": "#2b78dd", "size": 17},
        "encoding": {
            "x": {"field": "count", "type": "quantitative", "title": None, "axis": {"tickMinStep": 1}},
            "y": {"field": "name", "type": "nominal", "sort": "-x", "title": None, "axis": {"labelLimit": 175}},
            "tooltip": [{"field": "name", "title": "Назва"}, {"field": "count", "title": "Закупівлі"}],
        },
    })


def show_overview(dataset):
    tenders = dataset.tenders
    metrics = data.overview_metrics(tenders, dataset.high_threshold)
    start, end = metrics["first_date"], metrics["last_date"]
    period = f"Дати оголошень · {start:%d.%m.%Y} — {end:%d.%m.%Y}" if pd.notna(start) and pd.notna(end) else ""
    with st.container(key="pulse_hero"):
        st.html(
            '<div class="hero"><div class="hero-copy">'
            '<div class="hero-label"><i></i> ВІДКРИТІ ДАНІ PROZORRO</div>'
            '<h1>Пульс<br><em>закупівель.</em></h1>'
            '<p>Знаходь цивільні закупівлі з ознаками ризику й перевіряй дані першоджерела.</p>'
            '</div><div class="hero-art" aria-hidden="true"><div class="radar-orbit"></div><div class="radar-screen">'
            '<span></span><span></span><span></span><b></b><i></i><em></em><u></u>'
            '</div></div></div>'
        )
        with st.container(key="pulse_hero_actions", horizontal=True, gap="small"):
            st.button(
                "Дослідити тендери", icon=":material/arrow_forward:", type="primary",
                wrap=True,
                on_click=change_page, args=("Топ тендерів",), key="hero_top",
            )
            st.button(
                "Знайти замовника", icon=":material/search:", wrap=True,
                on_click=change_page, args=("Замовники",), key="hero_buyer",
            )
        if period:
            st.html(f'<div class="hero-period">{escape(period)}</div>')
    high_note = f"Від {dataset.high_threshold} балів"
    cards([
        ("Закупівлі", number(metrics["count"]), "Цивільні закупівлі у вибірці"),
        ("Одна пропозиція", percent(metrics["single_bid_share"]), "Серед конкурентних процедур"),
        ("Високий ризик", number(metrics["high_risk"]), high_note),
        ("Сума високого ризику", compact_money(metrics["high_value"]), "Фінальна вартість у гривнях"),
    ])
    left, right = st.columns([1.65, 1], gap="medium")
    with left, st.container(key="pulse_discover"):
        st.subheader("Першими до перевірки" if dataset.score_source != "raw" else "Закупівлі з однією пропозицією")
        signal = "all"
        if dataset.score_source != "raw":
            signal = st.segmented_control(
                "Сигнали ризику", list(SIGNALS), default="all", required=True,
                format_func=SIGNALS.get, key="overview_signal", width="stretch", wrap=True,
            )
        ranked = data.ranked_tenders(data.matching_tenders(tenders, signal=signal), dataset.score_source)
        st.caption(f"{number(len(ranked))} закупівель у добірці")
        for row in ranked.head(3).itertuples(index=False):
            score = f"{number(row.total_score, 1)} / 100" if pd.notna(row.total_score) else "1 пропозиція"
            with st.container(key=f"pulse_finding_{row.tender_id}"):
                st.html(
                    f'<div class="finding-meta"><span>{escape(text_value(row.buyer_region))}</span>'
                    f'<strong>{escape(score)}</strong></div>'
                )
                st.button(
                    text_value(row.title), icon=":material/arrow_outward:", key=f"open_{row.tender_id}",
                    width="stretch", wrap=True, on_click=change_page,
                    args=("Топ тендерів", row.tender_id, signal),
                )
        if ranked.empty:
            st.info("У вибірці поки немає записів для цього списку.")
        st.button(
            "Відкрити добірку", icon=":material/arrow_forward:", width="stretch",
            on_click=change_page, args=("Топ тендерів", None, signal), key="open_top",
        )
    with right, st.container(key="pulse_procedures"):
        st.subheader("Типи процедур")
        procedure_chart(data.procedure_counts(tenders))
    st.caption("Бал ризику допомагає визначити, які закупівлі перевірити вручну.")


def procedure_chart(counts):
    if counts.empty:
        st.info("Даних для графіка поки немає.")
        return
    colors = [PROCEDURE_COLORS[index % len(PROCEDURE_COLORS)] for index in range(len(counts))]
    figure = go.Figure(go.Pie(
        labels=counts["name"], values=counts["count"], hole=0.73, sort=False,
        rotation=34, marker={"colors": colors, "line": {"color": "#ffffff", "width": 4}},
        textinfo="none", hovertemplate="%{label}<br>%{value:,} закупівель · %{percent}<extra></extra>",
    ))
    figure.add_annotation(
        x=0.5, y=0.5, text=f"<b>{number(counts['count'].sum())}</b>",
        showarrow=False, font={"family": "Source Sans 3, sans-serif", "size": 32, "color": "#152922"},
    )
    figure.update_layout(
        margin={"t": 6, "r": 10, "b": 6, "l": 10}, height=237, showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
        hoverlabel={"bgcolor": "#10281f", "font_color": "#ffffff"},
    )
    st.plotly_chart(figure, width="stretch", theme=None, config={"displayModeBar": False})
    for row, color in zip(counts.itertuples(index=False), colors):
        st.html(
            f'<div class="procedure-line"><i style="--procedure-color:{color}"></i>'
            f'<span>{escape(row.name)}</span><strong>{number(row.count)}</strong></div>'
        )


def show_top(dataset):
    heading("АНАЛІЗ РИЗИКУ", "Топ тендерів", "Знайди закупівлю та відкрий рядок, щоб побачити причини її оцінки.")
    with st.container(key="pulse_filters"):
        query = st.text_input(
            "Пошук тендера", placeholder="Назва закупівлі, замовник або номер UA-…",
            icon=":material/search:", key="tender_search",
        ).strip()
        signal = "all"
        if dataset.score_source != "raw":
            st.session_state.setdefault("top_signal", "all")
            signal = st.segmented_control(
                "Сигнали ризику", list(SIGNALS), required=True, format_func=SIGNALS.get,
                key="top_signal", width="stretch", wrap=True,
            )
        slots = st.columns(4)
        chosen = {}
        for slot, (field, label) in zip(slots, FILTERS.items()):
            with slot:
                chosen[field] = st.multiselect(
                    label, data.filter_options(dataset.tenders, field),
                    placeholder="Усі", key=f"filter_{field}",
                )
        with slots[-1]:
            minimum = st.slider(
                "Мінімальний бал", 0, 100, step=1,
                disabled=dataset.score_source == "raw", key="minimum_score",
            )
    if dataset.score_source == "raw":
        st.info("Бали ризику ще недоступні. Показуємо конкурентні закупівлі з однією пропозицією.")
    subset = data.filter_tenders(dataset.tenders, chosen, None if dataset.score_source == "raw" else minimum)
    subset = data.matching_tenders(subset, query, signal)
    ranked = data.ranked_tenders(subset, dataset.score_source)
    results, details = st.columns([1.65, 1], gap="medium")
    with results, st.container(key="pulse_results"):
        st.subheader(f"Результати · {number(len(ranked))}")
        focused = st.session_state.get("top_focus")
        tender = tender_table(ranked, selectable=True, focus=focused)
    with details:
        if tender is not None:
            tender_detail(tender)


def tender_detail(tender):
    tender_id = tender["tender_id"]
    with st.container(key=f"pulse_detail_{tender_id}"):
        st.caption(text_value(tender["tender_code"]))
        st.subheader(text_value(tender["title"]))
        st.html(f'<p class="detail-buyer">{escape(text_value(tender["buyer_name"]))}</p>')
        score = tender["total_score"]
        if pd.notna(score):
            score_width = max(0.0, min(100.0, float(score)))
            st.html(
                '<div class="detail-score"><span>Бал ризику</span>'
                f'<strong>{number(score, 1)}<small> / 100</small></strong>'
                f'<div class="detail-meter" aria-hidden="true"><i style="width:{score_width:.1f}%"></i></div></div>'
            )
        facts = []
        if pd.notna(tender["number_of_bids"]):
            facts.append(f"Подано пропозицій: {number(tender['number_of_bids'])}")
        if pd.notna(tender["final_value"]):
            facts.append(f"Фінальна вартість: {money(tender['final_value'], text_value(tender['currency'], 'UAH'))}")
        if facts:
            st.caption(" · ".join(facts))
        if pd.notna(tender["reasons"]) and str(tender["reasons"]).strip():
            reasons = "".join(f"<li>{escape(reason)}</li>" for reason in tender["reasons"].split("; ") if reason)
            st.html(f'<ul class="tender-reasons">{reasons}</ul>')
        elif pd.isna(tender["total_score"]):
            st.text("У конкурентній закупівлі зафіксована одна пропозиція.")
        active = [label for field, label in FLAGS.items() if text_value(tender[field]) in {"1", "1.0", "True", "true"}]
        if active:
            st.caption(" · ".join(active))
        if link := tender_link(tender["tender_code"]):
            st.link_button("Відкрити на Prozorro", link, icon=":material/open_in_new:", width="stretch")
        st.button(
            "Порівняти ціни тендера", icon=":material/monitoring:", key="open_tender_prices", width="stretch",
            on_click=change_page, args=("Ціни на товар", tender_id),
        )


def search_examples(title, key, examples):
    st.caption(title)
    with st.container(horizontal=True, key=f"pulse_examples_{key}"):
        for label, value in examples:
            st.button(
                label, icon=":material/arrow_outward:", key=f"{key}_{value}",
                on_click=st.session_state.__setitem__, args=(key, value),
            )


def show_buyers(dataset):
    heading("ПРОФІЛІ", "Замовники", "Знайди організацію та переглянь її закупівлі й постачальників.")
    query = st.text_input(
        "Назва або ЄДРПОУ", placeholder="Наприклад, 01999265",
        icon=":material/search:", key="buyer_search",
    ).strip()
    if not query:
        search_examples("Замовники у вибірці", "buyer_search", [
            ("Диканська лікарня", "01999265"),
            ("Харківський ДЖКГ", "43927048"),
        ])
        return
    matched = data.matching_buyers(dataset.tenders, query)
    if matched.empty:
        st.info("Замовників за цим запитом немає.")
        return
    if len(matched) > 100:
        st.caption("Показано перші 100 замовників. Уточни запит, щоб звузити список.")
    buyers = matched.head(100).set_index("buyer_edrpou")["buyer_name"]
    code = st.selectbox(
        "Замовник", buyers.index.tolist(),
        format_func=lambda key: f"{text_value(buyers.at[key], 'Назва відсутня')} ({key})",
        key="buyer_choice",
    )
    tenders = dataset.tenders.loc[dataset.tenders["buyer_edrpou"].eq(code)]
    metrics = data.overview_metrics(tenders, dataset.high_threshold)
    with st.container(key=f"pulse_buyer_{code}"):
        st.subheader(text_value(buyers.at[code], code))
        cards([
            ("Закупівлі", number(metrics["count"]), "У вибірці цього замовника"),
            ("Одна пропозиція", percent(metrics["single_bid_share"]), "Серед конкурентних процедур"),
            ("Високий ризик", number(metrics["high_risk"]), f"Від {dataset.high_threshold} балів"),
        ])
    left, right = st.columns(2, gap="medium")
    with left, st.container(border=True):
        st.subheader("Найчастіші переможці")
        bars(data.supplier_counts(tenders, dataset.awards), tenders)
    with right, st.container(border=True):
        st.subheader("Закупівлі за місяцями")
        months = data.procurement_months(tenders)
        if months.empty:
            st.info("У цій вибірці немає дат закупівель.")
        else:
            chart("purchases_over_time", (tenders, "MS"), months.to_dict("records"), {
                "height": 214,
                "mark": {"type": "line", "color": "#087b8a", "point": {"size": 65}, "strokeWidth": 3},
                "encoding": {
                    "x": {"field": "month", "type": "ordinal", "title": None, "axis": {"labelAngle": -30}},
                    "y": {"field": "count", "type": "quantitative", "title": "Закупівлі", "axis": {"tickMinStep": 1}},
                    "tooltip": [{"field": "month", "title": "Місяць"}, {"field": "count", "title": "Закупівлі"}],
                },
            })
    st.subheader("Ризикові тендери" if dataset.score_source != "raw" else "Торги з однією пропозицією")
    highlighted = data.ranked_tenders(tenders, dataset.score_source)
    if dataset.score_source != "raw":
        highlighted = highlighted.loc[highlighted["total_score"].ge(dataset.high_threshold)]
    tender_table(highlighted, limit=50)


def price_chart(prices, median, selected, currency):
    chart("price_histogram", (prices, median, selected, f"Ціни за одиницю · {currency}"),
          [{"price": float(price)} for price in prices], {
        "height": 292,
        "layer": [
            {
                "mark": {"type": "bar", "color": "#9dcbfc", "cornerRadiusEnd": 4},
                "encoding": {
                    "x": {"field": "price", "type": "quantitative", "bin": {"maxbins": 27}, "title": f"Ціна за одиницю, {currency}"},
                    "y": {"aggregate": "count", "type": "quantitative", "title": "Позиції"},
                    "tooltip": [{"field": "price", "bin": True, "title": "Діапазон"},
                                {"aggregate": "count", "title": "Позиції"}],
                },
            },
            {
                "data": {"values": [{"price": float(median), "series": "Медіана"},
                                    {"price": float(selected), "series": "Вибрана позиція"}]},
                "mark": {"type": "rule", "strokeWidth": 3},
                "encoding": {
                    "x": {"field": "price", "type": "quantitative"},
                    "color": {"field": "series", "title": None,
                              "scale": {"domain": ["Медіана", "Вибрана позиція"], "range": ["#087b8a", "#c3452d"]}},
                    "tooltip": [{"field": "series", "title": "Позначка"},
                                {"field": "price", "type": "quantitative", "title": "Ціна"}],
                },
            },
        ],
    })


def show_prices(dataset):
    heading("ПОРІВНЯННЯ", "Ціни на товар", "Зістав ціну за одиницю з іншими позиціями того самого профілю або CPV-коду.")
    items = dataset.price_items
    tender_id = st.session_state.get("price_tender")
    if tender_id:
        tenders = dataset.tenders.loc[dataset.tenders["tender_id"].eq(tender_id)]
        if tenders.empty:
            st.info("Закупівлю не знайдено в поточній вибірці.")
            st.button("Шукати серед усіх товарів", key="reset_price_tender", on_click=change_page, args=("Ціни на товар",))
            return
        tender = tenders.iloc[0]
        with st.container(border=True):
            st.caption(f"Вибраний тендер · {text_value(tender['tender_code'])}")
            st.write(text_value(tender["title"]))
            st.button("Шукати серед усіх товарів", key="clear_price_tender", on_click=change_page, args=("Ціни на товар",))
        matched = data.matching_items(items, tender_id=tender_id)
    else:
        query = st.text_input(
            "Назва товару, код CPV або ID профілю", placeholder="Наприклад, папір",
            icon=":material/search:", key="item_search",
        ).strip()
        if not query:
            search_examples("Товари у вибірці", "item_search", [
                ("Папір", "папір"),
                ("Електрична енергія", "електрична енергія"),
            ])
            return
        matched = data.matching_items(items, query)
    if matched.empty:
        st.info("Порівнюваних позицій із відомою ціною за одиницю не знайдено.")
        return
    if len(matched) > 150:
        st.caption("Показано перші 150 позицій. Уточни пошук за назвою або кодом.")
    choices = matched.head(150).set_index(["tender_id", "item_id"])
    tender_codes = dataset.tenders.set_index("tender_id")["tender_code"]

    def item_label(key):
        description = text_value(choices.at[key, "description"])[:115]
        cost = money(choices.at[key, "unit_price"], choices.at[key, "currency"])
        code = text_value(tender_codes.get(key[0]), key[0])
        return f"{description} · {cost} · {code} · {key[1][:8]}"

    item_key = st.selectbox(
        "Позиція для порівняння", choices.index.tolist(),
        format_func=item_label, key="price_item",
    )
    selected = choices.loc[item_key]
    methods = [key for key, field in (("profile", "profile_id"), ("cpv", "cpv_id")) if pd.notna(selected[field])]
    if not methods:
        st.info("Позиція не має профілю або CPV-коду для порівняння.")
        return
    labels = {"profile": "Профіль товару", "cpv": "CPV-код"}
    price_basis = text_value(selected["price_basis"])
    method = st.segmented_control(
        "Спосіб порівняння", methods, default=price_basis if price_basis in methods else methods[0],
        required=True, format_func=labels.get, key=f"price_method_{item_key[0]}_{item_key[1]}",
    )
    prices, median, ratio = data.price_comparison(items, selected, method)
    currency = selected["currency"]
    cards([
        ("Вибрана ціна", money(selected["unit_price"], currency), f"За одиницю · {text_value(selected['unit_name'])}"),
        ("Медіана", money(median, currency), f"У групі з {number(len(prices))} позицій"),
        ("До медіани", f"×{number(ratio, 2)}" if pd.notna(ratio) else "—", "Вибрана ціна, поділена на медіану"),
    ])
    with st.container(border=True):
        st.subheader("Розподіл цін за одиницю")
        if len(prices) < 2:
            st.info("У групі лише одна позиція. Для розподілу потрібна принаймні ще одна.")
        else:
            price_chart(prices, median, selected["unit_price"], currency)
        group_id = selected["profile_id"] if method == "profile" else selected["cpv_id"]
        if method == "cpv":
            st.caption(f"CPV {group_id} дає ширше порівняння і може містити різні товари.")
        if method != price_basis:
            st.caption("Довідкове порівняння. Для бала ризику ядро використовує розрахунок за профілем товару.")
        if len(prices) < 20:
            st.caption("Вибірка має менше 20 позицій. Медіана наведена як орієнтир.")


def main():
    st.set_page_config(page_title="Пульс закупівель", layout="wide", initial_sidebar_state="expanded")
    st.html(data.ROOT / "dashboard.css")
    st.session_state.setdefault("page", "Огляд")
    try:
        dataset = snapshot()
    except Exception:
        LOGGER.exception("Could not load project data")
        st.error("Не вдалося завантажити дані. Перевірте CSV у папці data та модуль data_loader.")
        return
    sidebar(dataset)
    motion = "on" if st.session_state.get("motion_enabled", True) else "off"
    st.html(f'<span class="pulse-motion-state" data-motion="{motion}" aria-hidden="true"></span>')
    if dataset.score_source == "sample":
        st.info("Навчальні бали з sample_scores.csv. Реальні закупівлі відображаються з локальних даних Prozorro.")
    elif dataset.scoring_error:
        st.warning("Модуль оцінювання зараз недоступний. Показуємо фактичні дані Prozorro.")
    pages = dict(zip(PAGES, (show_overview, show_top, show_buyers, show_prices), strict=True))
    page = st.session_state["page"]
    with st.container(key=f"pulse_page_{PAGES.index(page)}"):
        pages[page](dataset)


if __name__ == "__main__":
    main()
