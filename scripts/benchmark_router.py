"""Benchmark Intent Router accuracy + latency on the test.csv question set.

Usage:
    python3 -m scripts.benchmark_router \\
        --csv test.csv \\
        --base-url http://localhost:18000 \\
        --out docs/benchmark_results.csv

What it does:
    1. Reads test.csv (columns: id, intent_expected, question)
    2. For each question, calls POST /chat/route to get predicted intent + latency
    3. Compares predicted vs expected, records correctness
    4. Computes metrics:
       - Overall accuracy
       - Per-intent precision / recall / F1
       - Macro-averaged F1
       - Latency P50 / P95 / mean
       - Confusion matrix
    5. Writes per-row results to docs/benchmark_results.csv
    6. Prints summary report to stdout
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


VALID_INTENTS = {"text_to_sql", "visualization", "web_search", "ingestion", "general"}


def post_route(base_url: str, message: str, timeout: float = 15.0) -> tuple[str | None, list[str], float, str | None]:
    """Call POST /chat/route. Returns (intent, tickers, latency_seconds, error_message)."""
    body = json.dumps({"message": message}).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/chat/route",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, [], time.perf_counter() - t0, str(exc)

    intent = payload.get("intent")
    tickers = payload.get("tickers") or []
    return intent, tickers, time.perf_counter() - t0, None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def compute_metrics(rows: list[dict]) -> dict[str, Any]:
    """Per-intent precision/recall/F1 + macro avg + latency stats."""
    intents = sorted(VALID_INTENTS)
    metrics: dict[str, Any] = {"per_intent": {}}

    # Per-intent counts: tp, fp, fn
    for intent in intents:
        tp = sum(1 for r in rows if r["intent_expected"] == intent and r["intent_predicted"] == intent)
        fp = sum(1 for r in rows if r["intent_expected"] != intent and r["intent_predicted"] == intent)
        fn = sum(1 for r in rows if r["intent_expected"] == intent and r["intent_predicted"] != intent)
        support = sum(1 for r in rows if r["intent_expected"] == intent)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        metrics["per_intent"][intent] = {
            "support": support,
            "tp": tp, "fp": fp, "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    # Overall accuracy
    correct = sum(1 for r in rows if r["correct"])
    total = len(rows)
    metrics["accuracy"] = correct / total if total > 0 else 0.0
    metrics["correct"] = correct
    metrics["total"] = total

    # Macro-averaged F1
    f1_values = [m["f1"] for m in metrics["per_intent"].values()]
    metrics["macro_f1"] = sum(f1_values) / len(f1_values) if f1_values else 0.0

    # Latency stats (only for successful calls)
    latencies = [r["latency_ms"] for r in rows if r["error"] is None]
    if latencies:
        metrics["latency_ms"] = {
            "mean": statistics.mean(latencies),
            "median": statistics.median(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "min": min(latencies),
            "max": max(latencies),
        }
    else:
        metrics["latency_ms"] = {}

    # Confusion matrix: expected → counter of predicted
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        confusion[r["intent_expected"]][r["intent_predicted"] or "ERROR"] += 1
    metrics["confusion"] = {k: dict(v) for k, v in confusion.items()}

    # Error count
    metrics["errors"] = sum(1 for r in rows if r["error"] is not None)

    return metrics


def print_report(metrics: dict[str, Any]) -> None:
    print()
    print("=" * 70)
    print("BENCHMARK REPORT — Intent Router")
    print("=" * 70)
    print()
    print(f"Total questions:     {metrics['total']}")
    print(f"Correctly routed:    {metrics['correct']}  ({metrics['accuracy']*100:.2f}%)")
    print(f"Errors (network):    {metrics['errors']}")
    print(f"Macro F1:            {metrics['macro_f1']*100:.2f}%")
    print()
    print("Per-intent metrics:")
    print(f"  {'Intent':<15} {'Support':>8} {'Precision':>10} {'Recall':>8} {'F1':>8}")
    print(f"  {'-'*15} {'-'*8} {'-'*10} {'-'*8} {'-'*8}")
    for intent, m in metrics["per_intent"].items():
        print(f"  {intent:<15} {m['support']:>8} {m['precision']*100:>9.2f}% {m['recall']*100:>7.2f}% {m['f1']*100:>7.2f}%")
    print()

    if metrics["latency_ms"]:
        lat = metrics["latency_ms"]
        print("Latency (ms):")
        print(f"  Mean:   {lat['mean']:.1f}")
        print(f"  Median: {lat['median']:.1f}")
        print(f"  P50:    {lat['p50']:.1f}")
        print(f"  P95:    {lat['p95']:.1f}")
        print(f"  P99:    {lat['p99']:.1f}")
        print(f"  Min:    {lat['min']:.1f}")
        print(f"  Max:    {lat['max']:.1f}")
        print()

    # Confusion matrix
    print("Confusion matrix (rows = expected, cols = predicted):")
    intents = sorted(VALID_INTENTS) + ["ERROR"]
    header = "  " + " " * 16 + "".join(f"{i[:8]:>10}" for i in intents)
    print(header)
    for expected in sorted(VALID_INTENTS):
        row = metrics["confusion"].get(expected, {})
        cells = "".join(f"{row.get(p, 0):>10}" for p in intents)
        print(f"  {expected:<16}{cells}")
    print()
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Intent Router accuracy")
    parser.add_argument("--csv", default="test.csv", help="Input CSV with id,intent_expected,question")
    parser.add_argument("--base-url", default="http://localhost:18000")
    parser.add_argument("--out", default="benchmark/benchmark_results.csv", help="Output per-row results CSV")
    parser.add_argument("--limit", type=int, default=0, help="Run only first N questions (0 = all)")
    parser.add_argument("--verbose", action="store_true", help="Print each question result as it runs")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 1

    # Read test cases
    with open(csv_path, encoding="utf-8") as f:
        test_cases = list(csv.DictReader(f))

    if args.limit > 0:
        test_cases = test_cases[: args.limit]

    print(f"Running benchmark on {len(test_cases)} questions...")
    print(f"Backend: {args.base_url}")
    print()

    # Run each case
    results: list[dict] = []
    for i, case in enumerate(test_cases, 1):
        expected = case["intent_expected"].strip()
        question = case["question"].strip()
        intent_pred, tickers, latency_s, error = post_route(args.base_url, question)
        latency_ms = latency_s * 1000
        correct = (intent_pred == expected)

        row = {
            "id": case["id"],
            "intent_expected": expected,
            "intent_predicted": intent_pred,
            "correct": correct,
            "latency_ms": latency_ms,
            "tickers_predicted": ",".join(tickers),
            "error": error,
            "question": question,
        }
        results.append(row)

        marker = "✓" if correct else ("✗" if error is None else "!")
        if args.verbose or not correct or error:
            print(f"  [{i:>3}/{len(test_cases)}] {marker} {expected:<14} → {intent_pred or 'ERROR':<14} ({latency_ms:6.0f}ms) {question[:60]}")
        elif i % 10 == 0:
            print(f"  [{i:>3}/{len(test_cases)}] processed...")

    # Compute metrics
    metrics = compute_metrics(results)
    print_report(metrics)

    # Write per-row results
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["id", "intent_expected", "intent_predicted", "correct",
                        "latency_ms", "tickers_predicted", "error", "question"],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    print(f"\nDetailed results saved to: {out_path}")

    # Write summary JSON for programmatic use
    summary_path = out_path.with_suffix(".summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"Summary metrics saved to: {summary_path}")

    return 0 if metrics["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
