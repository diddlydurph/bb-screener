"""
BB Touch Screener
------------------
Runs every 15 minutes during US market hours.
Checks all watchlist tickers against their lower Bollinger Band (20, 2).
Sends Telegram alert when price touches or crosses below lower BB.
No TradingView needed.
"""

import os
import time
import threading
import requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID",   "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))  # 15 min

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = [
    "NXT", "SPY", "QQQ", "HOOD", "PLTR", "SOFI", "KTOS", "IREN",
    "INOD", "GLW", "AVGO", "IBRX", "IBM", "DRAM", "CLS", "CCJ",
    "COO", "WDC", "STX", "SNDK", "CRDO", "VRT", "CDE", "MU",
    "AA", "ADI", "AMAT", "NVDA", "AMD", "AMZN", "APH", "APP",
    "ASML", "CAT", "CCL", "FCX", "IBIT", "LRCX", "META", "NEM",
    "ORCL", "RTX", "TIGR", "TSM", "AXTI", "MRVL", "TDOC", "TER",
    "SFII", "EUV", "SPCX", "GLW",
]
WATCHLIST = list(dict.fromkeys(WATCHLIST))  # deduplicate

# ── State ─────────────────────────────────────────────────────────────────────
# Tracks which tickers already alerted today to avoid repeat spam
alerted_today = set()
last_alert_date = None
status_cache = {
    "last_run":     None,
    "next_run":     None,
    "alerts_today": [],
    "errors":       [],
    "market_open":  False,
}


# ── Telegram ──────────────────────────────────────────────────────────────────
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


# ── Market hours check ────────────────────────────────────────────────────────
def is_market_open() -> bool:
    """Returns True if US market is currently open (Mon-Fri 09:30-16:00 ET)."""
    now_et = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    if now_et.weekday() >= 5:  # Saturday/Sunday
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et <= market_close


# ── Price data ────────────────────────────────────────────────────────────────
def get_daily_prices(ticker: str, period: int = 30) -> pd.DataFrame | None:
    """
    Fetch last `period` days of daily OHLCV from Yahoo Finance.
    Returns DataFrame with columns: open, high, low, close, volume
    """
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range={period}d"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quotes     = result["indicators"]["quote"][0]

        df = pd.DataFrame({
            "date":   pd.to_datetime(timestamps, unit="s"),
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


# ── Bollinger Band calculation ────────────────────────────────────────────────
def calculate_bb(df: pd.DataFrame, length: int = 20, std: float = 2.0) -> dict | None:
    """Calculate Bollinger Bands. Returns latest values."""
    if len(df) < length:
        return None
    close  = df["close"]
    basis  = close.rolling(length).mean()
    stddev = close.rolling(length).std()
    upper  = basis + std * stddev
    lower  = basis - std * stddev

    return {
        "basis":     round(basis.iloc[-1],  2),
        "upper":     round(upper.iloc[-1],  2),
        "lower":     round(lower.iloc[-1],  2),
        "close":     round(close.iloc[-1],  2),
        "low":       round(df["low"].iloc[-1], 2),
        "pct_from_lower": round(
            (close.iloc[-1] - lower.iloc[-1]) / lower.iloc[-1] * 100, 2
        ),
    }


# ── Screener ──────────────────────────────────────────────────────────────────
def run_screener():
    global alerted_today, last_alert_date

    # Reset daily alerts at midnight
    today = datetime.utcnow().date()
    if last_alert_date != today:
        alerted_today    = set()
        last_alert_date  = today
        status_cache["alerts_today"] = []

    now_str = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    print(f"\n[{now_str}] Running BB screener on {len(WATCHLIST)} tickers…")

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

            # Touch condition: today's LOW <= lower BB
            touched = bb["low"] <= bb["lower"]

            print(f"  {ticker:6s} close={bb['close']:8.2f}  lower={bb['lower']:8.2f}  "
                  f"low={bb['low']:8.2f}  {'⚡ TOUCH' if touched else ''}")

            if touched and ticker not in alerted_today:
                touches.append({
                    "ticker": ticker,
                    "close":  bb["close"],
                    "low":    bb["low"],
                    "lower":  bb["lower"],
                    "pct":    bb["pct_from_lower"],
                })
                alerted_today.add(ticker)

        except Exception as e:
            errors.append(f"{ticker}: {e}")
            print(f"  {ticker} ERROR: {e}")

        time.sleep(0.3)  # be polite to Yahoo

    # Send alerts
    for t in touches:
        msg = (
            f"🔔 <b>Lower BB Touch</b>\n\n"
            f"<b>{t['ticker']}</b>\n\n"
            f"💰 Close:    <code>{t['close']}</code>\n"
            f"📉 Low:      <code>{t['low']}</code>\n"
            f"📊 Lower BB: <code>{t['lower']}</code>\n"
            f"📏 % from BB: <code>{t['pct']:+.2f}%</code>\n"
            f"🕐 {now_str}\n\n"
            f"⚡ Potential LEAPS entry on <b>{t['ticker']}</b>"
        )
        send_telegram(msg)
        status_cache["alerts_today"].append({
            "ticker": t["ticker"],
            "time":   now_str,
            "close":  t["close"],
            "lower":  t["lower"],
        })
        print(f"  ✅ Alert sent for {t['ticker']}")

    if not touches:
        print("  No BB touches this run.")

    status_cache["last_run"]    = now_str
    status_cache["market_open"] = is_market_open()
    status_cache["errors"]      = errors


# ── Background loop ───────────────────────────────────────────────────────────
def screener_loop():
    # Wait 10s on startup before first run
    time.sleep(10)
    while True:
        try:
            if is_market_open():
                status_cache["market_open"] = True
                run_screener()
            else:
                status_cache["market_open"] = False
                print(f"[{datetime.utcnow().strftime('%H:%M')}] Market closed — skipping scan")
        except Exception as e:
            print(f"[Loop] Error: {e}")

        # Schedule next run
        next_run = datetime.utcnow().__class__.utcnow()
        status_cache["next_run"] = f"in {CHECK_INTERVAL // 60} minutes"
        time.sleep(CHECK_INTERVAL)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return (
        "<h2>BB Screener</h2>"
        "<p>Running every 15 minutes during US market hours.</p>"
        "<ul>"
        f"<li>Watching <b>{len(WATCHLIST)} tickers</b></li>"
        f"<li>Last run: <b>{status_cache['last_run'] or 'not yet'}</b></li>"
        f"<li>Market open: <b>{status_cache['market_open']}</b></li>"
        f"<li>Alerts today: <b>{len(status_cache['alerts_today'])}</b></li>"
        "</ul>"
        "<p><a href='/status'>Full status JSON</a> · "
        "<a href='/run'>Force a scan now</a> · "
        "<a href='/test'>Test Telegram</a></p>"
    ), 200


@app.route("/status")
def status():
    return jsonify(status_cache), 200


@app.route("/run")
def force_run():
    """Force an immediate scan — useful for testing."""
    threading.Thread(target=run_screener, daemon=True).start()
    return jsonify({"status": "scan started"}), 200


@app.route("/test")
def test():
    send_telegram(
        "✅ <b>BB Screener Active</b>\n\n"
        f"Watching <b>{len(WATCHLIST)} tickers</b> for lower BB touches.\n"
        "Scans run every 15 minutes during US market hours (09:30–16:00 ET)."
    )
    return jsonify({"status": "ok", "message": "Test alert sent"}), 200


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=screener_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
