"""
BB Touch Screener — Intraday with LEAPS Suggestions
-----------------------------------------------------
Alerts when:
  ✅ Price touches lower BB (20, 2) intraday
     (or mid-BB if beta > 2.2)
  ✅ Not the result of a missed earnings drop
  ✅ VIX >= 15

One alert per ticker per day.
Sends to Telegram + Discord.
"""

import os
import re
import time
import threading
import requests
import pandas as pd
from datetime import datetime, timezone, date, timedelta
from flask import Flask, jsonify

app = Flask(__name__)

TG_BOT_TOKEN    = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID      = os.environ.get("TG_CHAT_ID",   "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
FINNHUB_KEY     = os.environ.get("FINNHUB_KEY", "")
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "900"))
VOLUME_SPIKE_MULTIPLE = float(os.environ.get("VOLUME_SPIKE_MULTIPLE", "2.5"))
VIX_THRESHOLD   = float(os.environ.get("VIX_THRESHOLD", "15.0"))
HIGH_BETA_THRESHOLD = float(os.environ.get("HIGH_BETA_THRESHOLD", "2.2"))
EARNINGS_AVOID_DAYS = int(os.environ.get("EARNINGS_AVOID_DAYS", "2"))
MIN_DTE         = int(os.environ.get("MIN_DTE", "400"))
MIN_DELTA       = float(os.environ.get("MIN_DELTA", "0.70"))

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

alerted_today   = set()
last_alert_date = None
status_cache = {
    "last_run": None, "alerts_today": [], "filtered_today": [],
    "errors": [], "market_open": False,
}
HEADERS = {"User-Agent": "Mozilla/5.0"}


def send_telegram(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10)
        if not r.ok:
            print(f"[Telegram] {r.text}")
    except Exception as e:
        print(f"[Telegram] {e}")

def send_discord(message):
    if not DISCORD_WEBHOOK:
        return
    try:
        clean = re.sub(r"<[^>]+>", "", message)
        r = requests.post(DISCORD_WEBHOOK, json={"content": clean}, timeout=10)
        if not r.ok:
            print(f"[Discord] {r.text}")
    except Exception as e:
        print(f"[Discord] {e}")

def send_alert(message):
    send_telegram(message)
    send_discord(message)

def is_market_open():
    now_et = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    return (now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            <= now_et <=
            now_et.replace(hour=16, minute=0, second=0, microsecond=0))

def safe_float(val, default=None):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def get_vix():
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1m&range=1d",
            headers=HEADERS, timeout=10)
        v = safe_float(r.json()["chart"]["result"][0]["meta"].get("regularMarketPrice"))
        if v:
            return v
    except Exception:
        pass
    if FINNHUB_KEY:
        try:
            r = requests.get(f"https://finnhub.io/api/v1/quote?symbol=VIX&token={FINNHUB_KEY}", timeout=10)
            v = safe_float(r.json().get("c"))
            if v:
                return v
        except Exception:
            pass
    return None

def get_beta(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=defaultKeyStatistics"
        r   = requests.get(url, headers=HEADERS, timeout=10)
        b   = r.json()["quoteSummary"]["result"][0]["defaultKeyStatistics"].get("beta", {})
        return safe_float(b.get("raw") if isinstance(b, dict) else b)
    except Exception:
        return None

def get_daily_data(ticker, period=32):
    try:
        url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={period}d"
        r    = requests.get(url, headers=HEADERS, timeout=10)
        res  = r.json()["chart"]["result"][0]
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
        print(f"[{ticker}] Daily: {e}")
        return None

def get_live_price(ticker):
    try:
        url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
        meta = requests.get(url, headers=HEADERS, timeout=10).json()["chart"]["result"][0]["meta"]
        price = safe_float(meta.get("regularMarketPrice"))
        if price:
            return {"price": price, "day_low": safe_float(meta.get("regularMarketDayLow")),
                    "volume": safe_float(meta.get("regularMarketVolume"), 0),
                    "prev_close": safe_float(meta.get("chartPreviousClose") or meta.get("previousClose")),
                    "source": "yahoo"}
    except Exception:
        pass
    if FINNHUB_KEY:
        try:
            data  = requests.get(f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}", timeout=10).json()
            price = safe_float(data.get("c"))
            if price and price > 0:
                return {"price": price, "day_low": safe_float(data.get("l")),
                        "volume": None, "prev_close": safe_float(data.get("pc")), "source": "finnhub"}
        except Exception:
            pass
    return None

def calculate_bb(df, length=20, std=2.0):
    if len(df) < length:
        return None
    hist    = df.iloc[:-1] if len(df) > 1 else df
    close   = hist["close"]
    basis   = close.rolling(length).mean()
    stddev  = close.rolling(length).std()
    lower   = basis - std * stddev
    avg_vol = hist["volume"].rolling(length).mean().iloc[-1]
    lv = safe_float(lower.iloc[-1])
    bv = safe_float(basis.iloc[-1])
    if lv is None or bv is None:
        return None
    return {"lower": round(lv, 2), "basis": round(bv, 2), "avg_vol": safe_float(avg_vol, 0)}

def check_missed_earnings(ticker):
    if not FINNHUB_KEY:
        return False, None
    try:
        data    = requests.get(f"https://finnhub.io/api/v1/stock/earnings?symbol={ticker}&limit=4&token={FINNHUB_KEY}", timeout=10).json()
        today_d = datetime.utcnow().date()
        for r in data:
            period = r.get("period")
            actual = safe_float(r.get("actual"))
            est    = safe_float(r.get("estimate"))
            if not period:
                continue
            try:
                rdate = datetime.strptime(period, "%Y-%m-%d").date()
                if 0 <= (today_d - rdate).days <= 3 and actual is not None and est is not None and actual < est:
                    return True, period
            except Exception:
                continue
    except Exception as e:
        print(f"[{ticker}] Missed earn: {e}")
    return False, None


def get_leaps_suggestion(ticker, current_price):
    try:
        import math

        def estimate_delta(S, K, T, iv):
            """Approximate delta using Black-Scholes d1."""
            if iv is None or iv <= 0 or T <= 0:
                return None
            try:
                d1 = (math.log(S / K) + (0.05 + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
                # Approximate N(d1) using logistic function
                delta = 1 / (1 + math.exp(-1.7 * d1))
                return round(delta, 2)
            except Exception:
                return None

        today_d    = datetime.utcnow().date()
        min_expiry = today_d + timedelta(days=MIN_DTE)
        url  = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}"
        data = requests.get(url, headers=HEADERS, timeout=10).json()
        expirations = data.get("optionChain", {}).get("result", [{}])[0].get("expirationDates", [])
        best = None

        for exp_ts in expirations:
            exp_date = datetime.utcfromtimestamp(exp_ts).date()
            if exp_date < min_expiry:
                continue
            dte  = (exp_date - today_d).days
            T    = dte / 365.0  # time in years

            url2  = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}?date={exp_ts}"
            data2 = requests.get(url2, headers=HEADERS, timeout=10).json()
            calls = (data2.get("optionChain", {}).get("result", [{}])[0]
                         .get("options", [{}])[0].get("calls", []))

            for call in calls:
                greeks = call.get("greeks") or {}
                strike = safe_float(call.get("strike"))
                bid    = safe_float(call.get("bid"))
                ask    = safe_float(call.get("ask"))
                iv     = safe_float(call.get("impliedVolatility"))
                if strike is None:
                    continue

                # Try Yahoo delta first, fall back to BS estimate
                delta = safe_float(call.get("delta") or greeks.get("delta"))
                if delta is None and iv and iv > 0:
                    delta = estimate_delta(current_price, strike, T, iv)

                # If still no delta, skip deep OTM strikes (rough filter)
                if delta is None:
                    if strike > current_price * 1.15:
                        continue
                    delta = 0.70  # assume ITM strikes qualify

                if delta < MIN_DELTA:
                    continue

                mid = round((bid + ask) / 2, 2) if bid and ask else None

                if best is None or delta > best["delta"]:
                    best = {
                        "strike":  strike,
                        "expiry":  exp_date.strftime("%d %b %Y"),
                        "dte":     dte,
                        "delta":   delta,
                        "bid":     bid,
                        "ask":     ask,
                        "mid":     mid,
                        "iv":      round(iv * 100, 1) if iv else None,
                    }

            if best:
                break
            time.sleep(0.3)

        return best

    except Exception as e:
        print(f"[{ticker}] LEAPS: {e}")
        return None

def check_volume_spike(volume, avg_vol):
    v, a = safe_float(volume, 0), safe_float(avg_vol, 0)
    if a == 0 or v == 0:
        return False, 0.0
    m = v / a
    return m >= VOLUME_SPIKE_MULTIPLE, round(m, 1)

def run_screener():
    global alerted_today, last_alert_date
    today_d = datetime.utcnow().date()
    if last_alert_date != today_d:
        alerted_today = set()
        last_alert_date = today_d
        status_cache["alerts_today"] = []
        status_cache["filtered_today"] = []

    now_str = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    print(f"\n[{now_str}] Scanning {len(WATCHLIST)} tickers…")

    vix     = get_vix()
    vix_ok  = vix is not None and vix >= VIX_THRESHOLD
    vix_str = f"{vix:.2f}" if vix else "N/A"
    print(f"  VIX: {vix_str} ({'OK' if vix_ok else 'LOW'})")

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
                errors.append(f"{ticker}: no data")
                continue
            bb = calculate_bb(df)
            if bb is None:
                errors.append(f"{ticker}: BB failed")
                continue
            live = get_live_price(ticker)
            if live is None or live["price"] is None:
                errors.append(f"{ticker}: no price")
                continue

            price      = live["price"]
            day_low    = live["day_low"]
            volume     = live["volume"] or 0
            source     = live["source"]
            beta       = get_beta(ticker)
            high_beta  = beta is not None and beta > HIGH_BETA_THRESHOLD
            lower_touch = price <= bb["lower"] or (day_low and day_low <= bb["lower"])
            mid_touch   = price <= bb["basis"] or (day_low and day_low <= bb["basis"])
            touched     = mid_touch if high_beta else lower_touch
            entry_type  = "mid-BB (high β)" if high_beta else "lower BB"
            beta_disp   = f"{beta:.2f}" if beta else "N/A"

            print(f"  {ticker:6s} [{source}] β={beta_disp} price={price:.2f} lower={bb['lower']:.2f} {'⚡' if touched else ''}")

            if not touched:
                time.sleep(0.3)
                continue

            missed, miss_date = check_missed_earnings(ticker)
            if missed:
                reason = f"missed earnings {miss_date}"
                filtered.append({"ticker": ticker, "reason": reason, "time": now_str})
                status_cache["filtered_today"].append({"ticker": ticker, "reason": reason})
                alerted_today.add(ticker)
                time.sleep(0.3)
                continue

            is_spike, vol_mult = check_volume_spike(volume, bb["avg_vol"])
            vol_warn = f" ⚠️ Vol {vol_mult}x avg" if is_spike else ""
            pct      = round((price - bb["lower"]) / bb["lower"] * 100, 2)

            print(f"    📊 Fetching LEAPS for {ticker}…")
            leaps = get_leaps_suggestion(ticker, price)

            touches.append({
                "ticker": ticker, "price": price, "lower": bb["lower"],
                "basis": bb["basis"], "pct": pct, "vol_warn": vol_warn,
                "vix": vix_str, "vix_ok": vix_ok, "entry_type": entry_type,
                "beta": beta_disp, "leaps": leaps,
            })
            alerted_today.add(ticker)

        except Exception as e:
            errors.append(f"{ticker}: {e}")
            print(f"  {ticker} ERROR: {e}")
        time.sleep(0.3)

    for t in touches:
        vix_tick = "✅" if t["vix_ok"] else "❌"
        if t["leaps"]:
            l = t["leaps"]
            mid_str = f"${l['mid']:.2f}" if l["mid"] else "—"
            iv_str  = f"{l['iv']}%" if l["iv"] else "—"
            leaps_block = (
                f"\n\n📋 <b>Suggested LEAPS Entry</b>\n"
                f"Strike:  <code>{l['strike']}</code>\n"
                f"Expiry:  <code>{l['expiry']}</code> ({l['dte']} DTE)\n"
                f"Delta:   <code>{l['delta']}</code>\n"
                f"Mid:     <code>{mid_str}</code>\n"
                f"Bid/Ask: <code>${l['bid']} / ${l['ask']}</code>\n"
                f"IV:      <code>{iv_str}</code>"
            )
        else:
            leaps_block = "\n\n📋 <b>LEAPS:</b> No qualifying contract found (400+ DTE, 70+ delta)"

        msg = (
            f"🔔 <b>BB Touch Alert</b>{t['vol_warn']}\n\n"
            f"<b>{t['ticker']}</b>  β={t['beta']}\n"
            f"Entry: <i>{t['entry_type']}</i>\n\n"
            f"💰 Price:    <code>{t['price']}</code>\n"
            f"📉 Lower BB: <code>{t['lower']}</code>\n"
            f"〰️ Mid BB:   <code>{t['basis']}</code>\n"
            f"📏 % from lower: <code>{t['pct']:+.2f}%</code>\n\n"
            f"<b>Criteria</b>\n"
            f"✅ Lower BB touched\n"
            f"✅ Not a missed earnings drop\n"
            f"{vix_tick} VIX {'>=' if t['vix_ok'] else '<'} {VIX_THRESHOLD} (currently {t['vix']})"
            f"{leaps_block}\n\n"
            f"🕐 {now_str}"
        )
        send_alert(msg)
        status_cache["alerts_today"].append({"ticker": t["ticker"], "time": now_str, "price": t["price"]})
        print(f"  ✅ Alert sent for {t['ticker']}")

    if filtered:
        reasons = "\n".join(f"• {f['ticker']}: {f['reason']}" for f in filtered)
        send_alert(f"🚫 <b>BB Touch — Filtered Out</b>\n\n{reasons}")

    if not touches and not filtered:
        print("  No BB touches this run.")

    status_cache["last_run"]    = now_str
    status_cache["market_open"] = is_market_open()
    status_cache["errors"]      = errors

def screener_loop():
    time.sleep(10)
    while True:
        try:
            if is_market_open():
                run_screener()
            else:
                status_cache["market_open"] = False
                print(f"[{datetime.utcnow().strftime('%H:%M')}] Market closed")
        except Exception as e:
            print(f"[Loop] {e}")
        time.sleep(CHECK_INTERVAL)

@app.route("/")
def index():
    return (
        "<h2>BB Screener — Intraday + LEAPS</h2>"
        f"<p>Watching <b>{len(WATCHLIST)} tickers</b> · "
        f"Last run: <b>{status_cache['last_run'] or 'not yet'}</b> · "
        f"Market open: <b>{status_cache['market_open']}</b></p>"
        f"<p>Alerts today: <b>{len(status_cache['alerts_today'])}</b> · "
        f"Filtered: <b>{len(status_cache['filtered_today'])}</b></p>"
        "<p><a href='/status'>Status</a> · <a href='/run'>Force scan</a> · <a href='/test'>Test</a></p>"
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
        f"✅ <b>BB Screener — Intraday + LEAPS</b>\n\n"
        f"Watching <b>{len(WATCHLIST)} tickers</b>.\n"
        f"VIX threshold: {VIX_THRESHOLD} · High beta threshold: {HIGH_BETA_THRESHOLD}\n"
        f"LEAPS: {MIN_DTE}+ DTE, {int(MIN_DELTA*100)}+ delta\n"
        f"Earnings avoid window: {EARNINGS_AVOID_DAYS} days\n"
        f"Scans every 15 min · 09:30–16:00 ET"
    )
    return jsonify({"status": "ok"}), 200

threading.Thread(target=screener_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
