"""Cloud Run updater for SEASONSODA's existing GitHub Pages 200DMA app.

Prices are published before the optional FRED refresh. Does not alter historical
price observations. DRY_RUN=true (default) prevents GitHub writes during rollout.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from zoneinfo import ZoneInfo

LOG = logging.getLogger("market-200dma")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
NY = ZoneInfo("America/New_York")
REPO = os.getenv("GITHUB_REPO", "seasonsoda-afk/market-200dma")
TOKEN = os.getenv("GITHUB_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
GITHUB_API = f"https://api.github.com/repos/{REPO}/contents"
USER_AGENT = "SEASONSODA-cloud-run-200dma/1.0"


def request(url: str, *, method: str = "GET", payload=None, timeout=15):
    # GitHub's API media type must not be sent to Yahoo Finance. The
    # existing production workflow sends only a simple User-Agent to Yahoo.
    headers = {"User-Agent": "market-200dma-updater/1.0"}
    if "api.github.com/" in url:
        headers["Accept"] = "application/vnd.github+json"
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def get_file(path: str):
    raw = request(f"{GITHUB_API}/{path}")
    content = base64.b64decode(raw["content"]).decode("utf-8")
    return json.loads(content), raw["sha"]


def update_file(path: str, obj: dict, sha: str):
    content = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if DRY_RUN:
        LOG.info("DRY_RUN: would publish %s (%s bytes)", path, len(content))
        return
    if not TOKEN:
        raise RuntimeError("GITHUB_TOKEN required for non-dry-run publishing")
    request(
        f"{GITHUB_API}/{path}", method="PUT",
        payload={
            "message": "Update market history (Cloud Run)",
            "sha": sha,
            "content": base64.b64encode(content.encode()).decode(),
        },
    )


def fetch_yahoo(symbol: str, since: dt.date):
    start = int(dt.datetime.combine(since, dt.time.min, tzinfo=dt.timezone.utc).timestamp())
    end = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).timestamp())
    symbol_url = urllib.parse.quote(symbol, safe="")
    qs = urllib.parse.urlencode({
        "period1": start, "period2": end,
        "interval": "1d", "events": "history", "includePrePost": "false",
    })
    data = request(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_url}?{qs}")
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"{symbol}: Yahoo API returned no result")
    stamps = result.get("timestamp") or []
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    values = {}
    for stamp, close in zip(stamps, quotes):
        if close is None or float(close) <= 0:
            continue
        day = dt.datetime.fromtimestamp(stamp, NY).date().isoformat()
        values[day] = float(close)
    return values


def _decode_rates_csv(text: str) -> dict[str, float]:
    import csv
    from io import StringIO
    result = {}
    for row in csv.DictReader(StringIO(text)):
        day = row.get("observation_date") or row.get("DATE")
        value = row.get("DFF")
        if day and value and value != ".":
            result[day] = float(value)
    if not result:
        raise RuntimeError("No usable FRED DFF values")
    return result


def fetch_dff(since: dt.date):
    """Fetch only recent FRED observations; a full 1954+ CSV is unnecessary."""
    start = since - dt.timedelta(days=7)
    qs = urllib.parse.urlencode({"id": "DFF", "cosd": start.isoformat()})
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + qs
    req = urllib.request.Request(
        url, headers={"User-Agent": "market-200dma-updater/1.0"}
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        text = response.read().decode("utf-8-sig")
    return _decode_rates_csv(text)


def fetch_new_york_fed_effr():
    """Official EFFR series used as a fallback for the same effective rate.

    The NY Fed publishes the prior business day's EFFR at ~09:00 ET.
    The response is validated and only the latest short window is used.
    """
    url = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/20.json"
    req = urllib.request.Request(
        url, headers={"User-Agent": "market-200dma-updater/1.0",
                      "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        payload = json.load(response)
    records = payload.get("refRates", {}).get("effr")
    if not isinstance(records, list) or not records:
        raise RuntimeError("NY Fed response has no refRates.effr")
    rates = {}
    for item in records:
        day = item.get("effectiveDate")
        value = item.get("percentRate")
        if day is None or value is None:
            continue
        dt.date.fromisoformat(day)
        rate = float(value)
        if not 0 <= rate <= 30:
            raise RuntimeError("NY Fed EFFR outside sanity range")
        rates[day] = rate
    if not rates:
        raise RuntimeError("NY Fed EFFR response contains no valid rates")
    return rates


def fetch_recent_rates(since: dt.date):
    try:
        rates = fetch_dff(since)
        LOG.info("DFF source=FRED; last available date=%s", max(rates))
        return rates
    except Exception as fred_error:
        LOG.warning("FRED DFF unavailable (%s); trying official NY Fed EFFR", fred_error)
        rates = fetch_new_york_fed_effr()
        LOG.info("DFF source=NY Fed EFFR; last available date=%s", max(rates))
        return rates


def refresh_prices():
    history, history_sha = get_file("history.json")
    data, data_sha = get_file("data.json")
    rows = history["markets"]
    last_day = max(row["date"] for row in rows)
    today_ny = dt.datetime.now(NY).date().isoformat()
    LOG.info("Stored date=%s; New York today=%s; dry_run=%s", last_day, today_ny, DRY_RUN)

    sp = fetch_yahoo("^GSPC", dt.date.fromisoformat(last_day) - dt.timedelta(days=3))
    ndx = fetch_yahoo("^NDX", dt.date.fromisoformat(last_day) - dt.timedelta(days=3))
    new_days = sorted(d for d in (sp.keys() & ndx.keys()) if last_day < d <= today_ny)
    if not new_days:
        LOG.info("No new common market date; leaving prices untouched")
        return

    # Keep existing observed prices unchanged. A new day inherits the last
    # available DFF with its own as-of date in data.json.
    old_dff = float(rows[-1]["dff"])
    for day in new_days:
        rows.append({
            "date": day,
            "sp500_close": round(sp[day], 6),
            "nasdaq100_close": round(ndx[day], 6),
            "dff": old_dff,
        })
    rows.sort(key=lambda row: row["date"])
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    history["markets"] = rows
    history["end_date"] = rows[-1]["date"]
    history["updated_at_utc"] = stamp

    latest = rows[-1]
    values = {}
    for key, symbol in [("sp500", "^GSPC"), ("nasdaq100", "^NDX")]:
        column = key + "_close"
        sma = sum(float(row[column]) for row in rows[-200:]) / 200
        close = float(latest[column])
        values[key] = {
            "symbol": symbol, "date": latest["date"],
            "close": round(close, 6),
            "ma200": round(sma, 6),
            "deviation_pct": round((close / sma - 1) * 100, 6),
        }
    data["markets"] = values
    data["updated_at_utc"] = stamp

    # Stop if another publisher has advanced history after the initial read.
    _, newest_sha = get_file("history.json")
    if newest_sha != history_sha:
        raise RuntimeError("Concurrent history modification detected; retry next run")

    update_file("history.json", history, history_sha)
    # Retrieve latest SHA for data.json before publishing, to avoid blind writes.
    _, current_data_sha = get_file("data.json")
    if current_data_sha != data_sha:
        raise RuntimeError("Concurrent data.json modification detected; retry next run")
    update_file("data.json", data, data_sha)
    LOG.info("Prepared/published prices through %s", latest["date"])


def refresh_fred():
    # Price publication has already finished. Rate retrieval must not block
    # prices, and any failure must retain the last known as-of date.
    try:
        history, history_sha = get_file("history.json")
        data, data_sha = get_file("data.json")
        current_dff = data.get("dff", {})
        current_date_str = current_dff.get("date")
        if not current_date_str:
            raise RuntimeError("Stored DFF observation date is missing")
        current_date = dt.date.fromisoformat(current_date_str)
        rates = fetch_recent_rates(current_date)
        latest_rate_date = max(rates)
        if latest_rate_date <= current_date_str:
            LOG.info("No newer EFFR observation (stored=%s; available=%s)",
                     current_date_str, latest_rate_date)
            return

        # Only backfill dates newer than the stored published DFF date.
        # Never rewrite 25+ years of historical rate observations.
        new_rate_dates = sorted(day for day in rates if day > current_date_str)
        changes = 0
        for row in history["markets"]:
            if row["date"] <= current_date_str:
                continue
            pos = bisect_right(new_rate_dates, row["date"]) - 1
            if pos >= 0:
                latest_for_day = new_rate_dates[pos]
                value = round(rates[latest_for_day], 6)
                if row.get("dff") != value:
                    row["dff"] = value
                    changes += 1

        data["dff"] = {
            "date": latest_rate_date,
            "value": round(rates[latest_rate_date], 6),
        }
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        data["updated_at_utc"] = stamp
        if changes:
            history["updated_at_utc"] = stamp
            update_file("history.json", history, history_sha)
        update_file("data.json", data, data_sha)
        LOG.info("DFF updated through %s; corrected %s market rows",
                 latest_rate_date, changes)
    except Exception:
        LOG.exception("DFF refresh failed; last published DFF date remains unchanged")


def main():
    now = dt.datetime.now(NY)
    # Prevent accidental acceptance of a still-open trading session.
    if now.weekday() >= 5 or now.hour < 16 or (now.hour == 16 and now.minute < 15):
        LOG.info("Outside post-close weekday window; skipping")
        return
    try:
        refresh_prices()
    except Exception:
        LOG.exception("Price update failed; preserving existing data")
        raise
    refresh_fred()


if __name__ == "__main__":
    main()
