"""
BB Touch Screener
------------------
Runs every 15 minutes during US market hours.
Checks all watchlist tickers against their lower Bollinger Band (20, 2).
Sends alerts to both Telegram and Discord.
No TradingView needed.
"""

import os
import re
import time
import threading
import requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN    = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID      = os.environ.get("TG_CHAT_ID",   "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1518289051470528633/u7-5UtbUfiCRkJqp9Iz9BowMsTOqHLeHfcPQ55Bf5XYb9ao25vl8a7W82GgCt062dq-c")
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "900"))  # 15 min

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = [
    "NXT", "SPY", "QQQ", "HOOD", "PLTR", "SOFI", "KTOS", "IREN",
    "INOD", "GLW", "AVGO", "IBRX", "IBM", "DRAM", "CLS", "CCJ",
    "COO", "WDC", "STX", "SNDK", "CRDO", "VRT", "CDE", "MU",
    "AA", "ADI", "AMAT", "NVDA", "AMD", "AMZN", "APH", "APP",
    "ASML", "CAT", "CCL", "FCX", "IBIT", "LRCX", "META", "NEM",
    "ORCL", "RTX", "TIGR", "TSM", "AXTI", "MRVL", "TDOC", "TER",
    "SFII", "EUV", "SPCX",
]
WATCHLIST = list(dict.fromkeys(WATCHLIST))  # deduplicate

# ── State ─────────────────────────────────────────────────────────────────────
alerted_today   = set()
last_alert_date = None
status_cache = {
    "last_run":     None,
    "next_run":     None,
    "alerts_today": [],
    "errors":       [],
    "market_open":  False,
}


# ── Notifications ─────────────────────────────────────────────────────────────
def send_telegram(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[Telegram] No credentials set")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id":    TG_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }, timeout=10)
        if not r.ok:
            print(f"[Telegram] Error: {r.text}")
    except Exception as e:
        print(f"[Telegram] Exception: {e}")


def send_discord(message: str):
    if not DISCORD_WEBHOOK:
        print("[Discord] No webhook set")
        return
    try:
        clean = re.sub(r"<[^>]+>", "", message)  # strip HTML tags
        r = requests.post(DISCORD_WEBHOOK, json={"content": clean}, timeout=10)
        if not r.ok:
            print(f"[Discord] Error: {r.text}")
    except Exception as e:
        print(f"[Discord] Exception: {e}")


def send_alert(message: str):
    """Send to both Telegram and Discord."""
    send_telegram(message)
    send_discord(message)


# ── Market hours ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    now_et = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et <= market_close


# ── Price data ────────────────────────────────────────────────────────────────
def get_daily_prices(ticker: str, period: int = 30):
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range={period}d"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data   = r.json()
        result = data["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "date":   pd.to_datetime(result["timestamp"], unit="s"),
            "open":   quotes["open"],
            "high":   quotes["high"],
            "low":    quotes["low"],
            "close":  quotes["close"],
            "volume": quotes["volume"],
        }).dropna()
        return df
    except Exception as e:
        print(f"[{ticker}] Price fetch error: {e}")
        return None


# ── Bollinger Bands ───────────────────────────────────────────────────────────
def calculate_bb(df, length: int = 20, std: float = 2.0):
    if len(df) < length:
        return None
    close  = df["close"]
    basis  = close.rolling(length).mean()
    stddev = close.rolling(length).std()
    lower  = basis - std * stddev
    return {
        "lower": round(lower.iloc[-1], 2),
        "close": round(close.iloc[-1], 2),
        "low":   round(df["low"].iloc[-1], 2),
        "pct":   round((close.iloc[-1] - lower.iloc[-1]) / lower.iloc[-1] * 100, 2),
    }


# ── Screener ──────────────────────────────────────────────────────────────────
def run_screener():
    global alerted_today, last_alert_date

    today = datetime.utcnow().date()
    if last_alert_date != today:
        alerted_today = set()
        last_alert_date = today
        status_cache["alerts_today"] = []

    now_str = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    print(f"\n[{now_str}] Scanning {len(WATCHLIST)} tickers…")

    touches = []
    errors  = []

    for ticker in WATCHLIST:
        try:
            df = get_daily_prices(ticker, period=30)
            if df is None or len(df) < 20:
                continue
            bb = calculate_bb(df)
            if bb is None:
                continue

            touched = bb["low"] <= bb["lower"]
            print(f"  {ticker:6s} close={bb['close']:8.2f}  lower={bb['lower']:8.2f}  low={bb['low']:8.2f}  {'⚡ TOUCH' if touched else ''}")

            if touched and ticker not in alerted_today:
                touches.append({**bb, "ticker": ticker})
                alerted_today.add(ticker)

        except Exception as e:
            errors.append(f"{ticker}: {e}")
            print(f"  {ticker} ERROR: {e}")

        time.sleep(0.3)

    for t in touches:
        msg = (
            f"🔔 <b>Lower BB Touch</b>\n\n"
            f"<b>{t['ticker']}</b>\n\n"
            f"💰 Close:     <code>{t['close']}</code>\n"
            f"📉 Low:       <code>{t['low']}</code>\n"
            f"📊 Lower BB:  <code>{t['lower']}</code>\n"
            f"📏 % from BB: <code>{t['pct']:+.2f}%</code>\n"
            f"🕐 {now_str}\n\n"
            f"⚡ Potential LEAPS entry on <b>{t['ticker']}</b>"
        )
        send_alert(msg)
        status_cache["alerts_today"].append({"ticker": t["ticker"], "time": now_str})
        print(f"  ✅ Alert sent for {t['ticker']}")

    if not touches:
        print("  No BB touches this run.")

    status_cache["last_run"]    = now_str
    status_cache["market_open"] = is_market_open()
    status_cache["errors"]      = errors


# ── Background loop ───────────────────────────────────────────────────────────
def screener_loop():
    time.sleep(10)
    while True:
        try:
            if is_market_open():
                run_screener()
            else:
                status_cache["market_open"] = False
                print(f"[{datetime.utcnow().strftime('%H:%M')}] Market closed — skipping")
        except Exception as e:
            print(f"[Loop] Error: {e}")
        status_cache["next_run"] = f"in {CHECK_INTERVAL // 60} minutes"
        time.sleep(CHECK_INTERVAL)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return (
        "<h2>BB Screener</h2>"
        "<p>Scanning every 15 min during US market hours. Alerts → Telegram + Discord.</p>"
        "<ul>"
        f"<li>Watching <b>{len(WATCHLIST)} tickers</b></li>"
        f"<li>Last run: <b>{status_cache['last_run'] or 'not yet'}</b></li>"
        f"<li>Market open: <b>{status_cache['market_open']}</b></li>"
        f"<li>Alerts today: <b>{len(status_cache['alerts_today'])}</b></li>"
        "</ul>"
        "<p><a href='/status'>Status JSON</a> · "
        "<a href='/run'>Force scan</a> · "
        "<a href='/test'>Test alerts</a></p>"
    ), 200


@app.route("/status")
def status():
    return jsonify(status_cache), 200


@app.route("/run")
def force_run():
    threading.Thread(target=run_screener, daemon=True).start()
    return jsonify({"status": "scan started"}), 200


@app.route("/test")
def test():
    msg = (
        f"✅ <b>BB Screener Active</b>\n\n"
        f"Watching <b>{len(WATCHLIST)} tickers</b> for lower BB touches.\n"
        "Scans every 15 min during US market hours (09:30–16:00 ET).\n"
        "Alerts firing on both Telegram and Discord."
    )
    send_alert(msg)
    return jsonify({"status": "ok", "message": "Test alert sent to Telegram + Discord"}), 200


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=screener_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
