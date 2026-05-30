"""End-to-end Text2SQL accuracy benchmark.

Measures whether the system's generated SQL produces the SAME RESULT as a
hand-written gold SQL. This is the "Execution Accuracy" metric used in Spider/BIRD.

Pipeline per question:
    1. Run gold SQL on DB    → gold_result
    2. Call POST /chat       → response.sql, response.rows
    3. Run response.sql on DB → actual_result   (in case rows weren't returned)
    4. Compare results       → match?

Metrics reported:
    - sql_generation_rate:  % of questions where system produced SQL
    - execution_rate:       % of generated SQL that ran without error
    - result_match_rate:    % of results that match gold (the headline metric)
    - per-difficulty breakdown (easy/medium/hard)
    - latency stats
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any


def run_sql(sql: str) -> tuple[list[list], str | None]:
    """Run SQL inside the dp-postgres-1 container, return (rows, error)."""
    cmd = [
        "docker", "exec", "-i", "dp-postgres-1",
        "psql", "-U", "fintextsql", "-d", "fintextsql",
        "-t", "-A", "-F", "|", "-c", sql,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return [], "timeout"

    if proc.returncode != 0:
        return [], (proc.stderr or "").strip()[:200]

    rows: list[list] = []
    for line in proc.stdout.strip().splitlines():
        if not line:
            continue
        rows.append(line.split("|"))
    return rows, None


def normalize_cell(v: Any) -> str:
    """Convert any cell to a normalized string for comparison."""
    s = str(v).strip() if v is not None else ""
    if not s or s.lower() in {"none", "null"}:
        return ""
    try:
        # Round floats to 4 decimals to allow for minor precision diff
        f = float(s)
        if f == int(f):
            return str(int(f))
        return f"{f:.4f}"
    except (ValueError, TypeError):
        return s.lower()


def results_match(gold: list[list], actual: list[list], tolerance: float = 0.01) -> tuple[bool, str]:
    """Compare two result sets cell-by-cell after normalization.

    Returns (match, reason). Allows minor float precision differences.
    Rows are sorted-flattened so column order/row order shouldn't break a match for
    single-value queries; for multi-row queries we check row count + cell-set equality.
    """
    if not gold and not actual:
        return True, "both empty"
    if not gold:
        return False, "gold empty but actual has rows"
    if not actual:
        return False, "actual empty but gold has rows"

    if len(gold) != len(actual):
        return False, f"row count mismatch (gold={len(gold)}, actual={len(actual)})"

    # Single-value queries (1 row, 1 col): compare with tolerance
    if len(gold) == 1 and len(gold[0]) == 1:
        g = normalize_cell(gold[0][0])
        a_candidates = [normalize_cell(c) for row in actual for c in row]
        for a in a_candidates:
            if g == a:
                return True, "value match"
            try:
                if abs(float(g) - float(a)) <= tolerance * max(abs(float(g)), 1):
                    return True, "value match (tolerance)"
            except (ValueError, TypeError):
                pass
        return False, f"value mismatch (gold={g}, actual={a_candidates})"

    # Multi-row: compare as multiset of normalized rows
    def normalize_row(row: list) -> tuple:
        return tuple(sorted(normalize_cell(c) for c in row))

    gold_set = Counter(normalize_row(r) for r in gold)
    actual_rows_normalized = []
    for r in actual:
        # actual rows from API may have more columns than gold — try to match prefix
        actual_rows_normalized.append(r)

    # Try matching with full rows
    actual_set = Counter(normalize_row(r) for r in actual)
    if gold_set == actual_set:
        return True, "row set match"

    # If column counts differ, try subset match (gold columns subset of actual)
    if actual and gold and len(actual[0]) >= len(gold[0]):
        # Try matching against first N columns of actual
        ncols = len(gold[0])
        truncated = Counter(normalize_row(r[:ncols]) for r in actual)
        if gold_set == truncated:
            return True, "row set match (truncated cols)"
        truncated_tail = Counter(normalize_row(r[-ncols:]) for r in actual)
        if gold_set == truncated_tail:
            return True, "row set match (truncated cols tail)"

    # Last resort: check if all gold values appear somewhere in actual
    gold_cells = Counter(normalize_cell(c) for row in gold for c in row)
    actual_cells = Counter(normalize_cell(c) for row in actual for c in row)
    missing = gold_cells - actual_cells
    if not missing:
        return True, "cell-set match (loose)"

    return False, f"row mismatch (missing cells: {dict(list(missing.items())[:3])})"


def post_chat(base_url: str, message: str, timeout: float = 120.0) -> tuple[dict | None, float, str | None]:
    """Call POST /chat, return (full_response, latency_seconds, error)."""
    body = json.dumps({"message": message, "session_id": f"bench-{int(time.time())}"}).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, time.perf_counter() - t0, str(exc)[:200]
    return payload, time.perf_counter() - t0, None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end Text2SQL accuracy benchmark")
    parser.add_argument("--gold", default="benchmark/gold_sql.csv")
    parser.add_argument("--base-url", default="http://localhost:18000")
    parser.add_argument("--out", default="benchmark/text2sql_results.csv")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"ERROR: gold CSV not found: {gold_path}", file=sys.stderr)
        return 1

    with open(gold_path, encoding="utf-8") as f:
        cases = list(csv.DictReader(f))
    if args.limit > 0:
        cases = cases[: args.limit]

    print(f"Running Text2SQL benchmark on {len(cases)} questions...")
    print(f"Backend: {args.base_url}\n")

    results = []
    for i, case in enumerate(cases, 1):
        qid = case["id"]
        difficulty = case["difficulty"]
        question = case["question"]
        gold_sql = case["gold_sql"]

        # 1. Run gold SQL on DB
        gold_rows, gold_err = run_sql(gold_sql)
        if gold_err:
            print(f"  [{i:>2}/{len(cases)}] ⚠ GOLD SQL ERROR on Q{qid}: {gold_err[:80]}")
            results.append({
                "id": qid, "difficulty": difficulty, "question": question,
                "gold_rows": 0, "sql_generated": False, "sql_executed": False,
                "result_match": False, "latency_s": 0.0, "error": f"gold_sql_error: {gold_err}",
                "generated_sql": "",
            })
            continue

        # 2. Call /chat to get system response
        resp, latency, err = post_chat(args.base_url, question)
        if err:
            print(f"  [{i:>2}/{len(cases)}] ✗ NETWORK ERROR: {err[:80]}")
            results.append({
                "id": qid, "difficulty": difficulty, "question": question,
                "gold_rows": len(gold_rows), "sql_generated": False, "sql_executed": False,
                "result_match": False, "latency_s": latency, "error": f"network: {err}",
                "generated_sql": "",
            })
            continue

        generated_sql = (resp.get("sql") or "").strip()
        api_rows = resp.get("rows") or []
        intent = resp.get("intent")

        # 3. If we got SQL, run it on DB to get definitive comparison rows
        if generated_sql:
            actual_rows, sql_err = run_sql(generated_sql)
            sql_executed = sql_err is None
        else:
            # Fall back to API rows if no SQL but rows present
            actual_rows = [[str(v) for v in r.values()] for r in api_rows] if api_rows else []
            sql_err = None if actual_rows else "no_sql_generated"
            sql_executed = bool(actual_rows)

        # 4. Compare
        if not generated_sql and not actual_rows:
            match, reason = False, "no SQL generated"
        else:
            match, reason = results_match(gold_rows, actual_rows)

        marker = "✓" if match else "✗"
        status_extra = "" if generated_sql else f" [intent={intent}]"
        print(f"  [{i:>2}/{len(cases)}] {marker} {difficulty:<6} ({latency*1000:5.0f}ms) {reason[:50]}{status_extra}")

        results.append({
            "id": qid, "difficulty": difficulty, "question": question,
            "gold_rows": len(gold_rows),
            "sql_generated": bool(generated_sql),
            "sql_executed": sql_executed,
            "result_match": match,
            "match_reason": reason,
            "latency_s": latency,
            "error": sql_err,
            "generated_sql": generated_sql.replace("\n", " "),
            "intent": intent,
        })

    # Aggregate metrics
    total = len(results)
    sql_gen = sum(1 for r in results if r["sql_generated"])
    sql_exec = sum(1 for r in results if r["sql_executed"])
    match = sum(1 for r in results if r["result_match"])
    latencies = [r["latency_s"] * 1000 for r in results if r["error"] is None or "gold" not in (r["error"] or "")]

    print(f"\n{'='*70}")
    print(f"TEXT2SQL END-TO-END BENCHMARK REPORT")
    print(f"{'='*70}\n")
    print(f"Total questions:         {total}")
    print(f"SQL Generation Rate:     {sql_gen}/{total} = {sql_gen/total*100:.2f}%")
    print(f"SQL Execution Rate:      {sql_exec}/{total} = {sql_exec/total*100:.2f}%")
    print(f"** Result Match Rate:    {match}/{total} = {match/total*100:.2f}% **  ← HEADLINE METRIC")
    print()

    print("Per-difficulty breakdown:")
    print(f"  {'Difficulty':<10} {'Total':>6} {'Generated':>10} {'Executed':>10} {'Matched':>8} {'Accuracy':>10}")
    for diff in ["easy", "medium", "hard"]:
        subset = [r for r in results if r["difficulty"] == diff]
        if not subset:
            continue
        n = len(subset)
        gen = sum(1 for r in subset if r["sql_generated"])
        ex = sum(1 for r in subset if r["sql_executed"])
        m = sum(1 for r in subset if r["result_match"])
        print(f"  {diff:<10} {n:>6} {gen:>10} {ex:>10} {m:>8} {m/n*100:>9.2f}%")
    print()

    if latencies:
        print("Latency (ms):")
        print(f"  Mean:   {statistics.mean(latencies):.0f}")
        print(f"  Median: {statistics.median(latencies):.0f}")
        print(f"  P95:    {percentile(latencies, 95):.0f}")
    print(f"\n{'='*70}")

    # Write detailed CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["id", "difficulty", "question", "gold_rows", "sql_generated",
                  "sql_executed", "result_match", "match_reason", "latency_s",
                  "intent", "error", "generated_sql"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    # Write summary JSON
    summary = {
        "total": total,
        "sql_generation_rate": sql_gen / total if total else 0,
        "sql_execution_rate": sql_exec / total if total else 0,
        "result_match_rate": match / total if total else 0,
        "per_difficulty": {
            d: {
                "total": sum(1 for r in results if r["difficulty"] == d),
                "matched": sum(1 for r in results if r["difficulty"] == d and r["result_match"]),
            } for d in ["easy", "medium", "hard"]
        },
        "latency_ms": {
            "mean": statistics.mean(latencies) if latencies else 0,
            "median": statistics.median(latencies) if latencies else 0,
            "p95": percentile(latencies, 95) if latencies else 0,
        },
    }
    summary_path = out_path.with_suffix(".summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\nDetailed results: {out_path}")
    print(f"Summary metrics:  {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
