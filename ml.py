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

from flags import COMPETITIVE_METHODS

# --- Модель -----------------------------------------------------------------

RANDOM_STATE = 42     # фіксуємо, щоб бал не стрибав між запусками й на демо
N_ESTIMATORS = 200    # кількість дерев: більше = стабільніше, 200 рахується за секунди
MIN_TENDERS_FOR_MODEL = 50   # на меншій вибірці «аномальність» нічого не означає

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
    (потрібна колонка winner_share; якщо її немає, ознака дорівнює 0).
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
    Вихід: tender_id, ml_score (0–1), ml_reason (поки порожній).
    Модель вчиться лише на конкурентних тендерах; для reporting ml_score = 0.
    """
    result = tenders[["tender_id"]].drop_duplicates().set_index("tender_id")
    result["ml_score"] = 0.0
    result["ml_reason"] = ""

    features = build_features(tenders, flags)
    if len(features) >= MIN_TENDERS_FOR_MODEL:
        model = fit_model(features)
        result.loc[features.index, "ml_score"] = to_unit_scale(model.score_samples(features))

    result["ml_score"] = result["ml_score"].round(3)
    return result.reset_index()
