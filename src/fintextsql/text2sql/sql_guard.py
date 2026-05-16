from __future__ import annotations

import re

from sqlglot import exp, parse_one

ALLOWED_TABLES = {"companies", "prices", "fundamentals", "news_articles", "ingestion_runs"}
BLOCKED_EXPRESSIONS = tuple(
    getattr(exp, name)
    for name in ["Alter", "Create", "Delete", "Drop", "Insert", "Merge", "Update", "TruncateTable"]
    if hasattr(exp, name)
)


class SQLValidationError(ValueError):
    pass


def clean_sql(raw: str) -> str:
    value = raw.strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", value, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        value = fenced.group(1).strip()
    value = re.sub(r"^\s*sql\s*:\s*", "", value, flags=re.IGNORECASE)
    return value.strip().rstrip(";")


def validate_select_sql(sql: str) -> str:
    sql = clean_sql(sql)
    if not sql:
        raise SQLValidationError("SQL is empty")
    if ";" in sql:
        raise SQLValidationError("Only one SQL statement is allowed")

    try:
        expression = parse_one(sql, read="postgres")
    except Exception as exc:
        raise SQLValidationError(f"SQL parse error: {exc}") from exc

    if BLOCKED_EXPRESSIONS and expression.find(*BLOCKED_EXPRESSIONS):
        raise SQLValidationError("Only read-only SELECT queries are allowed")
    if not isinstance(expression, (exp.Select, exp.Union)):
        raise SQLValidationError("SQL must be a SELECT query")

    cte_names = {cte.alias.lower() for cte in expression.find_all(exp.CTE) if cte.alias}
    for table in expression.find_all(exp.Table):
        table_name = table.name.lower()
        if table_name in cte_names:
            continue
        if table_name not in ALLOWED_TABLES:
            raise SQLValidationError(f"Table '{table_name}' is not available")

    return sql


def ensure_limit(sql: str, limit: int) -> str:
    expression = parse_one(sql, read="postgres")
    if isinstance(expression, exp.Select) and expression.args.get("limit") is None:
        return f"{sql}\nLIMIT {limit}"
    return sql
