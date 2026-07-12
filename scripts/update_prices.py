#!/usr/bin/env python3
"""Skier Capital Daily Valuation — per-market price checks.

Each market is checked ~1 hour after its own close:
  ASX  — 5:00pm Sydney      (close 4:00pm)
  LSE  — 5:30pm London      (close 4:30pm)
  NYSE — 5:00pm New York    (close 4:00pm)

The GitHub Actions cron fires at both possible UTC times for each market
(to cover daylight saving in each country); this script checks the market's
own local clock and only updates the markets that are actually due.
Manual runs (workflow_dispatch) update everything.

Prices for markets not being checked are carried over from the previous
data.json, so every run recomputes all four tiles. The history (running
total) gets one entry per Sydney weekday, finalised by the last run of
that Sydney day.
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

SYDNEY = ZoneInfo("Australia/Sydney")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_PATH = os.path.join(ROOT, "holdings.json")
DATA_PATH = os.path.join(ROOT, "data.json")

MARKET_SUFFIX = {"AU": ".AX", "UK": ".L", "US": ""}
MARKET_CCY = {"AU": "AUD", "UK": "GBP", "US": "USD"}
MARKET_TZ = {
    "AU": ZoneInfo("Australia/Sydney"),
    "UK": ZoneInfo("Europe/London"),
    "US": ZoneInfo("America/New_York"),
}
# The local hour (market's own clock) in which its post-close check runs.
CHECK_HOUR = {"AU": 17, "UK": 17, "US": 17}  # LSE run fires at :30 past


def due_markets():
    """Which markets should this run update?"""
    forced = os.environ.get("MARKETS", "").strip()
    if forced:
        return [m for m in forced.upper().split(",") if m in MARKET_TZ]
    if os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return ["AU", "US", "UK"]  # manual runs refresh everything
    due = []
    for m, tz in MARKET_TZ.items():
        now = datetime.now(tz)
        if now.hour == CHECK_HOUR[m] and now.weekday() < 5:
            due.append(m)
    return due


def yahoo_symbol(h):
    return h["code"].upper().strip() + MARKET_SUFFIX[h["market"]]


def fetch_quote(symbol):
    """Return (last_close, currency, name) for a Yahoo symbol."""
    t = yf.Ticker(symbol)
    hist = t.history(period="5d", auto_adjust=False)
    if hist.empty:
        raise ValueError(f"no price data for {symbol}")
    price = float(hist["Close"].dropna().iloc[-1])
    currency, name = None, None
    try:
        fi = t.fast_info
        currency = fi.get("currency")
    except Exception:
        pass
    try:
        info = t.get_info()
        name = info.get("longName") or info.get("shortName")
        currency = currency or info.get("currency")
    except Exception:
        pass
    # LSE quotes arrive in pence (GBp/GBX) — normalise to pounds.
    if currency and currency.lower() in ("gbp", "gbx") and currency != "GBP":
        price /= 100.0
        currency = "GBP"
    elif currency is None and symbol.endswith(".L"):
        price /= 100.0
        currency = "GBP"
    return price, currency, name


def fetch_fx():
    fx = {}
    for pair, symbol in (("USDAUD", "USDAUD=X"), ("GBPAUD", "GBPAUD=X")):
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            raise ValueError(f"no FX data for {symbol}")
        fx[pair] = float(hist["Close"].dropna().iloc[-1])
    return fx


def main():
    due = due_markets()
    if not due:
        print("No market is due at this time — nothing to do.")
        sys.exit(0)
    print("Updating markets:", ", ".join(due))

    with open(HOLDINGS_PATH) as f:
        holdings = json.load(f).get("holdings", [])

    old_prices, history, checked = {}, [], {}
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH) as f:
                prev = json.load(f)
            old_prices = prev.get("prices", {})
            history = prev.get("history", [])
            checked = prev.get("checked", {})
        except Exception:
            pass

    data = {"prices": {}, "fx": {}, "tiles": {}, "checked": checked,
            "history": history, "errors": []}

    fx = fetch_fx()
    data["fx"] = {k: round(v, 6) for k, v in fx.items()}

    now_syd = datetime.now(SYDNEY)
    subtotals = {"AU": 0.0, "US": 0.0, "UK": 0.0}
    for h in holdings:
        symbol = yahoo_symbol(h)
        # Fetch when the market is due, or when we hold no price yet
        # (e.g. a share added since the last check of its market).
        if h["market"] in due or symbol not in old_prices:
            try:
                price, ccy, name = fetch_quote(symbol)
            except Exception as e:
                print(f"WARN {symbol}: {e}")
                data["errors"].append(f"{symbol}: {e}")
                if symbol in old_prices:  # keep the stale price over nothing
                    data["prices"][symbol] = old_prices[symbol]
                    subtotals[h["market"]] += old_prices[symbol]["price"] * float(h["quantity"])
                continue
            expected = MARKET_CCY[h["market"]]
            if ccy and ccy != expected:
                data["errors"].append(f"{symbol}: quoted in {ccy}, expected {expected}")
            data["prices"][symbol] = {
                "price": round(price, 4),
                "currency": expected,
                "name": name or h["code"].upper(),
            }
        else:
            data["prices"][symbol] = old_prices[symbol]
        subtotals[h["market"]] += data["prices"][symbol]["price"] * float(h["quantity"])

    for m in due:
        data["checked"][m] = now_syd.isoformat(timespec="seconds")

    to_aud = {"AU": 1.0, "US": fx["USDAUD"], "UK": fx["GBPAUD"]}
    total_aud = sum(subtotals[m] * to_aud[m] for m in subtotals)

    data["tiles"] = {
        "AU": round(subtotals["AU"], 2),
        "US": round(subtotals["US"], 2),
        "UK": round(subtotals["UK"], 2),
        "TOTAL_AUD": round(total_aud, 2),
    }

    # Every check upserts the history entry for the current Sydney date, so
    # the chart moves ~1h after each market's close regardless of the Sydney
    # day of week. NYSE/LSE Friday closes land Saturday morning Sydney and
    # produce a Saturday point that completes the global trading week.
    entry = {
        "date": now_syd.strftime("%Y-%m-%d"),
        "au": data["tiles"]["AU"],
        "us": data["tiles"]["US"],
        "uk": data["tiles"]["UK"],
        "total_aud": data["tiles"]["TOTAL_AUD"],
        "usdaud": data["fx"]["USDAUD"],
        "gbpaud": data["fx"]["GBPAUD"],
    }
    data["history"] = [e for e in data["history"] if e["date"] != entry["date"]]
    data["history"].append(entry)
    data["history"].sort(key=lambda e: e["date"])

    data["updated_at"] = now_syd.isoformat(timespec="seconds")

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=1)
    print(f"OK — total A${total_aud:,.2f}; checked {', '.join(due)}")
    if data["errors"]:
        print("Errors:", *data["errors"], sep="\n  ")


if __name__ == "__main__":
    main()
