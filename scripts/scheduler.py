"""Near real-time refresh scheduler.

Runs as a long-lived service inside Docker Compose. Every INTERVAL seconds
(default 600s = 10 min), if the NASDAQ regular session is open, calls the
backend /ingest endpoint to refresh today's running daily bar for every
ticker in the universe.

Notes:
- Schema stores daily bars (prices.price_date), so "near real-time" here
  means refreshing today's daily candle while the market is open. yfinance
  returns the in-progress candle as the most recent row when period="5d".
- Fundamentals and news are skipped — they rarely change intraday and are
  slow to fetch.
- Run inside the backend image: `python -m scripts.scheduler`.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from scripts.universe import universe

LOG = logging.getLogger("scheduler")

API_BASE_URL = os.getenv("SCHEDULER_API_BASE_URL", "http://backend:8000").rstrip("/")
INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "600"))
PERIOD = os.getenv("SCHEDULER_PERIOD", "5d")
INTERVAL_BAR = os.getenv("SCHEDULER_INTERVAL_BAR", "1d")
BATCH_SIZE = int(os.getenv("SCHEDULER_BATCH_SIZE", "20"))
REQUEST_TIMEOUT = float(os.getenv("SCHEDULER_REQUEST_TIMEOUT", "600"))
INGEST_NEWS = os.getenv("SCHEDULER_INGEST_NEWS", "false").lower() in {"1", "true", "yes"}
INGEST_FUNDAMENTALS = os.getenv("SCHEDULER_INGEST_FUNDAMENTALS", "false").lower() in {"1", "true", "yes"}
SKIP_MARKET_HOURS = os.getenv("SCHEDULER_SKIP_MARKET_HOURS", "false").lower() in {"1", "true", "yes"}

# NASDAQ regular session in US/Eastern: 09:30 → 16:00.
# Convert to UTC ignoring DST nuances: 13:30–21:00 UTC during DST,
# 14:30–22:00 UTC during standard time. We use a generous union so the
# scheduler errs on the side of running.
SESSION_OPEN_UTC_HOUR = 13   # 13:00 UTC (handles DST)
SESSION_OPEN_UTC_MIN = 0
SESSION_CLOSE_UTC_HOUR = 22  # 22:00 UTC
SESSION_CLOSE_UTC_MIN = 30

_RUNNING = True


def _signal_handler(signum, _frame):
    global _RUNNING
    LOG.info("Received signal %s — shutting down after current cycle", signum)
    _RUNNING = False


def _is_market_open(now_utc: datetime) -> bool:
    if SKIP_MARKET_HOURS:
        return True
    weekday = now_utc.weekday()  # Monday=0 ... Sunday=6
    if weekday >= 5:
        return False
    open_at = now_utc.replace(hour=SESSION_OPEN_UTC_HOUR, minute=SESSION_OPEN_UTC_MIN, second=0, microsecond=0)
    close_at = now_utc.replace(hour=SESSION_CLOSE_UTC_HOUR, minute=SESSION_CLOSE_UTC_MIN, second=0, microsecond=0)
    return open_at <= now_utc <= close_at


def _post_ingest(tickers: list[str]) -> dict:
    body = json.dumps(
        {
            "tickers": tickers,
            "period": PERIOD,
            "interval": INTERVAL_BAR,
            "include_fundamentals": INGEST_FUNDAMENTALS,
            "include_news": INGEST_NEWS,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url=f"{API_BASE_URL}/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _refresh_once(tickers: list[str]) -> None:
    total_rows = 0
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        try:
            result = _post_ingest(batch)
            rows = int(result.get("rows_loaded", 0))
            total_rows += rows
            LOG.info(
                "refresh batch %d-%d/%d status=%s rows=%d",
                i + 1, i + len(batch), len(tickers),
                result.get("status"), rows,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            LOG.error("batch %d-%d failed: %s", i + 1, i + len(batch), exc)
    LOG.info("cycle complete | total_rows=%d", total_rows)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    tickers = universe()
    LOG.info(
        "scheduler starting | universe=%d | interval=%ss | period=%s | batch=%d | base=%s",
        len(tickers), INTERVAL_SECONDS, PERIOD, BATCH_SIZE, API_BASE_URL,
    )

    while _RUNNING:
        now = datetime.now(timezone.utc)
        if _is_market_open(now):
            LOG.info("market open (%s UTC) — refreshing %d tickers", now.strftime("%H:%M:%S"), len(tickers))
            _refresh_once(tickers)
        else:
            LOG.info("market closed (%s UTC) — sleeping", now.strftime("%a %H:%M:%S"))

        # Sleep in small steps so SIGTERM exits quickly.
        slept = 0
        while _RUNNING and slept < INTERVAL_SECONDS:
            time.sleep(min(5, INTERVAL_SECONDS - slept))
            slept += 5

    LOG.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
