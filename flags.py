"""Червоні прапорці для тендерів Prozorro.

Чотири прості правила, кожне з поясненням українською:
переплата за одиницю, один учасник, постійний переможець, короткий строк подачі.

Публічні функції (див. CONTRACT.md):
    compute_flags(tenders, items, awards=None) -> по рядку на тендер
    item_price_ratios(items)                   -> порівняння цін на рівні позицій
"""

import pandas as pd

import app

# --- Загальні налаштування ---------------------------------------------------

COMPETITIVE_METHODS = app.COMPETITIVE_METHODS   # у reporting учасників і строків немає
FLAG_COLUMNS = list(app.FLAG_COLUMNS)
REASON_SEPARATOR = "; "

# --- Переплата за одиницю ------------------------------------------------------

REAL_PRICE_SOURCES = app.REAL_PRICE_SOURCES     # estimated_from_award не змішуємо з реальними
# «Той самий товар» = той самий профіль каталогу, одиниця виміру і режим ПДВ.
PROFILE_GROUP_KEYS = ["profile_id", "unit_code", "unit_price_vat_included"]
# Грубе порівняння для позицій без профілю. Лише для сторінки «Ціни на товар»,
# прапорець за ним НЕ ставимо: в одному CPV 33620000-2 лежать і фібриноген
# за 19 тис. грн, і препарати за 8 грн (на даних це 4,7 тис. хибних прапорців).
CPV_GROUP_KEYS = ["cpv_id", "unit_code", "unit_price_vat_included"]
MIN_PRICE_GROUP_SIZE = 20     # медіана з меншої кількості позицій ненадійна
PRICE_RATIO_THRESHOLD = 2.0   # ціна вдвічі вища за медіану
# Для «штук» ratio ≥ 10 часто означає упаковку замість ампули чи таблетки.
PACKAGING_CHECK_RATIO = 10.0
PIECE_UNIT_CODES = ("H87",)
ITEM_NAME_MAX_LEN = 45

# --- Один учасник ----------------------------------------------------------------

# Якщо в замовника з одним учасником проходить ≥ 80% тендерів, це вже система.
SYSTEMIC_SINGLE_BID_SHARE = 0.80
SYSTEMIC_MIN_TENDERS = 20
# Один учасник і ціна майже не впала: переможець фактично взяв очікувану суму.
NO_PRICE_DROP_PCT = 1.0

# --- Постійний переможець --------------------------------------------------------

REPEAT_WINNER_MIN_SHARE = 0.40   # частка тендерів замовника, виграних одним постачальником
REPEAT_WINNER_MIN_TENDERS = 20   # у малих замовників частка нічого не означає

# --- Короткий строк подачі -------------------------------------------------------

SHORT_PERIOD_QUANTILE = 0.10     # нижче 10-го перцентиля в межах свого method_type


# --- Допоміжні функції -------------------------------------------------------------

def format_number(value, digits=1):
    """12.345 -> '12,3' (українська десяткова кома)."""
    return f"{value:.{digits}f}".replace(".", ",")


def format_money(amount):
    """9300809 -> '9,3 млн грн', 46193 -> '46,2 тис. грн'."""
    if amount >= 1_000_000:
        return f"{format_number(amount / 1_000_000)} млн грн"
    if amount >= 1_000:
        return f"{format_number(amount / 1_000)} тис. грн"
    return f"{amount:.0f} грн"


def plural_positions(count):
    """1 позиція, 2 позиції, 5 позицій."""
    count = int(count)
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} позиція"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} позиції"
    return f"{count} позицій"


def shorten(text, max_len=ITEM_NAME_MAX_LEN):
    text = str(text).strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def to_float(series):
    """Число з будь-якого типу колонки (рядок, Int64 з NA). Порожнє -> NaN."""
    return pd.to_numeric(series, errors="coerce").astype(float)


def competitive_tenders(tenders):
    """Лише тендери з конкурентною процедурою."""
    return tenders[tenders["method_type"].isin(COMPETITIVE_METHODS)]


# --- Ціни на рівні позицій --------------------------------------------------------

def item_price_ratios(items):
    """Кожна позиція з реальною ціною проти медіани для того самого товару.

    Спершу порівнюємо за профілем (price_basis = "profile"). Позиції без профілю
    або з малою групою порівнюємо грубо за CPV (price_basis = "cpv").
    Колонки: app.ITEM_PRICE_COLUMNS + description, quantity, overpay_uah.
    """
    prices = items[items["price_source"].isin(REAL_PRICE_SOURCES)].copy()
    prices["unit_price"] = to_float(prices["unit_price"])
    prices["quantity"] = to_float(prices["quantity"])
    # Режим ПДВ приводимо до рядка, щоб True і "True" потрапили в одну групу.
    prices["unit_price_vat_included"] = prices["unit_price_vat_included"].astype(str)
    prices = prices[prices["unit_price"] > 0]
    if "currency" in prices:
        prices = prices[prices["currency"].fillna("UAH") == "UAH"]

    by_profile = _compare_within_groups(prices, PROFILE_GROUP_KEYS, "profile")
    rest = prices[~prices["item_id"].isin(by_profile["item_id"])]
    by_cpv = _compare_within_groups(rest, CPV_GROUP_KEYS, "cpv")

    result = pd.concat([by_profile, by_cpv], ignore_index=True)
    # Скільки заплатили понад медіану за всю кількість (оцінка, якщо фасовка та сама).
    overpay = (result["unit_price"] - result["group_median"]) * result["quantity"]
    result["overpay_uah"] = overpay.clip(lower=0).fillna(0).round(0)
    columns = [*app.ITEM_PRICE_COLUMNS, "description", "quantity", "overpay_uah"]
    return result[columns]


def _compare_within_groups(prices, keys, basis):
    """Медіана і ratio в межах груп за ключами; групи менші за MIN_PRICE_GROUP_SIZE відкидаємо."""
    prices = prices.dropna(subset=keys).copy()
    groups = prices.groupby(keys)["unit_price"]
    prices["group_size"] = groups.transform("size")
    prices["group_median"] = groups.transform("median")
    prices = prices[prices["group_size"] >= MIN_PRICE_GROUP_SIZE]

    prices["group_key"] = prices[keys[0]].astype(str)
    for key in keys[1:]:
        prices["group_key"] += "|" + prices[key].astype(str)
    prices["price_ratio"] = (prices["unit_price"] / prices["group_median"]).round(3)
    prices["price_basis"] = basis
    return prices


# --- Правила ----------------------------------------------------------------------

def price_flags(item_ratios):
    """Переплата: максимальний ratio серед позицій тендера, порівняних за профілем.

    Колонки: price_ratio, flag_price, price_needs_check, overpay_uah, price_reason.
    """
    by_profile = item_ratios[item_ratios["price_basis"] == "profile"]
    worst = (by_profile.sort_values("price_ratio", ascending=False)
                       .drop_duplicates("tender_id")
                       .set_index("tender_id"))
    flagged = worst["price_ratio"] >= PRICE_RATIO_THRESHOLD
    needs_check = (flagged & worst["unit_code"].isin(PIECE_UNIT_CODES)
                   & (worst["price_ratio"] >= PACKAGING_CHECK_RATIO))
    # Сумарна переплата по всіх позиціях тендера з прапорцем, не лише по найгіршій.
    over_threshold = by_profile[by_profile["price_ratio"] >= PRICE_RATIO_THRESHOLD]
    overpay = over_threshold.groupby("tender_id")["overpay_uah"].sum()

    result = pd.DataFrame(index=worst.index)
    result["price_ratio"] = worst["price_ratio"].round(2)
    result["flag_price"] = flagged.astype(int)
    result["price_needs_check"] = needs_check.astype(int)
    result["overpay_uah"] = overpay.reindex(worst.index).fillna(0)
    result["price_reason"] = [
        _price_reason(row, total, check) if flag else ""
        for row, total, check, flag in zip(worst.itertuples(), result["overpay_uah"],
                                           needs_check, flagged)
    ]
    return result


def _price_reason(row, overpay, needs_check):
    text = (f"ціна ×{format_number(row.price_ratio)} від медіани за профілем "
            f"({plural_positions(row.group_size)}): «{shorten(row.description)}»")
    if overpay >= 1:
        text += f", переплата ≈ {format_money(overpay)}"
    if needs_check:
        text += " (перевірити фасовку)"
    return text


def buyer_single_bid_stats(tenders):
    """Для кожного замовника: кількість конкурентних тендерів і частка з одним учасником."""
    competitive = competitive_tenders(tenders)
    single = to_float(competitive["number_of_bids"]) == 1
    stats = single.groupby(competitive["buyer_edrpou"]).agg(["size", "mean"])
    return stats.rename(columns={"size": "buyer_tenders", "mean": "single_bid_share"})


def single_bid_flags(tenders):
    """Один учасник у конкурентній процедурі.

    У поясненні уточнюємо, якщо ціна майже не знизилась або якщо в замовника
    так проходить більшість тендерів: одиничний випадок і система — різні речі.
    Колонки: flag_single_bid, buyer_single_share, single_bid_reason.
    """
    competitive = competitive_tenders(tenders)[
        ["tender_id", "buyer_edrpou", "number_of_bids", "price_drop_pct"]]
    competitive = competitive.join(buyer_single_bid_stats(tenders), on="buyer_edrpou")
    flag = to_float(competitive["number_of_bids"]) == 1
    systemic = ((competitive["single_bid_share"] >= SYSTEMIC_SINGLE_BID_SHARE)
                & (competitive["buyer_tenders"] >= SYSTEMIC_MIN_TENDERS))
    drops = to_float(competitive["price_drop_pct"])

    reasons = [
        _single_bid_reason(drop, share if is_systemic else None) if is_single else ""
        for is_single, is_systemic, share, drop
        in zip(flag, systemic, competitive["single_bid_share"], drops)
    ]
    result = competitive.set_index("tender_id")[[]]
    result["flag_single_bid"] = flag.astype(int).to_numpy()
    result["buyer_single_share"] = competitive["single_bid_share"].round(3).to_numpy()
    result["single_bid_reason"] = reasons
    return result


def _single_bid_reason(price_drop, systemic_share=None):
    text = "1 учасник у конкурентній процедурі"
    if price_drop == 0:
        text += ", ціну не знизили"
    elif price_drop < NO_PRICE_DROP_PCT:
        text += f", ціну знизили лише на {format_number(price_drop)}%"
    if systemic_share is not None:
        text += f" (у цього замовника так {systemic_share:.0%} тендерів)"
    return text


def winners_by_tender(tenders, awards=None):
    """Пари tender_id – постачальник для всіх переможців тендера.

    З awards беремо всі active-рішення (у багатолотових тендерах переможців кілька).
    Без awards — колонки winner_edrpou і winner_name з tenders.
    """
    if awards is not None and not awards.empty:
        winners = awards.loc[awards["status"] == "active",
                             ["tender_id", "supplier_edrpou", "supplier_name"]]
        winners = winners[winners["tender_id"].isin(tenders["tender_id"])]
    else:
        winners = tenders[["tender_id", "winner_edrpou", "winner_name"]].rename(
            columns={"winner_edrpou": "supplier_edrpou", "winner_name": "supplier_name"})
    winners = winners.dropna(subset=["supplier_edrpou"])
    return winners.drop_duplicates(["tender_id", "supplier_edrpou"])


def buyer_top_suppliers(tenders, awards=None):
    """Найчастіший переможець кожного замовника серед його конкурентних тендерів.

    Колонки: buyer_edrpou, top_supplier, top_supplier_name, wins, buyer_tenders, winner_share.
    """
    competitive = competitive_tenders(tenders)
    winners = winners_by_tender(competitive, awards).merge(
        competitive[["tender_id", "buyer_edrpou"]], on="tender_id")

    tenders_per_buyer = competitive.groupby("buyer_edrpou").size().rename("buyer_tenders")
    wins = (winners.groupby(["buyer_edrpou", "supplier_edrpou"])
                   .agg(wins=("tender_id", "size"), top_supplier_name=("supplier_name", "first"))
                   .reset_index())
    top = (wins.sort_values(["wins", "supplier_edrpou"], ascending=[False, True])
               .drop_duplicates("buyer_edrpou")
               .rename(columns={"supplier_edrpou": "top_supplier"})
               .join(tenders_per_buyer, on="buyer_edrpou"))
    top["winner_share"] = top["wins"] / top["buyer_tenders"]
    return top.reset_index(drop=True)


def repeat_winner_flags(tenders, awards=None):
    """Постійний переможець: один постачальник виграє велику частку тендерів замовника.

    Колонки: flag_repeat_winner, winner_share (частка найчастішого переможця
    замовника, ознака для ML), repeat_winner_reason.
    """
    competitive = competitive_tenders(tenders)
    top = buyer_top_suppliers(tenders, awards)
    top["is_repeat_buyer"] = ((top["winner_share"] >= REPEAT_WINNER_MIN_SHARE)
                              & (top["buyer_tenders"] >= REPEAT_WINNER_MIN_TENDERS))

    winners = winners_by_tender(competitive, awards).merge(
        competitive[["tender_id", "buyer_edrpou"]], on="tender_id")
    winners = winners.merge(top[["buyer_edrpou", "top_supplier"]], on="buyer_edrpou")
    won_by_top = set(winners.loc[winners["supplier_edrpou"] == winners["top_supplier"],
                                 "tender_id"])

    result = competitive[["tender_id", "buyer_edrpou"]].merge(top, on="buyer_edrpou", how="left")
    flag = (result["is_repeat_buyer"].fillna(False).astype(bool)
            & result["tender_id"].isin(won_by_top))

    result["flag_repeat_winner"] = flag.astype(int)
    result["repeat_winner_reason"] = [
        (f"переможець виграв {int(r.wins)} з {int(r.buyer_tenders)} конкурентних тендерів "
         f"цього замовника ({r.winner_share:.0%})") if f else ""
        for r, f in zip(result.itertuples(), flag)
    ]
    result["winner_share"] = result["winner_share"].fillna(0).round(3)
    return result.set_index("tender_id")[["flag_repeat_winner", "winner_share",
                                          "repeat_winner_reason"]]


def short_period_thresholds(tenders):
    """Поріг короткого строку (днів) для кожного типу процедури."""
    competitive = competitive_tenders(tenders)
    days = to_float(competitive["submission_days"])
    return days.groupby(competitive["method_type"]).quantile(SHORT_PERIOD_QUANTILE)


def short_period_flags(tenders):
    """Короткий строк подачі: менше, ніж у 90% тендерів того самого типу."""
    competitive = competitive_tenders(tenders)
    days = to_float(competitive["submission_days"])
    threshold = competitive["method_type"].map(short_period_thresholds(tenders)).astype(float)
    flag = days < threshold   # NaN порівнюється як False

    result = pd.DataFrame(index=competitive["tender_id"])
    result["flag_short_period"] = flag.astype(int).to_numpy()
    result["short_period_reason"] = [
        (f"строк подачі {format_number(d)} дн. — коротший, ніж у "
         f"{1 - SHORT_PERIOD_QUANTILE:.0%} тендерів типу {m}") if f else ""
        for d, m, f in zip(days, competitive["method_type"], flag)
    ]
    return result


# --- Збір усіх прапорців -------------------------------------------------------------

def compute_flags(tenders, items, awards=None):
    """Усі прапорці для кожного тендера (зокрема reporting, там прапорці 0).

    Вхід: tenders, items (і бажано awards) з data_loader.load_data().
    Вихід: app.FLAGS_COLUMNS + необов'язкові колонки для scoring і ML:
    winner_share, buyer_single_share, price_needs_check, overpay_uah.
    """
    rules = [
        price_flags(item_price_ratios(items)),
        single_bid_flags(tenders),
        repeat_winner_flags(tenders, awards),
        short_period_flags(tenders),
    ]
    result = tenders[["tender_id"]].drop_duplicates().set_index("tender_id")
    for rule in rules:
        result = result.join(rule, how="left")

    int_columns = [*FLAG_COLUMNS, "price_needs_check"]
    result[int_columns] = result[int_columns].fillna(0).astype(int)
    for column in ("winner_share", "buyer_single_share", "overpay_uah"):
        result[column] = result[column].fillna(0.0).astype(float)

    reason_columns = ["price_reason", "single_bid_reason",
                      "repeat_winner_reason", "short_period_reason"]
    reason_rows = result[reason_columns].fillna("").itertuples(index=False)
    result["reasons"] = [REASON_SEPARATOR.join(text for text in row if text)
                         for row in reason_rows]

    extra = ["winner_share", "buyer_single_share", "price_needs_check", "overpay_uah"]
    result = result.reset_index()[[*app.FLAGS_COLUMNS, *extra]]
    result["price_ratio"] = result["price_ratio"].astype(float)
    return result
