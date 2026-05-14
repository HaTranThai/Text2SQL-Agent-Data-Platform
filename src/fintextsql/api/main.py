from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from fintextsql.api.schemas import (
    ChatRequest,
    ChatResponse,
    CompanyResponse,
    HealthResponse,
    IngestionRequest,
    IngestionResponse,
    RoutePreviewResponse,
)
from fintextsql.core.config import Settings, get_settings
from fintextsql.core.intent import IntentRouter, RouteDecision
from fintextsql.db.models import Company
from fintextsql.db.session import get_db, init_db
from fintextsql.ingestion.yfinance_service import YFinanceIngestionService
from fintextsql.llm.client import LLMClient
from fintextsql.paths.news.service import NewsService
from fintextsql.paths.simple_finance.service import SimpleFinanceService
from fintextsql.paths.visualization.service import VisualizationService, infer_visualization
from fintextsql.text2sql.schema import full_schema_text
from fintextsql.text2sql.service import TextToSQLService

settings = get_settings()
SESSION_TICKERS: dict[str, list[str]] = {}
SESSION_MESSAGES: dict[str, str] = {}
SESSION_HISTORY: dict[str, list[dict[str, Any]]] = {}
MAX_SESSION_TURNS = 8
app = FastAPI(title="FinTextSQL", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", timestamp=datetime.now(timezone.utc))


@app.get("/schema")
def schema() -> dict[str, str]:
    return {"schema": full_schema_text()}


@app.get("/companies", response_model=list[CompanyResponse])
def companies(db: Session = Depends(get_db)) -> list[CompanyResponse]:
    rows = db.execute(select(Company).order_by(Company.ticker)).scalars().all()
    return [
        CompanyResponse(
            ticker=row.ticker,
            name=row.name,
            exchange=row.exchange,
            sector=row.sector,
            industry=row.industry,
            currency=row.currency,
        )
        for row in rows
    ]


@app.post("/ingest", response_model=IngestionResponse)
async def ingest(payload: IngestionRequest, db: Session = Depends(get_db)) -> IngestionResponse:
    run = await YFinanceIngestionService(db).ingest(
        tickers=payload.tickers,
        period=payload.period,
        interval=payload.interval,
        include_fundamentals=payload.include_fundamentals,
        include_news=payload.include_news,
    )
    return IngestionResponse(
        run_id=run.id,
        status=run.status,
        tickers=run.tickers,
        rows_loaded=run.rows_loaded,
        message=run.message,
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_settings: Settings = Depends(get_settings),
) -> ChatResponse:
    router = IntentRouter()
    decision = router.route(payload.message)
    explicit_tickers = list(decision.tickers)
    should_use_conversation_context = _should_use_conversation_context(payload.message, explicit_tickers)
    conversation_context = _conversation_context_text(payload.session_id) if should_use_conversation_context else ""
    context_tickers = _resolve_context_tickers(payload.session_id, payload.message, decision.tickers)
    if context_tickers:
        decision.tickers = context_tickers
        if context_tickers != explicit_tickers:
            decision.reason = f"{decision.reason}; reused session tickers"
    effective_message = _message_with_context_tickers(
        payload.message,
        decision.tickers,
        explicit_tickers,
        conversation_context=conversation_context,
    )
    llm = LLMClient(current_settings)
    text_to_sql = TextToSQLService(db, current_settings, llm)

    try:
        if decision.intent == "ingestion":
            tickers = decision.tickers or ["AAPL", "MSFT", "NVDA"]
            run = await YFinanceIngestionService(db).ingest(
                tickers=tickers,
                period=_period_from_message(payload.message),
                interval="1d",
                include_fundamentals=True,
                include_news=True,
            )
            response = ChatResponse(
                intent="ingestion",
                answer=run.message or "Ingestion finished.",
                rows=[
                    {
                        "run_id": run.id,
                        "status": run.status,
                        "tickers": ", ".join(run.tickers),
                        "rows_loaded": run.rows_loaded,
                    }
                ],
                columns=["run_id", "status", "tickers", "rows_loaded"],
                debug={
                    "router": asdict(decision),
                    "pipeline": ["parse_ingestion_request", "fetch_yfinance", "upsert_postgres"],
                },
            )
            _remember_session_turn(payload.session_id, payload.message, response, decision)
            return response

        if decision.intent == "news":
            answer, sources = await NewsService(db, llm).answer(payload.message, decision.tickers)
            response = ChatResponse(
                intent="news",
                answer=answer,
                rows=sources,
                columns=list(sources[0].keys()) if sources else [],
                sources=sources,
                debug={"router": asdict(decision), "pipeline": ["load_news", "analyze_news", "format_sources"]},
            )
            _remember_session_turn(payload.session_id, payload.message, response, decision)
            return response

        if decision.intent == "simple_finance":
            answer, rows = await SimpleFinanceService(db).answer(payload.message, decision.tickers)
            columns = list(rows[0].keys()) if rows else []
            response = ChatResponse(
                intent="simple_finance",
                answer=answer,
                rows=rows,
                columns=columns,
                visualization=infer_visualization(payload.message, columns, rows),
                sources=[{"source": row.get("source"), "ticker": row.get("ticker")} for row in rows],
                debug={
                    "router": asdict(decision),
                    "pipeline": ["quote_lookup", "fallback_postgres_if_needed", "infer_visualization"],
                },
            )
            _remember_session_turn(payload.session_id, payload.message, response, decision)
            return response

        if decision.intent == "visualization":
            result, viz = await VisualizationService(text_to_sql).answer(effective_message)
            response = ChatResponse(
                intent="visualization",
                answer=result.answer,
                sql=result.sql,
                rows=result.rows,
                columns=result.columns,
                visualization=viz,
                debug={
                    **result.debug,
                    "router": asdict(decision),
                    "pipeline": [*result.debug.get("pipeline", []), "infer_visualization"],
                },
            )
            _remember_session_turn(payload.session_id, payload.message, response, decision)
            return response

        result = await text_to_sql.answer(effective_message)
        response = ChatResponse(
            intent="text_to_sql",
            answer=result.answer,
            sql=result.sql,
            rows=result.rows,
            columns=result.columns,
            visualization=infer_visualization(payload.message, result.columns, result.rows),
            debug={**result.debug, "router": asdict(decision)},
        )
        _remember_session_turn(payload.session_id, payload.message, response, decision)
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/chat/route", response_model=RoutePreviewResponse)
async def chat_route(payload: ChatRequest) -> RoutePreviewResponse:
    router = IntentRouter()
    decision = router.route(payload.message)
    explicit_tickers = list(decision.tickers)
    context_tickers = _resolve_context_tickers(payload.session_id, payload.message, decision.tickers)
    if context_tickers:
        decision.tickers = context_tickers
        if context_tickers != explicit_tickers:
            decision.reason = f"{decision.reason}; reused session tickers"
    return RoutePreviewResponse(
        intent=decision.intent,
        tickers=decision.tickers,
        reason=decision.reason,
        pipeline=_preview_pipeline(decision.intent),
        router=asdict(decision),
    )


@app.post("/query/sql", response_model=ChatResponse)
async def query_sql(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_settings: Settings = Depends(get_settings),
) -> ChatResponse:
    result = await TextToSQLService(db, current_settings, LLMClient(current_settings)).answer(payload.message)
    return ChatResponse(
        intent="text_to_sql",
        answer=result.answer,
        sql=result.sql,
        rows=result.rows,
        columns=result.columns,
        visualization=infer_visualization(payload.message, result.columns, result.rows),
        debug=result.debug,
    )


def _period_from_message(message: str) -> str:
    text = message.lower()
    match = re.search(r"\b(1|2|5|10)y\b", text)
    if match:
        return f"{match.group(1)}y"
    match = re.search(r"\b(1|3|6)mo\b", text)
    if match:
        return f"{match.group(1)}mo"
    match = re.search(r"\b(\d+)\s*năm\b", text)
    if match and match.group(1) in {"1", "2", "5", "10"}:
        return f"{match.group(1)}y"
    return "1y"


def _preview_pipeline(intent: str) -> list[str]:
    if intent == "ingestion":
        return ["parse_ingestion_request", "fetch_yfinance", "upsert_postgres"]
    if intent == "news":
        return ["load_news", "analyze_news", "format_sources"]
    if intent == "simple_finance":
        return ["quote_lookup", "fallback_postgres_if_needed", "infer_visualization"]
    if intent == "visualization":
        return [
            "load_schema",
            "schema_selector",
            "planner",
            "sql_generator",
            "sql_guard",
            "execute_sql",
            "explainer",
            "infer_visualization",
        ]
    return ["load_schema", "schema_selector", "planner", "sql_generator", "sql_guard", "execute_sql", "explainer"]


def _resolve_context_tickers(
    session_id: str | None,
    message: str,
    explicit_tickers: list[str],
) -> list[str]:
    if session_id and explicit_tickers and _is_additive_ticker_reference(message):
        previous = SESSION_TICKERS.get(session_id, [])
        merged = _merge_tickers(previous, explicit_tickers)
        _remember_session_tickers(session_id, merged)
        return merged
    if explicit_tickers:
        _remember_session_tickers(session_id, explicit_tickers)
        return explicit_tickers
    if not session_id or not _is_follow_up_ticker_reference(message):
        return []
    return SESSION_TICKERS.get(session_id, [])


def _remember_session_tickers(session_id: str | None, tickers: list[str]) -> None:
    if session_id and tickers:
        SESSION_TICKERS[session_id] = tickers[:10]


def _remember_session_message(session_id: str | None, message: str) -> None:
    if session_id and message.strip():
        SESSION_MESSAGES[session_id] = message.strip()


def _remember_session_turn(
    session_id: str | None,
    user_message: str,
    response: ChatResponse,
    decision: RouteDecision,
) -> None:
    if not session_id or not user_message.strip():
        return

    _remember_session_message(session_id, user_message)
    _remember_session_tickers(session_id, decision.tickers)

    history = SESSION_HISTORY.setdefault(session_id, [])
    history.append(
        {
            "user": user_message.strip(),
            "intent": response.intent,
            "tickers": decision.tickers[:10],
            "answer": _compact_text(response.answer, 320),
            "sql": _compact_text(response.sql or "", 500),
            "columns": response.columns[:12],
            "row_count": len(response.rows),
        }
    )
    del history[:-MAX_SESSION_TURNS]


def _conversation_context_text(session_id: str | None, limit: int = 6) -> str:
    if not session_id:
        return ""
    history = SESSION_HISTORY.get(session_id, [])[-limit:]
    if not history:
        return ""

    lines = ["recent conversation context:"]
    for index, turn in enumerate(history, start=1):
        lines.append(f"turn {index} user question: {turn.get('user', '')}")
        tickers = turn.get("tickers") or []
        if tickers:
            lines.append(f"turn {index} tickers: {', '.join(tickers)}")
        lines.append(f"turn {index} intent: {turn.get('intent', '')}")
        columns = turn.get("columns") or []
        row_count = turn.get("row_count", 0)
        if columns:
            lines.append(f"turn {index} result shape: {row_count} rows; columns: {', '.join(columns)}")
        answer = str(turn.get("answer") or "")
        if answer:
            lines.append(f"turn {index} answer summary: {answer}")
    lines.append(
        "Use this context to resolve follow-up references like 'ma tren', '2 ma tren', "
        "'them another ticker', or 'the same tickers'."
    )
    lines.append("For follow-up SQL, use only the context tickers unless the latest user question explicitly replaces them.")
    return "\n".join(lines)


def _is_follow_up_ticker_reference(message: str) -> bool:
    text = message.lower()
    return any(
        phrase in text
        for phrase in [
            "mã trên",
            "ma tren",
            "2 mã",
            "hai mã",
            "các mã trên",
            "cac ma tren",
            "ở trên",
            "o tren",
            "above",
            "previous",
            "same tickers",
        ]
    )


def _is_additive_ticker_reference(message: str) -> bool:
    text = message.lower()
    return any(
        phrase in text
        for phrase in [
            "nữa",
            "nua",
            "thêm",
            "them",
            "so sánh với",
            "so sanh voi",
            "cùng với",
            "cung voi",
            "also",
            "add",
            "include",
            "with",
        ]
    )


def _should_use_conversation_context(message: str, explicit_tickers: list[str]) -> bool:
    if _is_follow_up_ticker_reference(message):
        return True
    if explicit_tickers and _is_additive_ticker_reference(message):
        return True
    return False


def _merge_tickers(previous: list[str], current: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for ticker in [*previous, *current]:
        if ticker in seen:
            continue
        seen.add(ticker)
        merged.append(ticker)
    return merged


def _message_with_context_tickers(
    message: str,
    tickers: list[str],
    explicit_tickers: list[str],
    conversation_context: str | None = None,
) -> str:
    context_lines: list[str] = []
    if conversation_context:
        context_lines.append(conversation_context)
    if tickers and (not explicit_tickers or tickers != explicit_tickers):
        context_lines.append(f"Context tickers: {', '.join(tickers)}")
    if conversation_context and (_is_follow_up_ticker_reference(message) or _is_additive_ticker_reference(message)):
        context_lines.append(
            "Resolved follow-up instruction: continue the prior analysis using the same metric and time window "
            "from recent conversation, and apply the context tickers above unless the latest user question "
            "explicitly asks for a different metric or window."
        )
    if not context_lines:
        return message
    return "\n\n".join([message, *context_lines])


def _compact_text(value: str, max_length: int) -> str:
    compacted = re.sub(r"\s+", " ", value).strip()
    if len(compacted) <= max_length:
        return compacted
    return compacted[: max_length - 3].rstrip() + "..."
