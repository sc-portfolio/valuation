# Skier Capital Daily Valuation

A private-use portfolio valuation page for ASX, US and LSE holdings.

- **index.html** — the page: four tiles (Australia A$, USA US$, UK £, Total A$),
  a running-total chart, the holdings table, and the manage-holdings form.
- **holdings.json** — the holdings you entered (code, market, quantity, purchase price).
- **data.json** — generated: latest closing prices, FX rates, tile values, and the
  day-by-day history.
- **scripts/update_prices.py** — fetches closes + FX and maintains the daily history.
- **.github/workflows/daily-valuation.yml** — checks each market ~1 hour after its
  own close (ASX 5pm Sydney, LSE 5:30pm London, NYSE 5pm New York; daylight saving
  handled by firing at both candidate UTC times and letting the script check the
  market's local clock). Also runs on demand via *Run workflow* or the page's
  "Fetch latest prices now" button. Every check upserts the running-total history
  entry for the current Sydney date — so the chart moves after each market's close,
  including the Friday US/UK closes that land on Saturday morning Sydney time.

## Notes

- UK (LSE) prices arrive in pence and are converted to pounds; enter UK purchase
  prices in **pounds** (7.42, not 742).
- At 4:30pm Sydney the US and UK figures are those markets' most recent close.
- Saving holdings from the page requires a fine-grained personal access token
  (Contents + Actions read/write on this repository only), pasted once per device.
- This repository is public: prices are public data anyway, but quantities are
  visible to anyone who finds it. Move to a private repo (GitHub Pro) if that matters.
