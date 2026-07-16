#!/usr/bin/env python3
"""Skier Capital Valuation — per-market price checks.

Each exchange is checked ~1 hour after its own close:
  ASX            — 5:00pm Sydney     (close 4:00pm)
  Tokyo          — 4:30pm Tokyo      (close 3:30pm)
  Hong Kong      — 5:00pm HK         (close 4:00pm)
  Singapore      — 6:00pm Singapore  (close 5:00pm)
  LSE            — 5:30pm London     (close 4:30pm)
  Euronext/XETRA — 6:30pm CET        (close 5:30pm; shares London's cron slots)
  NYSE           — 5:00pm New York   (close 4:00pm)

The GitHub Actions cron fires at every candidate UTC time (two per DST-using
region, one for the fixed-offset Asian exchanges); this script checks each
exchange's local clock and only updates the ones actually due. Manual runs
(workflow_dispatch) update everything.

Prices for exchanges not being checked are carried over from the previous
data.json, so every run recomputes all tiles. Tiles are grouped by category:
Australia (A$), USA (US$), UK (£), Europe (€ — euro-zone exchanges), Asia
(mixed currencies, displayed converted to A$), plus the AUD Total.

Every run upserts the history entry for the current Sydney date, so the chart
moves after each market's close regardless of the Sydney day of week.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

SYDNEY = ZoneInfo("Australia/Sydney")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_PATH = os.path.join(ROOT, "holdings.json")
DATA_PATH = os.path.join(ROOT, "data.json")

# exchange -> Yahoo suffix, quote currency, tile category, local tz,
# local hour in which its post-close check runs (some crons fire at :30)
EXCHANGES = {
    "AU": {"suffix": ".AX", "ccy": "AUD", "cat": "AU",   "tz": "Australia/Sydney",  "hour": 17},
    "US": {"suffix": "",    "ccy": "USD", "cat": "US",   "tz": "America/New_York",  "hour": 17},
    "UK": {"suffix": ".L",  "ccy": "GBP", "cat": "UK",   "tz": "Europe/London",     "hour": 17},
    "FR": {"suffix": ".PA", "ccy": "EUR", "cat": "EU",   "tz": "Europe/Paris",      "hour": 18},
    "NL": {"suffix": ".AS", "ccy": "EUR", "cat": "EU",   "tz": "Europe/Amsterdam",  "hour": 18},
    "BE": {"suffix": ".BR", "ccy": "EUR", "cat": "EU",   "tz": "Europe/Brussels",   "hour": 18},
    "DE": {"suffix": ".DE", "ccy": "EUR", "cat": "EU",   "tz": "Europe/Berlin",     "hour": 18},
    "HK": {"suffix": ".HK", "ccy": "HKD", "cat": "ASIA", "tz": "Asia/Hong_Kong",    "hour": 17},
    "SG": {"suffix": ".SI", "ccy": "SGD", "cat": "ASIA", "tz": "Asia/Singapore",    "hour": 18},
    "JP": {"suffix": ".T",  "ccy": "JPY", "cat": "ASIA", "tz": "Asia/Tokyo",        "hour": 16},
}
CATS = ["AU", "US", "UK", "EU", "ASIA"]
FX_CCYS = ["USD", "GBP", "EUR", "HKD", "SGD", "JPY"]  # vs AUD


def due_exchanges(checked):
    """Which exchanges should this run update?

    GitHub's cron fires late — sometimes by hours — so never demand
    punctuality. An exchange is due when its most recent post-close check
    moment (its check hour on the latest weekday) has passed but no check
    has been recorded since. Any run, however delayed, then catches up
    every exchange whose close slipped through.
    """
    forced = os.environ.get("MARKETS", "").strip()
    if forced:
        return [m for m in forced.upper().split(",") if m in EXCHANGES]
    if os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return list(EXCHANGES)  # manual runs refresh everything
    due = []
    for m, ex in EXCHANGES.items():
        now = datetime.now(ZoneInfo(ex["tz"]))
        cand = now.replace(hour=ex["hour"], minute=0, second=0, microsecond=0)
        if cand > now:
            cand -= timedelta(days=1)
        while cand.weekday() >= 5:  # roll back over weekends
            cand -= timedelta(days=1)
        prev = checked.get(m)
        try:
            prev_dt = datetime.fromisoformat(prev) if prev else None
        except ValueError:
            prev_dt = None
        if prev_dt is None or prev_dt < cand:
            due.append(m)
    return due


def yahoo_symbol(h):
    return h["code"].upper().strip() + EXCHANGES[h["market"]]["suffix"]


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
    fx = {"AUDAUD": 1.0}
    for ccy in FX_CCYS:
        symbol = f"{ccy}AUD=X"
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            raise ValueError(f"no FX data for {symbol}")
        fx[f"{ccy}AUD"] = float(hist["Close"].dropna().iloc[-1])
    return fx


def main():
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

    due = due_exchanges(checked)
    if not due:
        print("All exchanges already checked since their last close — nothing to do.")
        sys.exit(0)
    print("Updating exchanges:", ", ".join(due))

    data = {"prices": {}, "fx": {}, "tiles": {}, "checked": checked,
            "history": history, "errors": []}

    fx = fetch_fx()
    data["fx"] = {k: round(v, 6) for k, v in fx.items()}

    def to_aud(ccy, amount):
        return amount * fx[f"{ccy}AUD"]

    now_syd = datetime.now(SYDNEY)
    # subtotal per category, in the category's display currency
    # (AU/US/UK/EU in their own currency; ASIA converted to AUD per holding)
    subtotals = {c: 0.0 for c in CATS}
    total_aud = 0.0
    for h in holdings:
        if h["market"] not in EXCHANGES:
            data["errors"].append(f"{h['code']}: unknown market {h['market']}")
            continue
        ex = EXCHANGES[h["market"]]
        symbol = yahoo_symbol(h)
        # Fetch when the exchange is due, or when we hold no price yet
        # (e.g. a share added since the last check of its exchange).
        if h["market"] in due or symbol not in old_prices:
            try:
                price, ccy, name = fetch_quote(symbol)
            except Exception as e:
                print(f"WARN {symbol}: {e}")
                data["errors"].append(f"{symbol}: {e}")
                if symbol not in old_prices:
                    continue
            else:
                if ccy and ccy != ex["ccy"]:
                    data["errors"].append(f"{symbol}: quoted in {ccy}, expected {ex['ccy']}")
                old_prices[symbol] = {
                    "price": round(price, 4),
                    "currency": ex["ccy"],
                    "name": name or h["code"].upper(),
                }
        data["prices"][symbol] = old_prices[symbol]
        value_local = old_prices[symbol]["price"] * float(h["quantity"])
        value_aud = to_aud(ex["ccy"], value_local)
        total_aud += value_aud
        subtotals[ex["cat"]] += value_aud if ex["cat"] == "ASIA" else value_local

    for m in due:
        data["checked"][m] = now_syd.isoformat(timespec="seconds")

    data["tiles"] = {
        "AU": round(subtotals["AU"], 2),
        "US": round(subtotals["US"], 2),
        "UK": round(subtotals["UK"], 2),
        "EU": round(subtotals["EU"], 2),
        "ASIA_AUD": round(subtotals["ASIA"], 2),
        "TOTAL_AUD": round(total_aud, 2),
    }

    # Every check upserts the history entry for the current Sydney date.
    # (No entry while the portfolio is empty — a $0 point would be noise.)
    if holdings:
        entry = {
            "date": now_syd.strftime("%Y-%m-%d"),
            "au": data["tiles"]["AU"],
            "us": data["tiles"]["US"],
            "uk": data["tiles"]["UK"],
            "eu": data["tiles"]["EU"],
            "asia_aud": data["tiles"]["ASIA_AUD"],
            "total_aud": data["tiles"]["TOTAL_AUD"],
            "fx": {k: data["fx"][k] for k in data["fx"] if k != "AUDAUD"},
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
