"""ML-оцінка аномальності тендерів.

IsolationForest дивиться на всі ознаки тендера разом і знаходить ті, що
«не схожі на інші», навіть якщо жоден окремий прапорець не спрацював.

Головна функція: anomaly_scores(tenders, flags) -> tender_id, ml_score, ml_reason.
"""

import numpy as np
import pandas as pd

from flags import COMPETITIVE_METHODS

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
