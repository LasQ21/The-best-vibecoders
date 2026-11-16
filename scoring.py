"""Підсумковий бал підозрілості тендера (0–100).

score_tenders(flags, ml_scores=None) об'єднує прапорці з flags.compute_flags()
і оцінку аномальності з ml.anomaly_scores() в одну таблицю для сайту.
"""

from flags import (FLAG_COLUMNS, REASON_SEPARATOR, buyer_single_bid_stats,
                   buyer_top_suppliers)

# Ваги складових. Сума = 100, тож тендер з усіма прапорцями і ml_score = 1 отримує 100.
WEIGHTS = {
    "flag_price": 30,          # переплата: найпряміша ознака втрат
    "flag_single_bid": 20,
    "flag_repeat_winner": 20,
    "flag_short_period": 10,   # сам по собі часто законний мінімум, тому вага менша
    "ml_score": 20,
}

# З якого балу тендер вважаємо «з високим балом» на сторінці «Огляд».
HIGH_SCORE_THRESHOLD = 50

# Замовників з меншою кількістю конкурентних тендерів не ранжуємо: частки там випадкові.
MIN_BUYER_TENDERS = 20

OUTPUT_COLUMNS = ["tender_id", *FLAG_COLUMNS, "price_ratio", "ml_score",
                  "total_score", "reasons"]


def score_tenders(flags, ml_scores=None):
    """Таблиця для сайту: по рядку на тендер, відсортована за total_score.

    flags: результат flags.compute_flags().
    ml_scores: результат ml.anomaly_scores() з колонками tender_id, ml_score (0–1)
        і за бажанням ml_reason. Якщо ML ще не готовий (None), бал рахується лише
        з прапорців і масштабується до тих самих 0–100.
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

    weighted_sum = sum(scores[column] * weight for column, weight in weights.items())
    scores["total_score"] = (weighted_sum / sum(weights.values()) * 100).round(1)

    scores = scores.sort_values(["total_score", "price_ratio"], ascending=False,
                                na_position="last")
    return scores[OUTPUT_COLUMNS].reset_index(drop=True)


def _append_reason(reasons, extra):
    """Додає пояснення ML до тексту прапорців через «; »."""
    extra = extra.fillna("")
    joined = reasons.str.cat(extra, sep=REASON_SEPARATOR)
    return joined.where((reasons != "") & (extra != ""), reasons + extra)


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
    for column in ("single_bid_share", "top_supplier_share", "mean_score"):
        summary[column] = summary[column].round(3)

    summary["enough_data"] = summary["competitive_tenders"] >= min_tenders
    summary = summary.sort_values(["enough_data", "mean_score"], ascending=False)
    return summary.drop(columns="enough_data").reset_index()
