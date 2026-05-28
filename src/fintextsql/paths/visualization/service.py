from __future__ import annotations

import re
from typing import Any

from fintextsql.api.schemas import VisualizationSpec
from fintextsql.text2sql.service import TextToSQLResult, TextToSQLService

DEFAULT_NUMERIC_COLUMNS = [
    # Return / volatility metrics — if the SQL bothered to compute these,
    # they are always the answer; pick them over raw price columns.
    "period_return_pct",
    "return_pct",
    "annual_return_pct",
    "ytd_return_pct",
    "daily_volatility_pct",
    "return_volatility_ratio",
    "pct_change",
    "close",
    "adj_close",
    "last_price",
    "previous_close",
    "volume",
    "market_cap",
    "trailing_pe",
    "forward_pe",
    "price_to_book",
    "beta",
    "day_high",
    "day_low",
    "rows_loaded",
]

# Columns that look numeric but represent a dimension/identifier — never use as Y.
DIMENSION_COLUMNS = {
    "id",
    "company_id",
    "year",
    "month",
    "quarter",
    "week",
    "day",
    "rn_asc",
    "rn_desc",
    "return_observations",
}


class VisualizationService:
    def __init__(self, text_to_sql: TextToSQLService):
        self.text_to_sql = text_to_sql

    async def answer(self, question: str) -> tuple[TextToSQLResult, VisualizationSpec]:
        result = await self.text_to_sql.answer(question)
        viz = infer_visualization(question, result.columns, result.rows)
        return result, viz or VisualizationSpec(type="line", title="Finance visualization")


def infer_visualization(
    question: str,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> VisualizationSpec | None:
    if not rows or not columns:
        return None
    question_l = question.lower()

    # Scatter: volume vs daily_return_pct paired observations.
    if (
        _wants_scatter(question_l)
        and "volume" in columns
        and "daily_return_pct" in columns
    ):
        series = "ticker" if "ticker" in columns else None
        return VisualizationSpec(
            type="scatter",
            x="volume",
            y="daily_return_pct",
            series=series,
            title="Volume vs daily return",
        )

    y = _first_existing(columns, _preferred_y_columns(question_l))
    if y and not _column_has_number(rows, y):
        y = None
    if not y:
        y = _first_numeric_column(rows, columns, exclude=DIMENSION_COLUMNS)
    x = _choose_x_column(question_l, columns, y)
    if not x:
        return None
    if not y or y == x:
        return None

    if "ticker" in columns and (_is_cross_sectional_metric(y) or "top" in question_l):
        x = "ticker"

    # Year/period dimension takes priority over ticker for X when present
    # (e.g. compare returns across years: x=year, y=return_pct, series=ticker).
    for period_col in ("year", "month", "quarter"):
        if period_col in columns and x == "ticker" and y != period_col:
            x = period_col
            break

    # Multi-metric time series for a single ticker (e.g. close + MA20 + MA50): plot every
    # price-family column as its own line instead of collapsing to a single close line.
    if x in {"date", "as_of_date"}:
        distinct_tickers = {str(row.get("ticker")) for row in rows if row.get("ticker")}
        family = _price_family_columns(columns, rows, exclude={x})
        if len(distinct_tickers) <= 1 and len(family) >= 2:
            chart_type = "bar" if any(word in question_l for word in ["bar", "cột"]) else "line"
            return VisualizationSpec(
                type=chart_type,
                x=x,
                y=family[0],
                y_series=family,
                series=None,
                title="Finance visualization",
            )

    series = "ticker" if "ticker" in columns and x != "ticker" else None
    chart_type = "line"
    if any(word in question_l for word in ["bar", "cột"]) or x == "ticker":
        chart_type = "bar"
    if x in {"year", "month", "quarter"}:
        chart_type = "bar"
    if "top" in question_l and x != "date":
        chart_type = "bar"
    return VisualizationSpec(type=chart_type, x=x, y=y, series=series, title="Finance visualization")


_PRICE_FAMILY = {"close", "adj_close", "open", "high", "low"}


def _wants_scatter(question_l: str) -> bool:
    return any(t in question_l for t in ["scatter", "scatterplot", "phân tán", "phan tan", "điểm phân tán"])


def _price_family_columns(
    columns: list[str], rows: list[dict[str, Any]], *, exclude: set[str]
) -> list[str]:
    family: list[str] = []
    for column in columns:
        if column in exclude:
            continue
        lowered = column.lower()
        if (lowered in _PRICE_FAMILY or re.fullmatch(r"ma\d+", lowered)) and _column_has_number(rows, column):
            family.append(column)
    return family


def _preferred_y_columns(question_l: str) -> list[str]:
    # Growth / return / volatility comparisons — prefer the rate column over price.
    if any(
        word in question_l
        for word in [
            "return", "tăng trưởng", "tang truong", "growth",
            "tốc độ tăng", "toc do tang", "lợi nhuận", "loi nhuan",
            "% tăng", "% tang", "ổn định", "on dinh", "volatility", "biến động",
        ]
    ):
        return [
            "period_return_pct",
            "return_pct",
            "annual_return_pct",
            "ytd_return_pct",
            "daily_volatility_pct",
            "return_volatility_ratio",
            *DEFAULT_NUMERIC_COLUMNS,
        ]
    if any(word in question_l for word in ["market cap", "vốn hóa", "von hoa"]):
        return ["market_cap", *DEFAULT_NUMERIC_COLUMNS]
    if any(word in question_l for word in ["volume", "khối lượng", "khoi luong"]):
        return ["volume", *DEFAULT_NUMERIC_COLUMNS]
    if any(word in question_l for word in ["pe", "p/e"]):
        return ["trailing_pe", "forward_pe", "price_to_book", *DEFAULT_NUMERIC_COLUMNS]
    if "beta" in question_l:
        return ["beta", *DEFAULT_NUMERIC_COLUMNS]
    if any(word in question_l for word in ["price", "close", "quote", "giá", "gia"]):
        return [
            "last_price",
            "close",
            "adj_close",
            "previous_close",
            "day_high",
            "day_low",
            *DEFAULT_NUMERIC_COLUMNS,
        ]
    return DEFAULT_NUMERIC_COLUMNS


def _choose_x_column(question_l: str, columns: list[str], y: str | None) -> str | None:
    if y and "ticker" in columns and (_is_cross_sectional_metric(y) or "top" in question_l):
        return "ticker"
    return _first_existing(columns, ["date", "as_of_date", "published_at", "ticker", "symbol"])


def _first_existing(columns: list[str], preferred: list[str]) -> str | None:
    for column in preferred:
        if column in columns:
            return column
    return None


def _first_numeric_column(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    exclude: set[str],
) -> str | None:
    for column in columns:
        if column in exclude:
            continue
        if _column_has_number(rows, column):
            return column
    return None


def _column_has_number(rows: list[dict[str, Any]], column: str) -> bool:
    return any(_is_number(row.get(column)) for row in rows[:20])


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_cross_sectional_metric(column: str) -> bool:
    return column in {
        "market_cap",
        "trailing_pe",
        "forward_pe",
        "price_to_book",
        "dividend_yield",
        "beta",
        "fifty_two_week_high",
        "fifty_two_week_low",
        "revenue_growth",
        "gross_margins",
        "profit_margins",
        "total_revenue",
        "ebitda",
        "debt_to_equity",
        "free_cashflow",
        "last_price",
        "previous_close",
        "day_high",
        "day_low",
    }
