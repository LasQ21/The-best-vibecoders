"""ML-оцінка аномальності тендерів.

IsolationForest дивиться на всі ознаки тендера разом і знаходить ті, що
«не схожі на інші», навіть якщо жоден окремий прапорець не спрацював.

Головна функція: anomaly_scores(tenders, flags) -> tender_id, ml_score, ml_reason.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from flags import (COMPETITIVE_METHODS, REASON_SEPARATOR, REPEAT_WINNER_MIN_TENDERS,
                   format_number)

# --- Модель -----------------------------------------------------------------

RANDOM_STATE = 42     # фіксуємо, щоб бал не стрибав між запусками й на демо
N_ESTIMATORS = 200    # кількість дерев: більше = стабільніше, 200 рахується за секунди
MIN_TENDERS_FOR_MODEL = 50   # на меншій вибірці «аномальність» нічого не означає

# --- Пояснення ML -----------------------------------------------------------

ML_REASON_MIN_SCORE = 0.5   # пояснюємо лише топові тендери (≈ 10% найаномальніших)
ML_REASON_MIN_Z = 2.0       # ознака «незвична», якщо відхиляється на ≥ 2 стандартних відхилення
ML_REASON_MAX_FEATURES = 2  # більше двох причин уже не читається

# Як назвати відхилення ознаки: (якщо значення вище типового, якщо нижче).
# None означає, що відхилення в цей бік нічого не каже.
FEATURE_PHRASES = {
    "log_expected_value": ("незвично велика очікувана сума", "незвично мала очікувана сума"),
    "price_drop_pct": ("незвично велике зниження ціни", "ціну майже не знизили"),
    "number_of_bids": (None, None),   # багато учасників — це здорова конкуренція, а не підозра
    "submission_days": ("незвично довгий строк подачі", "незвично короткий строк подачі"),
    "winner_share": ("переможець дуже часто виграє в цього замовника", None),
    "log_n_items": ("незвично багато позицій", None),
}

# --- Ознаки -----------------------------------------------------------------

# Числові ознаки тендера (назва колонки в таблиці ознак).
NUMERIC_FEATURES = [
    "log_expected_value",   # сума сильно скошена, тому беремо логарифм
    "price_drop_pct",       # наскільки переможець знизив ціну від очікуваної
    "number_of_bids",
    "submission_days",
    "winner_share",         # частка найчастішого переможця в замовника (з flags.py)
    "log_n_items",          # кількість позицій теж скошена (медіана 1, максимум >1000)
]

# Категорійні ознаки, які кодуємо one-hot.
CATEGORICAL_FEATURES = ["method_type", "main_category"]


def build_features(tenders, flags):
    """Таблиця ознак для ML, по рядку на конкурентний тендер.

    Вхід: tenders з data_loader.load_data(), flags з flags.compute_flags()
    (потрібна колонка winner_share; якщо її немає, ознака дорівнює 0;
    для замовників з < REPEAT_WINNER_MIN_TENDERS тендерів вона замінюється медіаною).
    Вихід: DataFrame з індексом tender_id. Числові ознаки без пропусків
    (пропуски заповнено медіаною колонки), далі one-hot колонки method_type_*
    і main_category_*. Масштабування робить модель, а не ця функція.
    """
    competitive = tenders[tenders["method_type"].isin(COMPETITIVE_METHODS)]
    competitive = competitive.drop_duplicates("tender_id").set_index("tender_id")

    features = pd.DataFrame(index=competitive.index)
    expected_value = pd.to_numeric(competitive["expected_value"], errors="coerce")
    features["log_expected_value"] = np.log1p(expected_value.clip(lower=0))
    for column in ("price_drop_pct", "number_of_bids", "submission_days"):
        features[column] = pd.to_numeric(competitive[column], errors="coerce")
    n_items = pd.to_numeric(competitive["n_items"], errors="coerce")
    features["log_n_items"] = np.log1p(n_items.clip(lower=0))

    if "winner_share" in flags:
        winner_share = flags.drop_duplicates("tender_id").set_index("tender_id")["winner_share"]
        features["winner_share"] = winner_share.reindex(features.index)
        # У замовника з 1–2 тендерами частка 100% нічого не означає: беремо медіану.
        buyer_tenders = competitive.groupby("buyer_edrpou")["buyer_edrpou"].transform("size")
        features.loc[buyer_tenders < REPEAT_WINNER_MIN_TENDERS, "winner_share"] = np.nan
    else:
        features["winner_share"] = 0.0

    features = features[NUMERIC_FEATURES].astype(float)
    features = features.fillna(features.median()).fillna(0.0)

    dummies = pd.get_dummies(competitive[CATEGORICAL_FEATURES].fillna("unknown"),
                             prefix=CATEGORICAL_FEATURES, dtype=float)
    return features.join(dummies)


# --- Оцінка аномальності -------------------------------------------------------

def fit_model(features):
    """Навчає StandardScaler + IsolationForest на таблиці ознак.

    StandardScaler зводить ознаки до одного масштабу (середнє 0, відхилення 1),
    щоб сума в мільйонах не важила більше за кількість учасників.
    """
    model = make_pipeline(
        StandardScaler(),
        IsolationForest(n_estimators=N_ESTIMATORS, random_state=RANDOM_STATE),
    )
    return model.fit(features)


def to_unit_scale(raw_scores):
    """Сирі score_samples -> ml_score від 0 до 1, де 1 означає найаномальніший.

    У score_samples менше значення = аномальніше, тому перевертаємо знак
    і лінійно розтягуємо на [0, 1].
    """
    anomaly = -np.asarray(raw_scores, dtype=float)
    span = anomaly.max() - anomaly.min()
    if span == 0:
        return np.zeros_like(anomaly)
    return (anomaly - anomaly.min()) / span


def anomaly_scores(tenders, flags):
    """ML-оцінка для кожного тендера.

    Вхід: tenders з data_loader.load_data(), flags з flags.compute_flags().
    Вихід: tender_id, ml_score (0–1), ml_reason (пояснення для топових, інакше "").
    Модель вчиться лише на конкурентних тендерах; для reporting ml_score = 0.
    """
    result = tenders[["tender_id"]].drop_duplicates().set_index("tender_id")
    result["ml_score"] = 0.0
    result["ml_reason"] = ""

    features = build_features(tenders, flags)
    if len(features) >= MIN_TENDERS_FOR_MODEL:
        model = fit_model(features)
        result.loc[features.index, "ml_score"] = to_unit_scale(model.score_samples(features))
        method_type = tenders.drop_duplicates("tender_id").set_index("tender_id")["method_type"]
        reasons = explain_anomalies(features, method_type.reindex(features.index),
                                    result.loc[features.index, "ml_score"])
        result.loc[reasons.index, "ml_reason"] = reasons

    result["ml_score"] = result["ml_score"].round(3)
    return result.reset_index()


# --- Пояснення ---------------------------------------------------------------

def group_z_scores(features, groups):
    """z-score кожної числової ознаки в межах групи (типу процедури).

    z = (значення - середнє групи) / стандартне відхилення групи.
    Порівнюємо в межах типу, бо строки й суми в priceQuotation і aboveThreshold різні.
    """
    numeric = features[NUMERIC_FEATURES]
    grouped = numeric.groupby(groups)
    std = grouped.transform("std").replace(0, np.nan)
    return ((numeric - grouped.transform("mean")) / std).fillna(0.0)


def format_feature_value(feature, value):
    """Значення ознаки так, як його зрозуміє людина (логарифми розгортаємо назад)."""
    if feature == "log_expected_value":
        amount = np.expm1(value)
        if amount >= 1_000_000:
            return f"{format_number(amount / 1_000_000)} млн грн"
        return f"{format_number(amount / 1_000)} тис. грн"
    if feature == "log_n_items":
        return f"{round(np.expm1(value))} поз."
    if feature == "price_drop_pct":
        return f"{format_number(value)}%"
    if feature == "submission_days":
        return f"{format_number(value)} дн."
    if feature == "winner_share":
        return f"{value:.0%}"
    return format_number(value, 0)


def explain_one(z_row, values, typical, method_type):
    """Текст пояснення для одного тендера з 1–2 найбільш незвичних ознак."""
    parts = []
    for feature in z_row.abs().sort_values(ascending=False).index:
        z = z_row[feature]
        if abs(z) < ML_REASON_MIN_Z or len(parts) == ML_REASON_MAX_FEATURES:
            break
        phrase = FEATURE_PHRASES[feature][0 if z > 0 else 1]
        if phrase is None:
            continue
        parts.append(f"{phrase} ({format_feature_value(feature, values[feature])} "
                     f"проти типових {format_feature_value(feature, typical[feature])} "
                     f"для {method_type})")
    return "ML: " + REASON_SEPARATOR.join(parts) if parts else ""


def explain_anomalies(features, method_type, ml_score):
    """Пояснення ML для тендерів з ml_score ≥ ML_REASON_MIN_SCORE.

    Повертає Series з індексом tender_id (лише для пояснених тендерів).
    Типове значення — медіана ознаки в межах того самого типу процедури.
    """
    z_scores = group_z_scores(features, method_type)
    typical = features[NUMERIC_FEATURES].groupby(method_type).median()
    top = ml_score[ml_score >= ML_REASON_MIN_SCORE].index
    reasons = {
        tender_id: explain_one(z_scores.loc[tender_id], features.loc[tender_id],
                               typical.loc[method_type[tender_id]], method_type[tender_id])
        for tender_id in top
    }
    return pd.Series(reasons, dtype=object)
