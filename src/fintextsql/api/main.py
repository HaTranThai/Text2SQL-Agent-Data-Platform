from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from unicodedata import category, normalize

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
    TaskResult,
    VisualizationSpec,
)
from fintextsql.core.config import Settings, get_settings
from fintextsql.core.intent import IntentRouter, RouteDecision
from fintextsql.core.policy import PolicyDecision, PolicyGuard
from fintextsql.core.planner import AgentPlan, PlannedTask, TaskPlanner
from fintextsql.core.tickers import extract_tickers
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
SESSION_STATE: dict[str, dict[str, Any]] = {}
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
    policy = PolicyGuard().check(payload.message)
    if policy.triggered:
        return _policy_chat_response(policy)

    # Resolve context-borrowing follow-ups ("so nó với GOOG", "còn TSLA thì sao") into a
    # self-contained question before routing/planning, so the same analysis applies.
    resolved = _rewrite_follow_up(payload.session_id, payload.message)
    if resolved and resolved != payload.message:
        payload.message = resolved

    router = IntentRouter()
    agent_plan = TaskPlanner(SESSION_STATE.get(payload.session_id)).plan(payload.message)
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
    if len(agent_plan.tasks) == 1 and decision.intent in {"text_to_sql", "visualization"}:
        effective_message = agent_plan.tasks[0].message
        if conversation_context:
            effective_message = "\n\n".join([effective_message, conversation_context])
    llm = LLMClient(current_settings)
    text_to_sql = TextToSQLService(db, current_settings, llm)

    try:
        if len(agent_plan.tasks) > 1:
            response = await _execute_agent_plan(
                payload=payload,
                plan=agent_plan,
                db=db,
                settings=current_settings,
                llm=llm,
            )
            _remember_session_turn(payload.session_id, payload.message, response, decision)
            return response

        if decision.intent == "general":
            response = ChatResponse(
                intent="general",
                answer=_general_chat_answer(payload.message),
                debug={
                    "router": asdict(decision),
                    "pipeline": ["general_help"],
                    "planner": agent_plan.to_debug(),
                    "task_count": len(agent_plan.tasks),
                    "context_used": agent_plan.context_used,
                    "resolved_state": agent_plan.resolved_state,
                },
            )
            _remember_session_turn(payload.session_id, payload.message, response, decision)
            return response

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
                    "planner": agent_plan.to_debug(),
                    "task_count": len(agent_plan.tasks),
                    "context_used": agent_plan.context_used,
                    "resolved_state": agent_plan.resolved_state,
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
                debug={
                    "router": asdict(decision),
                    "pipeline": ["load_news", "analyze_news", "format_sources"],
                    "planner": agent_plan.to_debug(),
                    "task_count": len(agent_plan.tasks),
                    "context_used": agent_plan.context_used,
                    "resolved_state": agent_plan.resolved_state,
                },
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
                    "planner": agent_plan.to_debug(),
                    "task_count": len(agent_plan.tasks),
                    "context_used": agent_plan.context_used,
                    "resolved_state": agent_plan.resolved_state,
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
                    "planner": agent_plan.to_debug(),
                    "task_count": len(agent_plan.tasks),
                    "context_used": agent_plan.context_used,
                    "resolved_state": agent_plan.resolved_state,
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
            debug={
                **result.debug,
                "router": asdict(decision),
                "planner": agent_plan.to_debug(),
                "task_count": len(agent_plan.tasks),
                "context_used": agent_plan.context_used,
                "resolved_state": agent_plan.resolved_state,
            },
        )
        _remember_session_turn(payload.session_id, payload.message, response, decision)
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/chat/route", response_model=RoutePreviewResponse)
async def chat_route(payload: ChatRequest) -> RoutePreviewResponse:
    policy = PolicyGuard().check(payload.message)
    if policy.triggered:
        return RoutePreviewResponse(
            intent="general",
            tickers=[],
            reason=policy.answer,
            pipeline=["policy_guard"],
            router={"intent": "general", "confidence": "high", "tickers": [], "policy_guard": policy.to_debug()},
        )

    router = IntentRouter()
    decision = router.route(payload.message)
    agent_plan = TaskPlanner(SESSION_STATE.get(payload.session_id)).plan(payload.message)
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
        router={**asdict(decision), "planner": agent_plan.to_debug(), "task_count": len(agent_plan.tasks)},
    )


async def _execute_agent_plan(
    *,
    payload: ChatRequest,
    plan: AgentPlan,
    db: Session,
    settings: Settings,
    llm: LLMClient,
) -> ChatResponse:
    sub_results: list[TaskResult] = []
    for task in plan.tasks:
        sub_results.append(await _execute_planned_task(task, db=db, settings=settings, llm=llm))

    primary = sub_results[0]
    answer_parts = [f"### {result.title}\n{result.answer}" for result in sub_results]
    return ChatResponse(
        intent=primary.intent,
        answer="\n\n".join(answer_parts),
        sql=primary.sql,
        rows=primary.rows,
        columns=primary.columns,
        visualization=primary.visualization,
        sub_results=sub_results,
        debug={
            "pipeline": ["task_planner", "execute_tasks", "synthesize_response"],
            "planner": plan.to_debug(),
            "task_count": len(plan.tasks),
            "context_used": plan.context_used,
            "resolved_state": plan.resolved_state,
        },
    )


async def _execute_planned_task(
    task: PlannedTask,
    *,
    db: Session,
    settings: Settings,
    llm: LLMClient,
) -> TaskResult:
    if task.intent == "news":
        answer, sources = await NewsService(db, llm).answer(task.message, task.tickers)
        return TaskResult(
            intent="news",
            title=task.title,
            answer=answer,
            rows=sources,
            columns=list(sources[0].keys()) if sources else [],
            debug={"pipeline": ["load_news", "analyze_news", "format_sources"], "task": asdict(task)},
        )
    text_to_sql = TextToSQLService(db, settings, llm)
    if task.intent == "visualization":
        result, viz = await VisualizationService(text_to_sql).answer(task.message)
        if {"quarter", "pct_change"}.issubset(set(result.columns)):
            viz = VisualizationSpec(type="bar", x="quarter", y="pct_change", series="ticker", title="Quarterly close % change")
        return TaskResult(
            intent="visualization",
            title=task.title,
            answer="Đã tạo biểu đồ từ cùng truy vấn để dễ so sánh các quý.",
            sql=result.sql,
            rows=result.rows,
            columns=result.columns,
            visualization=viz,
            debug={**result.debug, "pipeline": [*result.debug.get("pipeline", []), "infer_visualization"], "task": asdict(task)},
        )
    result = await text_to_sql.answer(task.message)
    return TaskResult(
        intent="text_to_sql",
        title=task.title,
        answer=result.answer,
        sql=result.sql,
        rows=result.rows,
        columns=result.columns,
        visualization=infer_visualization(task.message, result.columns, result.rows),
        debug={**result.debug, "task": asdict(task)},
    )


@app.post("/query/sql", response_model=ChatResponse)
async def query_sql(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_settings: Settings = Depends(get_settings),
) -> ChatResponse:
    policy = PolicyGuard().check(payload.message)
    if policy.triggered:
        return _policy_chat_response(policy)

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


def _policy_chat_response(policy: PolicyDecision) -> ChatResponse:
    return ChatResponse(
        intent="general",
        answer=policy.answer,
        rows=[],
        columns=[],
        debug={
            "pipeline": ["policy_guard"],
            "policy_guard": policy.to_debug(),
            "router": {
                "intent": "general",
                "confidence": "high",
                "tickers": [],
                "reason": policy.category or "policy_guard",
            },
        },
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
    if intent == "general":
        return ["general_help"]
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


def _general_chat_answer(message: str) -> str:
    text = _strip_vietnamese_accents(message.lower())
    if any(word in text for word in ["xin chao", "hello", "hi"]):
        opener = "Chào bạn. Mình là trợ lý phân tích dữ liệu tài chính của FinTextSQL."
    else:
        opener = "Mình có thể hỗ trợ bạn phân tích dữ liệu tài chính bằng câu hỏi tự nhiên."

    return "\n".join(
        [
            opener,
            "",
            "### Mình làm tốt các việc này",
            "- Tra cứu và so sánh giá đóng cửa, volume, market cap, P/E, beta.",
            "- Sinh SQL an toàn từ câu hỏi và trả bảng kết quả.",
            "- Vẽ chart giá theo thời gian cho một hoặc nhiều mã.",
            "- Lấy và tóm tắt tin tức có thể ảnh hưởng tới một ticker.",
            "- Nhớ ngữ cảnh ngắn hạn trong cùng cuộc trò chuyện, ví dụ “các mã đó”, “cùng khoảng thời gian”, “cái đó”.",
            "",
            "### Ví dụ bạn có thể hỏi",
            "- `Giá đóng cửa cao nhất trong 8 tháng qua của AAPL là ngày nào?`",
            "- `Liệt kê giá đóng cửa cao nhất từng tháng của AAPL trong 8 tháng qua`",
            "- `So sánh AAPL, MSFT, NVDA trong 180 ngày gần nhất`",
            "- `Có tin gì mới có thể ảnh hưởng tới giá Apple không?`",
            "- `Vẽ chart giá đóng cửa của AAPL và MSFT trong 60 ngày gần nhất`",
            "",
            "Bạn cứ hỏi như đang nói chuyện bình thường; nếu viết `apple`, `appl` hoặc lỡ gõ `apply`, mình sẽ hiểu là `AAPL`.",
        ]
    )


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
    if not session_id or not (
        _is_follow_up_ticker_reference(message) or _is_implicit_same_ticker_reference(message)
    ):
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
    state = _extract_turn_state(user_message, response, decision)
    _remember_session_state(session_id, state)

    history = SESSION_HISTORY.setdefault(session_id, [])
    history.append(
        {
            "user": _compact_text(user_message.strip(), 180),
            "intent": response.intent,
            "tickers": decision.tickers[:10],
            "columns": response.columns[:12],
            "row_count": len(response.rows),
            "summary": _turn_context_summary(user_message, response, decision),
            "state": state,
        }
    )
    del history[:-MAX_SESSION_TURNS]


def _conversation_context_text(session_id: str | None, limit: int = 6) -> str:
    if not session_id:
        return ""
    history = SESSION_HISTORY.get(session_id, [])[-limit:]
    if not history:
        return ""

    lines = [
        "recent conversation context:",
        "This is a compact summary only. Never copy a previous answer verbatim; always answer the latest question with fresh reasoning and fresh SQL when data is needed.",
    ]
    state = SESSION_STATE.get(session_id)
    if state:
        lines.append(f"current structured state: {_format_state(state)}")
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
        turn_state = turn.get("state") or {}
        if turn_state:
            lines.append(f"turn {index} structured state: {_format_state(turn_state)}")
        summary = str(turn.get("summary") or "")
        if summary:
            lines.append(f"turn {index} result summary: {summary}")
    lines.append(
        "Use this context to resolve follow-up references like 'cai do', 'ma tren', '2 ma tren', "
        "'them another ticker', 'the same tickers', or verification questions about the previous result."
    )
    lines.append("For follow-up SQL, use only the context tickers unless the latest user question explicitly replaces them.")
    lines.append(
        "When the latest question says 'same period', 'cung khoang thoi gian', 'cai do', or 'ket qua do', "
        "reuse metric and time_window from current structured state unless explicitly changed."
    )
    return "\n".join(lines)


def _is_follow_up_ticker_reference(message: str) -> bool:
    text = message.lower()
    ascii_text = _strip_vietnamese_accents(text)
    variants = (text, ascii_text)
    return any(
        phrase in variant
        for variant in variants
        for phrase in [
            "cái đó",
            "cai do",
            "cái này",
            "cai nay",
            "điều đó",
            "dieu do",
            "kết quả đó",
            "ket qua do",
            "kết quả trên",
            "ket qua tren",
            "có chắc",
            "co chac",
            "chắc không",
            "chac khong",
            "mã trên",
            "ma tren",
            "2 mã",
            "2 ma",
            "hai mã",
            "hai ma",
            "các mã trên",
            "cac ma tren",
            "ở trên",
            "o tren",
            "vừa rồi",
            "vua roi",
            "ban nãy",
            "ban nay",
            "trước đó",
            "truoc do",
            "câu trước",
            "cau truoc",
            "lượt trước",
            "luot truoc",
            "previous result",
            "above",
            "previous",
            "same tickers",
            "same ticker",
            "that result",
            "that one",
            "cùng khoảng thời gian",
            "cung khoang thoi gian",
            "cùng giai đoạn",
            "cung giai doan",
            "same period",
            "same window",
        ]
    )


def _is_additive_ticker_reference(message: str) -> bool:
    text = message.lower()
    ascii_text = _strip_vietnamese_accents(text)
    variants = (text, ascii_text)
    return any(
        phrase in variant
        for variant in variants
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
        ]
    )


def _should_use_conversation_context(message: str, explicit_tickers: list[str]) -> bool:
    if _is_follow_up_ticker_reference(message):
        return True
    if explicit_tickers and _is_additive_ticker_reference(message):
        return True
    if not explicit_tickers and _is_implicit_same_ticker_reference(message):
        return True
    return False


def _is_implicit_same_ticker_reference(message: str) -> bool:
    text = _strip_vietnamese_accents(message.lower())
    has_metric = any(
        phrase in text
        for phrase in [
            "volume",
            "khoi luong",
            "giao dich",
            "gia",
            "close",
            "open",
            "high",
            "low",
            "ohlc",
            "return",
            "loi suat",
            "bien dong",
            "market cap",
            "von hoa",
        ]
    )
    has_specific_request = any(
        phrase in text
        for phrase in [
            "lay ",
            "cho toi",
            "hien thi",
            "so sanh",
            "vao ngay",
            "phien",
            "quy ",
            "thang ",
            "nam ",
        ]
    ) or bool(re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text))
    asks_universe = any(
        phrase in text
        for phrase in [
            "ticker nao",
            "ma nao",
            "co phieu nao",
            "cong ty nao",
            "top ",
            "xep hang",
            "tat ca ma",
            "cac ma",
            "cac ticker",
        ]
    )
    return has_metric and has_specific_request and not asks_universe


def _remember_session_state(session_id: str | None, state: dict[str, Any]) -> None:
    if not session_id:
        return
    previous = SESSION_STATE.get(session_id, {})
    merged = {**previous, **{key: value for key, value in state.items() if value not in (None, [], "")}}
    if merged:
        SESSION_STATE[session_id] = merged


def _extract_turn_state(
    user_message: str,
    response: ChatResponse,
    decision: RouteDecision,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "intent": response.intent,
        "tickers": decision.tickers[:10],
        "active_tickers": decision.tickers[:10],
    }
    metric = _infer_metric(user_message, response.columns)
    if metric:
        state["metric"] = metric
        state["last_metric"] = metric
    window = _infer_time_window(user_message)
    if window:
        state["time_window"] = window
        state["last_time_window"] = window
    grouping = _infer_grouping(user_message, response.columns)
    if grouping:
        state["grouping"] = grouping
        state["last_grouping"] = grouping
    if response.sql:
        state["last_sql_kind"] = _infer_sql_kind(response.sql, response.columns)
    if response.columns:
        state["columns"] = response.columns[:12]
    state["row_count"] = len(response.rows)
    state["last_task_result"] = {
        "intent": response.intent,
        "row_count": len(response.rows),
        "columns": response.columns[:12],
        "summary": _turn_context_summary(user_message, response, decision),
    }
    return state


def _infer_metric(message: str, columns: list[str]) -> str | None:
    text = _strip_vietnamese_accents(message.lower())
    column_text = " ".join(columns).lower()
    combined = f"{text} {column_text}"
    if "market cap" in combined or "von hoa" in combined:
        return "market_cap"
    if ("open" in combined or "gia mo cua" in combined) and ("high" in combined or "cao nhat" in combined) and (
        "low" in combined or "thap nhat" in combined
    ):
        return "ohlc"
    if "volume" in combined:
        return "volume"
    if "pe" in combined or "p/e" in combined:
        return "pe_ratio"
    if "close" in combined or "gia dong cua" in combined:
        if "cao nhat" in combined or "highest" in combined or "max" in combined:
            return "highest_close"
        if "thap nhat" in combined or "lowest" in combined or "min" in combined:
            return "lowest_close"
        return "close_price"
    if "tin tuc" in combined or "news" in combined:
        return "news"
    return None


def _infer_time_window(message: str) -> str | None:
    text = _strip_vietnamese_accents(message.lower())
    patterns = [
        (r"\b(\d{1,4})\s*(ngay|days?|d)\b", "days"),
        (r"\b(\d{1,2})\s*(thang|months?|mo)\b", "months"),
        (r"\b(\d{1,2})\s*(nam|years?|yrs?|y)\b", "years"),
    ]
    for pattern, unit in patterns:
        match = re.search(pattern, text)
        if match:
            return f"{int(match.group(1))} {unit}"
    return None


def _infer_grouping(message: str, columns: list[str]) -> str | None:
    text = _strip_vietnamese_accents(message.lower())
    column_text = " ".join(columns).lower()
    if "quarter" in column_text or "quy" in text:
        return "quarter"
    if "month" in column_text or "thang" in text:
        return "month"
    return None


def _infer_sql_kind(sql: str, columns: list[str]) -> str:
    sql_l = sql.lower()
    if "date_trunc('month'" in sql_l:
        return "monthly_aggregate"
    if "row_number()" in sql_l and "order by p.close desc" in sql_l:
        return "ranked_high_close"
    if {"date", "close"}.issubset(set(columns)):
        return "daily_price_series"
    return "select"


def _format_state(state: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in [
        "intent",
        "active_tickers",
        "tickers",
        "metric",
        "last_metric",
        "time_window",
        "last_time_window",
        "grouping",
        "last_grouping",
        "last_sql_kind",
        "columns",
        "row_count",
    ]:
        value = state.get(key)
        if value not in (None, [], ""):
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _turn_context_summary(user_message: str, response: ChatResponse, decision: RouteDecision) -> str:
    parts: list[str] = []
    tickers = decision.tickers[:10]
    metric = _infer_metric(user_message, response.columns)
    window = _infer_time_window(user_message)
    grouping = _infer_grouping(user_message, response.columns)
    if tickers:
        parts.append(f"tickers={', '.join(tickers)}")
    if metric:
        parts.append(f"metric={metric}")
    if window:
        parts.append(f"window={window}")
    if grouping:
        parts.append(f"grouping={grouping}")
    parts.append(f"intent={response.intent}")
    parts.append(f"rows={len(response.rows)}")
    if response.columns:
        parts.append(f"columns={', '.join(response.columns[:8])}")

    profile = _result_profile(response.rows, response.columns)
    if profile:
        parts.append(profile)
    return _compact_text("; ".join(parts), 420)


def _result_profile(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "result=empty"
    profiles: list[str] = []
    date_columns = [column for column in columns if column.endswith("date") or column == "date"]
    for column in date_columns[:2]:
        dates = sorted(str(row.get(column))[:10] for row in rows if row.get(column))
        if dates:
            profiles.append(f"{column}_range={dates[0]}..{dates[-1]}")

    for column in ["ticker", "target_volume", "quarter_max_volume", "volume", "close", "market_cap", "pct_change"]:
        if column not in columns:
            continue
        values = [row.get(column) for row in rows if row.get(column) not in (None, "")]
        if not values:
            continue
        if column == "ticker":
            unique = []
            for value in values:
                text = str(value)
                if text not in unique:
                    unique.append(text)
            profiles.append(f"tickers_in_result={', '.join(unique[:6])}")
        elif _is_scalar_number(values[0]):
            numeric_values = [float(value) for value in values if _is_scalar_number(value)]
            if numeric_values:
                profiles.append(
                    f"{column}_min={_format_profile_number(min(numeric_values))}, {column}_max={_format_profile_number(max(numeric_values))}"
                )
        else:
            profiles.append(f"{column}_sample={values[0]}")
    return "; ".join(profiles[:5])


def _is_scalar_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_profile_number(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value:,.0f}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


_METRIC_TERMS = [
    "gia", "close", "volume", "khoi luong", "market cap", "von hoa", "pe", "p/e", "beta",
    "drawdown", "volatility", "bien dong", "correlation", "tuong quan", "return", "loi suat",
    "ma20", "ma50", "ma200", "dinh 52", "52 tuan", "cao nhat", "thap nhat", "trung binh", "ohlc",
    "tang", "giam", "rui ro",
]


def _has_metric_term(text_ascii: str) -> bool:
    return any(term in text_ascii for term in _METRIC_TERMS)


def _replace_tickers_in_text(text: str, old_tickers: list[str], target_tickers: list[str]) -> str | None:
    """Replace the first old ticker with the target list, drop the rest. None if no literal match."""
    target = ", ".join(target_tickers)
    result = text
    replaced = False
    for ticker in old_tickers:
        pattern = rf"\b{re.escape(ticker)}\b"
        if re.search(pattern, result):
            result = re.sub(pattern, target if not replaced else "", result, count=1)
            replaced = True
    if not replaced:
        return None
    return re.sub(r"\s+", " ", result).strip().strip(",").strip()


def _rewrite_follow_up(session_id: str | None, message: str) -> str | None:
    """Resolve a context-borrowing follow-up by rewriting the previous question.

    Handles comparison ("so nó với GOOG" -> merge tickers) and continuation
    ("còn TSLA thì sao" -> swap ticker), reusing the prior analysis/metric/window.
    Returns None when the message is self-sufficient or not a follow-up.
    """
    if not session_id:
        return None
    prev = SESSION_MESSAGES.get(session_id)
    if not prev:
        return None
    text = _strip_vietnamese_accents(message.lower())
    if _has_metric_term(text):
        return None  # the message already names its own metric -> not borrowing context
    words = text.split()
    is_compare = ("so" in words or "so sanh" in text or "doi chieu" in text or "vs" in words) and (
        "voi" in words or "cung" in words or "vs" in words
    )
    is_continuation = any(
        phrase in text for phrase in ["con ", "the con", "thi sao", "tuong tu", "con lai", "tiep theo"]
    )
    is_pronoun = any(phrase in text for phrase in ["cai do", "cai nay", "cai kia", "chung", "cac ma do"]) or "no" in words
    if not (is_compare or is_continuation or is_pronoun):
        return None
    old_tickers = extract_tickers(prev)
    if not old_tickers:
        return None
    new_tickers = extract_tickers(message)
    if new_tickers:
        target = _merge_tickers(old_tickers, new_tickers) if is_compare else new_tickers
        rewritten = _replace_tickers_in_text(prev, old_tickers, target)
        if rewritten:
            return rewritten
        return f"{prev} — áp dụng cho {', '.join(target)}"
    return prev  # pronoun-only follow-up: reuse the previous question verbatim


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
    state_instruction = _state_follow_up_instruction(message, tickers)
    if state_instruction:
        context_lines.append(state_instruction)
    if not context_lines:
        return message
    return "\n\n".join([message, *context_lines])


def _state_follow_up_instruction(message: str, tickers: list[str]) -> str:
    text = _strip_vietnamese_accents(message.lower())
    if not any(
        phrase in text
        for phrase in [
            "cai do",
            "ket qua do",
            "cung khoang thoi gian",
            "cung giai doan",
            "same period",
            "same window",
            "that result",
        ]
    ):
        return ""
    ticker_text = f" Use only these tickers: {', '.join(tickers)}." if tickers else ""
    return (
        "Structured follow-up contract: resolve pronouns and omitted details from current structured state. "
        "Do not broaden to all tickers or a different time window unless explicitly requested."
        f"{ticker_text}"
    )


def _compact_text(value: str, max_length: int) -> str:
    compacted = re.sub(r"\s+", " ", value).strip()
    if len(compacted) <= max_length:
        return compacted
    return compacted[: max_length - 3].rstrip() + "..."


def _strip_vietnamese_accents(value: str) -> str:
    value = value.replace("đ", "d").replace("Đ", "D")
    return "".join(char for char in normalize("NFD", value) if category(char) != "Mn")
