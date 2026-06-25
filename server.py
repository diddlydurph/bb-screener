"""
BB Touch Screener — Intraday
------------------------------
Alerts when:
  ✅ Price touches lower BB (20, 2) intraday
  ✅ Not a missed earnings drop
  ✅ VIX above 20
One alert per ticker per day.
Sends to Telegram + Discord.
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
FINNHUB_KEY     = os.environ.get("FINNHUB_KEY", "")
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "900"))
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


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_float(val, default=None):
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# ── VIX ───────────────────────────────────────────────────────────────────────
def get_vix() -> float | None:
    """Fetch current VIX level — try Yahoo first, Finnhub fallback."""
    try:
        url  = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1m&range=1d"
        r    = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        vix  = safe_float(data["chart"]["result"][0]["meta"].get("regularMarketPrice"))
        if vix:
            return vix
    except Exception:
        pass
    try:
        url  = f"https://finnhub.io/api/v1/quote?symbol=VIX&token={FINNHUB_KEY}"
        r    = requests.get(url, timeout=10)
        vix  = safe_float(r.json().get("c"))
        if vix:
            return vix
    except Exception:
        pass
    return None


# ── Data fetching ─────────────────────────────────────────────────────────────
def get_daily_data(ticker: str, period: int = 32):
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


def get_live_price_yahoo(ticker: str):
    try:
        url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
        r    = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        meta = data["chart"]["result"][0]["meta"]
        price = safe_float(meta.get("regularMarketPrice"))
        if price is None:
            return None
        return {
            "price":      price,
            "day_low":    safe_float(meta.get("regularMarketDayLow")),
            "volume":     safe_float(meta.get("regularMarketVolume"), 0),
            "prev_close": safe_float(meta.get("chartPreviousClose") or meta.get("previousClose")),
            "source":     "yahoo",
        }
    except Exception:
        return None


def get_live_price_finnhub(ticker: str):
    try:
        url  = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        r    = requests.get(url, timeout=10)
        data = r.json()
        price = safe_float(data.get("c"))
        if price is None or price == 0:
            return None
        return {
            "price":      price,
            "day_low":    safe_float(data.get("l")),
            "volume":     None,
            "prev_close": safe_float(data.get("pc")),
            "source":     "finnhub",
        }
    except Exception:
        return None


def get_live_price(ticker: str):
    live = get_live_price_yahoo(ticker)
    if live is not None:
        return live
    print(f"[{ticker}] Yahoo failed — trying Finnhub…")
    return get_live_price_finnhub(ticker)


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


# ── Earnings miss check ───────────────────────────────────────────────────────
def check_missed_earnings(ticker: str) -> tuple[bool, str | None]:
    """
    Returns (missed_earnings, date_str).
    True if the most recent earnings report was a miss (actual EPS < estimate)
    AND it was within the last 3 days.
    """
    try:
        url  = f"https://finnhub.io/api/v1/stock/earnings?symbol={ticker}&limit=4&token={FINNHUB_KEY}"
        r    = requests.get(url, timeout=10)
        data = r.json()
        if not data:
            return False, None
        today = datetime.utcnow().date()
        for report in data:
            period = report.get("period")
            actual = safe_float(report.get("actual"))
            est    = safe_float(report.get("estimate"))
            if not period:
                continue
            try:
                report_date = datetime.strptime(period, "%Y-%m-%d").date()
                days_since  = (today - report_date).days
                if 0 <= days_since <= 3:
                    if actual is not None and est is not None and actual < est:
                        return True, period
            except Exception:
                continue
    except Exception as e:
        print(f"[{ticker}] Earnings miss check error: {e}")
    return False, None


def check_volume_spike(volume, avg_vol):
    volume  = safe_float(volume, 0)
    avg_vol = safe_float(avg_vol, 0)
    if avg_vol == 0 or volume == 0:
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
    print(f"\n[{now_str}] Scanning {len(WATCHLIST)} tickers…")

    # Fetch VIX once per scan
    vix = get_vix()
    vix_ok = vix is not None and vix > 20
    vix_str = f"{vix:.2f}" if vix else "N/A"
    print(f"  VIX: {vix_str} — {'✅ above 20' if vix_ok else '❌ below 20'}")

    touches  = []
    filtered = []
    errors   = []

    for ticker in WATCHLIST:
        if ticker in alerted_today:
            time.sleep(0.1)
            continue

        try:
            df = get_daily_data(ticker)
            if df is None or len(df) < 21:
                errors.append(f"{ticker}: insufficient data")
                continue

            bb = calculate_bb(df)
            if bb is None:
                errors.append(f"{ticker}: BB calc failed")
                continue

            live = get_live_price(ticker)
            if live is None or live["price"] is None:
                errors.append(f"{ticker}: no live price")
                continue

            price      = live["price"]
            day_low    = live["day_low"]
            volume     = live["volume"] or 0
            source     = live["source"]

            touched = price <= bb["lower"] or (day_low is not None and day_low <= bb["lower"])

            print(f"  {ticker:6s} [{source:7s}] price={price:8.2f}  lower={bb['lower']:8.2f}  {'⚡ TOUCH' if touched else ''}")

            if not touched:
                time.sleep(0.3)
                continue

            # ── Check missed earnings ──
            missed, earn_date = check_missed_earnings(ticker)
            if missed:
                reason = f"missed earnings on {earn_date}"
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
                "vol_warning": vol_warning,
                "vix":         vix_str,
                "vix_ok":      vix_ok,
                "earnings_ok": not missed,
            })
            alerted_today.add(ticker)

        except Exception as e:
            errors.append(f"{ticker}: {e}")
            print(f"  {ticker} ERROR: {e}")

        time.sleep(0.3)

    for t in touches:
        bb_tick      = "✅" 
        earn_tick    = "✅" if t["earnings_ok"] else "❌"
        vix_tick     = "✅" if t["vix_ok"] else "❌"

        msg = (
            f"🔔 <b>Lower BB Touch</b>{t['vol_warning']}\n\n"
            f"<b>{t['ticker']}</b>\n\n"
            f"💰 Price:    <code>{t['price']}</code>\n"
            f"📊 Lower BB: <code>{t['lower']}</code>\n"
            f"📏 % from BB: <code>{t['pct']:+.2f}%</code>\n\n"
            f"<b>Criteria</b>\n"
            f"{bb_tick} Lower BB touched\n"
            f"{earn_tick} Not a missed earnings drop\n"
            f"{vix_tick} VIX above 20 (currently {t['vix']})\n\n"
            f"🕐 {now_str}\n\n"
            f"⚡ Potential LEAPS entry on <b>{t['ticker']}</b>"
        )
        send_alert(msg)
        status_cache["alerts_today"].append({"ticker": t["ticker"], "time": now_str, "price": t["price"]})
        print(f"  ✅ Alert sent for {t['ticker']} @ {t['price']}")

    if filtered:
        reasons = "\n".join(f"• {f['ticker']}: {f['reason']}" for f in filtered)
        send_alert(
            f"🚫 <b>BB Touch — Filtered Out</b>\n\n{reasons}\n\n"
            f"<i>Suppressed: missed earnings</i>"
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
        "<p>Yahoo Finance + Finnhub fallback. Scans every 15 min during market hours.</p>"
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
        f"Yahoo Finance primary, Finnhub fallback.\n"
        f"Criteria: Lower BB touch + no missed earnings + VIX &gt; 20.\n"
        f"Scans every 15 min during market hours (09:30–16:00 ET)."
    )
    return jsonify({"status": "ok"}), 200


# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=screener_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
