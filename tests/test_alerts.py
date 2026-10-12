"""Tests for the BB touch alert now that the LEAPS lookup is gone.

Run from the repository root:  python -m unittest discover -s tests -v
Every network call is replaced with a stub, so the tests run offline.
"""

import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

import flask  # noqa: F401  (imported before the patch below so Flask keeps the real Thread)
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Importing server starts the background scan loop. Stop that during tests.
with mock.patch.object(threading, "Thread"):
    import server

NOW = "12 Oct 2026 15:00 UTC"


def touch(**overrides):
    t = {
        "ticker": "AAA", "price": 99.0, "lower": 100.0, "basis": 110.0,
        "pct": -1.0, "vol_warn": "", "vix": "20.00", "vix_ok": True,
        "entry_type": "lower BB", "beta": "1.20",
    }
    t.update(overrides)
    return t


class AlertMessageTests(unittest.TestCase):
    def test_alert_has_no_leaps_block(self):
        msg = server.build_alert_message(touch(), NOW)
        self.assertNotIn("LEAPS", msg)
        self.assertNotIn("No qualifying contract found", msg)

    def test_alert_keeps_bollinger_and_criteria_lines(self):
        msg = server.build_alert_message(touch(), NOW)
        self.assertIn("🔔 <b>BB Touch Alert</b>", msg)
        self.assertIn("<b>AAA</b>  β=1.20", msg)
        self.assertIn("Entry: <i>lower BB</i>", msg)
        self.assertIn("💰 Price:    <code>99.0</code>", msg)
        self.assertIn("📉 Lower BB: <code>100.0</code>", msg)
        self.assertIn("〰️ Mid BB:   <code>110.0</code>", msg)
        self.assertIn("📏 % from lower: <code>-1.00%</code>", msg)
        self.assertIn("✅ Lower BB touched", msg)
        self.assertIn("✅ Not a missed earnings drop", msg)
        self.assertIn("✅ VIX >= 15.0 (currently 20.00)", msg)
        self.assertTrue(msg.endswith(f")\n\n🕐 {NOW}"))

    def test_low_vix_and_volume_warning(self):
        msg = server.build_alert_message(touch(vix="12.50", vix_ok=False, vol_warn=" ⚠️ Vol 3.0x avg"), NOW)
        self.assertIn("❌ VIX < 15.0 (currently 12.50)", msg)
        self.assertTrue(msg.startswith("🔔 <b>BB Touch Alert</b> ⚠️ Vol 3.0x avg"))


class ScreenerRunTests(unittest.TestCase):
    """run_screener end to end, with every data source stubbed."""

    def setUp(self):
        server.alerted_today = set()
        server.last_alert_date = None
        self.sent = []
        df = pd.DataFrame({"close": [100.0] * 25, "volume": [1_000_000] * 25})
        stubs = {
            "WATCHLIST": ["AAA"],
            "get_vix": mock.Mock(return_value=20.0),
            "get_daily_data": mock.Mock(return_value=df),
            "calculate_bb": mock.Mock(return_value={"lower": 100.0, "basis": 110.0, "avg_vol": 1_000_000}),
            "get_live_price": mock.Mock(return_value={"price": 99.0, "day_low": 98.5, "volume": 3_000_000, "source": "yahoo"}),
            "get_beta": mock.Mock(return_value=1.2),
            "check_missed_earnings": mock.Mock(return_value=(False, None)),
            "send_alert": self.sent.append,
            "is_market_open": mock.Mock(return_value=True),
        }
        for name, value in stubs.items():
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            mock.patch.object(server.time, "sleep"),
            # Any real HTTP call (marketdata.app included) fails the test.
            mock.patch.object(requests, "get", side_effect=AssertionError("unexpected HTTP GET")),
            mock.patch.object(requests, "post", side_effect=AssertionError("unexpected HTTP POST")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_touch_sends_one_alert_without_leaps(self):
        server.run_screener()
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertIn("<b>AAA</b>", msg)
        self.assertIn("⚠️ Vol 3.0x avg", msg)
        self.assertNotIn("LEAPS", msg)
        self.assertNotIn("No qualifying contract found", msg)
        self.assertEqual(server.status_cache["errors"], [])
        self.assertEqual([a["ticker"] for a in server.status_cache["alerts_today"]], ["AAA"])
        self.assertIn("AAA", server.alerted_today)

    def test_one_alert_per_ticker_per_day(self):
        server.run_screener()
        server.run_screener()
        self.assertEqual(len(self.sent), 1)

    def test_missed_earnings_is_filtered_not_alerted(self):
        server.check_missed_earnings.return_value = (True, "2026-10-09")
        server.run_screener()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("BB Touch — Filtered Out", self.sent[0])
        self.assertIn("AAA: missed earnings 2026-10-09", self.sent[0])

    def test_no_touch_no_alert(self):
        server.get_live_price.return_value = {"price": 105.0, "day_low": 104.0, "volume": 1_000_000, "source": "yahoo"}
        server.run_screener()
        self.assertEqual(self.sent, [])

    def test_high_beta_uses_mid_band(self):
        server.get_beta.return_value = 2.5
        server.get_live_price.return_value = {"price": 108.0, "day_low": 107.0, "volume": 1_000_000, "source": "yahoo"}
        server.run_screener()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Entry: <i>mid-BB (high β)</i>", self.sent[0])

    def test_low_vix_is_marked_in_the_alert(self):
        server.get_vix.return_value = 12.0
        server.run_screener()
        self.assertIn("❌ VIX < 15.0 (currently 12.00)", self.sent[0])


class SendAlertTests(unittest.TestCase):
    def test_alert_goes_to_telegram_and_discord(self):
        with mock.patch.object(server, "TG_BOT_TOKEN", "test-token"), \
             mock.patch.object(server, "TG_CHAT_ID", "test-chat"), \
             mock.patch.object(server, "DISCORD_WEBHOOK", "https://discord.example/hook"), \
             mock.patch.object(server.requests, "post") as post:
            post.return_value.ok = True
            server.send_alert("<b>AAA</b> alert")
        urls = [c.args[0] for c in post.call_args_list]
        self.assertEqual(urls, ["https://api.telegram.org/bottest-token/sendMessage", "https://discord.example/hook"])
        self.assertEqual(post.call_args_list[0].kwargs["json"]["text"], "<b>AAA</b> alert")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["content"], "AAA alert")


class EarningsTests(unittest.TestCase):
    def test_recent_miss_is_detected(self):
        period = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
        response = mock.Mock()
        response.json.return_value = [{"period": period, "actual": 1.0, "estimate": 1.2}]
        with mock.patch.object(server, "FINNHUB_KEY", "test-key"), \
             mock.patch.object(server.requests, "get", return_value=response):
            self.assertEqual(server.check_missed_earnings("AAA"), (True, period))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_status_route(self):
        r = self.client.get("/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("alerts_today", r.get_json())

    def test_run_route_starts_a_scan(self):
        with mock.patch.object(server.threading, "Thread") as thread:
            r = self.client.get("/run")
        self.assertEqual(r.get_json(), {"status": "scan started"})
        self.assertIs(thread.call_args.kwargs["target"], server.run_screener)
        thread.return_value.start.assert_called_once()

    def test_home_page_and_test_alert_do_not_offer_leaps(self):
        self.assertNotIn("LEAPS", self.client.get("/").get_data(as_text=True))
        sent = []
        with mock.patch.object(server, "send_alert", sent.append):
            self.client.get("/test")
        self.assertEqual(len(sent), 1)
        self.assertNotIn("LEAPS", sent[0])

    def test_debug_leaps_route_is_kept(self):
        rules = {r.rule for r in server.app.url_map.iter_rules()}
        self.assertIn("/debug/leaps/<ticker>", rules)


class NoMarketDataTests(unittest.TestCase):
    def test_marketdata_app_is_not_referenced(self):
        self.assertFalse(hasattr(server, "get_leaps_suggestion"))
        for name in ("server.py", "render.yaml"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                self.assertNotIn("marketdata", f.read().lower(), name)


if __name__ == "__main__":
    unittest.main()
