"""Bulk historical ingestion for the configured universe.

Calls the FastAPI /ingest endpoint in batches so we re-use the same upsert
path used by the live system. Run from inside the backend container or any
host that can reach the API.

Usage (host):
    python -m scripts.ingest_universe --base-url http://localhost:18000

Usage (inside backend container):
    docker compose exec backend python -m scripts.ingest_universe \\
        --base-url http://localhost:8000 --period 10y --batch-size 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Iterable

import urllib.error
import urllib.request

from scripts.universe import universe


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _post_ingest(
    base_url: str,
    tickers: list[str],
    period: str,
    interval: str,
    include_fundamentals: bool,
    include_news: bool,
    timeout: float,
) -> dict:
    body = json.dumps(
        {
            "tickers": tickers,
            "period": period,
            "interval": interval,
            "include_fundamentals": include_fundamentals,
            "include_news": include_news,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest historical data for the universe")
    parser.add_argument("--base-url", default="http://localhost:18000")
    parser.add_argument("--period", default="10y", help="yfinance period (default: 10y → covers 2020+)")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--sleep", type=float, default=2.0, help="Seconds to sleep between batches")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--no-fundamentals", action="store_true")
    parser.add_argument("--no-news", action="store_true")
    parser.add_argument("--only", nargs="*", help="Only ingest these tickers (skip the rest)")
    parser.add_argument("--skip", nargs="*", default=[], help="Skip these tickers")
    args = parser.parse_args()

    base_tickers = args.only if args.only else universe()
    skip = {t.upper() for t in args.skip}
    tickers = [t for t in base_tickers if t.upper() not in skip]

    print(f"[ingest] universe size = {len(tickers)} | batch={args.batch_size} | period={args.period}")
    total_rows = 0
    failed: list[str] = []
    started = time.time()

    for idx, batch in enumerate(_chunked(tickers, args.batch_size), start=1):
        attempt = 0
        while True:
            attempt += 1
            try:
                t0 = time.time()
                result = _post_ingest(
                    args.base_url,
                    batch,
                    args.period,
                    args.interval,
                    include_fundamentals=not args.no_fundamentals,
                    include_news=not args.no_news,
                    timeout=args.timeout,
                )
                elapsed = time.time() - t0
                rows = int(result.get("rows_loaded", 0))
                total_rows += rows
                status = result.get("status", "?")
                print(
                    f"[ingest] batch {idx:>3} ({', '.join(batch)}): "
                    f"status={status} rows={rows} elapsed={elapsed:.1f}s"
                )
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= 3:
                    print(f"[ingest] batch {idx} FAILED after {attempt} attempts: {exc}", file=sys.stderr)
                    failed.extend(batch)
                    break
                wait = 5 * attempt
                print(f"[ingest] batch {idx} retry {attempt} in {wait}s ({exc})", file=sys.stderr)
                time.sleep(wait)
        time.sleep(args.sleep)

    elapsed_total = time.time() - started
    print(
        f"[ingest] done in {elapsed_total/60:.1f}m | total_rows={total_rows} | "
        f"failed_tickers={len(failed)}"
    )
    if failed:
        print("[ingest] failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
