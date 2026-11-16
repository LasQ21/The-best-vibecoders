"""Червоні прапорці для тендерів Prozorro.

Чотири прості правила, кожне з поясненням українською:
переплата за одиницю, один учасник, постійний переможець, короткий строк подачі.

Головна функція: compute_flags(tenders, items, awards=None).
Повертає одну таблицю, по рядку на тендер.
"""

import pandas as pd

# --- Загальні налаштування -------------------------------------------------

# Конкурентні процедури. У reporting (прямий договір) учасників і строків немає за визначенням.
COMPETITIVE_METHODS = ("priceQuotation", "aboveThreshold", "belowThreshold")

# --- Переплата за одиницю ----------------------------------------------------

# Беремо лише реальні ціни. estimated_from_award (сума ÷ кількість) не змішуємо з ними.
REAL_PRICE_SOURCES = ("contract_item", "tender_item")
# «Той самий товар» = той самий профіль каталогу, одиниця виміру і режим ПДВ.
# CPV не використовуємо: в одному коді 33620000-2 лежать і фібриноген за 19 тис. грн,
# і препарати за 8 грн, тож порівняння за CPV дає тисячі хибних прапорців.
PRICE_GROUP_KEYS = ["profile_id", "unit_code", "unit_price_vat_included"]
MIN_PRICE_GROUP_SIZE = 20     # медіана з меншої кількості позицій ненадійна
PRICE_RATIO_THRESHOLD = 2.0   # ціна вдвічі вища за медіану
# Для «штук» дуже великий ratio часто означає упаковку замість ампули чи таблетки.
PACKAGING_CHECK_RATIO = 10.0
PIECE_UNIT_CODES = ("H87",)
ITEM_NAME_MAX_LEN = 45

# --- Один учасник ---------------------------------------------------------------

# Якщо в замовника з одним учасником проходить ≥ 80% тендерів, кажемо про це в поясненні.
SYSTEMIC_SINGLE_BID_SHARE = 0.80
SYSTEMIC_MIN_TENDERS = 20

# --- Постійний переможець ------------------------------------------------------

REPEAT_WINNER_MIN_SHARE = 0.40   # частка тендерів замовника, виграних одним постачальником
REPEAT_WINNER_MIN_TENDERS = 20   # у малих замовників частка нічого не означає

# --- Короткий строк подачі -----------------------------------------------------

# Поріг окремо для кожного типу процедури: нижче 10-го перцентиля в межах типу.
SHORT_PERIOD_QUANTILE = 0.10

FLAG_COLUMNS = ["flag_price", "flag_single_bid", "flag_repeat_winner", "flag_short_period"]
REASON_SEPARATOR = "; "


# --- Допоміжні функції -----------------------------------------------------------

def format_number(value, digits=1):
    """12.345 -> '12,3' (українська десяткова кома)."""
    return f"{value:.{digits}f}".replace(".", ",")


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


def competitive_tenders(tenders):
    """Лише тендери з конкурентною процедурою."""
    return tenders[tenders["method_type"].isin(COMPETITIVE_METHODS)]


# --- Правила -----------------------------------------------------------------------

def price_flags(items):
    """Переплата: ціна за одиницю проти медіани для того самого профілю товару.

    Повертає по рядку на тендер, де вдалося порівняти хоча б одну позицію:
    tender_id, price_ratio (максимум по позиціях), flag_price, price_reason.
    """
    prices = items[items["price_source"].isin(REAL_PRICE_SOURCES)].copy()
    prices["unit_price"] = pd.to_numeric(prices["unit_price"], errors="coerce")
    # Режим ПДВ приводимо до рядка, щоб True і "True" потрапили в одну групу.
    prices["unit_price_vat_included"] = prices["unit_price_vat_included"].astype(str)
    prices = prices[(prices["unit_price"] > 0) & prices["profile_id"].notna()]

    groups = prices.groupby(PRICE_GROUP_KEYS)["unit_price"]
    prices["group_size"] = groups.transform("size")
    prices["group_median"] = groups.transform("median")
    prices = prices[prices["group_size"] >= MIN_PRICE_GROUP_SIZE]
    prices["price_ratio"] = prices["unit_price"] / prices["group_median"]

    # Для кожного тендера лишаємо позицію з найбільшою переплатою.
    worst = (prices.sort_values("price_ratio", ascending=False)
                   .drop_duplicates("tender_id")
                   .set_index("tender_id"))

    result = pd.DataFrame(index=worst.index)
    result["price_ratio"] = worst["price_ratio"].round(2)
    result["flag_price"] = (worst["price_ratio"] >= PRICE_RATIO_THRESHOLD).astype(int)
    result["price_reason"] = [
        _price_reason(row) if row.price_ratio >= PRICE_RATIO_THRESHOLD else ""
        for row in worst.itertuples()
    ]
    return result


def _price_reason(row):
    text = (f"ціна ×{format_number(row.price_ratio)} від медіани за профілем "
            f"({plural_positions(row.group_size)}): «{shorten(row.description)}»")
    if row.unit_code in PIECE_UNIT_CODES and row.price_ratio >= PACKAGING_CHECK_RATIO:
        text += " (перевірити фасовку)"
    return text


def buyer_single_bid_stats(tenders):
    """Для кожного замовника: кількість конкурентних тендерів і частка з одним учасником."""
    competitive = competitive_tenders(tenders)
    single = pd.to_numeric(competitive["number_of_bids"], errors="coerce") == 1
    stats = single.groupby(competitive["buyer_edrpou"]).agg(["size", "mean"])
    return stats.rename(columns={"size": "buyer_tenders", "mean": "single_bid_share"})


def single_bid_flags(tenders):
    """Один учасник у конкурентній процедурі.

    Якщо в замовника так проходить більшість тендерів, це додається в пояснення:
    одиничний випадок і система — різні речі.
    """
    competitive = competitive_tenders(tenders)[["tender_id", "buyer_edrpou", "number_of_bids"]]
    competitive = competitive.join(buyer_single_bid_stats(tenders), on="buyer_edrpou")
    flag = pd.to_numeric(competitive["number_of_bids"], errors="coerce") == 1
    systemic = ((competitive["single_bid_share"] >= SYSTEMIC_SINGLE_BID_SHARE)
                & (competitive["buyer_tenders"] >= SYSTEMIC_MIN_TENDERS))

    reasons = []
    for is_single, is_systemic, share in zip(flag, systemic, competitive["single_bid_share"]):
        text = "1 учасник у конкурентній процедурі" if is_single else ""
        if is_single and is_systemic:
            text += f" (у цього замовника так {share:.0%} тендерів)"
        reasons.append(text)

    result = competitive.set_index("tender_id")[[]]
    result["flag_single_bid"] = flag.astype(int).to_numpy()
    result["single_bid_reason"] = reasons
    return result


def winners_by_tender(tenders, awards=None):
    """Пари tender_id – постачальник для всіх переможців тендера.

    З awards беремо всі active-рішення (у багатолотових тендерах переможців кілька).
    Без awards — колонки winner_edrpou і winner_name з tenders.
    """
    if awards is not None and not awards.empty:
        winners = awards.loc[awards["status"] == "active",
                             ["tender_id", "supplier_edrpou", "supplier_name"]]
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

    Повертає для кожного конкурентного тендера частку найчастішого переможця
    його замовника (winner_share) і прапорець, якщо тендер виграв саме він.
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
    days = pd.to_numeric(competitive["submission_days"], errors="coerce")
    return days.groupby(competitive["method_type"]).quantile(SHORT_PERIOD_QUANTILE)


def short_period_flags(tenders):
    """Короткий строк подачі: менше, ніж у 90% тендерів того самого типу."""
    competitive = competitive_tenders(tenders)
    days = pd.to_numeric(competitive["submission_days"], errors="coerce")
    threshold = competitive["method_type"].map(short_period_thresholds(tenders))
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
    """Усі прапорці для кожного тендера.

    Вхід: tenders, items (і за бажанням awards) з data_loader.load_data().
    Вихід: tender_id, flag_price, flag_single_bid, flag_repeat_winner, flag_short_period (0/1),
    price_ratio (NaN, якщо ціну не було з чим порівняти), winner_share (ознака для ML), reasons.
    """
    rules = [
        price_flags(items),
        single_bid_flags(tenders),
        repeat_winner_flags(tenders, awards),
        short_period_flags(tenders),
    ]
    result = tenders[["tender_id"]].drop_duplicates().set_index("tender_id")
    for rule in rules:
        result = result.join(rule, how="left")

    result[FLAG_COLUMNS] = result[FLAG_COLUMNS].fillna(0).astype(int)
    result["winner_share"] = result["winner_share"].fillna(0.0)
    reason_columns = ["price_reason", "single_bid_reason",
                      "repeat_winner_reason", "short_period_reason"]
    reason_rows = result[reason_columns].fillna("").itertuples(index=False)
    result["reasons"] = [REASON_SEPARATOR.join(text for text in row if text)
                         for row in reason_rows]

    columns = ["tender_id", *FLAG_COLUMNS, "price_ratio", "winner_share", "reasons"]
    return result.reset_index()[columns]
