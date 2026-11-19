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
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if "api.github.com/" in url and TOKEN:
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
        "interval": "1d", "includePrePost": "false",
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


def fetch_dff():
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
    headers = {"User-Agent": USER_AGENT}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=12) as response:
        text = response.read().decode("utf-8-sig")
    import csv
    from io import StringIO
    result = {}
    for row in csv.DictReader(StringIO(text)):
        day = row.get("observation_date") or row.get("DATE")
        val = row.get("DFF")
        if day and val and val != ".":
            result[day] = float(val)
    if not result:
        raise RuntimeError("No usable FRED DFF values")
    return result


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
    # This is intentionally after the price publish. A slow FRED endpoint
    # must never block price availability.
    try:
        rates = fetch_dff()
        history, history_sha = get_file("history.json")
        data, data_sha = get_file("data.json")
        sorted_dates = sorted(rates)
        latest_rate = sorted_dates[-1]
        if data.get("dff", {}).get("date") == latest_rate:
            LOG.info("DFF already up to date through %s", latest_rate)
            return
        changed = False
        for row in history["markets"]:
            pos = bisect_right(sorted_dates, row["date"]) - 1
            if pos >= 0:
                new_rate = round(rates[sorted_dates[pos]], 6)
                if row.get("dff") != new_rate:
                    row["dff"] = new_rate
                    changed = True
        data["dff"] = {"date": latest_rate, "value": round(rates[latest_rate], 6)}
        history["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        data["updated_at_utc"] = history["updated_at_utc"]
        if changed:
            update_file("history.json", history, history_sha)
        update_file("data.json", data, data_sha)
        LOG.info("Prepared/published DFF through %s", latest_rate)
    except Exception:
        LOG.exception("DFF refresh failed; previously published prices remain intact")


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
