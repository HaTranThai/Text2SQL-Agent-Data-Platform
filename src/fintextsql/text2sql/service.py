from __future__ import annotations

import operator
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from fintextsql.core.config import Settings
from fintextsql.core.tickers import extract_tickers
from fintextsql.llm.client import LLMClient, LLMError, LLMMessage
from fintextsql.text2sql.knowledge import Knowledge, extract_knowledge
from fintextsql.text2sql.schema import SelectedSchema, generation_schema_text, select_schema
from fintextsql.text2sql.sql_guard import (
    SQLValidationError,
    clean_sql,
    ensure_limit,
    validate_select_sql,
)

CANDIDATE_COUNT = 3


@dataclass(slots=True)
class TextToSQLResult:
    answer: str
    sql: str
    rows: list[dict[str, Any]]
    columns: list[str]
    debug: dict[str, Any] = field(default_factory=dict)


class T2SState(TypedDict, total=False):
    """State flowing through the text-to-SQL LangGraph pipeline."""

    question: str
    knowledge: str
    selected_tables: list[str]
    plan: str
    sql: str
    candidates: list[str]
    rows: list[dict[str, Any]]
    columns: list[str]
    attempt: int
    error: str | None
    last_error: str | None
    answer: str
    pipeline: Annotated[list[str], operator.add]


class TextToSQLService:
    def __init__(self, db: Session, settings: Settings, llm: LLMClient):
        self.db = db
        self.settings = settings
        self.llm = llm

    async def answer(self, question: str) -> TextToSQLResult:
        graph = self._build_graph()
        final = await graph.ainvoke({"question": question, "attempt": 0, "pipeline": []})
        return TextToSQLResult(
            answer=final.get("answer", ""),
            sql=final.get("sql", ""),
            rows=final.get("rows", []),
            columns=final.get("columns", []),
            debug={
                "selected_tables": final.get("selected_tables", []),
                "pipeline": final.get("pipeline", []),
                "plan": final.get("plan", ""),
                "repair_error": final.get("last_error"),
            },
        )

    def _build_graph(self):
        service = self
        max_rows = self.settings.max_sql_rows

        async def build_sql(state: T2SState) -> dict[str, Any]:
            question = state["question"]
            knowledge = extract_knowledge(question)
            selected_schema = select_schema(question)
            deterministic_sql = _deterministic_sql(question, max_rows)
            if deterministic_sql:
                return {
                    "knowledge": knowledge.as_prompt(),
                    "selected_tables": selected_schema.tables,
                    "plan": "Use deterministic SQL for the clearly recognized finance metric.",
                    "sql": deterministic_sql,
                    "candidates": [deterministic_sql],
                    "pipeline": ["load_schema", "knowledge_extractor", "schema_selector", "deterministic_sql"],
                }
            plan = await service._plan(question, selected_schema, knowledge)
            candidates = await service._generate_candidates(question, selected_schema, plan, knowledge)
            return {
                "knowledge": knowledge.as_prompt(),
                "selected_tables": selected_schema.tables,
                "plan": plan,
                "sql": candidates[0],
                "candidates": candidates,
                "pipeline": [
                    "load_schema",
                    "knowledge_extractor",
                    "schema_selector",
                    "planner",
                    "candidate_generator",
                    "sql_guard",
                ],
            }

        def execute(state: T2SState) -> dict[str, Any]:
            candidates = state.get("candidates") or ([state["sql"]] if state.get("sql") else [])
            multi = len(candidates) > 1
            best: tuple[tuple[int, int], list[dict[str, Any]], list[str], str] | None = None
            last_error: str | None = None
            for candidate in candidates:
                try:
                    rows, columns, sql = service._execute(candidate)
                except (SQLValidationError, SQLAlchemyError) as exc:
                    service.db.rollback()
                    last_error = str(exc)
                    if not multi:
                        break
                    continue
                # Execute-based score: prefer queries that return rows, then shorter/cleaner SQL.
                score = (2 if rows else 1, -len(sql))
                if best is None or score > best[0]:
                    best = (score, rows, columns, sql)
                if not multi:
                    break
            if best is None:
                message = last_error or "SQL execution returned no usable candidate"
                return {"error": message, "last_error": message}
            _score, rows, columns, sql = best
            pipeline: list[str] = []
            if multi and "candidate_selector" not in state.get("pipeline", []):
                pipeline.append("candidate_selector")
            if "execute_sql" not in state.get("pipeline", []):
                pipeline.append("execute_sql")
            return {"rows": rows, "columns": columns, "sql": sql, "error": None, "pipeline": pipeline}

        def route_after_execute(state: T2SState) -> str:
            if state.get("error"):
                return "fail" if state.get("attempt", 0) >= 1 else "repair_error"
            rows = state.get("rows") or []
            if (
                not rows
                and state.get("attempt", 0) < 1
                and not _should_keep_empty_result(state["question"], state["sql"])
            ):
                return "repair_empty"
            return "explain"

        async def repair_error(state: T2SState) -> dict[str, Any]:
            selected_schema = select_schema(state["question"])
            sql = await service._repair_sql(
                state["question"], selected_schema, bad_sql=state["sql"], error=state.get("error") or ""
            )
            return {
                "sql": sql,
                "candidates": [sql],
                "attempt": state.get("attempt", 0) + 1,
                "error": None,
                "pipeline": ["repair_sql_error"],
            }

        async def repair_empty(state: T2SState) -> dict[str, Any]:
            selected_schema = select_schema(state["question"])
            sql = await service._repair_sql(
                state["question"],
                selected_schema,
                bad_sql=state["sql"],
                error="Query executed successfully but returned no rows.",
            )
            return {
                "sql": sql,
                "candidates": [sql],
                "attempt": state.get("attempt", 0) + 1,
                "pipeline": ["repair_empty_result"],
            }

        def fail(state: T2SState) -> dict[str, Any]:
            # Repair exhausted: return a friendly message instead of raising a 500.
            return {
                "answer": (
                    "Xin lỗi, mình chưa tạo được truy vấn SQL hợp lệ cho câu hỏi này. "
                    "Bạn thử diễn đạt cụ thể hơn (mã cổ phiếu, khoảng thời gian, chỉ số cần xem) giúp mình nhé."
                ),
                "rows": [],
                "columns": [],
                "pipeline": ["fail_handler"],
            }

        async def explain(state: T2SState) -> dict[str, Any]:
            answer = _sanitize_answer(
                await service._explain(state["question"], state["sql"], state.get("rows") or [])
            )
            return {"answer": answer, "pipeline": ["explainer"]}

        graph = StateGraph(T2SState)
        graph.add_node("build_sql", build_sql)
        graph.add_node("execute", execute)
        graph.add_node("repair_error", repair_error)
        graph.add_node("repair_empty", repair_empty)
        graph.add_node("explain", explain)
        graph.add_node("fail", fail)
        graph.add_edge(START, "build_sql")
        graph.add_edge("build_sql", "execute")
        graph.add_conditional_edges(
            "execute",
            route_after_execute,
            {
                "repair_error": "repair_error",
                "repair_empty": "repair_empty",
                "explain": "explain",
                "fail": "fail",
            },
        )
        graph.add_edge("repair_error", "execute")
        graph.add_edge("repair_empty", "execute")
        graph.add_edge("explain", END)
        graph.add_edge("fail", END)
        return graph.compile()

    async def _plan(self, question: str, selected_schema: SelectedSchema, knowledge: Knowledge | None = None) -> str:
        fallback = (
            "Join companies to the relevant finance table, filter tickers with upper-case symbols, "
            "choose the latest date when the question asks for current/latest values, and return a small result set."
        )
        knowledge_block = f"\n\nExtracted knowledge:\n{knowledge.as_prompt()}" if knowledge and knowledge.as_prompt() else ""
        try:
            return await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "You are a concise finance data analyst. Produce a short SQL plan, not SQL.",
                    ),
                    LLMMessage(
                        "user",
                        f"Question:\n{question}\n\nAvailable schema:\n{generation_schema_text()}"
                        f"\n\nMost relevant tables for this question: {', '.join(selected_schema.tables)}"
                        f"{knowledge_block}",
                    ),
                ],
                temperature=0,
                max_tokens=400,
            )
        except LLMError:
            return fallback

    async def _generate_candidates(
        self,
        question: str,
        selected_schema: SelectedSchema,
        plan: str,
        knowledge: Knowledge | None = None,
        count: int = CANDIDATE_COUNT,
    ) -> list[str]:
        """Generate up to `count` distinct candidate SELECT queries in a single LLM call.

        Returns validated, LIMIT-capped SQL strings; falls back to a heuristic query if the
        LLM is unavailable or every candidate fails validation.
        """
        knowledge_block = (
            f"\n\nExtracted knowledge:\n{knowledge.as_prompt()}" if knowledge and knowledge.as_prompt() else ""
        )
        try:
            raw = await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "\n".join(
                            [
                                "You generate safe PostgreSQL SELECT queries for a finance database.",
                                f"Propose {count} DISTINCT candidate queries that each fully answer the question.",
                                "Vary the approach across candidates (e.g. window functions vs CTE vs aggregation) so a selector can pick the best.",
                                "Output ONLY the SQL, each candidate in its own ```sql fenced block. No prose.",
                                "Rules for every candidate:",
                                "- Use only the provided tables and columns.",
                                "- Never write INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, COPY.",
                                "- For ticker filters, compare c.ticker to uppercase literals.",
                                "- Use companies c joined by c.id = table.company_id when needed.",
                                "- For time-series questions, preserve the requested time window with a date filter.",
                                "- If the user does NOT specify a time window for a price/volume query, default to the most recent ~365 days (e.g. p.date >= CURRENT_DATE - INTERVAL '365 days'). NEVER return the oldest rows in the table.",
                                "- For chartable time-series comparisons, order by date ASC and use enough rows for all tickers.",
                                f"- Add LIMIT {self.settings.max_sql_rows} unless a smaller limit is explicitly requested.",
                            ]
                        ),
                    ),
                    LLMMessage(
                        "user",
                        f"Question:\n{question}\n\nSchema:\n{generation_schema_text()}"
                        f"\n\nMost relevant tables for this question: {', '.join(selected_schema.tables)}"
                        f"{knowledge_block}"
                        f"\n\nPlan:\n{plan}",
                    ),
                ],
                temperature=0.3,
                max_tokens=1600,
            )
        except LLMError:
            return [self._fallback_sql(question)]

        candidates: list[str] = []
        seen: set[str] = set()
        for raw_sql in _parse_sql_candidates(raw):
            try:
                safe = ensure_limit(validate_select_sql(raw_sql), self.settings.max_sql_rows)
            except SQLValidationError:
                continue
            if safe not in seen:
                seen.add(safe)
                candidates.append(safe)
            if len(candidates) >= count:
                break
        return candidates or [self._fallback_sql(question)]

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
                                f"Schema:\n{generation_schema_text()}",
                                f"Most relevant tables: {', '.join(selected_schema.tables)}",
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
            nearest_dates = self._nearest_price_dates_for_exact_request(question)
            no_rows = _deterministic_empty_result_explanation(question)
            if no_rows:
                if nearest_dates:
                    no_rows = f"{no_rows}\n\n### Phiên gần nhất có trong dữ liệu\n{nearest_dates}"
                return no_rows
            volume_gap = self._missing_requested_year_explanation(question)
            if volume_gap:
                return volume_gap
            return (
                "Không tìm thấy dữ liệu phù hợp trong Postgres. Bạn có thể ingest thêm ticker hoặc mở rộng khoảng thời gian dữ liệu."
            )
        deterministic_ohlc = _deterministic_exact_ohlc_explanation(question, rows)
        if deterministic_ohlc:
            return deterministic_ohlc
        deterministic_month_data = _deterministic_month_price_data_explanation(question, rows)
        if deterministic_month_data:
            return deterministic_month_data
        deterministic_month_close = _deterministic_month_close_explanation(question, rows)
        if deterministic_month_close:
            return deterministic_month_close
        deterministic_aggregate = _deterministic_aggregate_explanation(question, rows)
        if deterministic_aggregate:
            return deterministic_aggregate
        deterministic_up_sessions = _deterministic_up_sessions_explanation(question, rows)
        if deterministic_up_sessions:
            return deterministic_up_sessions
        deterministic_streak = _deterministic_streak_explanation(question, rows)
        if deterministic_streak:
            return deterministic_streak
        deterministic_ma = _deterministic_ma_explanation(question, rows)
        if deterministic_ma:
            return deterministic_ma
        deterministic_recovery = _deterministic_recovery_explanation(question, rows)
        if deterministic_recovery:
            return deterministic_recovery
        deterministic_corr = _deterministic_correlation_explanation(question, rows)
        if deterministic_corr:
            return deterministic_corr
        deterministic_year_return = _deterministic_year_return_explanation(question, rows)
        if deterministic_year_return:
            return deterministic_year_return
        deterministic_drawdown = _deterministic_drawdown_explanation(question, rows)
        if deterministic_drawdown:
            return deterministic_drawdown
        deterministic_volume_compare = _deterministic_volume_date_vs_quarter_max_explanation(question, rows)
        if deterministic_volume_compare:
            return deterministic_volume_compare
        deterministic_top_volume = _deterministic_top_volume_explanation(question, rows)
        if deterministic_top_volume:
            return deterministic_top_volume
        deterministic_volume = _deterministic_latest_volume_explanation(question, rows)
        if deterministic_volume:
            return deterministic_volume
        deterministic_lowest_volume = _deterministic_lowest_volume_explanation(question, rows)
        if deterministic_lowest_volume:
            return deterministic_lowest_volume
        deterministic_risk_adjusted = _deterministic_return_volatility_explanation(question, rows)
        if deterministic_risk_adjusted:
            return deterministic_risk_adjusted
        deterministic_outperform_spy = _deterministic_outperform_spy_lower_volatility_explanation(question, rows)
        if deterministic_outperform_spy:
            return deterministic_outperform_spy
        deterministic_latest_close = _deterministic_latest_close_explanation(question, rows)
        if deterministic_latest_close:
            return deterministic_latest_close
        deterministic_monthly_high = _deterministic_monthly_high_close_explanation(question, rows)
        if deterministic_monthly_high:
            return deterministic_monthly_high
        deterministic_high = _deterministic_high_close_explanation(question, rows)
        if deterministic_high:
            return deterministic_high
        deterministic_quarterly = _deterministic_quarterly_close_explanation(question, rows)
        if deterministic_quarterly:
            return deterministic_quarterly
        deterministic = _deterministic_price_comparison_explanation(question, rows)
        if deterministic:
            return deterministic
        deterministic_market_cap = _deterministic_market_cap_explanation(question, rows)
        if deterministic_market_cap:
            return deterministic_market_cap

        rows_for_explanation = _rows_for_explanation(rows)
        profile = _result_profile(rows)
        try:
            return await self.llm.chat(
                [
                    LLMMessage(
                        "system",
                        "\n".join(
                            [
                                "You explain SQL results for finance users in Vietnamese.",
                                "Be concise and numerically careful.",
                                "Never infer the full date range from only the first rows.",
                                "Use the provided result profile for row count and min/max dates.",
                                "If rows_for_explanation is marked incomplete, say it is a preview and avoid claiming it contains all rows.",
                                "Do not say the query failed when SQL returned rows.",
                            ]
                        ),
                    ),
                    LLMMessage(
                        "user",
                        "\n\n".join(
                            [
                                f"Question:\n{question}",
                                f"SQL:\n{sql}",
                                f"Result profile:\n{profile}",
                                f"Rows for explanation:\n{rows_for_explanation}",
                            ]
                        ),
                    ),
                ],
                temperature=0.2,
                max_tokens=600,
            )
        except LLMError:
            return _fallback_explanation(rows)

    def _nearest_price_dates_for_exact_request(self, question: str) -> str | None:
        exact_date = _requested_exact_date(question)
        tickers = _context_tickers(question) or extract_tickers(question)
        if not exact_date or not tickers or not _asks_for_ohlc(question):
            return None

        ticker_filter = _ticker_condition(tickers, "c.ticker")
        if not ticker_filter:
            return None
        nearest_sql = (
            "SELECT c.ticker, p.date, p.open, p.high, p.low, p.close, p.volume "
            "FROM companies c JOIN prices p ON p.company_id = c.id "
            f"WHERE {ticker_filter} AND p.date BETWEEN DATE '{exact_date}' - INTERVAL '7 days' "
            f"AND DATE '{exact_date}' + INTERVAL '7 days' "
            "ORDER BY ABS(p.date - DATE '{date}') ASC, p.date ASC, c.ticker ASC LIMIT 4"
        ).format(date=exact_date)
        try:
            rows, _columns, _sql = self._execute(nearest_sql)
        except (SQLValidationError, SQLAlchemyError):
            self.db.rollback()
            return None
        if not rows:
            return None
        lines: list[str] = []
        for row in rows:
            lines.append(
                "- {ticker} {date}: open {open}, high {high}, low {low}, close {close}, volume {volume}.".format(
                    ticker=row.get("ticker"),
                    date=str(row.get("date"))[:10],
                    open=_format_number(float(row["open"])) if _is_number(row.get("open")) else "N/A",
                    high=_format_number(float(row["high"])) if _is_number(row.get("high")) else "N/A",
                    low=_format_number(float(row["low"])) if _is_number(row.get("low")) else "N/A",
                    close=_format_number(float(row["close"])) if _is_number(row.get("close")) else "N/A",
                    volume=_format_integer(row.get("volume")) if _is_number(row.get("volume")) else "N/A",
                )
            )
        return "\n".join(lines)

    def _missing_requested_year_explanation(self, question: str) -> str | None:
        year = _requested_year(question)
        tickers = _context_tickers(question) or extract_tickers(question)
        if not year or not tickers:
            return None

        ranges = self._available_price_ranges(tickers)
        ticker_text = ", ".join(tickers)
        metric_text = "dữ liệu phù hợp"
        if _asks_for_lowest_volume(question):
            metric_text = "dữ liệu volume"
        elif _asks_for_high_close(question):
            metric_text = "dữ liệu giá đóng cửa"
        lines = [
            f"Không tìm thấy {metric_text} cho {ticker_text} trong năm {year} trong Postgres.",
            "",
            "### Dữ liệu hiện có",
        ]
        if ranges:
            for row in ranges:
                lines.append(f"- {row['ticker']}: từ {row['min_date']} đến {row['max_date']}, {row['row_count']} dòng.")
        else:
            lines.append(f"- Chưa có dữ liệu giá cho {ticker_text}.")
        lines.extend(
            [
                "",
                "### Lưu ý",
                f"- Truy vấn đã giữ đúng năm {year}; kết quả rỗng vì dữ liệu năm đó chưa được ingest cho ticker được hỏi.",
                "- Cần sync thêm dữ liệu lịch sử bao phủ năm này nếu muốn tính đúng câu hỏi.",
            ]
        )
        return "\n".join(lines)

    def _available_price_ranges(self, tickers: list[str]) -> list[dict[str, Any]]:
        ticker_filter = _ticker_condition(tickers, "c.ticker")
        if not ticker_filter:
            return []
        sql = (
            "SELECT c.ticker, MIN(p.date) AS min_date, MAX(p.date) AS max_date, COUNT(*) AS row_count "
            "FROM companies c JOIN prices p ON p.company_id = c.id "
            f"WHERE {ticker_filter} GROUP BY c.ticker ORDER BY c.ticker ASC LIMIT 20"
        )
        try:
            rows, _columns, _sql = self._execute(sql)
        except (SQLValidationError, SQLAlchemyError):
            self.db.rollback()
            return []
        return rows

    def _fallback_sql(self, question: str) -> str:
        tickers = extract_tickers(question)
        question_l = question.lower()
        deterministic_sql = _deterministic_sql(question, self.settings.max_sql_rows)
        if deterministic_sql:
            return deterministic_sql
        price_change_sql = _price_change_sql(question, self.settings.max_sql_rows)
        if price_change_sql:
            return price_change_sql
        quarterly_sql = _quarterly_close_comparison_sql(question, self.settings.max_sql_rows)
        if quarterly_sql:
            return quarterly_sql
        monthly_high_sql = _monthly_high_close_sql(question, self.settings.max_sql_rows)
        if monthly_high_sql:
            return monthly_high_sql
        high_close_sql = _high_close_sql(question, self.settings.max_sql_rows)
        if high_close_sql:
            return high_close_sql

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
        ticker_cond = _ticker_condition(tickers, "c.ticker")
        date_cond = _date_condition(question)
        if not date_cond:
            # Default to the most recent ~year so a vague "data of X" returns recent rows,
            # not the oldest data in the database.
            default_days = _requested_window_days(question) or 365
            date_cond = f"p.date >= CURRENT_DATE - INTERVAL '{default_days} days'"
        filters = [condition for condition in [ticker_cond, date_cond] if condition]
        where = f"WHERE {' AND '.join(filters)} " if filters else ""
        return (
            "SELECT c.ticker, c.name, p.date, p.close, p.volume "
            "FROM companies c JOIN prices p ON p.company_id = c.id "
            f"{where}ORDER BY p.date ASC, c.ticker ASC LIMIT {_time_series_limit(question, tickers, self.settings.max_sql_rows)}"
        )


def _parse_sql_candidates(raw: str) -> list[str]:
    """Split an LLM response into candidate SQL strings.

    Prefers ```sql fenced blocks; falls back to treating the whole response as one query.
    """
    blocks = re.findall(r"```(?:sql)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    if not blocks:
        blocks = [raw]
    candidates: list[str] = []
    for block in blocks:
        cleaned = clean_sql(block)
        if cleaned:
            candidates.append(cleaned)
    return candidates


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


def _deterministic_sql(question: str, max_limit: int) -> str | None:
    return (
        _exact_ohlc_sql(question, max_limit)
        or _avg_close_quarter_sql(question, max_limit)
        or _avg_close_year_sql(question, max_limit)
        or _volume_date_vs_quarter_max_sql(question, max_limit)
        or _year_high_close_sql(question, max_limit)
        or _top_volume_sql(question, max_limit)
        or _lowest_volume_sql(question, max_limit)
        or _latest_volume_sql(question, max_limit)
        or _count_up_sessions_sql(question, max_limit)
        or _longest_down_streak_sql(question, max_limit)
        or _correlation_sql(question, max_limit)
        or _ma_screen_sql(question, max_limit)
        or _ma_series_sql(question, max_limit)
        or _year_return_sql(question, max_limit)
        or _max_drawdown_year_sql(question, max_limit)
        or _ma_compare_sql(question, max_limit)
        or _recovery_after_biggest_drop_sql(question, max_limit)
        or _volume_price_correlation_sql(question, max_limit)
        or _month_price_data_sql(question, max_limit)
        or _month_close_sql(question, max_limit)
        or _latest_close_sql(question, max_limit)
        or _outperform_spy_lower_volatility_sql(question, max_limit)
        or _return_volatility_sql(question, max_limit)
        or _price_change_sql(question, max_limit)
        or _quarterly_close_comparison_sql(question, max_limit)
        or _monthly_high_close_sql(question, max_limit)
        or _high_close_sql(question, max_limit)
        or _market_cap_sql(question, max_limit)
        or _price_series_sql(question, max_limit)
    )


def _exact_ohlc_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    date_range = _requested_date_range(question)
    exact_date = _requested_exact_date(question)
    if not tickers or not (date_range or exact_date) or not _asks_for_ohlc(question):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    if date_range:
        start_date, end_date = date_range
        limit = min(max(len(tickers) * 40, 40), max_limit)
        return (
            "SELECT c.ticker, c.name, p.date, p.open, p.high, p.low, p.close, p.volume "
            "FROM companies c JOIN prices p ON p.company_id = c.id "
            f"WHERE {ticker_filter} AND p.date >= DATE '{start_date}' AND p.date <= DATE '{end_date}' "
            f"ORDER BY p.date ASC, c.ticker ASC LIMIT {limit}"
        )
    limit = min(max(len(tickers), 1), max_limit)
    if _asks_for_nearest_day(question):
        return (
            "SELECT ticker, name, date, open, high, low, close, volume "
            "FROM ("
            " SELECT c.ticker, c.name, p.date, p.open, p.high, p.low, p.close, p.volume,"
            f" ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY ABS(p.date - DATE '{exact_date}') ASC, p.date ASC) AS rn"
            " FROM companies c JOIN prices p ON p.company_id = c.id"
            f" WHERE {ticker_filter}"
            f" AND p.date BETWEEN DATE '{exact_date}' - INTERVAL '10 days' AND DATE '{exact_date}' + INTERVAL '10 days'"
            " ) nearest_day "
            f"WHERE rn = 1 ORDER BY ticker ASC LIMIT {limit}"
        )
    return (
        "SELECT c.ticker, c.name, p.date, p.open, p.high, p.low, p.close, p.volume "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date = DATE '{exact_date}' "
        f"ORDER BY c.ticker ASC LIMIT {limit}"
    )


def _avg_close_year_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    if not tickers or not year or not _asks_for_avg_close(question):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "SELECT c.ticker, c.name, "
        f"DATE '{year}-01-01' AS period_start, DATE '{year}-12-31' AS period_end, "
        "ROUND(AVG(p.close)::numeric, 4)::float AS avg_close, COUNT(*) AS trading_days "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01' "
        f"GROUP BY c.ticker, c.name ORDER BY avg_close DESC NULLS LAST LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _avg_close_quarter_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    quarter = _requested_quarter(question)
    if not tickers or not quarter or not _asks_for_avg_close(question):
        return None
    year, quarter_number = quarter
    month = (quarter_number - 1) * 3 + 1
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if quarter_number == 4 else date(year, month + 3, 1)
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "SELECT c.ticker, c.name, "
        f"DATE '{start.isoformat()}' AS period_start, DATE '{(end).isoformat()}' AS period_end, "
        "ROUND(AVG(p.close)::numeric, 4)::float AS avg_close, COUNT(*) AS trading_days "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= DATE '{start.isoformat()}' AND p.date < DATE '{end.isoformat()}' "
        f"GROUP BY c.ticker, c.name ORDER BY avg_close DESC NULLS LAST LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _year_high_close_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    if not tickers or not year or not _asks_for_high_close(question):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "SELECT ticker, name, date, close FROM ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.close DESC NULLS LAST, p.date DESC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'"
        " ) year_high_close "
        f"WHERE rn = 1 ORDER BY close DESC NULLS LAST, ticker ASC LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _latest_volume_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    if not tickers or not _asks_for_latest_volume(question):
        return None
    session_count = _requested_session_count(question) or 5
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers) * session_count, session_count), max_limit)
    return (
        "SELECT ticker, name, date, volume "
        "FROM ("
        " SELECT c.ticker, c.name, p.date, p.volume,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.volume IS NOT NULL"
        " ) latest_volume "
        f"WHERE rn <= {session_count} ORDER BY ticker ASC, date DESC LIMIT {limit}"
    )


def _lowest_volume_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    if not tickers or not year or not _asks_for_lowest_volume(question):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "SELECT ticker, name, date, volume "
        "FROM ("
        " SELECT c.ticker, c.name, p.date, p.volume,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.volume ASC NULLS LAST, p.date ASC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'"
        " AND p.volume IS NOT NULL"
        " ) lowest_volume "
        f"WHERE rn = 1 ORDER BY volume ASC NULLS LAST, ticker ASC LIMIT {limit}"
    )


def _top_volume_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    normalized = _normalize_text(question.lower())
    has_volume = any(phrase in normalized for phrase in ["volume", "khoi luong", "giao dich"])
    has_top = any(phrase in normalized for phrase in ["top", "lon nhat", "cao nhat", "highest", "largest", "max"])
    if not tickers or not has_volume or not has_top:
        return None
    top_n = _requested_top_count(question) or 10
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers) * top_n, top_n), max_limit)
    return (
        "SELECT ticker, name, date, volume, close "
        "FROM ("
        " SELECT c.ticker, c.name, p.date, p.volume, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.volume DESC NULLS LAST, p.date ASC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.volume IS NOT NULL"
        ") top_volume "
        f"WHERE rn <= {top_n} ORDER BY ticker ASC, volume DESC NULLS LAST, date ASC LIMIT {limit}"
    )


def _volume_date_vs_quarter_max_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    exact_date = _requested_exact_date(question)
    quarter = _requested_quarter(question)
    normalized = _normalize_text(question.lower())
    has_volume = any(phrase in normalized for phrase in ["volume", "khoi luong", "giao dich"])
    has_compare = any(phrase in normalized for phrase in ["so sanh", "compare", "voi"])
    has_max = any(phrase in normalized for phrase in ["lon nhat", "cao nhat", "max", "maximum"])
    if not tickers or not exact_date or not quarter or not has_volume or not has_compare or not has_max:
        return None
    year, quarter_number = quarter
    month = (quarter_number - 1) * 3 + 1
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if quarter_number == 4 else date(year, month + 3, 1)
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "WITH target_day AS ("
        " SELECT c.ticker, c.name, p.date AS target_date, p.volume AS target_volume"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date = DATE '{exact_date}' AND p.volume IS NOT NULL"
        "), quarter_volume AS ("
        " SELECT c.ticker, c.name, p.date, p.volume,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.volume DESC NULLS LAST, p.date ASC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{start.isoformat()}' AND p.date < DATE '{end.isoformat()}'"
        " AND p.volume IS NOT NULL"
        "), quarter_max AS ("
        " SELECT ticker, name, date AS max_volume_date, volume AS quarter_max_volume"
        " FROM quarter_volume WHERE rn = 1"
        ") SELECT q.ticker, q.name,"
        f" DATE '{exact_date}' AS target_date,"
        " t.target_volume, q.max_volume_date, q.quarter_max_volume,"
        " ROUND((((t.target_volume - q.quarter_max_volume)::numeric / NULLIF(q.quarter_max_volume, 0)) * 100), 2)::float AS pct_vs_quarter_max"
        " FROM quarter_max q LEFT JOIN target_day t ON t.ticker = q.ticker"
        f" ORDER BY q.ticker ASC LIMIT {limit}"
    )


def _count_up_sessions_sql(question: str, max_limit: int) -> str | None:
    """Count how many sessions a ticker rose/fell (vs prior close, or close vs open) in a window."""
    tickers = _context_tickers(question) or extract_tickers(question)
    normalized = _normalize_text(_latest_user_question(question).lower())
    if not tickers:
        return None
    if not (re.search(r"bao nhieu (phien|ngay)", normalized) or "so phien" in normalized):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None

    if "cao hon mo cua" in normalized or ("dong cua" in normalized and "mo cua" in normalized):
        condition = "close > open"
    elif "giam" in normalized:
        condition = "close < prev_close"
    else:
        condition = "close > prev_close"

    sessions = _requested_session_count(question)
    if sessions and "phien" in normalized:
        date_filter = ""
        window_filter = f"WHERE rn <= {sessions}"
    else:
        days = _requested_window_days(question) or 30
        date_filter = f" AND p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        window_filter = ""
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "WITH base AS ("
        " SELECT c.ticker, c.name, p.date, p.open, p.close,"
        " LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter}{date_filter}"
        ") SELECT ticker, name,"
        f" COUNT(*) FILTER (WHERE {condition}) AS matching_sessions,"
        " COUNT(*) AS total_sessions"
        f" FROM base {window_filter} "
        f"GROUP BY ticker, name ORDER BY ticker ASC LIMIT {limit}"
    )


def _longest_down_streak_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    normalized = _normalize_text(question.lower())
    if not tickers or not year or not ("giam lien tiep" in normalized or "consecutive" in normalized):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "WITH daily AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_close"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'"
        "), flags AS ("
        " SELECT *, CASE WHEN close < prev_close THEN 1 ELSE 0 END AS is_down FROM daily"
        "), groups AS ("
        " SELECT *, SUM(CASE WHEN is_down = 0 THEN 1 ELSE 0 END) OVER (PARTITION BY ticker ORDER BY date) AS streak_group"
        " FROM flags"
        "), streaks AS ("
        " SELECT ticker, name, MIN(date) AS start_date, MAX(date) AS end_date, COUNT(*) AS down_sessions"
        " FROM groups WHERE is_down = 1 GROUP BY ticker, name, streak_group"
        "), ranked AS ("
        " SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY down_sessions DESC, end_date ASC) AS rn FROM streaks"
        ") SELECT ticker, name, start_date, end_date, down_sessions "
        f"FROM ranked WHERE rn = 1 ORDER BY down_sessions DESC, ticker ASC LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _ma_compare_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    normalized = _normalize_text(question.lower())
    ma_match = re.search(r"\bma\s*(20|50|200)\b", normalized)
    if not ma_match:
        ma_match = re.search(r"\bma(20|50|200)\b", normalized)
    if not tickers or not ma_match or "so sanh" not in normalized:
        return None
    window = int(ma_match.group(1))
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "WITH ranked_prices AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.close IS NOT NULL"
        "), metrics AS ("
        " SELECT ticker, name,"
        " MAX(CASE WHEN rn = 1 THEN date END) AS latest_date,"
        " MAX(CASE WHEN rn = 1 THEN close END) AS latest_close,"
        f" AVG(CASE WHEN rn <= {window} THEN close END) AS ma{window},"
        f" COUNT(CASE WHEN rn <= {window} THEN 1 END) AS ma{window}_observations"
        " FROM ranked_prices GROUP BY ticker, name"
        f") SELECT ticker, name, latest_date, latest_close, ROUND(ma{window}::numeric, 4)::float AS ma{window},"
        f" ROUND((latest_close - ma{window})::numeric, 4)::float AS difference,"
        f" ROUND((((latest_close - ma{window}) / NULLIF(ma{window}, 0)) * 100)::numeric, 2)::float AS pct_vs_ma{window},"
        f" ma{window}_observations FROM metrics ORDER BY ticker ASC LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _ma_series_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    normalized = _normalize_text(question.lower())
    windows = sorted({int(value) for value in re.findall(r"\bma\s*(20|50|200)\b|\bma(20|50|200)\b", normalized) for value in value if value})
    if not tickers or not windows or "tinh" not in normalized:
        return None
    days = _requested_window_days(question) or max(windows) + 50
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    select_parts = [
        "c.ticker",
        "c.name",
        "p.date",
        "p.close",
    ]
    for window in windows:
        select_parts.append(
            f"ROUND((AVG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW))::numeric, 4)::float AS ma{window}"
        )
    return (
        f"SELECT {', '.join(select_parts)} "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '{days} days' "
        f"ORDER BY p.date ASC, c.ticker ASC LIMIT {_time_series_limit(question, tickers, max_limit)}"
    )


def _ma_screen_sql(question: str, max_limit: int) -> str | None:
    normalized = _normalize_text(question.lower())
    windows = sorted({int(value) for value in re.findall(r"\bma\s*(20|50|200)\b|\bma(20|50|200)\b", normalized) for value in value if value})
    if not windows or not any(phrase in normalized for phrase in ["cao hon", "higher", "tren ma"]):
        return None
    needs_52w_discount = "52" in normalized and any(phrase in normalized for phrase in ["thap hon", "duoi", "below"])
    latest_select = [
        "ticker",
        "name",
        "latest_date",
        "latest_close",
        *[f"ROUND(ma{window}::numeric, 4)::float AS ma{window}" for window in windows],
    ]
    if needs_52w_discount:
        latest_select.extend(
            [
                "ROUND(high_52w::numeric, 4)::float AS high_52w",
                "ROUND((((high_52w - latest_close) / NULLIF(high_52w, 0)) * 100)::numeric, 2)::float AS pct_below_52w_high",
            ]
        )
    predicates = [f"latest_close > ma{window}" for window in windows]
    if needs_52w_discount:
        predicates.append("latest_close <= high_52w * 0.85")
    return (
        "WITH ranked AS ("
        " SELECT c.ticker, c.name, p.date, p.close, p.high,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id WHERE p.close IS NOT NULL"
        "), metrics AS ("
        " SELECT ticker, name,"
        " MAX(CASE WHEN rn = 1 THEN date END) AS latest_date,"
        " MAX(CASE WHEN rn = 1 THEN close END) AS latest_close,"
        + "".join([f" AVG(CASE WHEN rn <= {window} THEN close END) AS ma{window}," for window in windows])
        + " MAX(CASE WHEN rn <= 252 THEN high END) AS high_52w"
        " FROM ranked GROUP BY ticker, name"
        f") SELECT {', '.join(latest_select)} FROM metrics WHERE {' AND '.join(predicates)} "
        f"ORDER BY ticker ASC LIMIT {min(200, max_limit)}"
    )


def _year_return_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    normalized = _normalize_text(question.lower())
    if not tickers or not year or not any(phrase in normalized for phrase in ["loi suat", "return"]):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "WITH yearly AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date ASC) AS rn_start,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn_end"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'"
        ") SELECT ticker, name,"
        " MAX(CASE WHEN rn_start = 1 THEN date END) AS start_date,"
        " MAX(CASE WHEN rn_start = 1 THEN close END) AS start_close,"
        " MAX(CASE WHEN rn_end = 1 THEN date END) AS end_date,"
        " MAX(CASE WHEN rn_end = 1 THEN close END) AS end_close,"
        " ROUND(((((MAX(CASE WHEN rn_end = 1 THEN close END) - MAX(CASE WHEN rn_start = 1 THEN close END)) / "
        "NULLIF(MAX(CASE WHEN rn_start = 1 THEN close END), 0)) * 100)::numeric), 2)::float AS return_pct"
        f" FROM yearly GROUP BY ticker, name ORDER BY return_pct DESC NULLS LAST LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _max_drawdown_year_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    if not tickers or not year or "drawdown" not in _normalize_text(question.lower()):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "WITH prices_window AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " MAX(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_peak"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'"
        "), drawdowns AS ("
        " SELECT ticker, name, date, close, running_peak,"
        " ((close - running_peak) / NULLIF(running_peak, 0)) * 100 AS drawdown_pct"
        " FROM prices_window"
        "), ranked AS ("
        " SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY drawdown_pct ASC, date ASC) AS rn FROM drawdowns"
        ") SELECT ticker, name, date, close, running_peak, ROUND(drawdown_pct::numeric, 2)::float AS max_drawdown_pct "
        f"FROM ranked WHERE rn = 1 ORDER BY max_drawdown_pct ASC LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _correlation_sql(question: str, max_limit: int) -> str | None:
    normalized = _normalize_text(question.lower())
    if not any(phrase in normalized for phrase in ["correlation", "tuong quan"]):
        return None
    tickers = _context_tickers(question) or extract_tickers(question)
    days = _requested_window_days(question) or 366
    year = _requested_year(question)
    if "spy" in normalized and any(phrase in normalized for phrase in ["cao nhat", "highest"]):
        start_filter = f"p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'" if year else f"p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        return _benchmark_correlation_sql("SPY", start_filter, highest=True, max_limit=max_limit)
    if "aapl" in normalized and any(phrase in normalized for phrase in ["thap nhat", "lowest"]):
        start_filter = f"p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        return _benchmark_correlation_sql("AAPL", start_filter, highest=False, max_limit=max_limit)
    if len(tickers) >= 2:
        left, right = tickers[0], tickers[1]
        start_filter = f"p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        return (
            "WITH daily AS ("
            " SELECT c.ticker, p.date, p.close, LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_close"
            " FROM companies c JOIN prices p ON p.company_id = c.id"
            f" WHERE c.ticker IN ('{left}', '{right}') AND {start_filter}"
            "), returns AS ("
            " SELECT ticker, date, CASE WHEN prev_close IS NOT NULL AND prev_close <> 0 THEN (close - prev_close) / prev_close END AS daily_return FROM daily"
            "), paired AS ("
            f" SELECT l.date, l.daily_return AS {left.lower()}_return, r.daily_return AS {right.lower()}_return"
            f" FROM returns l JOIN returns r ON r.date = l.date WHERE l.ticker = '{left}' AND r.ticker = '{right}'"
            f") SELECT '{left}' AS ticker_a, '{right}' AS ticker_b, COUNT(*) AS observations,"
            f" ROUND(CORR({left.lower()}_return, {right.lower()}_return)::numeric, 4)::float AS correlation"
            " FROM paired"
        )
    return None


def _benchmark_correlation_sql(benchmark: str, date_filter: str, *, highest: bool, max_limit: int) -> str:
    order = "DESC" if highest else "ASC"
    return (
        "WITH daily AS ("
        " SELECT c.ticker, p.date, p.close, LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_close"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {date_filter}"
        "), returns AS ("
        " SELECT ticker, date, CASE WHEN prev_close IS NOT NULL AND prev_close <> 0 THEN (close - prev_close) / prev_close END AS daily_return FROM daily"
        "), benchmark AS ("
        f" SELECT date, daily_return AS benchmark_return FROM returns WHERE ticker = '{benchmark}'"
        "), scored AS ("
        " SELECT r.ticker, COUNT(*) AS observations, CORR(r.daily_return, b.benchmark_return) AS correlation"
        " FROM returns r JOIN benchmark b ON b.date = r.date"
        f" WHERE r.ticker <> '{benchmark}' GROUP BY r.ticker"
        f") SELECT ticker, '{benchmark}' AS benchmark_ticker, observations, ROUND(correlation::numeric, 4)::float AS correlation "
        f"FROM scored WHERE correlation IS NOT NULL ORDER BY correlation {order} NULLS LAST, ticker ASC LIMIT {min(20, max_limit)}"
    )


def _recovery_after_biggest_drop_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    year = _requested_year(question)
    normalized = _normalize_text(question.lower())
    if not tickers or not year or not ("phuc hoi" in normalized and "giam manh nhat" in normalized):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "WITH daily AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " LAG(p.date) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_date,"
        " LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_close"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= DATE '{year}-01-01' AND p.date < DATE '{year + 1}-01-01'"
        "), biggest_drop AS ("
        " SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ((close - prev_close) / NULLIF(prev_close, 0)) ASC) AS rn"
        " FROM daily WHERE prev_close IS NOT NULL"
        "), recovery AS ("
        " SELECT bd.ticker, bd.name, bd.prev_date AS pre_drop_date, bd.prev_close AS pre_drop_close,"
        " bd.date AS drop_date, bd.close AS drop_close,"
        " MIN(p.date) FILTER (WHERE p.date > bd.date AND p.close >= bd.prev_close) AS recovery_date"
        " FROM biggest_drop bd"
        " JOIN companies c ON c.ticker = bd.ticker"
        " JOIN prices p ON p.company_id = c.id"
        " WHERE bd.rn = 1"
        " GROUP BY bd.ticker, bd.name, bd.prev_date, bd.prev_close, bd.date, bd.close"
        ") SELECT ticker, name, pre_drop_date, pre_drop_close, drop_date, drop_close,"
        " ROUND((((drop_close - pre_drop_close) / NULLIF(pre_drop_close, 0)) * 100)::numeric, 2)::float AS drop_pct,"
        " recovery_date, CASE WHEN recovery_date IS NOT NULL THEN recovery_date - drop_date END AS recovery_calendar_days"
        f" FROM recovery ORDER BY ticker ASC LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _volume_price_correlation_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    normalized = _normalize_text(question.lower())
    if not tickers or not ("correlation" in normalized or "tuong quan" in normalized) or "volume" not in normalized:
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    return (
        "WITH daily AS ("
        " SELECT c.ticker, c.name, p.date, p.volume, p.close,"
        " LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date) AS prev_close"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '1 year' AND p.volume IS NOT NULL AND p.close IS NOT NULL"
        "), returns AS ("
        " SELECT ticker, name, date, volume,"
        " CASE WHEN prev_close IS NOT NULL AND prev_close <> 0 THEN ABS((close - prev_close) / prev_close) END AS abs_daily_return,"
        " CASE WHEN prev_close IS NOT NULL AND prev_close <> 0 THEN (close - prev_close) / prev_close END AS daily_return"
        " FROM daily"
        ") SELECT ticker, name, COUNT(abs_daily_return) AS observations,"
        " ROUND(CORR(volume::float, abs_daily_return)::numeric, 4)::float AS corr_volume_abs_return,"
        " ROUND(CORR(volume::float, daily_return)::numeric, 4)::float AS corr_volume_return"
        " FROM returns GROUP BY ticker, name"
        f" ORDER BY ticker ASC LIMIT {min(max(len(tickers), 1), max_limit)}"
    )


def _month_price_data_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    month_window = _requested_month_window(question)
    if not tickers or not month_window or not _asks_for_month_price_data(question) or _should_defer_to_llm(question):
        return None
    start_date, end_date = month_window
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers) * 35, 40), max_limit)
    return (
        "SELECT c.ticker, p.id, p.company_id, p.date, p.open, p.high, p.low, p.close, "
        "p.adj_close, p.volume, p.created_at "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= DATE '{start_date}' AND p.date < DATE '{end_date}' "
        f"ORDER BY p.date ASC, c.ticker ASC LIMIT {limit}"
    )


def _month_close_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    month_window = _requested_month_window(question)
    if not tickers or not month_window or not _asks_for_month_close(question) or _should_defer_to_llm(question):
        return None
    start_date, end_date = month_window
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers) * 35, 40), max_limit)
    return (
        "SELECT c.ticker, c.name, p.date, p.close "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= DATE '{start_date}' AND p.date < DATE '{end_date}' "
        f"ORDER BY p.date ASC, c.ticker ASC LIMIT {limit}"
    )


def _latest_close_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    question_l = question.lower()
    normalized = _normalize_text(question_l)
    if not tickers:
        return None
    has_close = any(phrase in normalized for phrase in ["gia dong cua", "close", "closing price"])
    has_latest = any(phrase in normalized for phrase in ["gan nhat", "latest", "recent", "last"])
    if (
        not has_close
        or not has_latest
        or _asks_for_high_close(question)
        or _asks_for_price_change(question_l)
        or _should_defer_to_llm(question)
    ):
        return None
    days = _requested_window_days(question)
    sessions = _requested_session_count(question)
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    if sessions and "phien" in normalized:
        limit = min(max(len(tickers) * sessions, sessions), max_limit)
        return (
            "SELECT ticker, name, date, close "
            "FROM ("
            " SELECT c.ticker, c.name, p.date, p.close,"
            " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn"
            " FROM companies c JOIN prices p ON p.company_id = c.id"
            f" WHERE {ticker_filter}"
            " ) latest_close "
            f"WHERE rn <= {sessions} ORDER BY ticker ASC, date DESC LIMIT {limit}"
        )
    days = days or sessions or 30
    limit = _time_series_limit(question, tickers, max_limit)
    return (
        "SELECT c.ticker, c.name, p.date, p.close "
        "FROM companies c JOIN prices p ON p.company_id = c.id "
        f"WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '{days} days' "
        f"ORDER BY p.date ASC, c.ticker ASC LIMIT {limit}"
    )


def _return_volatility_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    if not tickers or not _asks_for_return_volatility(question):
        return None
    days = _requested_window_days(question) or 30
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "WITH daily_prices AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date ASC) AS prev_close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date ASC) AS rn_asc,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn_desc"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '{days} days' AND p.close IS NOT NULL"
        "), daily_returns AS ("
        " SELECT ticker, name, date, close, rn_asc, rn_desc,"
        " CASE WHEN prev_close IS NOT NULL AND prev_close <> 0 THEN (close - prev_close) / prev_close END AS daily_return"
        " FROM daily_prices"
        "), metrics AS ("
        " SELECT ticker, name,"
        " MAX(CASE WHEN rn_asc = 1 THEN date END) AS start_date,"
        " MAX(CASE WHEN rn_asc = 1 THEN close END) AS start_close,"
        " MAX(CASE WHEN rn_desc = 1 THEN date END) AS end_date,"
        " MAX(CASE WHEN rn_desc = 1 THEN close END) AS end_close,"
        " STDDEV_SAMP(daily_return) AS daily_volatility,"
        " COUNT(daily_return) AS return_observations"
        " FROM daily_returns GROUP BY ticker, name"
        ") SELECT ticker, name, start_date, start_close, end_date, end_close,"
        " ROUND((((end_close - start_close) / NULLIF(start_close, 0)) * 100)::numeric, 2)::float AS period_return_pct,"
        " ROUND((daily_volatility * 100)::numeric, 4)::float AS daily_volatility_pct,"
        " ROUND(((((end_close - start_close) / NULLIF(start_close, 0)) / NULLIF(daily_volatility, 0)))::numeric, 4)::float AS return_volatility_ratio,"
        " return_observations"
        " FROM metrics"
        " WHERE start_close IS NOT NULL AND end_close IS NOT NULL AND daily_volatility IS NOT NULL"
        f" ORDER BY return_volatility_ratio DESC NULLS LAST LIMIT {limit}"
    )


def _outperform_spy_lower_volatility_sql(question: str, max_limit: int) -> str | None:
    if not _asks_for_outperform_spy_lower_volatility(question):
        return None
    days = _requested_window_days(question) or 366
    limit = min(50, max_limit)
    return (
        "WITH daily_prices AS ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " LAG(p.close) OVER (PARTITION BY c.ticker ORDER BY p.date ASC) AS prev_close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date ASC) AS rn_asc,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.date DESC) AS rn_desc"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE p.date >= CURRENT_DATE - INTERVAL '{days} days' AND p.close IS NOT NULL"
        "), daily_returns AS ("
        " SELECT ticker, name, date, close, rn_asc, rn_desc,"
        " CASE WHEN prev_close IS NOT NULL AND prev_close <> 0 THEN (close - prev_close) / prev_close END AS daily_return"
        " FROM daily_prices"
        "), metrics AS ("
        " SELECT ticker, name,"
        " MAX(CASE WHEN rn_asc = 1 THEN date END) AS start_date,"
        " MAX(CASE WHEN rn_asc = 1 THEN close END) AS start_close,"
        " MAX(CASE WHEN rn_desc = 1 THEN date END) AS end_date,"
        " MAX(CASE WHEN rn_desc = 1 THEN close END) AS end_close,"
        " STDDEV_SAMP(daily_return) AS daily_volatility,"
        " COUNT(daily_return) AS return_observations"
        " FROM daily_returns GROUP BY ticker, name"
        "), scored AS ("
        " SELECT ticker, name, start_date, start_close, end_date, end_close, daily_volatility, return_observations,"
        " ((end_close - start_close) / NULLIF(start_close, 0)) AS period_return"
        " FROM metrics WHERE start_close IS NOT NULL AND end_close IS NOT NULL AND daily_volatility IS NOT NULL"
        "), spy AS ("
        " SELECT period_return AS spy_return, daily_volatility AS spy_daily_volatility FROM scored WHERE ticker = 'SPY'"
        ") SELECT s.ticker, s.name, s.start_date, s.start_close, s.end_date, s.end_close,"
        " ROUND((s.period_return * 100)::numeric, 2)::float AS period_return_pct,"
        " ROUND((spy.spy_return * 100)::numeric, 2)::float AS spy_return_pct,"
        " ROUND((s.daily_volatility * 100)::numeric, 4)::float AS daily_volatility_pct,"
        " ROUND((spy.spy_daily_volatility * 100)::numeric, 4)::float AS spy_daily_volatility_pct,"
        " s.return_observations"
        " FROM scored s CROSS JOIN spy"
        " WHERE s.ticker <> 'SPY' AND s.period_return > spy.spy_return AND s.daily_volatility < spy.spy_daily_volatility"
        f" ORDER BY s.period_return DESC NULLS LAST, s.daily_volatility ASC LIMIT {limit}"
    )


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


def _quarterly_close_comparison_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    if not tickers or not _asks_for_quarterly_close_comparison(question):
        return None
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers) * 2, 2), max_limit)
    return (
        "WITH quarter_prices AS ("
        " SELECT c.ticker, c.name, DATE_TRUNC('quarter', p.date)::date AS quarter,"
        " p.date, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker, DATE_TRUNC('quarter', p.date) ORDER BY p.date ASC) AS rn_start,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker, DATE_TRUNC('quarter', p.date) ORDER BY p.date DESC) AS rn_end"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '9 months'"
        "), quarter_endpoints AS ("
        " SELECT ticker, name, quarter,"
        " MAX(CASE WHEN rn_start = 1 THEN date END) AS start_date,"
        " MAX(CASE WHEN rn_start = 1 THEN close END) AS start_close,"
        " MAX(CASE WHEN rn_end = 1 THEN date END) AS end_date,"
        " MAX(CASE WHEN rn_end = 1 THEN close END) AS end_close"
        " FROM quarter_prices GROUP BY ticker, name, quarter"
        "), ranked AS ("
        " SELECT *, DENSE_RANK() OVER (PARTITION BY ticker ORDER BY quarter DESC) AS quarter_rank"
        " FROM quarter_endpoints"
        ") SELECT ticker, name, quarter, start_date, start_close, end_date, end_close,"
        " ROUND((((end_close - start_close) / NULLIF(start_close, 0)) * 100)::numeric, 2)::float AS pct_change,"
        " quarter_rank"
        " FROM ranked WHERE quarter_rank <= 2"
        f" ORDER BY ticker ASC, quarter ASC LIMIT {limit}"
    )


def _monthly_high_close_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    if not tickers or not _asks_for_monthly_high_close(question):
        return None
    days = _requested_window_days(question) or 31 * 8
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers) * 14, 20), max_limit)
    return (
        "SELECT ticker, name, month, date, close "
        "FROM ("
        " SELECT c.ticker, c.name, DATE_TRUNC('month', p.date)::date AS month, p.date, p.close,"
        " ROW_NUMBER() OVER ("
        "PARTITION BY c.ticker, DATE_TRUNC('month', p.date) "
        "ORDER BY p.close DESC NULLS LAST, p.date DESC"
        " ) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        " ) monthly_highs "
        f"WHERE rn = 1 ORDER BY ticker ASC, month ASC LIMIT {limit}"
    )


def _high_close_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    if (
        not tickers
        or not _asks_for_high_close(question)
        or _asks_for_monthly_high_close(question)
        or _should_defer_to_llm(question)
    ):
        return None
    days = _requested_window_days(question) or 31 * 8
    ticker_filter = _ticker_condition(tickers, "c.ticker")
    if not ticker_filter:
        return None
    limit = min(max(len(tickers), 1), max_limit)
    return (
        "SELECT ticker, name, date, close "
        "FROM ("
        " SELECT c.ticker, c.name, p.date, p.close,"
        " ROW_NUMBER() OVER (PARTITION BY c.ticker ORDER BY p.close DESC NULLS LAST, p.date DESC) AS rn"
        " FROM companies c JOIN prices p ON p.company_id = c.id"
        f" WHERE {ticker_filter} AND p.date >= CURRENT_DATE - INTERVAL '{days} days'"
        " ) highest_closes "
        f"WHERE rn = 1 ORDER BY close DESC NULLS LAST, ticker ASC LIMIT {limit}"
    )


def _price_series_sql(question: str, max_limit: int) -> str | None:
    tickers = _context_tickers(question) or extract_tickers(question)
    question_l = question.lower()
    if not tickers or not _asks_for_price_series(question_l) or _should_defer_to_llm(question):
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


def _asks_for_average(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    return any(phrase in normalized for phrase in ["trung binh", "average", "avg", "binh quan"])


def _should_defer_to_llm(question: str) -> bool:
    """True when a question needs computation (count / average / single-day extreme move /
    up-down vs previous session) that the simple price/volume series builders cannot answer,
    so it should fall through to the LLM candidate generator instead of a raw series dump.
    """
    normalized = _normalize_text(_latest_user_question(question).lower())
    if _asks_for_average(question):
        return True
    if re.search(r"bao nhieu (phien|ngay|lan|buoi)", normalized) or "so phien" in normalized:
        return True
    if (
        "manh nhat" in normalized
        and ("tang" in normalized or "giam" in normalized)
        and "cao nhat" not in normalized
        and "thap nhat" not in normalized
    ):
        return True
    if "so voi phien truoc" in normalized and any(
        token in normalized for token in ["tang", "giam", "liet ke", "cac ngay", "ngay nao"]
    ):
        return True
    return False


def _asks_for_quarterly_close_comparison(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_close = any(phrase in normalized for phrase in ["gia dong cua", "close", "closing price"])
    has_quarter = any(phrase in normalized for phrase in ["quy", "quarter", "quarterly", "grouping=quarter"])
    has_compare = any(
        phrase in normalized
        for phrase in [
            "so sanh",
            "compare",
            "xu huong",
            "trend",
            "quy truoc",
            "latest_quarter_vs_previous",
            "comparison=latest_quarter_vs_previous",
        ]
    )
    return has_close and has_quarter and has_compare


def _asks_for_ohlc(question: str) -> bool:
    question = _latest_user_question(question)
    normalized = _normalize_text(question.lower())
    ohlc_terms = ["ohlc", "open", "high", "low", "close", "gia mo cua", "cao nhat", "thap nhat", "gia dong cua"]
    requested_terms = [term for term in ohlc_terms if term in normalized]
    if "cao nhat" in normalized and "gia dong cua" in normalized and "vao ngay" not in normalized:
        return False
    return len(requested_terms) >= 2 or "ohlc" in normalized


def _asks_for_nearest_day(question: str) -> bool:
    normalized = _normalize_text(_latest_user_question(question).lower())
    return any(
        phrase in normalized
        for phrase in [
            "ngay gan do",
            "ngay gan day",
            "ngay gan nhat",
            "gan nhat neu",
            "gan do neu",
            "khong co du lieu",
            "khong co phien",
            "neu khong co",
            "nearest",
            "closest",
            "nearby",
        ]
    )


def _asks_for_latest_volume(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_volume = any(phrase in normalized for phrase in ["volume", "khoi luong", "giao dich"])
    has_latest = any(phrase in normalized for phrase in ["gan nhat", "latest", "recent", "last", "moi nhat"])
    # "volume trung bình ..." is an aggregate, not a recent-sessions dump → let the LLM handle it.
    return has_volume and has_latest and not _asks_for_average(question)


def _asks_for_lowest_volume(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_volume = any(phrase in normalized for phrase in ["volume", "khoi luong", "giao dich"])
    has_lowest = any(phrase in normalized for phrase in ["thap nhat", "lowest", "minimum", "min"])
    return has_volume and has_lowest


def _asks_for_month_price_data(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_data = any(phrase in normalized for phrase in ["toan bo du lieu", "tat ca du lieu", "du lieu", "all data"])
    has_month = _requested_month_window(question) is not None
    return has_data and has_month


def _asks_for_month_close(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_close = any(phrase in normalized for phrase in ["gia dong cua", "close", "closing price"])
    return has_close and _requested_month_window(question) is not None


def _asks_for_avg_close(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_avg = any(phrase in normalized for phrase in ["trung binh", "average", "avg"])
    has_close = any(phrase in normalized for phrase in ["gia dong cua", "close", "closing price"])
    return has_avg and has_close


def _asks_for_return_volatility(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_return = any(phrase in normalized for phrase in ["return", "loi suat", "loi nhuan", "tang"])
    has_risk = any(phrase in normalized for phrase in ["volatility", "bien dong", "rui ro"])
    has_ratio = any(phrase in normalized for phrase in ["ty le", "ti le", "ratio", "/volatility", "return/volatility"])
    return has_return and has_risk and has_ratio


def _asks_for_outperform_spy_lower_volatility(question: str) -> bool:
    normalized = _normalize_text(question.lower())
    has_spy = "spy" in normalized
    has_outperform = any(phrase in normalized for phrase in ["outperform", "cao hon spy", "hon spy"])
    has_volatility = any(phrase in normalized for phrase in ["volatility", "bien dong"])
    has_lower = any(phrase in normalized for phrase in ["thap hon", "lower", "it hon"])
    return has_spy and has_outperform and has_volatility and has_lower


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


def _latest_user_question(question: str) -> str:
    markers = [
        "\n\nContext tickers:",
        "\n\nAgent task contract:",
        "\n\nrecent conversation context:",
        "\n\nStructured follow-up contract:",
    ]
    cut = len(question)
    for marker in markers:
        index = question.find(marker)
        if index != -1:
            cut = min(cut, index)
    return question[:cut].strip() or question


def _requested_exact_date(question: str) -> str | None:
    dates = _requested_iso_dates(question)
    if len(dates) != 1:
        return None
    return dates[0]


def _requested_date_range(question: str) -> tuple[str, str] | None:
    dates = _requested_iso_dates(question)
    if len(dates) < 2:
        return None
    start, end = dates[0], dates[1]
    return (start, end) if start <= end else (end, start)


def _requested_iso_dates(question: str) -> list[str]:
    question = _latest_user_question(question)
    values: list[str] = []
    for match in re.finditer(r"\b(20\d{2}-\d{2}-\d{2})\b", question):
        try:
            parsed = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        values.append(parsed.isoformat())
    return values


def _requested_year(question: str) -> int | None:
    question = _latest_user_question(question)
    normalized = _normalize_text(question.lower())
    match = re.search(r"\b(?:nam|year)\s+(20\d{2})\b", normalized)
    if match:
        return int(match.group(1))
    years = sorted({int(value) for value in re.findall(r"\b(20\d{2})\b", normalized)})
    if len(years) == 1:
        return years[0]
    return None


def _requested_quarter(question: str) -> tuple[int, int] | None:
    question = _latest_user_question(question)
    normalized = _normalize_text(question.lower())
    match = re.search(r"\bquy\s*([1-4])(?:\s+nam)?\s+(20\d{2})\b", normalized)
    if not match:
        match = re.search(r"\bq([1-4])\s*(20\d{2})\b", normalized)
    if not match:
        return None
    return int(match.group(2)), int(match.group(1))


def _requested_session_count(question: str) -> int | None:
    question = _latest_user_question(question)
    normalized = _normalize_text(question.lower())
    match = re.search(r"\b(\d{1,3})\s*(phien|sessions?|ngay|days?)\b", normalized)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(last|latest|gan nhat)\s+(\d{1,3})\b", normalized)
    if match:
        return int(match.group(2))
    return None


def _requested_top_count(question: str) -> int | None:
    question = _latest_user_question(question)
    normalized = _normalize_text(question.lower())
    match = re.search(r"\btop\s*(\d{1,3})\b", normalized)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{1,3})\s*(phien|sessions?|ngay|days?)\b", normalized)
    if match and any(phrase in normalized for phrase in ["lon nhat", "cao nhat", "highest", "largest"]):
        return int(match.group(1))
    return None


def _requested_month_window(question: str) -> tuple[str, str] | None:
    question = _latest_user_question(question)
    normalized = _normalize_text(question.lower())
    match = re.search(r"\bthang\s+(\d{1,2})(?:\s+nam)?\s+(20\d{2})\b", normalized)
    if not match:
        match = re.search(r"\b(20\d{2})-(\d{1,2})\b", normalized)
        if not match:
            return None
        year = int(match.group(1))
        month = int(match.group(2))
    else:
        month = int(match.group(1))
        year = int(match.group(2))
    if month < 1 or month > 12:
        return None
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def _requested_window_days(question: str) -> int | None:
    question = _latest_user_question(question)
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


def _should_keep_empty_result(question: str, sql: str) -> bool:
    sql_l = sql.lower()
    has_strict_date = "p.date = date" in sql_l or ("p.date >= date" in sql_l and "p.date <= date" in sql_l)
    return (_requested_exact_date(question) is not None or _requested_date_range(question) is not None) and has_strict_date


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


def _deterministic_empty_result_explanation(question: str) -> str | None:
    date_range = _requested_date_range(question)
    exact_date = _requested_exact_date(question)
    tickers = _context_tickers(question) or extract_tickers(question)
    if date_range and _asks_for_ohlc(question):
        ticker_text = ", ".join(tickers) if tickers else "mã được hỏi"
        return (
            f"Không tìm thấy dữ liệu giao dịch trong khoảng {date_range[0]} đến {date_range[1]} cho {ticker_text} trong Postgres.\n\n"
            "### Lưu ý\n"
            "- Truy vấn giữ nguyên khoảng ngày bạn hỏi, không tự đổi sang ngày khác.\n"
            "- Nếu khoảng này trùng ngày nghỉ hoặc chưa có dữ liệu ingest, bảng giá sẽ không có dòng phù hợp."
        )
    if exact_date and _asks_for_ohlc(question):
        ticker_text = ", ".join(tickers) if tickers else "mã được hỏi"
        return (
            f"Không tìm thấy dữ liệu giao dịch đúng ngày {exact_date} cho {ticker_text} trong Postgres.\n\n"
            "### Lưu ý\n"
            "- Truy vấn giữ nguyên ngày bạn hỏi, không tự đổi sang ngày khác.\n"
            "- Nếu đó là ngày nghỉ hoặc thị trường đóng cửa, bảng giá sẽ không có phiên giao dịch cho ngày này."
        )
    return None


def _deterministic_exact_ohlc_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_ohlc(question):
        return None
    required = {"ticker", "date", "open", "high", "low", "close"}
    if not required.issubset(rows[0]):
        return None
    date_range = _requested_date_range(question)
    if date_range:
        dates = sorted(str(row.get("date"))[:10] for row in rows if row.get("date"))
        tickers = sorted({str(row.get("ticker")) for row in rows if row.get("ticker")})
        subject = ", ".join(tickers) if tickers else "mã được hỏi"
        return "\n".join(
            [
                f"Đã lấy {len(rows)} dòng dữ liệu OHLC cho {subject}, từ {dates[0]} đến {dates[-1]}.",
                "",
                "### Nội dung bảng",
                "- Bảng bên dưới gồm open, high, low, close và volume cho từng phiên giao dịch trong khoảng ngày được hỏi.",
                "- Truy vấn giữ nguyên khoảng ngày, không rút về ngày đầu tiên.",
            ]
        )

    requested_date = _requested_exact_date(question)
    nearest_mode = _asks_for_nearest_day(question)
    used_nearest = nearest_mode and requested_date is not None and any(
        str(row.get("date"))[:10] != requested_date for row in rows
    )
    if used_nearest:
        header = f"Ngày {requested_date} không có phiên giao dịch nên đã lấy phiên gần nhất."
    else:
        header = "Đã lấy dữ liệu OHLC đúng theo ngày được hỏi."
    lines = [header, "", "### Kết quả"]
    for row in sorted(rows, key=lambda item: str(item.get("ticker", ""))):
        lines.append(
            "- {ticker} ngày {date}: open {open}, high {high}, low {low}, close {close}{volume}.".format(
                ticker=row.get("ticker"),
                date=str(row.get("date"))[:10],
                open=_format_number(float(row["open"])),
                high=_format_number(float(row["high"])),
                low=_format_number(float(row["low"])),
                close=_format_number(float(row["close"])),
                volume=f", volume {_format_integer(row.get('volume'))}" if _is_number(row.get("volume")) else "",
            )
        )
    if used_nearest:
        lines.extend(["", "### Lưu ý", f"- {requested_date} là ngày nghỉ/không có dữ liệu; bảng hiển thị phiên giao dịch gần nhất."])
    else:
        lines.extend(["", "### Lưu ý", "- Truy vấn dùng điều kiện ngày chính xác, không lấy ngày gần nhất thay thế."])
    return "\n".join(lines)


def _deterministic_month_price_data_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_month_price_data(question):
        return None
    required = {"ticker", "date", "open", "high", "low", "close"}
    if not required.issubset(rows[0]):
        return None
    dates = sorted(str(row.get("date"))[:10] for row in rows if row.get("date"))
    tickers = sorted({str(row.get("ticker")) for row in rows if row.get("ticker")})
    subject = ", ".join(tickers) if tickers else "mã được hỏi"
    if not dates:
        return None
    return "\n".join(
        [
            f"Đã lấy {len(rows)} dòng dữ liệu giá cho {subject}, từ {dates[0]} đến {dates[-1]}.",
            "",
            "### Nội dung bảng",
            "- Bảng bên dưới gồm open, high, low, close, adj_close và volume cho từng phiên giao dịch.",
            "- Phần trả lời chỉ tóm tắt phạm vi dữ liệu; chi tiết đầy đủ nằm trong bảng kết quả.",
        ]
    )


def _deterministic_month_close_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_month_close(question):
        return None
    if not {"ticker", "date", "close"}.issubset(rows[0]):
        return None
    usable = [row for row in rows if row.get("ticker") and row.get("date") and _is_number(row.get("close"))]
    if not usable:
        return None
    dates = sorted(str(row["date"])[:10] for row in usable)
    tickers = sorted({str(row["ticker"]) for row in usable})
    subject = ", ".join(tickers)
    first = min(usable, key=lambda row: str(row["date"]))
    last = max(usable, key=lambda row: str(row["date"]))
    lines = [
        f"Đã lấy giá đóng cửa của {subject} trong {len(usable)} phiên, từ {dates[0]} đến {dates[-1]}.",
        "",
        "### Tóm tắt",
        (
            f"- {first['ticker']}: close đầu kỳ {_format_number(float(first['close']))} ngày {str(first['date'])[:10]}, "
            f"close cuối kỳ {_format_number(float(last['close']))} ngày {str(last['date'])[:10]}."
        ),
        "",
        "Chi tiết từng phiên nằm trong bảng kết quả bên dưới.",
    ]
    return "\n".join(lines)


def _deterministic_latest_volume_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_latest_volume(question):
        return None
    if not {"ticker", "date", "volume"}.issubset(rows[0]):
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("ticker") and row.get("date") and _is_number(row.get("volume")):
            grouped.setdefault(str(row["ticker"]), []).append(row)
    if not grouped:
        return None
    lines = ["Đã lấy volume giao dịch theo phiên.", ""]
    for ticker, ticker_rows in sorted(grouped.items()):
        ordered = sorted(ticker_rows, key=lambda row: str(row.get("date", "")), reverse=True)
        count = len(ordered)
        avg = sum(float(row["volume"]) for row in ordered) / count
        highest = max(ordered, key=lambda row: float(row["volume"]))
        lowest = min(ordered, key=lambda row: float(row["volume"]))
        lines.append(f"### {ticker}")
        lines.append(f"- {count} phiên, từ {str(ordered[-1]['date'])[:10]} đến {str(ordered[0]['date'])[:10]}.")
        lines.append(f"- Trung bình: {_format_integer(int(round(avg)))}/phiên.")
        lines.append(
            f"- Cao nhất: {_format_integer(highest['volume'])} ({str(highest['date'])[:10]}); "
            f"thấp nhất: {_format_integer(lowest['volume'])} ({str(lowest['date'])[:10]})."
        )
        if count <= 8:
            for row in ordered:
                lines.append(f"  - {str(row['date'])[:10]}: {_format_integer(row['volume'])}")
        else:
            lines.append("- Vài phiên gần nhất:")
            for row in ordered[:5]:
                lines.append(f"  - {str(row['date'])[:10]}: {_format_integer(row['volume'])}")
        lines.append("")
    lines.append("Chi tiết từng phiên xem bảng và biểu đồ bên dưới.")
    return "\n".join(lines).strip()


def _deterministic_top_volume_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    normalized = _normalize_text(question.lower())
    if not ("volume" in normalized and any(phrase in normalized for phrase in ["top", "lon nhat", "cao nhat", "highest", "largest"])):
        return None
    if not {"ticker", "date", "volume"}.issubset(rows[0]):
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("ticker") and row.get("date") and _is_number(row.get("volume")):
            grouped.setdefault(str(row["ticker"]), []).append(row)
    if not grouped:
        return None
    top_n = _requested_top_count(question) or len(rows)
    lines = [f"Đã lấy top {top_n} phiên có volume lớn nhất cho các mã được hỏi.", "", "### Kết quả"]
    for ticker, ticker_rows in sorted(grouped.items()):
        ordered = sorted(ticker_rows, key=lambda row: float(row["volume"]), reverse=True)
        lines.append(f"- {ticker}:")
        for row in ordered[:top_n]:
            close_text = f", close {_format_number(float(row['close']))}" if _is_number(row.get("close")) else ""
            lines.append(f"  - {str(row['date'])[:10]}: volume {_format_integer(row['volume'])}{close_text}")
    lines.extend(["", "### Lưu ý", "- Bảng bên dưới giữ đầy đủ các dòng top được truy vấn để bạn kiểm tra lại."])
    return "\n".join(lines)


def _deterministic_volume_date_vs_quarter_max_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    required = {"ticker", "target_date", "target_volume", "max_volume_date", "quarter_max_volume", "pct_vs_quarter_max"}
    if not required.issubset(rows[0]):
        return None
    quarter = _requested_quarter(question)
    quarter_label = f"quý {quarter[1]}/{quarter[0]}" if quarter else "quý được hỏi"
    lines = [f"Đã so sánh volume phiên được hỏi với volume lớn nhất trong {quarter_label}.", "", "### Kết quả"]
    for row in rows:
        ticker = row.get("ticker")
        target_volume = row.get("target_volume")
        if not _is_number(target_volume):
            lines.append(
                f"- {ticker}: không thấy volume cho ngày {row.get('target_date')} trong dữ liệu hiện có; "
                f"max {quarter_label} là {_format_integer(row.get('quarter_max_volume'))} ngày {row.get('max_volume_date')}."
            )
            continue
        pct = float(row.get("pct_vs_quarter_max") or 0)
        relation = "thấp hơn" if pct < 0 else "cao hơn" if pct > 0 else "bằng"
        lines.append(
            f"- {ticker}: ngày {row.get('target_date')} volume {_format_integer(target_volume)}; "
            f"max {quarter_label} là {_format_integer(row.get('quarter_max_volume'))} ngày {row.get('max_volume_date')}. "
            f"Phiên {row.get('target_date')} {relation} max {quarter_label} {_format_percent(abs(pct))}."
        )
    lines.extend(["", "### Lưu ý", "- So sánh này dùng đúng ticker trong ngữ cảnh nếu câu sau không nêu ticker mới."])
    return "\n".join(lines)


def _deterministic_aggregate_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "ticker" not in rows[0]:
        return None
    if "avg_close" in rows[0]:
        lines = ["Đã tính giá đóng cửa trung bình theo đúng kỳ được hỏi.", "", "### Kết quả"]
        for row in rows:
            period = ""
            if row.get("period_start") and row.get("period_end"):
                period = f" ({row.get('period_start')} đến trước {row.get('period_end')})"
            lines.append(
                f"- {row['ticker']}{period}: avg close {_format_number(float(row['avg_close']))}, "
                f"{_format_integer(row.get('trading_days'))} phiên."
            )
        return "\n".join(lines)
    if "date" in rows[0] and "close" in rows[0] and _asks_for_high_close(question) and _requested_year(question):
        row = rows[0]
        return "\n".join(
            [
                f"Giá đóng cửa cao nhất trong năm {_requested_year(question)} là {row.get('ticker')} ngày {row.get('date')}: {_format_number(float(row['close']))}.",
                "",
                "### Lưu ý",
                "- Truy vấn lọc đúng năm được hỏi và xếp theo `close` giảm dần.",
            ]
        )
    return None


def _deterministic_lowest_volume_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_lowest_volume(question):
        return None
    if not {"ticker", "date", "volume"}.issubset(rows[0]):
        return None
    usable = [row for row in rows if row.get("ticker") and row.get("date") and _is_number(row.get("volume"))]
    if not usable:
        return None
    year = _requested_year(question)
    lines = [f"Đã tìm phiên có volume giao dịch thấp nhất{f' trong năm {year}' if year else ''}.", "", "### Kết quả"]
    for row in sorted(usable, key=lambda item: (str(item.get("ticker")), float(item["volume"]))):
        lines.append(f"- {row['ticker']}: {str(row['date'])[:10]}, volume {_format_integer(row['volume'])}.")
    lines.extend(["", "### Lưu ý", "- Kết quả được tính từ cột `volume`, không dùng giá đóng cửa thay thế."])
    return "\n".join(lines)


def _deterministic_up_sessions_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not {"matching_sessions", "total_sessions"}.issubset(rows[0]):
        return None
    normalized = _normalize_text(_latest_user_question(question).lower())
    if "cao hon mo cua" in normalized or ("dong cua" in normalized and "mo cua" in normalized):
        label = "có giá đóng cửa cao hơn giá mở cửa"
    elif "giam" in normalized:
        label = "giảm giá so với phiên trước"
    else:
        label = "tăng giá so với phiên trước"
    lines = ["### Số phiên thỏa điều kiện"]
    for row in rows:
        matching = _format_integer(row.get("matching_sessions"))
        total = _format_integer(row.get("total_sessions"))
        lines.append(f"- {row.get('ticker')}: {matching}/{total} phiên {label}.")
    return "\n".join(lines)


def _deterministic_streak_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "down_sessions" not in rows[0]:
        return None
    row = rows[0]
    return "\n".join(
        [
            f"Chuỗi phiên giảm liên tiếp dài nhất của {row.get('ticker')} là {_format_integer(row.get('down_sessions'))} phiên.",
            "",
            "### Chi tiết",
            f"- Bắt đầu: {row.get('start_date')}",
            f"- Kết thúc: {row.get('end_date')}",
            "- Kết quả dùng toàn bộ dữ liệu trong năm được hỏi.",
        ]
    )


def _deterministic_ma_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "latest_close" not in rows[0]:
        return None
    ma_columns = [column for column in rows[0] if re.fullmatch(r"ma(20|50|200)", column)]
    if not ma_columns:
        return None
    if "difference" not in rows[0]:
        lines = [f"Đã lọc {len(rows)} mã thỏa điều kiện so với {', '.join(column.upper() for column in ma_columns)}.", "", "### Kết quả"]
        for row in rows[:12]:
            ma_text = ", ".join(f"{column.upper()} {_format_number(float(row[column]))}" for column in ma_columns if _is_number(row.get(column)))
            extra = ""
            if _is_number(row.get("pct_below_52w_high")):
                extra = f", thấp hơn đỉnh 52 tuần {_format_percent(float(row['pct_below_52w_high']))}"
            lines.append(f"- {row['ticker']}: close {_format_number(float(row['latest_close']))} ngày {row.get('latest_date')}; {ma_text}{extra}.")
        if len(rows) > 12:
            lines.append(f"- Còn {len(rows) - 12} mã khác trong bảng kết quả.")
        return "\n".join(lines)

    ma_column = ma_columns[0]
    lines = [f"Đã so sánh giá đóng cửa mới nhất với {ma_column.upper()}.", "", "### Kết quả"]
    for row in rows:
        diff = float(row.get("difference") or 0)
        direction = "cao hơn" if diff > 0 else "thấp hơn" if diff < 0 else "bằng"
        lines.append(
            f"- {row['ticker']}: close mới nhất {_format_number(float(row['latest_close']))} ngày {row.get('latest_date')} "
            f"{direction} {ma_column.upper()} {_format_number(float(row[ma_column]))} "
            f"({_format_percent(float(row.get('pct_vs_' + ma_column) or 0))})."
        )
    return "\n".join(lines)


def _deterministic_recovery_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "drop_pct" not in rows[0] or "recovery_date" not in rows[0]:
        return None
    row = rows[0]
    recovery = row.get("recovery_date")
    if recovery:
        recovery_line = f"- Ngày phục hồi: {recovery}, sau {_format_integer(row.get('recovery_calendar_days'))} ngày lịch."
    else:
        recovery_line = "- Chưa thấy ngày phục hồi về mức trước nhịp giảm trong dữ liệu hiện có."
    return "\n".join(
        [
            f"Đã tìm nhịp giảm mạnh nhất của {row.get('ticker')} trong năm được hỏi và kiểm tra thời gian phục hồi.",
            "",
            "### Chi tiết",
            f"- Trước giảm: {row.get('pre_drop_date')} close {_format_number(float(row['pre_drop_close']))}.",
            f"- Ngày giảm mạnh nhất: {row.get('drop_date')} close {_format_number(float(row['drop_close']))}, giảm {_format_percent(float(row['drop_pct']))}.",
            recovery_line,
        ]
    )


def _deterministic_correlation_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    if "correlation" in rows[0]:
        lines = ["Đã tính correlation lợi suất ngày theo đúng câu hỏi.", "", "### Kết quả"]
        for row in rows[:10]:
            if row.get("ticker_a") and row.get("ticker_b"):
                lines.append(
                    f"- {row['ticker_a']} vs {row['ticker_b']}: correlation {_format_number(float(row['correlation']))}, "
                    f"{_format_integer(row.get('observations'))} quan sát."
                )
            else:
                lines.append(
                    f"- {row.get('ticker')} với {row.get('benchmark_ticker')}: correlation {_format_number(float(row['correlation']))}, "
                    f"{_format_integer(row.get('observations'))} quan sát."
                )
        return "\n".join(lines)
    if "corr_volume_abs_return" not in rows[0]:
        return None
    row = rows[0]
    return "\n".join(
        [
            f"Đã tính correlation giữa volume và biến động giá của {row.get('ticker')}.",
            "",
            "### Kết quả",
            f"- Corr(volume, |daily return|): {_format_number(float(row['corr_volume_abs_return']))}.",
            f"- Corr(volume, daily return): {_format_number(float(row['corr_volume_return']))}.",
            f"- Số quan sát: {_format_integer(row.get('observations'))}.",
            "",
            "### Lưu ý",
            "- `|daily return|` đo độ lớn biến động giá trong ngày, phù hợp hơn khi hỏi volume liên quan tới biến động mạnh/yếu.",
        ]
    )


def _deterministic_year_return_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "return_pct" not in rows[0]:
        return None
    lines = ["Đã tính lợi suất theo đúng khoảng năm được hỏi.", "", "### Kết quả"]
    for row in rows:
        lines.append(
            f"- {row['ticker']}: từ {_format_number(float(row['start_close']))} ngày {row.get('start_date')} "
            f"đến {_format_number(float(row['end_close']))} ngày {row.get('end_date')}, return {_format_percent(float(row['return_pct']))}."
        )
    return "\n".join(lines)


def _deterministic_drawdown_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or "max_drawdown_pct" not in rows[0]:
        return None
    row = rows[0]
    return "\n".join(
        [
            f"Max drawdown của {row.get('ticker')} trong năm được hỏi là {_format_percent(float(row['max_drawdown_pct']))}.",
            "",
            "### Chi tiết",
            f"- Ngày đáy drawdown: {row.get('date')}",
            f"- Close tại ngày đó: {_format_number(float(row['close']))}",
            f"- Running peak trước đó: {_format_number(float(row['running_peak']))}",
        ]
    )


def _deterministic_return_volatility_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_return_volatility(question):
        return None
    required = {"ticker", "period_return_pct", "daily_volatility_pct", "return_volatility_ratio"}
    if not required.issubset(rows[0]):
        return None

    usable = [
        row
        for row in rows
        if row.get("ticker")
        and _is_number(row.get("period_return_pct"))
        and _is_number(row.get("daily_volatility_pct"))
        and _is_number(row.get("return_volatility_ratio"))
    ]
    if not usable:
        return None

    ordered = sorted(usable, key=lambda row: float(row["return_volatility_ratio"]), reverse=True)
    leader = ordered[0]
    start_dates = [str(row.get("start_date"))[:10] for row in ordered if row.get("start_date")]
    end_dates = [str(row.get("end_date"))[:10] for row in ordered if row.get("end_date")]
    range_text = f", từ {min(start_dates)} đến {max(end_dates)}" if start_dates and end_dates else ""

    lines = [
        f"Đã xếp hạng theo tỷ lệ return/volatility{return_window_suffix(question)}{range_text}.",
        "",
        "### Kết quả",
    ]
    for row in ordered:
        lines.append(
            "- {ticker}: return {ret}, daily volatility {vol}, ratio {ratio}.".format(
                ticker=row["ticker"],
                ret=_format_percent(float(row["period_return_pct"])),
                vol=_format_percent(float(row["daily_volatility_pct"])),
                ratio=_format_number(float(row["return_volatility_ratio"])),
            )
        )
    lines.extend(
        [
            "",
            "### Kết luận",
            f"- Mã có tỷ lệ return/volatility tốt nhất trong nhóm là {leader['ticker']} với ratio {_format_number(float(leader['return_volatility_ratio']))}.",
            "- Ratio này dùng return cả kỳ chia cho độ lệch chuẩn lợi suất ngày; đây là thước đo lịch sử, không phải khuyến nghị mua bán.",
        ]
    )
    return "\n".join(lines)


def _deterministic_outperform_spy_lower_volatility_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_outperform_spy_lower_volatility(question):
        return None
    required = {"ticker", "period_return_pct", "spy_return_pct", "daily_volatility_pct", "spy_daily_volatility_pct"}
    if not required.issubset(rows[0]):
        return None
    usable = [
        row
        for row in rows
        if row.get("ticker")
        and _is_number(row.get("period_return_pct"))
        and _is_number(row.get("daily_volatility_pct"))
    ]
    if not usable:
        return None
    ordered = sorted(usable, key=lambda row: float(row["period_return_pct"]), reverse=True)
    spy_return = float(ordered[0]["spy_return_pct"])
    spy_vol = float(ordered[0]["spy_daily_volatility_pct"])
    lines = [
        "Đã lọc các mã có return cao hơn SPY và daily volatility thấp hơn SPY trong khoảng dữ liệu trả về.",
        "",
        "### Kết quả",
    ]
    for row in ordered[:10]:
        lines.append(
            "- {ticker}: return {ret}, volatility {vol}; SPY return {spy_ret}, SPY volatility {spy_vol}.".format(
                ticker=row["ticker"],
                ret=_format_percent(float(row["period_return_pct"])),
                vol=_format_percent(float(row["daily_volatility_pct"])),
                spy_ret=_format_percent(spy_return),
                spy_vol=_format_percent(spy_vol),
            )
        )
    lines.extend(
        [
            "",
            "### Kết luận",
            f"- Tìm thấy {len(ordered)} mã thỏa điều kiện trong dữ liệu hiện có.",
            "- Đây là so sánh lịch sử từ bảng giá, không phải dự báo hay khuyến nghị đầu tư.",
        ]
    )
    return "\n".join(lines)


def return_window_suffix(question: str) -> str:
    label = _requested_window_label(question)
    return f" trong {label}" if label else " trong 30 ngày gần nhất"


def _deterministic_latest_close_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    normalized = _normalize_text(question.lower())
    if not rows or "date" not in rows[0] or "close" not in rows[0]:
        return None
    if "gia dong cua" not in normalized and "close" not in normalized:
        return None
    if "gan nhat" not in normalized and "latest" not in normalized and "recent" not in normalized:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("ticker") and row.get("date") and _is_number(row.get("close")):
            grouped.setdefault(str(row["ticker"]), []).append(row)
    if not grouped:
        return None
    dates = sorted(str(row.get("date"))[:10] for row in rows if row.get("date"))
    tickers = ", ".join(sorted(grouped))
    lines = [
        f"Đã lấy giá đóng cửa của {tickers} trong {len(rows)} phiên giao dịch, từ {dates[0]} đến {dates[-1]}.",
        "",
        "### Tóm tắt",
    ]
    for ticker, ticker_rows in sorted(grouped.items()):
        ordered = sorted(ticker_rows, key=lambda row: str(row.get("date", "")))
        first = ordered[0]
        last = ordered[-1]
        lines.append(
            f"- {ticker}: từ {_format_number(float(first['close']))} ngày {str(first['date'])[:10]} "
            f"đến {_format_number(float(last['close']))} ngày {str(last['date'])[:10]}."
        )
    lines.extend(["", "Chi tiết từng phiên nằm trong bảng kết quả bên dưới."])
    return "\n".join(lines)


def _deterministic_monthly_high_close_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_monthly_high_close(question):
        return None

    monthly_rows = _monthly_high_rows_from_aggregate(rows) or _monthly_high_rows_from_daily(rows)
    if not monthly_rows:
        return None

    ordered = sorted(monthly_rows, key=lambda row: (str(row.get("ticker") or ""), str(row["month"])))
    best = max(ordered, key=lambda row: float(row["close"]))
    tickers = sorted({str(row.get("ticker")) for row in ordered if row.get("ticker")})
    subject = ", ".join(tickers) if tickers else "dữ liệu được trả về"

    lines = [
        f"Đã kiểm tra giá đóng cửa cao nhất theo từng tháng cho {subject}.",
        "",
        "### Cao nhất từng tháng",
    ]
    for row in ordered:
        ticker_prefix = f"{row['ticker']} - " if row.get("ticker") else ""
        date_text = f" vào {row['date']}" if row.get("date") else ""
        lines.append(f"- {ticker_prefix}{row['month']}: close cao nhất {_format_number(float(row['close']))}{date_text}.")

    lines.extend(
        [
            "",
            "### Kết luận",
            (
                f"- Mức cao nhất trong các tháng được trả về là {_format_number(float(best['close']))}"
                f"{' của ' + str(best['ticker']) if best.get('ticker') else ''}"
                f" ở tháng {best['month']}{' vào ' + str(best['date']) if best.get('date') else ''}."
            ),
            "- Kết luận này dùng toàn bộ rows backend trả về cho truy vấn hiện tại, không chỉ 8 dòng đầu để hiển thị.",
        ]
    )
    return "\n".join(lines)


def _deterministic_high_close_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_high_close(question) or _asks_for_monthly_high_close(question):
        return None
    daily_rows = _daily_close_rows(rows)
    if not daily_rows:
        return None
    best = max(daily_rows, key=lambda row: float(row["close"]))
    ticker_text = f" của {best['ticker']}" if best.get("ticker") else ""
    window_label = _requested_window_label(question)
    if len(daily_rows) > 1:
        start_date = min(str(row["date"]) for row in daily_rows if row.get("date"))
        end_date = max(str(row["date"]) for row in daily_rows if row.get("date"))
        range_line = f"- Khoảng ngày được kiểm tra trong rows trả về: {start_date} đến {end_date}."
    elif window_label:
        range_line = f"- Khoảng lọc của truy vấn: {window_label}."
    else:
        range_line = "- Truy vấn đã trả về dòng có giá đóng cửa cao nhất theo điều kiện lọc hiện tại."
    return "\n".join(
        [
            f"Giá đóng cửa cao nhất{ticker_text} trong dữ liệu truy vấn trả về là {_format_number(float(best['close']))}.",
            "",
            "### Chi tiết",
            f"- Ngày đạt mức cao nhất: {best.get('date') or 'không rõ ngày'}.",
            range_line,
            f"- Số dòng giá đã xét: {len(daily_rows)}.",
            "",
            "### Lưu ý",
            "- Kết luận này được tính từ toàn bộ rows backend trả về, không dựa vào vài dòng đầu trong bảng.",
        ]
    )


def _asks_for_monthly_high_close(question: str) -> bool:
    latest_question = _latest_user_question(question)
    normalized = _normalize_text(latest_question.lower())
    return _asks_for_high_close(question) and any(
        phrase in normalized for phrase in ["tung thang", "moi thang", "theo thang", "hang thang", "monthly", "per month"]
    )


def _asks_for_high_close(question: str) -> bool:
    latest_question = _latest_user_question(question)
    normalized = _normalize_text(latest_question.lower())
    has_close = any(phrase in normalized for phrase in ["gia dong cua", "close", "closing price"])
    has_high = any(phrase in normalized for phrase in ["cao nhat", "max", "maximum", "highest"])
    return has_close and has_high


def _requested_window_label(question: str) -> str | None:
    question_l = question.lower()
    match = re.search(r"\b(\d{1,4})\s*(ngày|ngay|days?|d)\b", question_l)
    if match:
        return f"{int(match.group(1))} ngày gần nhất"
    match = re.search(r"\b(\d{1,2})\s*(tháng|thang|months?|mo)\b", question_l)
    if match:
        return f"{int(match.group(1))} tháng gần nhất"
    match = re.search(r"\b(\d{1,2})\s*(năm|nam|years?|yrs?|y)\b", question_l)
    if match:
        return f"{int(match.group(1))} năm gần nhất"
    normalized = _normalize_text(question_l)
    match = re.search(r"\b(\d{1,4})\s*(ngay|days?|d)\b", normalized)
    if match:
        return f"{int(match.group(1))} ngày gần nhất"
    match = re.search(r"\b(\d{1,2})\s*(thang|months?|mo)\b", normalized)
    if match:
        return f"{int(match.group(1))} tháng gần nhất"
    match = re.search(r"\b(\d{1,2})\s*(nam|years?|yrs?|y)\b", normalized)
    if match:
        return f"{int(match.group(1))} năm gần nhất"
    return None


def _monthly_high_rows_from_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily_rows = _daily_close_rows(rows)
    monthly: dict[tuple[str, str], dict[str, Any]] = {}
    for row in daily_rows:
        month = str(row["date"])[:7]
        ticker = str(row.get("ticker") or "")
        key = (ticker, month)
        current = monthly.get(key)
        if current is None or float(row["close"]) > float(current["close"]):
            monthly[key] = {**row, "month": month}
    return list(monthly.values())


def _monthly_high_rows_from_aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first = rows[0]
    month_column = _first_existing_column(first, ["month", "month_start", "year_month", "period"])
    close_column = _first_matching_column(first, lambda column: "close" in column.lower() and any(
        token in column.lower() for token in ["max", "highest", "high"]
    ))
    if close_column is None and "close" in first and len(rows) <= 24:
        close_column = "close"
    if not month_column or not close_column:
        return []

    date_column = _first_existing_column(first, ["date", "max_close_date", "highest_close_date", "trading_date"])
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        close = row.get(close_column)
        month_value = row.get(month_column)
        if not _is_number(close) or month_value is None:
            continue
        normalized_rows.append(
            {
                "ticker": row.get("ticker"),
                "month": str(month_value)[:7],
                "date": str(row.get(date_column))[:10] if date_column and row.get(date_column) else None,
                "close": float(close),
            }
        )
    return normalized_rows


def _daily_close_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        date_value = row.get("date") or row.get("trading_date")
        close_value = row.get("close")
        if close_value is None:
            close_column = _first_matching_column(row, lambda column: column.lower().endswith("_close"))
            close_value = row.get(close_column) if close_column else None
        if not date_value or not _is_number(close_value):
            continue
        normalized_rows.append(
            {
                "ticker": row.get("ticker"),
                "date": str(date_value)[:10],
                "close": float(close_value),
            }
        )
    return normalized_rows


def _first_existing_column(row: dict[str, Any], candidates: list[str]) -> str | None:
    lowered = {column.lower(): column for column in row}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _first_matching_column(row: dict[str, Any], predicate: Any) -> str | None:
    for column in row:
        if predicate(column):
            return column
    return None


def _rows_for_explanation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) <= 80:
        return {"complete": True, "rows": rows}
    return {"complete": False, "head": rows[:8], "tail": rows[-8:]}


def _result_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    profile: dict[str, Any] = {"row_count": len(rows), "columns": list(rows[0].keys()) if rows else []}
    date_values = [
        str(value)[:10]
        for row in rows
        for value in [row.get("date") or row.get("trading_date") or row.get("month") or row.get("month_start")]
        if value
    ]
    if date_values:
        profile["min_date"] = min(date_values)
        profile["max_date"] = max(date_values)
    tickers = sorted({str(row.get("ticker")) for row in rows if row.get("ticker")})
    if tickers:
        profile["tickers"] = tickers
    return profile


def _deterministic_quarterly_close_explanation(question: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows or not _asks_for_quarterly_close_comparison(question):
        return None
    required = {"ticker", "quarter", "start_date", "start_close", "end_date", "end_close", "pct_change"}
    if not required.issubset(rows[0]):
        return None

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ticker = row.get("ticker")
        if isinstance(ticker, str):
            grouped.setdefault(ticker, []).append(row)
    if not grouped:
        return None

    lines = ["Đã so sánh xu hướng giá đóng cửa theo quý gần nhất với quý trước đó.", "", "### Kết quả theo quý"]
    for ticker, ticker_rows in sorted(grouped.items()):
        ordered = sorted(ticker_rows, key=lambda item: str(item.get("quarter", "")))
        lines.append(f"- {ticker}:")
        for row in ordered:
            lines.append(
                "  - {quarter}: {start} → {end}, thay đổi {delta} ({pct}), từ {start_date} đến {end_date}.".format(
                    quarter=str(row.get("quarter"))[:10],
                    start=_format_number(float(row["start_close"])),
                    end=_format_number(float(row["end_close"])),
                    delta=_format_signed_number(float(row["end_close"]) - float(row["start_close"])),
                    pct=_format_percent(float(row["pct_change"])),
                    start_date=row.get("start_date"),
                    end_date=row.get("end_date"),
                )
            )
        if len(ordered) >= 2:
            previous, latest = ordered[-2], ordered[-1]
            latest_pct = float(latest["pct_change"])
            previous_pct = float(previous["pct_change"])
            direction = "cải thiện" if latest_pct > previous_pct else "yếu hơn" if latest_pct < previous_pct else "tương đương"
            lines.append(
                f"  - So với quý trước, quý gần nhất {direction}: {ticker} đổi từ "
                f"{_format_percent(previous_pct)} sang {_format_percent(latest_pct)}."
            )

    lines.extend(
        [
            "",
            "### Lưu ý",
            "- Kết quả dùng các phiên giao dịch có trong Postgres; quý hiện tại có thể chưa đủ toàn bộ phiên nếu chưa kết thúc.",
            "- Truy vấn chỉ dùng ticker được hỏi rõ trong câu, không mở rộng từ context cũ.",
        ]
    )
    return "\n".join(lines)


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
            f"Đã lấy {total_points} dòng giá đóng cửa cho {len(summaries)} mã trong {window_label}, "
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


def _format_integer(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if _is_number(value):
        return f"{float(value):,.0f}"
    return str(value)


def _format_percent(value: float) -> str:
    return f"{value:+.2f}%"


def _format_large_number(value: float) -> str:
    units = [("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000)]
    for suffix, divisor in units:
        if abs(value) >= divisor:
            return f"{value / divisor:,.2f}{suffix}"
    return f"{value:,.0f}"


def _sanitize_answer(answer: str) -> str:
    answer = _sanitize_latex_math(answer)
    lines = answer.splitlines()
    sanitized: list[str] = []
    removed_table = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_markdown_table_line(line) and index + 1 < len(lines) and _is_markdown_table_divider(lines[index + 1]):
            removed_table = True
            while index < len(lines) and _is_markdown_table_line(lines[index]):
                index += 1
            if not sanitized or sanitized[-1].strip():
                sanitized.append("")
            sanitized.append("Chi tiết dạng bảng nằm trong bảng kết quả bên dưới.")
            continue
        sanitized.append(line)
        index += 1

    text = "\n".join(sanitized)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if removed_table and "Chi tiết dạng bảng nằm trong bảng kết quả bên dưới." not in text:
        text = f"{text}\n\nChi tiết dạng bảng nằm trong bảng kết quả bên dưới.".strip()
    return text


def _sanitize_latex_math(answer: str) -> str:
    def replace_block(match: re.Match[str]) -> str:
        return f"Công thức: {_plain_math(match.group(1))}"

    text = re.sub(r"\\\[(.*?)\\\]", replace_block, answer, flags=re.DOTALL)
    text = re.sub(r"\\\((.*?)\\\)", lambda match: _plain_math(match.group(1)), text, flags=re.DOTALL)
    return text


def _plain_math(value: str) -> str:
    text = " ".join(value.strip().split())
    for _ in range(4):
        updated = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1) / (\2)", text)
        if updated == text:
            break
        text = updated
    replacements = {
        r"\times": "x",
        r"\cdot": "x",
        r"\approx": "≈",
        r"\%": "%",
        r"\left": "",
        r"\right": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_markdown_table_divider(line: str) -> bool:
    stripped = line.strip()
    if not _is_markdown_table_line(stripped):
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in stripped.strip("|").split("|"))


def _fallback_explanation(rows: list[dict[str, Any]]) -> str:
    count = len(rows)
    first = rows[0]
    keys = ", ".join(first.keys())
    return f"Tìm thấy {count} dòng dữ liệu. Các cột chính gồm: {keys}."
