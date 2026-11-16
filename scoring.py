"""Підсумковий бал підозрілості тендера (0–100) і агрегати для сайту.

Публічні функції (див. CONTRACT.md):
    score_tenders(tenders, items, awards=None) -> єдина таблиця, яку читає сайт
    overview_metrics(tenders, scores)          -> цифри для сторінки «Огляд»
    buyer_summary(tenders, scores, awards)     -> зведення по замовниках
"""

import logging
import math

import app
from flags import (FLAG_COLUMNS, PRICE_RATIO_THRESHOLD, REASON_SEPARATOR,
                   buyer_single_bid_stats, buyer_top_suppliers, compute_flags,
                   competitive_tenders, to_float)

logger = logging.getLogger(__name__)

# --- Ваги ---------------------------------------------------------------------

# Кожна складова від 0 до 1, сума ваг 100: тендер з усіма прапорцями на максимумі
# і ml_score = 1 отримує 100.
WEIGHTS = {
    "flag_price": 30,          # переплата: найпряміша ознака втрат
    "flag_single_bid": 20,
    "flag_repeat_winner": 20,
    "flag_short_period": 10,   # сам по собі часто законний мінімум, тому вага менша
    "ml_score": 20,
}

# Переплата важить більше, що вона більша: ×2 дає половину ваги, ×10 і більше — повну.
PRICE_MIN_STRENGTH = 0.5
PRICE_FULL_RATIO = 10.0
# Підозра на різну фасовку (штука = упаковка?) не важить більше за мінімальну переплату,
# інакше топ заповнять ліки, де просто порівняли ампулу з коробкою.
PRICE_NEEDS_CHECK_MAX_STRENGTH = 0.5
# Один учасник важить від половини до повної ваги залежно від того, як часто
# в цього замовника тендери проходять без конкуренції.
SINGLE_BID_MIN_STRENGTH = 0.5

# --- Агрегати -------------------------------------------------------------------

HIGH_SCORE_THRESHOLD = 40    # «високий бал»: ≈ 1% тендерів (171 з 17 981)
MIN_BUYER_TENDERS = 20       # менших замовників не ранжуємо: частки там випадкові


# --- Бал ------------------------------------------------------------------------

def price_strength(flags):
    """Сила прапорця переплати від 0 до 1 (логарифмічна шкала від порогу до ×10)."""
    ratio = flags["price_ratio"].fillna(0).clip(lower=PRICE_RATIO_THRESHOLD)
    progress = (ratio / PRICE_RATIO_THRESHOLD).map(math.log) / math.log(
        PRICE_FULL_RATIO / PRICE_RATIO_THRESHOLD)
    strength = (PRICE_MIN_STRENGTH + (1 - PRICE_MIN_STRENGTH) * progress).clip(upper=1)
    if "price_needs_check" in flags:
        capped = strength.clip(upper=PRICE_NEEDS_CHECK_MAX_STRENGTH)
        strength = strength.where(flags["price_needs_check"] == 0, capped)
    return strength * flags["flag_price"]


def single_bid_strength(flags):
    """Сила прапорця «один учасник»: більша, якщо в замовника це система."""
    share = flags.get("buyer_single_share", 0.0)
    return (SINGLE_BID_MIN_STRENGTH + (1 - SINGLE_BID_MIN_STRENGTH) * share) * flags["flag_single_bid"]


def score_components(flags):
    """Складові балу від 0 до 1 для кожного тендера (ключі як у WEIGHTS)."""
    return {
        "flag_price": price_strength(flags),
        "flag_single_bid": single_bid_strength(flags),
        "flag_repeat_winner": flags["flag_repeat_winner"],
        "flag_short_period": flags["flag_short_period"],
        "ml_score": flags["ml_score"],
    }


def combine_scores(flags, ml_scores=None):
    """Прапорці + ML -> таблиця app.SCORE_COLUMNS, відсортована за total_score.

    Якщо ML немає (None), бал рахується лише з прапорців і масштабується до 0–100.
    """
    scores = flags.copy()
    weights = dict(WEIGHTS)

    if ml_scores is None or ml_scores.empty:
        scores["ml_score"] = 0.0
        weights.pop("ml_score")
    else:
        ml_columns = [c for c in ("tender_id", "ml_score", "ml_reason") if c in ml_scores]
        scores = scores.merge(ml_scores[ml_columns], on="tender_id", how="left")
        scores["ml_score"] = scores["ml_score"].fillna(0.0).clip(0, 1)
        if "ml_reason" in scores:
            scores["reasons"] = _append_reason(scores["reasons"], scores["ml_reason"])

    components = score_components(scores)
    weighted_sum = sum(components[name] * weight for name, weight in weights.items())
    scores["total_score"] = (weighted_sum / sum(weights.values()) * 100).round(1).clip(0, 100)

    scores = scores.sort_values(["total_score", "price_ratio"], ascending=False,
                                na_position="last")
    return scores[list(app.SCORE_COLUMNS)].reset_index(drop=True)


def score_tenders(tenders, items, awards=None):
    """Єдина функція ядра для сайту: прапорці + ML + підсумковий бал.

    Вхід: tenders, items, awards з data_loader.load_data() (awards необов'язкові,
    але з ними точніше рахуються переможці багатолотових тендерів).
    Вихід: колонки рівно app.SCORE_COLUMNS, відсортовані за total_score спадно.
    """
    flags = compute_flags(tenders, items, awards)
    return combine_scores(flags, safe_anomaly_scores(tenders, flags))


def safe_anomaly_scores(tenders, flags):
    """ml.anomaly_scores(), але без падіння сайту: якщо ML зламався, повертає None.

    Імпорт усередині, щоб навіть помилка імпорту ml.py чи sklearn не зупинила демо.
    """
    try:
        import ml
        return ml.anomaly_scores(tenders, flags)
    except Exception:   # noqa: BLE001 — на демо краще бал без ML, ніж traceback
        logger.exception("ML недоступний, рахуємо бал лише з прапорців")
        return None


def _append_reason(reasons, extra):
    """Додає пояснення ML до тексту прапорців через «; »."""
    extra = extra.fillna("")
    joined = reasons.str.cat(extra, sep=REASON_SEPARATOR)
    return joined.where((reasons != "") & (extra != ""), reasons + extra)


# --- Агрегати для сторінок сайту ---------------------------------------------------

def overview_metrics(tenders, scores, high_score=HIGH_SCORE_THRESHOLD):
    """Головні цифри для сторінки «Огляд» (словник, готовий для st.metric)."""
    competitive = competitive_tenders(tenders)
    single = (to_float(competitive["number_of_bids"]) == 1).mean() if len(competitive) else 0.0
    high_ids = scores.loc[scores["total_score"] >= high_score, "tender_id"]
    high_value = to_float(tenders.loc[tenders["tender_id"].isin(high_ids), "final_value"]).sum()
    return {
        "tenders": len(tenders),
        "competitive_tenders": len(competitive),
        "single_bid_share": round(float(single), 3),
        "high_score_tenders": len(high_ids),
        "high_score_value_uah": round(float(high_value), 2),
        "flag_counts": {column: int(scores[column].sum()) for column in FLAG_COLUMNS},
    }


def buyer_summary(tenders, scores, awards=None, min_tenders=MIN_BUYER_TENDERS):
    """Зведення по замовниках для сторінки «Замовник» і рейтингу замовників.

    Колонки: buyer_edrpou, buyer_name, buyer_region, tenders_total, competitive_tenders,
    single_bid_share, top_supplier_name, top_supplier_wins, top_supplier_share,
    high_score_tenders, mean_score. Відсортовано за mean_score; замовники з
    competitive_tenders < min_tenders ідуть у кінець.
    """
    buyers = (tenders.groupby("buyer_edrpou")
                     .agg(buyer_name=("buyer_name", "first"),
                          buyer_region=("buyer_region", "first"),
                          tenders_total=("tender_id", "size")))
    single = buyer_single_bid_stats(tenders).rename(columns={"buyer_tenders": "competitive_tenders"})
    top = (buyer_top_suppliers(tenders, awards)
           .set_index("buyer_edrpou")[["top_supplier_name", "wins", "winner_share"]]
           .rename(columns={"wins": "top_supplier_wins", "winner_share": "top_supplier_share"}))

    with_buyer = scores.merge(tenders[["tender_id", "buyer_edrpou"]], on="tender_id")
    score_stats = with_buyer.groupby("buyer_edrpou")["total_score"].agg(
        high_score_tenders=lambda s: int((s >= HIGH_SCORE_THRESHOLD).sum()),
        mean_score="mean")

    summary = buyers.join([single, top, score_stats])
    summary["competitive_tenders"] = summary["competitive_tenders"].fillna(0).astype(int)
    summary["top_supplier_wins"] = summary["top_supplier_wins"].fillna(0).astype(int)
    summary["high_score_tenders"] = summary["high_score_tenders"].fillna(0).astype(int)
    for column in ("single_bid_share", "top_supplier_share", "mean_score"):
        summary[column] = summary[column].astype(float).round(3)

    summary["enough_data"] = summary["competitive_tenders"] >= min_tenders
    summary = summary.sort_values(["enough_data", "mean_score"], ascending=False)
    return summary.drop(columns="enough_data").reset_index()
