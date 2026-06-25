"""
BB Touch Screener — Intraday
------------------------------
Runs every 15 minutes during US market hours.
Checks LIVE price against lower BB calculated from daily closes.
Fires alert as soon as price touches lower BB intraday.
Filters: gap down >3%, earnings within 3 days, volume spike warning.
Sends alerts to Telegram and Discord.
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
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "900"))

GAP_DOWN_THRESHOLD    = float(os.environ.get("GAP_DOWN_THRESHOLD",    "3.0"))
EARNINGS_WINDOW_DAYS  = int(os.environ.get("EARNINGS_WINDOW_DAYS",    "3"))
VOLUME_SPIKE_MULTIPLE = float(os.environ.get("VOLUME_SPIKE_MULTIPLE", "2.5"))

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = [
    "SPY", "QQQ", "HOOD", "PLTR", "SOFI", "KTOS", "IREN",
    "INOD", "GLW", "AVGO", "IBRX", "IBM", "DRAM", "CLS", "CCJ",
    "COO", "WDC", "STX", "SNDK", "CRDO", "VRT", "CDE", "MU",
    "AA", "ADI", "AMAT", "NVDA", "AMD", "AMZN", "APH", "APP",
    "ASML", "CAT", "CCL", "FCX", "IBIT", "LRCX", "META", "NEM",
    "ORCL", "RTX", "TIGR", "TSM", "AXTI", "MRVL", "TDOC", "TER",
    "EUV", "SPCX",
]
WATCHLIST = list(dict.fromkeys(WATCHLIST))

# ── State ─────────────────────────────────────────────────────────────────────
alerted_today   = set()
last_alert_date = None
status_cache = {
    "last_run":       None,
    "alerts_today":   [],
    "filtered_today": [],
    "errors":         [],
    "market_open":    False,
}


# ── Notifications ─────────────────────────────────────────────────────────────
def send_telegram(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if not r.ok:
            print(f"[Telegram] Error: {r.text}")
    except Exception as e:
        print(f"[Telegram] Exception: {e}")


def send_discord(message: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        clean = re.sub(r"<[^>]+>", "", message)
        r = requests.post(DISCORD_WEBHOOK, json={"content": clean}, timeout=10)
        if not r.ok:
            print(f"[Discord] Error: {r.text}")
    except Exception as e:
        print(f"[Discord] Exception: {e}")


def send_alert(message: str):
    send_telegram(message)
    send_discord(message)


# ── Market hours ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    now_et = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    if now_et.weekday() >= 5:
        return False
    open_  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_ <= now_et <= close_


# ── Data fetching ─────────────────────────────────────────────────────────────
def safe_float(val, default=None):
    """Safely convert a value to float, returning default if None or invalid."""
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def get_daily_data(ticker: str, period: int = 30):
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range={period}d"
        )
        r    = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        res  = data["chart"]["result"][0]
        q    = res["indicators"]["quote"][0]
        df   = pd.DataFrame({
            "date":   pd.to_datetime(res["timestamp"], unit="s"),
            "open":   [safe_float(v) for v in q["open"]],
            "high":   [safe_float(v) for v in q["high"]],
            "low":    [safe_float(v) for v in q["low"]],
            "close":  [safe_float(v) for v in q["close"]],
            "volume": [safe_float(v, 0) for v in q["volume"]],
        }).dropna()
        return df
    except Exception as e:
        print(f"[{ticker}] Daily data error: {e}")
        return None


def get_live_price(ticker: str):
    try:
        url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
        r    = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        res  = data["chart"]["result"][0]
        meta = res["meta"]
        return {
            "price":      safe_float(meta.get("regularMarketPrice")),
            "day_low":    safe_float(meta.get("regularMarketDayLow")),
            "day_open":   safe_float(meta.get("regularMarketOpen")),
            "volume":     safe_float(meta.get("regularMarketVolume"), 0),
            "prev_close": safe_float(meta.get("chartPreviousClose") or meta.get("previousClose")),
        }
    except Exception as e:
        print(f"[{ticker}] Live price error: {e}")
        return None


# ── Bollinger Bands ───────────────────────────────────────────────────────────
def calculate_bb(df, length: int = 20, std: float = 2.0):
    if len(df) < length:
        return None
    hist    = df.iloc[:-1] if len(df) > 1 else df
    close   = hist["close"]
    basis   = close.rolling(length).mean()
    stddev  = close.rolling(length).std()
    lower   = basis - std * stddev
    avg_vol = hist["volume"].rolling(length).mean().iloc[-1]
    lower_val = safe_float(lower.iloc[-1])
    basis_val = safe_float(basis.iloc[-1])
    if lower_val is None or basis_val is None:
        return None
    return {
        "lower":   round(lower_val, 2),
        "basis":   round(basis_val, 2),
        "avg_vol": safe_float(avg_vol, 0),
    }


# ── Filters ───────────────────────────────────────────────────────────────────
def check_gap_down(day_open, prev_close):
    day_open   = safe_float(day_open)
    prev_close = safe_float(prev_close)
    if day_open is None or prev_close is None or prev_close == 0:
        return False, 0.0
    gap_pct = (day_open - prev_close) / prev_close * 100
    return gap_pct <= -GAP_DOWN_THRESHOLD, round(gap_pct, 2)


def check_earnings(ticker: str):
    try:
        url  = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=earningsHistory"
        r    = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        history = (
            data.get("quoteSummary", {})
                .get("result", [{}])[0]
                .get("earningsHistory", {})
                .get("history", [])
        )
        today = datetime.utcnow().date()
        for item in history:
            raw = item.get("quarter", {}).get("fmt")
            if raw:
                try:
                    d = datetime.strptime(raw, "%Y-%m-%d").date()
                    if 0 <= (today - d).days <= EARNINGS_WINDOW_DAYS:
                        return True, raw
                except Exception:
                    continue
    except Exception as e:
        print(f"[{ticker}] Earnings check error: {e}")
    return False, None


def check_volume_spike(volume, avg_vol):
    volume  = safe_float(volume, 0)
    avg_vol = safe_float(avg_vol, 0)
    if avg_vol == 0:
        return False, 0.0
    multiple = volume / avg_vol
    return multiple >= VOLUME_SPIKE_MULTIPLE, round(multiple, 1)


# ── Screener ──────────────────────────────────────────────────────────────────
def run_screener():
    global alerted_today, last_alert_date

    today = datetime.utcnow().date()
    if last_alert_date != today:
        alerted_today = set()
        last_alert_date = today
        status_cache["alerts_today"]   = []
        status_cache["filtered_today"] = []

    now_str = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    print(f"\n[{now_str}] Scanning {len(WATCHLIST)} tickers (intraday)…")

    touches  = []
    filtered = []
    errors   = []

    for ticker in WATCHLIST:
        if ticker in alerted_today:
            time.sleep(0.1)
            continue

        try:
            df = get_daily_data(ticker, period=32)
            if df is None or len(df) < 21:
                continue

            bb = calculate_bb(df)
            if bb is None:
                continue

            live = get_live_price(ticker)
            if live is None or live["price"] is None:
                print(f"  {ticker:6s} — no live price, skipping")
                continue

            price      = live["price"]
            day_low    = live["day_low"]
            day_open   = live["day_open"]
            volume     = live["volume"] or 0
            prev_close = live["prev_close"]

            touched = price <= bb["lower"] or (day_low is not None and day_low <= bb["lower"])

            print(f"  {ticker:6s} price={price:8.2f}  lower={bb['lower']:8.2f}  {'⚡ TOUCH' if touched else ''}")

            if not touched:
                time.sleep(0.3)
                continue

            is_gap, gap_pct = check_gap_down(day_open, prev_close)
            if is_gap:
                reason = f"gap down {gap_pct}% (threshold: -{GAP_DOWN_THRESHOLD}%)"
                print(f"    ❌ Filtered — {reason}")
                filtered.append({"ticker": ticker, "reason": reason, "time": now_str})
                status_cache["filtered_today"].append({"ticker": ticker, "reason": reason})
                alerted_today.add(ticker)
                time.sleep(0.3)
                continue

            near_earnings, earn_date = check_earnings(ticker)
            if near_earnings:
                reason = f"within {EARNINGS_WINDOW_DAYS}d of earnings ({earn_date})"
                print(f"    ❌ Filtered — {reason}")
                filtered.append({"ticker": ticker, "reason": reason, "time": now_str})
                status_cache["filtered_today"].append({"ticker": ticker, "reason": reason})
                alerted_today.add(ticker)
                time.sleep(0.3)
                continue

            is_spike, vol_multiple = check_volume_spike(volume, bb["avg_vol"])
            vol_warning = f" ⚠️ Vol {vol_multiple}x avg" if is_spike else ""
            pct_from_bb = round((price - bb["lower"]) / bb["lower"] * 100, 2)

            touches.append({
                "ticker":      ticker,
                "price":       price,
                "lower":       bb["lower"],
                "pct":         pct_from_bb,
                "gap_pct":     gap_pct,
                "vol_warning": vol_warning,
            })
            alerted_today.add(ticker)

        except Exception as e:
            errors.append(f"{ticker}: {e}")
            print(f"  {ticker} ERROR: {e}")

        time.sleep(0.3)

    for t in touches:
        msg = (
            f"🔔 <b>Lower BB Touch</b>{t['vol_warning']}\n\n"
            f"<b>{t['ticker']}</b>\n\n"
            f"💰 Price:     <code>{t['price']}</code>\n"
            f"📊 Lower BB:  <code>{t['lower']}</code>\n"
            f"📏 % from BB: <code>{t['pct']:+.2f}%</code>\n"
            f"📈 Day gap:   <code>{t['gap_pct']:+.2f}%</code>\n"
            f"🕐 {now_str}\n\n"
            f"✅ Passed earnings &amp; gap filters\n"
            f"⚡ Potential LEAPS entry on <b>{t['ticker']}</b>"
        )
        send_alert(msg)
        status_cache["alerts_today"].append({"ticker": t["ticker"], "time": now_str, "price": t["price"]})
        print(f"  ✅ Alert sent for {t['ticker']} @ {t['price']}")

    if filtered:
        reasons = "\n".join(f"• {f['ticker']}: {f['reason']}" for f in filtered)
        send_alert(
            f"🚫 <b>BB Touch — Filtered Out</b>\n\n{reasons}\n\n"
            f"<i>Suppressed: earnings proximity or gap down</i>"
        )

    if not touches and not filtered:
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
        "<h2>BB Screener — Intraday</h2>"
        "<p>Checks live price vs lower BB every 15 min during market hours.</p>"
        "<ul>"
        f"<li>Watching <b>{len(WATCHLIST)} tickers</b></li>"
        f"<li>Last run: <b>{status_cache['last_run'] or 'not yet'}</b></li>"
        f"<li>Market open: <b>{status_cache['market_open']}</b></li>"
        f"<li>Alerts today: <b>{len(status_cache['alerts_today'])}</b></li>"
        f"<li>Filtered today: <b>{len(status_cache['filtered_today'])}</b></li>"
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
    send_alert(
        f"✅ <b>BB Screener Active — Intraday Mode</b>\n\n"
        f"Watching <b>{len(WATCHLIST)} tickers</b>.\n"
        f"Fires alert as soon as live price touches lower BB.\n"
        f"Scans every 15 min during market hours (09:30–16:00 ET)."
    )
    return jsonify({"status": "ok"}), 200


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=screener_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
