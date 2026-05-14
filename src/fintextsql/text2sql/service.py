from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from fintextsql.core.config import Settings
from fintextsql.core.tickers import extract_tickers
from fintextsql.llm.client import LLMClient, LLMError, LLMMessage
from fintextsql.text2sql.schema import SelectedSchema, select_schema
from fintextsql.text2sql.sql_guard import SQLValidationError, ensure_limit, validate_select_sql


@dataclass(slots=True)
class TextToSQLResult:
    answer: str
    sql: str
    rows: list[dict[str, Any]]
    columns: list[str]
    debug: dict[str, Any] = field(default_factory=dict)


class TextToSQLService:
    def __init__(self, db: Session, settings: Settings, llm: LLMClient):
        self.db = db
        self.settings = settings
        self.llm = llm

    async def answer(self, question: str) -> TextToSQLResult:
        selected_schema = select_schema(question)
        debug: dict[str, Any] = {
            "selected_tables": selected_schema.tables,
            "pipeline": ["load_schema", "schema_selector"],
        }
        deterministic_sql = (
            _price_change_sql(question, self.settings.max_sql_rows)
            or _market_cap_sql(question, self.settings.max_sql_rows)
            or _price_series_sql(question, self.settings.max_sql_rows)
        )
        if deterministic_sql:
            plan = "Use deterministic SQL for the clearly recognized finance metric."
            sql = deterministic_sql
            debug["pipeline"].append("deterministic_sql")
        else:
            plan = await self._plan(question, selected_schema)
            sql = await self._generate_sql(question, selected_schema, plan)
            debug["pipeline"].extend(["planner", "sql_generator", "sql_guard"])

        debug["plan"] = plan
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        error: str | None = None

        for attempt in range(2):
            try:
                rows, columns, sql = self._execute(sql)
                if "execute_sql" not in debug["pipeline"]:
                    debug["pipeline"].append("execute_sql")
                if rows or attempt == 1:
                    break
                debug["pipeline"].append("repair_empty_result")
                sql = await self._repair_sql(
                    question,
                    selected_schema,
                    bad_sql=sql,
                    error="Query executed successfully but returned no rows.",
                )
            except (SQLValidationError, SQLAlchemyError) as exc:
                error = str(exc)
                self.db.rollback()
                if attempt == 1:
                    raise
                debug["pipeline"].append("repair_sql_error")
                sql = await self._repair_sql(question, selected_schema, bad_sql=sql, error=error)

        debug["repair_error"] = error
        answer = await self._explain(question, sql, rows)
        debug["pipeline"].append("explainer")
        return TextToSQLResult(answer=answer, sql=sql, rows=rows, columns=columns, debug=debug)

    async def _plan(self, question: str, selected_schema: SelectedSchema) -> str:
        fallback = (
            "Join companies to the relevant finance table, filter tickers with upper-case symbols, "
            "choose the latest date when the question asks for current/latest values, and return a small result set."
        )
        try:
            return await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "You are a concise finance data analyst. Produce a short SQL plan, not SQL.",
                    ),
                    LLMMessage(
                        "user",
                        f"Question:\n{question}\n\nAvailable schema:\n{selected_schema.schema_text}",
                    ),
                ],
                temperature=0,
                max_tokens=400,
            )
        except LLMError:
            return fallback

    async def _generate_sql(self, question: str, selected_schema: SelectedSchema, plan: str) -> str:
        deterministic_sql = (
            _price_change_sql(question, self.settings.max_sql_rows)
            or _market_cap_sql(question, self.settings.max_sql_rows)
            or _price_series_sql(question, self.settings.max_sql_rows)
        )
        if deterministic_sql:
            return ensure_limit(validate_select_sql(deterministic_sql), self.settings.max_sql_rows)

        try:
            sql = await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "\n".join(
                            [
                                "You generate safe PostgreSQL SELECT queries for a finance database.",
                                "Return SQL only. No prose.",
                                "Rules:",
                                "- Use only the provided tables and columns.",
                                "- Never write INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, COPY.",
                                "- For ticker filters, compare c.ticker to uppercase literals.",
                                "- Use companies c joined by c.id = table.company_id when needed.",
                                "- For time-series questions, preserve the requested time window with a date filter.",
                                "- For chartable time-series comparisons, order by date ASC and use enough rows for all tickers.",
                                f"- Add LIMIT {self.settings.max_sql_rows} unless a smaller limit is explicitly requested.",
                            ]
                        ),
                    ),
                    LLMMessage(
                        "user",
                        f"Question:\n{question}\n\nSchema:\n{selected_schema.schema_text}\n\nPlan:\n{plan}",
                    ),
                ],
                temperature=0,
                max_tokens=800,
            )
            return ensure_limit(validate_select_sql(sql), self.settings.max_sql_rows)
        except (LLMError, SQLValidationError):
            return self._fallback_sql(question)

    async def _repair_sql(
        self,
        question: str,
        selected_schema: SelectedSchema,
        *,
        bad_sql: str,
        error: str,
    ) -> str:
        try:
            repaired = await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "Repair the PostgreSQL SELECT query. Return only SQL. Keep it read-only and limited.",
                    ),
                    LLMMessage(
                        "user",
                        "\n\n".join(
                            [
                                f"Question:\n{question}",
                                f"Schema:\n{selected_schema.schema_text}",
                                f"Bad SQL:\n{bad_sql}",
                                f"Error or issue:\n{error}",
                            ]
                        ),
                    ),
                ],
                temperature=0,
                max_tokens=800,
            )
            return ensure_limit(validate_select_sql(repaired), self.settings.max_sql_rows)
        except (LLMError, SQLValidationError):
            return self._fallback_sql(question)

    def _execute(self, sql: str) -> tuple[list[dict[str, Any]], list[str], str]:
        safe_sql = ensure_limit(validate_select_sql(sql), self.settings.max_sql_rows)
        result = self.db.execute(text(safe_sql))
        columns = list(result.keys())
        rows = [
            {column: _jsonable_value(value) for column, value in row._mapping.items()}
            for row in result.fetchall()
        ]
        return rows, columns, safe_sql

    async def _explain(self, question: str, sql: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return (
                "Không tìm thấy dữ liệu phù hợp trong Postgres. Bạn có thể ingest thêm ticker hoặc mở rộng khoảng thời gian dữ liệu."
            )
        deterministic = _deterministic_price_comparison_explanation(question, rows)
        if deterministic:
            return deterministic
        deterministic_market_cap = _deterministic_market_cap_explanation(question, rows)
        if deterministic_market_cap:
            return deterministic_market_cap

        sample_rows = rows[:8]
        try:
            return await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "You explain SQL results for finance users in Vietnamese. Be concise, use all available rows, and mention caveats. Do not call the rows a user-provided sample.",
                    ),
                    LLMMessage(
                        "user",
                        f"Question:\n{question}\n\nSQL:\n{sql}\n\nTotal rows returned: {len(rows)}\nRows sample:\n{sample_rows}",
                    ),
                ],
                temperature=0.2,
                max_tokens=600,
            )
        except LLMError:
            return _fallback_explanation(rows)

    def _fallback_sql(self, question: str) -> str:
        tickers = extract_tickers(question)
        question_l = question.lower()
        price_change_sql = _price_change_sql(question, self.settings.max_sql_rows)
        if price_change_sql:
            return price_change_sql

        if any(word in question_l for word in ["news", "tin tức", "headline"]):
            where = _ticker_filter(tickers, column="ticker")
            return (
                "SELECT ticker, title, publisher, published_at, link "
                f"FROM news_articles {where} ORDER BY published_at DESC NULLS LAST LIMIT 20"
            )
        if any(word in question_l for word in ["pe", "p/e", "market cap", "beta", "fundamental"]):
            where = _ticker_filter(tickers, column="c.ticker")
            return (
                "SELECT c.ticker, c.name, f.as_of_date, f.market_cap, f.trailing_pe, f.forward_pe, "
                "f.price_to_book, f.beta "
                "FROM companies c JOIN fundamentals f ON f.company_id = c.id "
                f"{where}ORDER BY f.market_cap DESC NULLS LAST, f.as_of_date DESC LIMIT 50"
            )
        filters = [
            condition for condition in [_ticker_condition(tickers, "c.ticker"), _date_condition(question)] if condition
        ]
        where = f"WHERE {' AND '.join(filters)} " if filters else ""
        return (
            "SELECT c.ticker, c.name, p.date, p.close, p.volume "
            "FROM companies c JOIN prices p ON p.company_id = c.id "
            f"{where}ORDER BY p.date ASC, c.ticker ASC LIMIT {_time_series_limit(question, tickers, self.settings.max_sql_rows)}"
        )


def _ticker_filter(tickers: list[str], column: str) -> str:
    condition = _ticker_condition(tickers, column)
    return f"WHERE {condition} " if condition else ""


def _ticker_condition(tickers: list[str], column: str) -> str | None:
    if not tickers:
        return None
    values = ", ".join(f"'{ticker}'" for ticker in tickers)
    return f"{column} IN ({values})"


def _date_condition(question: str) -> str | None:
    question_l = question.lower()
    match = re.search(r"\b(\d{1,4})\s*(ngày|ngay|days?|d)\b", question_l)
    if match:
        return f"p.date >= CURRENT_DATE - INTERVAL '{int(match.group(1))} days'"
    match = re.search(r"\b(\d{1,2})\s*(tháng|thang|months?|mo)\b", question_l)
    if match:
        return f"p.date >= CURRENT_DATE - INTERVAL '{int(match.group(1))} months'"
    match = re.search(r"\b(\d{1,2})\s*(năm|nam|years?|yrs?|y)\b", question_l)
    if match:
        return f"p.date >= CURRENT_DATE - INTERVAL '{int(match.group(1))} years'"
    return None


def _time_series_limit(question: str, tickers: list[str], max_limit: int) -> int:
    ticker_count = max(len(tickers), 1)
    days = _requested_window_days(question) or 365
    estimated_trading_rows = int(ticker_count * days * 0.72) + 80
    return min(max(300, estimated_trading_rows), max_limit)


def _price_change_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    question_l = question.lower()
    if not tickers or not _asks_for_price_change(question_l):
        return None

    days = _requested_window_days(question) or 30
    pct_column = f"pct_change_{days}d"
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    ticker_prefix = f"{ticker_filter} AND " if ticker_filter else ""
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "SELECT ticker, name, start_date, start_close, end_date, end_close,"
        f" ROUND((((end_close - start_close) / NULLIF(start_close, 0)) * 100)::numeric, 2)::float AS {pct_column}"
        " FROM ("
        " SELECT ticker, name,"
        " MAX(CASE WHEN rn_asc = 1 THEN date END) AS start_date,"
        " MAX(CASE WHEN rn_asc = 1 THEN close END) AS start_close,"
        " MAX(CASE WHEN rn_desc = 1 THEN date END) AS end_date,"
        " MAX(CASE WHEN rn_desc = 1 THEN close END) AS end_close"
        " FROM ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date ASC) AS rn_asc,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn_desc"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_prefix}p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        " ) price_window GROUP BY ticker, name"
        f" ) endpoints ORDER BY {pct_column} DESC NULLS LAST LIMIT {limit}"
    )


def _market_cap_sql(question: str, max_limit: int) -> str | None:
    question_l = question.lower()
    if "market cap" not in question_l and "von hoa" not in question_l and "vốn hóa" not in question_l:
        return None
    tickers = _context_tickers(question) or extract_tickers(question)
    if not tickers:
        return None
    ticker_filter = _ticker_filter(tickers, column="c.ticker")
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "SELECT c.ticker, c.name, f.as_of_date, f.market_cap, f.trailing_pe, f.forward_pe, f.beta "
        "FROM companies c JOIN fundamentals f ON f.company_id = c.id "
        f"{ticker_filter}ORDER BY f.market_cap DESC NULLS LAST, c.ticker ASC LIMIT {limit}"
    )


def _price_series_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    question_l = question.lower()
    if not tickers or not _asks_for_price_series(question_l):
        return None

    days = _requested_window_days(question) or 30
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = _time_series_limit(question, tickers, max_limit)
    return (
        "SELECT c.ticker, c.name, p.date, p.close "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '{days} days' "
        f"ORDER BY p.date ASC, c.ticker ASC LIMIT {limit}"
    )


def _asks_for_price_series(question_l: str) -> bool:
    normalized = _normalize_text(question_l)
    if _asks_for_price_change(question_l):
        return False
    has_price = any(
        phrase in question_l
        for phrase in ["close price", "closing price", "giá đóng cửa", "gia dong cua", "close", "giá", "gia"]
    ) or any(phrase in normalized for phrase in ["close price", "closing price", "gia dong cua", "close", "gia"])
    has_series_intent = any(
        phrase in question_l
        for phrase in ["so sánh", "so sanh", "compare", "chart", "biểu đồ", "bieu do", "vẽ", "ve", "plot"]
    ) or any(phrase in normalized for phrase in ["so sanh", "compare", "chart", "bieu do", "ve", "plot"])
    return has_price and has_series_intent


def _asks_for_price_change(question_l: str) -> bool:
    normalized = _normalize_text(question_l)
    if any(phrase in question_l for phrase in ["% tăng/giảm", "% tăng giảm", "phần trăm tăng giảm", "tăng/giảm"]):
        return True
    return any(
        phrase in question_l
        for phrase in [
            "% tang giam",
            "% tăng giảm",
            "phan tram tang giam",
            "phần trăm tăng giảm",
            "percent change",
            "percentage change",
            "pct change",
            "return",
            "returns",
            "performance",
        ]
    ) or any(
        phrase in normalized
        for phrase in [
            "% tang/giam",
            "% tang giam",
            "phan tram tang giam",
            "tang/giam",
            "percent change",
            "percentage change",
            "pct change",
            "return",
            "returns",
            "performance",
        ]
    )


def _context_tickers(question: str) -> list[str]:
    match = re.search(r"^Context tickers:\s*([A-Z0-9.,\s-]+)$", question, re.MULTILINE)
    if not match:
        return []
    return extract_tickers(match.group(1))


def _requested_window_days(question: str) -> int | None:
    question_l = question.lower()
    match = re.search(r"\b(\d{1,4})\s*(ngày|ngay|days?|d)\b", question_l)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{1,2})\s*(tháng|thang|months?|mo)\b", question_l)
    if match:
        return int(match.group(1)) * 31
    match = re.search(r"\b(\d{1,2})\s*(năm|nam|years?|yrs?|y)\b", question_l)
    if match:
        return int(match.group(1)) * 366
    normalized = _normalize_text(question_l)
    match = re.search(r"\b(\d{1,4})\s*(ngay|days?|d)\b", normalized)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{1,2})\s*(thang|months?|mo)\b", normalized)
    if match:
        return int(match.group(1)) * 31
    match = re.search(r"\b(\d{1,2})\s*(nam|years?|yrs?|y)\b", normalized)
    if match:
        return int(match.group(1)) * 366
    return None


def _normalize_text(value: str) -> str:
    candidates = [value]
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except UnicodeError:
            continue
        if repaired != value:
            candidates.append(repaired)

    normalized_candidates: list[str] = []
    for candidate in candidates:
        decomposed = unicodedata.normalize("NFD", candidate)
        without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
        normalized_candidates.append(without_accents.replace("đ", "d").replace("Đ", "D").lower())
    return " ".join(normalized_candidates)


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _deterministic_market_cap_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "market_cap" not in rows[0] or "ticker" not in rows[0]:
        return None
    if "market cap" not in question.lower() and "von hoa" not in question.lower() and "vốn hóa" not in question.lower():
        return None

    usable = [row for row in rows if _is_number(row.get("market_cap")) and row.get("ticker")]
    if not usable:
        return None
    ordered = sorted(usable, key=lambda row: float(row["market_cap"]), reverse=True)
    lines = [
        "Đã lấy dữ liệu market cap cho các mã được hỏi. Biểu đồ trong chat dùng `ticker` làm trục X và `market_cap` làm trục Y.",
        "",
        "### Nhận xét nhanh",
    ]
    for row in ordered:
        as_of = f" tại {row.get('as_of_date')}" if row.get("as_of_date") else ""
        lines.append(f"- {row['ticker']}: market cap khoảng {_format_large_number(float(row['market_cap']))}{as_of}.")
    leader = ordered[0]["ticker"]
    lines.extend(
        [
            "",
            "### Kết luận ngắn",
            f"- Trong các mã đang hiển thị, {leader} có market cap lớn nhất.",
            "- Đây là chỉ tiêu quy mô doanh nghiệp, không phải trực tiếp là hiệu suất đầu tư.",
        ]
    )
    return "\n".join(lines)


def _deterministic_price_comparison_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "date" not in rows[0]:
        return None

    long_form = _long_price_change_explanation(question, rows)
    if long_form:
        return long_form

    close_columns = [
        column
        for column in rows[0]
        if column.lower().endswith("_close") and any(_is_number(row.get(column)) for row in rows)
    ]
    if len(close_columns) < 2:
        return None

    ordered_rows = sorted(rows, key=lambda row: str(row.get("date", "")))
    dates = [str(row.get("date")) for row in ordered_rows if row.get("date")]
    if not dates:
        return None

    summaries = [_series_summary(column, ordered_rows) for column in close_columns]
    summaries = [summary for summary in summaries if summary is not None]
    if len(summaries) < 2:
        return None

    comparison = _constant_higher_summary(summaries, ordered_rows)
    leader = max(summaries, key=lambda item: item["pct_change"])

    lines = [
        f"Kết quả đang so sánh giá đóng cửa theo ngày trong {len(ordered_rows)} phiên giao dịch, từ {dates[0]} đến {dates[-1]}.",
        "",
        "### Nhận xét nhanh",
    ]
    if comparison:
        lines.append(f"- {comparison}")
    for summary in summaries:
        lines.append(
            "- {ticker}: từ {first} lên {last}, thay đổi {delta} điểm ({pct}).".format(
                ticker=summary["ticker"],
                first=_format_number(summary["first"]),
                last=_format_number(summary["last"]),
                delta=_format_signed_number(summary["delta"]),
                pct=_format_percent(summary["pct_change"]),
            )
        )
    lines.extend(
        [
            f"- Xét theo phần trăm thay đổi trong khoảng dữ liệu này, {leader['ticker']} tăng mạnh nhất.",
            "",
            "### Kết luận ngắn",
            "- So sánh mức giá tuyệt đối chỉ cho biết cổ phiếu nào có giá mỗi share cao hơn.",
            "- Để so hiệu suất đầu tư, nên nhìn vào phần trăm thay đổi hoặc chuỗi giá đã chuẩn hóa về cùng mốc 100.",
            "",
            "### Caveat",
            "- Khoảng 30 ngày lịch thường có ít hơn 30 phiên giao dịch vì cuối tuần và ngày nghỉ.",
            "- Kết quả phụ thuộc vào dữ liệu đã ingest trong Postgres tại thời điểm truy vấn.",
        ]
    )
    return "\n".join(lines)


def _long_price_change_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    first_row = rows[0]
    if not {"ticker", "date", "close"}.issubset(first_row):
        return None

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not row.get("date") or not _is_number(row.get("close")):
            continue
        grouped.setdefault(ticker, []).append(row)
    if len(grouped) < 2:
        return None

    summaries: list[dict[str, Any]] = []
    total_points = 0
    for ticker, ticker_rows in sorted(grouped.items()):
        ordered = sorted(ticker_rows, key=lambda row: str(row.get("date", "")))
        total_points += len(ordered)
        first = ordered[0]
        last = ordered[-1]
        first_close = float(first["close"])
        last_close = float(last["close"])
        delta = last_close - first_close
        pct_change = (delta / first_close) * 100 if first_close else 0.0
        summaries.append(
            {
                "ticker": ticker,
                "count": len(ordered),
                "first_date": str(first["date"]),
                "last_date": str(last["date"]),
                "first": first_close,
                "last": last_close,
                "delta": delta,
                "pct_change": pct_change,
            }
        )

    if len(summaries) < 2:
        return None

    start_date = min(summary["first_date"] for summary in summaries)
    end_date = max(summary["last_date"] for summary in summaries)
    leader = max(summaries, key=lambda item: item["pct_change"])
    laggard = min(summaries, key=lambda item: item["pct_change"])

    days = _requested_window_days(question)
    window_label = f"{days} ngày gần nhất" if days else "khoảng dữ liệu được trả về"
    chart_requested = any(word in question.lower() for word in ["chart", "biểu đồ", "bieu do", "vẽ", "ve", "plot"])
    lines = [
        (
            f"Đã lấy dữ liệu giá đóng cửa cho {len(summaries)} mã trong {window_label}, "
            f"từ {start_date} đến {end_date}."
        ),
    ]
    if chart_requested:
        lines.append("Chart trong chat dùng `date` làm trục X, `close` làm trục Y và tách từng mã thành một đường riêng.")
    lines.extend(["", "### Tóm tắt nhanh"])
    for summary in sorted(summaries, key=lambda item: item["pct_change"], reverse=True):
        lines.append(
            "- {ticker}: {first} → {last}, thay đổi {delta} điểm ({pct}), trên {count} phiên.".format(
                ticker=summary["ticker"],
                first=_format_number(summary["first"]),
                last=_format_number(summary["last"]),
                delta=_format_signed_number(summary["delta"]),
                pct=_format_percent(summary["pct_change"]),
                count=summary["count"],
            )
        )

    lines.extend(
        [
            "",
            "### Kết luận ngắn",
            f"- Mã tăng mạnh nhất theo % trong khoảng này là {leader['ticker']} ({_format_percent(leader['pct_change'])}).",
            f"- Mã yếu nhất theo % trong khoảng này là {laggard['ticker']} ({_format_percent(laggard['pct_change'])}).",
            "- Nếu mục tiêu là so hiệu suất đầu tư, nên nhìn thêm % thay đổi hoặc normalized price thay vì chỉ so giá tuyệt đối.",
            "",
            "### Caveat",
            f"- {window_label.capitalize()} là theo ngày lịch trong SQL, nên số phiên giao dịch thực tế sẽ ít hơn số ngày lịch.",
            "- Kết quả phụ thuộc vào dữ liệu đã ingest trong Postgres tại thời điểm truy vấn.",
        ]
    )
    return "\n".join(lines)


def _series_summary(column: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    values = [
        (str(row.get("date")), float(row[column]))
        for row in rows
        if row.get("date") and _is_number(row.get(column))
    ]
    if len(values) < 2:
        return None
    first_date, first_value = values[0]
    last_date, last_value = values[-1]
    delta = last_value - first_value
    pct_change = (delta / first_value) * 100 if first_value else 0.0
    return {
        "column": column,
        "ticker": column.removesuffix("_close").upper(),
        "first_date": first_date,
        "last_date": last_date,
        "first": first_value,
        "last": last_value,
        "delta": delta,
        "pct_change": pct_change,
    }


def _constant_higher_summary(summaries: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str | None:
    if len(summaries) != 2:
        return None
    left, right = summaries
    comparable_rows = [
        row for row in rows if _is_number(row.get(left["column"])) and _is_number(row.get(right["column"]))
    ]
    if not comparable_rows:
        return None
    left_always_higher = all(float(row[left["column"]]) > float(row[right["column"]]) for row in comparable_rows)
    right_always_higher = all(float(row[right["column"]]) > float(row[left["column"]]) for row in comparable_rows)
    if left_always_higher:
        return f"{left['ticker']} luôn có close price cao hơn {right['ticker']} trong các phiên được trả về."
    if right_always_higher:
        return f"{right['ticker']} luôn có close price cao hơn {left['ticker']} trong các phiên được trả về."
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_number(value: float) -> str:
    return f"{value:,.2f}"


def _format_signed_number(value: float) -> str:
    return f"{value:+,.2f}"


def _format_percent(value: float) -> str:
    return f"{value:+.2f}%"


def _format_large_number(value: float) -> str:
    units = [("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000)]
    for suffix, divisor in units:
        if abs(value) >= divisor:
            return f"{value / divisor:,.2f}{suffix}"
    return f"{value:,.0f}"


def _fallback_explanation(rows: list[dict[str, Any]]) -> str:
    count = len(rows)
    first = rows[0]
    keys = ", ".join(first.keys())
    return f"Tìm thấy {count} dòng dữ liệu. Các cột chính gồm: {keys}."
