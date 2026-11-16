"""FastAPI-застосунок дашборда: оболонка Mini App, фрагменти екранів (htmx), фігури plotly.

Сервер рендерить HTML (Jinja2), браузер лише вставляє фрагменти. Бізнес-логіки в JS немає.
Той самий застосунок приймає вебхук Telegram і виклики планувальника (/cron/poll) на Vercel.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import pandas as pd
import plotly
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

import app as contract
import charts
import flags as flags_module
import scoring

from .. import formatting as fmt
from .. import queries
from ..alerts import preview_count_fast
from ..services import Services
from ..storage import MIN_SCORE_RANGE, Subscription
from . import figures
from .auth import AuthError, WebUser, authenticate

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
TEMPLATES_DIR = HERE / "templates"
PLOTLY_JS = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
ASSET_VERSION = "1"
FLAG_SHORT = {"flag_price": "Переплата", "flag_single_bid": "1 учасник",
              "flag_repeat_winner": "Пост. переможець", "flag_short_period": "Короткий строк"}
ACCENT_RGB = (235, 104, 52)


# --- фільтри шаблонів ---------------------------------------------------------------

def heat(value) -> str:
    """Колір фону для балу: від нейтрального (прозорий акцент) до насиченого акценту.

    Не «світлофор»: зелений читався б як «тендер чистий», а модель цього не стверджує.
    """
    share = max(0.0, min(1.0, fmt.safe_float(value, 0) / 70))
    alpha = 0.10 + 0.85 * share
    r, g, b = ACCENT_RGB
    return f"rgba({r},{g},{b},{alpha:.2f})"


def heat_ink(value) -> str:
    return "#fff" if fmt.safe_float(value, 0) >= 35 else "var(--text)"


def short_region(value) -> str:
    text = fmt.clean(value)
    return text.replace(" область", " обл.") if text else fmt.DASH


def make_env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters.update(
        number=fmt.number, score=fmt.score, percent=fmt.percent, amount=fmt.amount, days=fmt.days,
        date=fmt.date, datetime=fmt.datetime_short, ago=fmt.ago, truncate_text=fmt.truncate,
        clean=fmt.clean, buyer=fmt.buyer_label, short_name=lambda v, n=60: charts.short_name(fmt.clean(v), n),
        heat=heat, heat_ink=heat_ink, region=short_region, missing=fmt.is_missing,
        plural=fmt.plural, reasons=fmt.reasons_list, tender_amount=fmt.tender_amount,
        bids=fmt.bids, active_flags=fmt.active_flags, tender_url=fmt.tender_url,
        procedure=fmt.procedure, sf=fmt.safe_float,
    )
    env.globals.update(DISCLAIMER=fmt.DISCLAIMER, FLAG_LABELS=fmt.FLAG_LABELS,
                       FLAG_SHORT=FLAG_SHORT, FLAG_HINTS=fmt.FLAG_HINTS, ASSET_VERSION=ASSET_VERSION,
                       TENDER_FORMS=fmt.TENDER_FORMS, ALERT_FORMS=fmt.ALERT_FORMS,
                       FLAG_COLUMNS=contract.FLAG_COLUMNS)
    return env


# --- допоміжне ------------------------------------------------------------------------------

def _list_param(request: Request, name: str) -> tuple[str, ...]:
    values = []
    for raw in request.query_params.getlist(name):
        values += [part for part in raw.split("|") if part]
    return tuple(dict.fromkeys(values))


def _float_param(request: Request, name: str, default=None):
    raw = request.query_params.get(name, "")
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _int_param(request: Request, name: str, default: int = 0) -> int:
    raw = request.query_params.get(name, "")
    return int(raw) if raw.isdigit() else default


def _check_secret(request: Request, secret: str) -> bool:
    if not secret:
        return False
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("key", "")
    return hmac.compare_digest(supplied.encode(), secret.encode())


def public_base(services: Services) -> str:
    url = services.settings.webapp_url
    return url[: -len("/app")] if url.endswith("/app") else url.rsplit("/", 1)[0]


def create_app(services: Services, dispatcher=None) -> FastAPI:
    settings = services.settings
    env = make_env()
    state = {"webhook_checked": False}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        if settings.on_vercel:
            # Знімок починаємо вантажити одразу: перший запит чекатиме менше.
            asyncio.get_running_loop().create_task(_warm_up())
        yield

    async def _warm_up():
        try:
            await services.snapshots.get()
        except Exception:
            logger.exception("Не вдалося завантажити знімок при старті")

    web = FastAPI(title="Prozorro radar", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    web.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(name: str, status: int = 200, **context) -> HTMLResponse:
        html = env.get_template(name).render(**context)
        return HTMLResponse(html, status_code=status,
                            headers={"Cache-Control": "no-store", "Vary": "Authorization"})

    async def require_user(request: Request) -> WebUser:
        try:
            return authenticate(settings, request.headers.get("authorization"))
        except AuthError as error:
            raise HTTPException(status_code=401, detail=error.reason) from error

    @web.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        if request.url.path.startswith("/app/v/"):
            return render("error.html", status=error.status_code, status_code=error.status_code,
                          message=error.detail)
        return JSONResponse({"detail": error.detail}, status_code=error.status_code)

    async def snapshot():
        try:
            return await services.snapshots.get()
        except Exception as error:
            raise HTTPException(status_code=503, detail="Дані ще завантажуються, спробуйте за хвилину") \
                from error

    # --- оболонка ------------------------------------------------------------------------

    @web.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/app")

    @web.get("/app", response_class=HTMLResponse)
    async def shell():
        return render("shell.html", dev=settings.dev_auth_bypass)

    @web.get("/vendor/plotly.min.js", include_in_schema=False)
    async def plotly_js():
        return FileResponse(PLOTLY_JS, media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @web.get("/healthz")
    async def healthz():
        current = services.snapshots.current
        status = services.poller.status() if services.poller else {}
        return {
            "ok": True,
            "snapshot_version": current.version if current else None,
            "snapshot_loaded_at": current.loaded_at.isoformat() if current else None,
            "tenders": len(current.tenders) if current else None,
            "last_poll_success": status.get("last_success").isoformat() if status.get("last_success") else None,
            "mode": "vercel" if settings.on_vercel else "process",
            "bot": bool(settings.bot_token),
        }

    # --- екрани ------------------------------------------------------------------------------

    @web.get("/app/v/overview", response_class=HTMLResponse)
    async def v_overview(user: WebUser = Depends(require_user)):
        snap = await snapshot()
        data = queries.overview(snap)
        top5, _ = queries.top(snap, None, 0, 5)
        status = services.poller.status() if services.poller else {}
        return render("overview.html", o=data, top=top5, threshold=snap.threshold,
                      snapshot=snap, last_success=status.get("last_success"), user=user,
                      max_flag=max(data["flag_counts"].values() or [1]),
                      max_bin=max([b["count"] for b in data["distribution"]] or [1]))

    def _tender_filters(request: Request) -> queries.TenderFilters:
        min_score = _float_param(request, "min_score")
        return queries.TenderFilters(
            regions=_list_param(request, "region"), procedures=_list_param(request, "procedure"),
            categories=_list_param(request, "category"),
            flags=tuple(f for f in _list_param(request, "flag") if f in contract.FLAG_COLUMNS),
            min_score=min_score if min_score and min_score > 0 else None,
            buyer=request.query_params.get("buyer") or None,
            since_hours=_int_param(request, "since_hours") or None)

    def _filter_query(filters: queries.TenderFilters, offset: int) -> str:
        params = [("region", v) for v in filters.regions] + [("procedure", v) for v in filters.procedures]
        params += [("category", v) for v in filters.categories] + [("flag", v) for v in filters.flags]
        if filters.min_score:
            params.append(("min_score", int(filters.min_score)))
        if filters.buyer:
            params.append(("buyer", filters.buyer))
        if filters.since_hours:
            params.append(("since_hours", filters.since_hours))
        params.append(("offset", offset))
        return urlencode(params)

    @web.get("/app/v/tenders", response_class=HTMLResponse)
    async def v_tenders(request: Request, user: WebUser = Depends(require_user)):
        snap = await snapshot()
        filters = _tender_filters(request)
        offset = _int_param(request, "offset")
        table = queries.filtered(snap, filters)
        rows = queries.page(table, offset)
        next_offset = offset + len(rows)
        context = dict(rows=rows, total=len(table), offset=offset, threshold=snap.threshold,
                       more=next_offset < len(table),
                       next_query=_filter_query(filters, next_offset), filters=filters)
        part = request.query_params.get("part")
        if part == "rows":
            return render("tender_rows.html", **context)
        if part == "list":
            return render("tender_list.html", **context)
        return render("tenders.html", **context,
                      regions=queries.filter_options(snap, "buyer_region"),
                      procedures=[p for p in queries.filter_options(snap, "procedure")],
                      categories=queries.filter_options(snap, "category"))

    @web.get("/app/v/tenders/{tender_id}", response_class=HTMLResponse)
    async def v_tender(tender_id: str, user: WebUser = Depends(require_user)):
        snap = await snapshot()
        row = queries.get_tender(snap, tender_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Такого тендера немає в даних")
        items = queries.tender_items(snap, tender_id)
        items = items.sort_values("price_ratio", ascending=False, na_position="last")
        item_rows = [item for _, item in items.head(30).iterrows()]
        sub = services.storage.get_subscription(user.chat_id)
        edrpou = None if fmt.is_missing(row.get("buyer_edrpou")) else str(row["buyer_edrpou"])
        return render("tender.html", t=row, items=item_rows, items_total=queries.all_items_count(snap, tender_id),
                      threshold=snap.threshold, flags=fmt.active_flags(row),
                      max_weights=scoring.WEIGHTS, followed=edrpou in sub.buyers if edrpou else False,
                      bot_username=services.extra.get("bot_username"))

    @web.get("/app/v/buyers", response_class=HTMLResponse)
    async def v_buyers(request: Request, user: WebUser = Depends(require_user)):
        snap = await snapshot()
        query = (request.query_params.get("q") or "").strip()
        offset = _int_param(request, "offset")
        if query:
            table = queries.search_buyers(snap, query, limit=offset + 30)
        else:
            table = queries.buyer_rating(snap)
        rows = [row for _, row in table.iloc[offset: offset + 30].iterrows()]
        more = not query and offset + 30 < len(table)
        context = dict(rows=rows, query=query, offset=offset, more=more, total=len(table),
                       next_query=urlencode({"q": query, "offset": offset + 30}),
                       min_tenders=scoring.MIN_BUYER_TENDERS, threshold=snap.threshold)
        part = request.query_params.get("part")
        if part == "rows":
            return render("buyer_rows.html", **context)
        if part == "list":
            return render("buyer_list.html", **context)
        return render("buyers.html", **context)

    @web.get("/app/v/buyers/{edrpou}", response_class=HTMLResponse)
    async def v_buyer(edrpou: str, user: WebUser = Depends(require_user)):
        snap = await snapshot()
        profile = queries.buyer_profile(snap, edrpou)
        if profile is None:
            raise HTTPException(status_code=404, detail="Замовника не знайдено")
        sub = services.storage.get_subscription(user.chat_id)
        return render("buyer.html", b=profile, threshold=snap.threshold,
                      followed=edrpou in sub.buyers, muted=edrpou in sub.muted, sub=sub)

    @web.post("/app/v/buyers/{edrpou}/follow", response_class=HTMLResponse)
    async def v_follow(edrpou: str, request: Request, user: WebUser = Depends(require_user)):
        services.refresh_db()
        follow = request.query_params.get("on", "1") == "1"
        services.storage.follow_buyer(user.chat_id, edrpou, follow=follow)
        await services.flush()
        sub = services.storage.get_subscription(user.chat_id)
        return render("follow_button.html", edrpou=edrpou, followed=follow, sub=sub)

    # --- сповіщення ------------------------------------------------------------------------------

    def _sub_from_form(base: Subscription, form: dict[str, list[str]]) -> Subscription:
        def one(name, default):
            values = form.get(name)
            return values[0] if values else default

        try:
            min_score = float(one("min_score", base.min_score))
        except ValueError:
            min_score = base.min_score
        try:
            follow_min = float(one("follow_min_score", base.follow_min_score))
        except ValueError:
            follow_min = base.follow_min_score
        flags = tuple(f for f in form.get("flag", []) if f in contract.FLAG_COLUMNS)
        regions = tuple(dict.fromkeys(r for r in form.get("region", []) if r))
        active = one("active", "0") in ("1", "on", "true")
        return base.with_changes(active=active, min_score=min_score, follow_min_score=follow_min,
                                 flags=flags, regions=regions)

    async def _alerts_context(user: WebUser, sub: Subscription):
        snap = await snapshot()
        recent = queries.recent_candidates(snap, 7)
        history = []
        for item in services.storage.recent_alerts(user.chat_id, 20):
            row = queries.get_tender(snap, item["tender_id"])
            history.append({**item, "row": row})
        names = {}
        for edrpou in list(sub.buyers) + list(sub.muted):
            own = queries.buyer_tenders(snap, edrpou)
            names[edrpou] = own["buyer_name"].iloc[0] if len(own) else ""
        return dict(sub=sub, preview=preview_count_fast(recent, sub),
                    regions=queries.filter_options(snap, "buyer_region"), history=history,
                    names=names, score_range=MIN_SCORE_RANGE, bot=bool(services.bot or settings.bot_token))

    @web.get("/app/v/alerts", response_class=HTMLResponse)
    async def v_alerts(user: WebUser = Depends(require_user)):
        services.refresh_db()
        sub = services.storage.get_subscription(user.chat_id)
        return render("alerts.html", **await _alerts_context(user, sub))

    @web.put("/app/v/alerts", response_class=HTMLResponse)
    async def v_alerts_save(request: Request, user: WebUser = Depends(require_user)):
        services.refresh_db()
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=False)
        sub = _sub_from_form(services.storage.get_subscription(user.chat_id), form)
        sub = services.storage.save_subscription(sub)
        await services.flush()
        snap = await snapshot()
        count = preview_count_fast(queries.recent_candidates(snap, 7), sub)
        return render("alerts_preview.html", sub=sub, preview=count, saved=True)

    @web.get("/app/v/alerts/preview", response_class=HTMLResponse)
    async def v_alerts_preview(request: Request, user: WebUser = Depends(require_user)):
        form = {key: request.query_params.getlist(key) for key in request.query_params.keys()}
        sub = _sub_from_form(services.storage.get_subscription(user.chat_id), form)
        snap = await snapshot()
        count = preview_count_fast(queries.recent_candidates(snap, 7), sub)
        return render("alerts_preview.html", sub=sub, preview=count, saved=False)

    @web.post("/app/v/alerts/buyers/{edrpou}", response_class=HTMLResponse)
    async def v_alerts_buyer(edrpou: str, request: Request, user: WebUser = Depends(require_user)):
        services.refresh_db()
        action = request.query_params.get("action")
        if action == "unfollow":
            services.storage.follow_buyer(user.chat_id, edrpou, follow=False)
        elif action == "unmute":
            services.storage.mute_buyer(user.chat_id, edrpou, mute=False)
        await services.flush()
        return HTMLResponse("")

    @web.get("/app/v/about", response_class=HTMLResponse)
    async def v_about(user: WebUser = Depends(require_user)):
        snap = services.snapshots.current
        return render("about.html", weights=scoring.WEIGHTS, threshold=scoring.HIGH_SCORE_THRESHOLD,
                      flags_module=flags_module, scoring=scoring, snapshot=snap,
                      min_buyer=scoring.MIN_BUYER_TENDERS)

    # --- фігури ------------------------------------------------------------------------------

    def fig_response(fig, theme: str):
        return JSONResponse(figures.themed(fig, theme), headers={"Cache-Control": "private, max-age=300"})

    @web.get("/app/fig/scores")
    async def fig_scores(theme: str = "light", user: WebUser = Depends(require_user)):
        snap = await snapshot()
        tenders = snap.tenders
        competitive = tenders.loc[tenders["method_type"].isin(contract.COMPETITIVE_METHODS), "total_score"]
        return fig_response(figures.score_distribution(competitive, snap.threshold), theme)

    @web.get("/app/fig/price")
    async def fig_price(tender_id: str, item_id: str, basis: str | None = None, theme: str = "light",
                        user: WebUser = Depends(require_user)):
        snap = await snapshot()
        found = queries.price_comparison(snap, tender_id, item_id, basis)
        if found is None:
            raise HTTPException(status_code=404, detail="Позицію не знайдено")
        prices, median, _ratio, selected, _method = found
        fig = charts.price_histogram(prices, median, highlight=selected["unit_price"])
        return fig_response(figures.compact(fig, height=340), theme)

    @web.get("/app/fig/buyer/{edrpou}/winners")
    async def fig_winners(edrpou: str, theme: str = "light", user: WebUser = Depends(require_user)):
        snap = await snapshot()
        own = queries.buyer_tenders(snap, edrpou)
        return fig_response(figures.compact(charts.top_winners(own, n=8)), theme)

    @web.get("/app/fig/buyer/{edrpou}/timeline")
    async def fig_timeline(edrpou: str, freq: str = "W", theme: str = "light",
                           user: WebUser = Depends(require_user)):
        snap = await snapshot()
        own = queries.buyer_tenders(snap, edrpou)
        freq = freq if freq in ("D", "W", "M") else "W"
        return fig_response(figures.compact(charts.purchases_over_time(own, freq=freq), height=420), theme)

    # --- Telegram і планувальник -----------------------------------------------------------------

    async def ensure_webhook(force: bool = False) -> dict:
        """На Vercel: вебхук, команди й кнопка меню (ідемпотентно)."""
        bot = services.bot
        if bot is None or not settings.webapp_url:
            return {"webhook": "skipped"}
        url = f"{public_base(services)}/tg/webhook"
        info = await bot.get_webhook_info()
        changed = False
        if force or info.url != url:
            await bot.set_webhook(url=url, secret_token=settings.webhook_secret,
                                  allowed_updates=["message", "callback_query", "inline_query"],
                                  drop_pending_updates=False)
            changed = True
        if force or changed:
            from ..services import setup_bot_profile
            await setup_bot_profile(services)
        me = await bot.get_me()
        services.extra["bot_username"] = me.username
        state["webhook_checked"] = True
        return {"webhook": url, "changed": changed}

    if dispatcher is not None:
        @web.post("/tg/webhook", include_in_schema=False)
        async def tg_webhook(request: Request):
            from aiogram.types import Update

            token = request.headers.get("x-telegram-bot-api-secret-token", "")
            if not settings.webhook_secret or not hmac.compare_digest(token, settings.webhook_secret):
                raise HTTPException(status_code=403, detail="forbidden")
            update = Update.model_validate(await request.json(), context={"bot": services.bot})
            try:
                await dispatcher.feed_update(services.bot, update)
            except Exception:
                logger.exception("Помилка обробки оновлення Telegram")
            return {"ok": True}

    @web.api_route("/cron/poll", methods=["GET", "POST"], include_in_schema=False)
    async def cron_poll(request: Request):
        if not _check_secret(request, settings.cron_secret):
            raise HTTPException(status_code=401, detail="unauthorized")
        setup = None
        if not state["webhook_checked"] and settings.on_vercel:
            try:
                setup = await ensure_webhook()
            except Exception as error:
                logger.exception("Не вдалося налаштувати вебхук")
                setup = {"error": str(error)}
        services.refresh_db()
        results = await services.poller.run()
        return {"setup": setup, "cycles": [r.__dict__ for r in results]}

    @web.api_route("/tg/setup", methods=["GET", "POST"], include_in_schema=False)
    async def tg_setup(request: Request):
        if not _check_secret(request, settings.cron_secret):
            raise HTTPException(status_code=401, detail="unauthorized")
        return await ensure_webhook(force=True)

    web.state.services = services
    return web
