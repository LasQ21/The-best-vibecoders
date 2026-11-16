"""Вебсервер дашборда: авторизація initData, маршрути на вигаданих таблицях, темна палітра."""

import asyncio
import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import numpy as np
from fastapi.testclient import TestClient

import charts
from bot.config import ConfigError, Settings, load_settings
from bot.persist import LocalPersistence
from bot.services import Services
from bot.snapshot import SnapshotStore
from bot.storage import Storage
from bot.web import figures
from bot.web.auth import AuthError, authenticate
from bot.web.server import create_app
from bot_fixtures import NOW, TOKEN, default_rows, make_dashboard, temp_db_path, tender


def init_data(token=TOKEN, user_id=777, auth_date=None, tamper=False):
    fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())),
              "query_id": "AAH", "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": 1, "first_name": "Інший"})
    return urlencode(fields)


class AdminConfigTest(unittest.TestCase):
    def test_ids_and_usernames(self):
        settings = load_settings({"ADMIN_IDS": "123, @Bohdan_K, other_admin"})
        self.assertEqual(settings.admin_ids, frozenset({123}))
        self.assertTrue(settings.is_admin(None, "bohdan_k"))
        self.assertTrue(settings.is_admin(123))
        self.assertFalse(settings.is_admin(5, "someone"))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings({"ADMIN_IDS": "@a b"})

    def test_username_admin_chat_is_remembered(self):
        _client, services = make_client(admin_usernames=frozenset({"bohdan"}))
        self.assertFalse(services.is_admin_chat(42, None))
        self.assertTrue(asyncio.run(services.remember_admin(42, "Bohdan")))
        self.assertIn(42, services.admin_chat_ids())
        self.assertTrue(services.is_admin_chat(42, None))
        self.assertFalse(asyncio.run(services.remember_admin(43, "stranger")))


class AuthTest(unittest.TestCase):
    settings = Settings(bot_token=TOKEN)

    def test_valid_signature(self):
        user = authenticate(self.settings, f"tma {init_data()}")
        self.assertEqual(user.chat_id, 777)

    def test_forged(self):
        with self.assertRaises(AuthError):
            authenticate(self.settings, f"tma {init_data(tamper=True)}")
        with self.assertRaises(AuthError):
            authenticate(self.settings, f"tma {init_data(token='999:other')}")

    def test_expired(self):
        old = time.time() - 25 * 3600
        with self.assertRaises(AuthError):
            authenticate(self.settings, f"tma {init_data(auth_date=old)}")

    def test_missing(self):
        with self.assertRaises(AuthError):
            authenticate(self.settings, None)
        with self.assertRaises(AuthError):
            authenticate(self.settings, "Bearer xyz")

    def test_dev_bypass_only_in_dev_on_localhost(self):
        base = {"DEV_AUTH_BYPASS": "1", "BOT_ENV": "dev", "HOST": "127.0.0.1"}
        self.assertTrue(load_settings(base).dev_auth_bypass)
        for override in ({"BOT_ENV": "prod"}, {"HOST": "0.0.0.0"}, {"VERCEL": "1"}):
            with self.assertRaises(ConfigError):
                load_settings({**base, **override})


def make_client(rows=None, dispatcher=None, **settings_kwargs):
    settings = Settings(bot_token=TOKEN, webapp_url="https://example.test/app",
                        cron_secret="cron-secret", **settings_kwargs)
    storage = Storage(temp_db_path())
    data = make_dashboard(rows)
    services = Services(settings=settings, storage=storage,
                        persistence=LocalPersistence("/nonexistent", storage.path),
                        snapshots=SnapshotStore(loader=lambda _d: data))
    services.poller = None
    return TestClient(create_app(services, dispatcher)), services


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = default_rows() + [tender(9, number_of_bids=None, total_score=np.nan, reasons="",
                                        price_ratio=np.nan, buyer_region=None, title=None)]
        cls.client, cls.services = make_client(rows)
        cls.auth = {"Authorization": f"tma {init_data()}"}
        cls.tid = f"{1:032x}"

    def get(self, path):
        return self.client.get(path, headers=self.auth)

    def test_without_init_data_is_401(self):
        for path in ("/app/v/overview", "/app/fig/scores", f"/app/v/tenders/{self.tid}"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_shell_is_open_and_light(self):
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 200)
        self.assertIn("telegram-web-app.js", response.text)
        self.assertNotIn("plotly.min.js", response.text)

    def test_every_screen_renders(self):
        paths = ["/app/v/overview", "/app/v/tenders", "/app/v/tenders?part=list&flag=flag_price&min_score=40",
                 "/app/v/tenders?part=rows&offset=1", "/app/v/tenders?region=%D0%BC.%20%D0%9A%D0%B8%D1%97%D0%B2",
                 f"/app/v/tenders/{self.tid}", f"/app/v/tenders/{3:032x}", f"/app/v/tenders/{9:032x}",
                 "/app/v/buyers", "/app/v/buyers?q=01999265&part=list", "/app/v/buyers/01999265",
                 "/app/v/buyers/43927048", "/app/v/alerts", "/app/v/alerts/preview?min_score=30",
                 "/app/v/about", "/healthz"]
        for path in paths:
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn(">nan<", response.text.lower(), path)

    def test_html_from_data_is_escaped(self):
        response = self.get(f"/app/v/tenders/{self.tid}")
        self.assertIn("&lt;b&gt;свіжа&lt;/b&gt;", response.text)

    def test_unknown_tender_is_404_fragment(self):
        response = self.get("/app/v/tenders/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error-box", response.text)

    def test_figures(self):
        for path in ("/app/fig/scores?theme=dark", f"/app/fig/price?tender_id={self.tid}&item_id=i1",
                     "/app/fig/buyer/01999265/winners", "/app/fig/buyer/01999265/timeline?freq=M&theme=dark"):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("data", response.json())

    def test_alert_settings_use_chat_id_from_init_data(self):
        body = "active=1&min_score=35&region=%D0%BC.%20%D0%9A%D0%B8%D1%97%D0%B2&flag=flag_price&chat_id=1"
        response = self.client.put("/app/v/alerts", content=body,
                                   headers={**self.auth, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 200)
        sub = self.services.storage.get_subscription(777)
        self.assertTrue(sub.active)
        self.assertEqual(sub.min_score, 35)
        self.assertEqual(sub.regions, ("м. Київ",))
        self.assertFalse(self.services.storage.has_subscription(1))

    def test_follow_buyer(self):
        response = self.client.post("/app/v/buyers/01999265/follow?on=1", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("01999265", self.services.storage.followed_buyers(777))

    def test_cron_requires_secret(self):
        self.assertEqual(self.client.post("/cron/poll").status_code, 401)
        self.assertEqual(self.client.post("/cron/poll", headers={"Authorization": "Bearer wrong"}).status_code, 401)


class FiguresTest(unittest.TestCase):
    def test_dark_palette_leaves_no_light_colors(self):
        data = make_dashboard()
        tenders = data.tenders
        figs = [charts.top_winners(tenders), charts.purchases_over_time(tenders),
                charts.price_histogram(data.price_items["unit_price"], 19.0, 41.94),
                figures.score_distribution(tenders["total_score"], 40)]
        light = figures.light_colors() | {"white"}
        for fig in figs:
            text = json.dumps(figures.themed(fig, "dark")).lower()
            for color in light:
                self.assertNotIn(f'"{color}"', text)
                self.assertNotIn(f"color:{color}", text)

    def test_mapping_is_built_from_charts_constants(self):
        self.assertIn(charts.SURFACE_COLOR.lower(), figures.palette("dark"))
        self.assertIn(charts.GRID_COLOR.lower(), figures.palette("dark"))


if __name__ == "__main__":
    unittest.main()
