from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from fintextsql.api.schemas import IntentName
from fintextsql.core.intent import IntentRouter
from fintextsql.core.tickers import extract_tickers


@dataclass(slots=True)
class PlannedTask:
    intent: IntentName
    title: str
    message: str
    tickers: list[str] = field(default_factory=list)
    metric: str | None = None
    time_window: str | None = None
    grouping: str | None = None
    comparison: str | None = None
    requires_chart: bool = False


@dataclass(slots=True)
class AgentPlan:
    tasks: list[PlannedTask]
    resolved_state: dict[str, Any]
    context_used: bool
    reason: str

    def to_debug(self) -> dict[str, Any]:
        return {
            "tasks": [asdict(task) for task in self.tasks],
            "resolved_state": self.resolved_state,
            "context_used": self.context_used,
            "reason": self.reason,
        }


class TaskPlanner:
    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state or {}
        self.router = IntentRouter()

    def plan(self, message: str, decision=None) -> AgentPlan:
        """Plan tasks from a question.

        `decision` (RouteDecision) can be passed in so the planner reuses an
        upstream LLM router result instead of re-running the rule router. When
        omitted, falls back to the embedded rule-based router for backward
        compat.
        """
        text = _normalize_text(message)
        explicit_tickers = extract_tickers(message)
        context_used = _is_follow_up(text) or (
            not explicit_tickers and _is_implicit_same_ticker_reference(text)
        )
        tickers = self._resolve_tickers(text, explicit_tickers)
        metric = _infer_metric(text, message, self.state)
        time_window = _infer_time_window(text, self.state if context_used else {})
        grouping = _infer_grouping(text, self.state if context_used else {})
        comparison = _infer_comparison(text)
        wants_chart = _contains_any(text, ["chart", "ve chart", "ve bieu do", "bieu do", "do thi", "plot", "visualize"])

        route = decision if decision is not None else self.router.route(message)
        base_intent = route.intent
        # Web search covers both news AND company facts (CEO, founder, website).
        if route.intent == "web_search":
            base_intent = "web_search"
        elif _contains_any(text, ["tin tuc", "tin moi", "co tin", "news", "headline", "bai bao"]):
            base_intent = "web_search"
        elif wants_chart and not _contains_any(text, ["so sanh", "compare", "xu huong", "trend"]):
            base_intent = "visualization"
        elif route.intent == "general":
            base_intent = "general"
        else:
            base_intent = "text_to_sql"

        task_message = _augment_message(message, tickers, metric, time_window, grouping, comparison)
        tasks = [
            PlannedTask(
                intent=base_intent,
                title=_task_title(base_intent, metric, grouping, comparison),
                message=task_message,
                tickers=tickers,
                metric=metric,
                time_window=time_window,
                grouping=grouping,
                comparison=comparison,
                requires_chart=wants_chart,
            )
        ]

        if wants_chart and base_intent == "text_to_sql":
            tasks.append(
                PlannedTask(
                    intent="visualization",
                    title="Biểu đồ",
                    message=task_message,
                    tickers=tickers,
                    metric=metric,
                    time_window=time_window,
                    grouping=grouping,
                    comparison=comparison,
                    requires_chart=True,
                )
            )

        extra_tasks = self._extra_tasks(message, text, tickers)
        tasks.extend(extra_tasks)

        resolved_state = {
            "active_tickers": tickers,
            "last_metric": metric,
            "last_time_window": time_window,
            "last_grouping": grouping,
            "last_comparison": comparison,
        }
        return AgentPlan(
            tasks=tasks,
            resolved_state={key: value for key, value in resolved_state.items() if value not in (None, [], "")},
            context_used=context_used,
            reason="explicit fields override context; context only used for follow-up references",
        )

    def _resolve_tickers(self, text: str, explicit_tickers: list[str]) -> list[str]:
        previous = list(self.state.get("active_tickers") or self.state.get("tickers") or [])
        if explicit_tickers and _is_additive(text):
            return _merge(previous, explicit_tickers)
        if explicit_tickers:
            return explicit_tickers
        if _is_follow_up(text) or _is_implicit_same_ticker_reference(text):
            return previous
        return []

    def _extra_tasks(self, message: str, text: str, tickers: list[str]) -> list[PlannedTask]:
        tasks: list[PlannedTask] = []
        if _contains_any(text, [" va tin tuc", " va co tin", " and news", " kem tin tuc"]):
            news_message = _augment_message(f"Tin tức có thể ảnh hưởng đến {', '.join(tickers)}", tickers, "news", None, None, None)
            tasks.append(PlannedTask("news", "Tin tức liên quan", news_message, tickers=tickers, metric="news"))
        if _contains_any(text, [" va ve chart", " va ve bieu do", " and chart"]) and not _contains_any(text, ["chart", "bieu do"]):
            chart_message = _augment_message(message, tickers, _infer_metric(text, message, self.state), None, None, None)
            tasks.append(PlannedTask("visualization", "Biểu đồ", chart_message, tickers=tickers, requires_chart=True))
        return tasks


def _augment_message(
    message: str,
    tickers: list[str],
    metric: str | None,
    time_window: str | None,
    grouping: str | None,
    comparison: str | None,
) -> str:
    lines = [message]
    if tickers:
        lines.append(f"Context tickers: {', '.join(tickers)}")
    contract = [
        "Agent task contract:",
        "Use explicit task fields below. They override older conversation context.",
    ]
    if metric:
        contract.append(f"metric={metric}")
    if time_window:
        contract.append(f"time_window={time_window}")
    if grouping:
        contract.append(f"grouping={grouping}")
    if comparison:
        contract.append(f"comparison={comparison}")
    lines.append("\n".join(contract))
    return "\n\n".join(lines)


def _infer_metric(text: str, original: str, state: dict[str, Any]) -> str | None:
    if _contains_any(text, ["market cap", "von hoa"]):
        return "market_cap"
    if _contains_any(text, ["open", "gia mo cua"]) and _contains_any(text, ["high", "cao nhat"]) and _contains_any(
        text, ["low", "thap nhat"]
    ):
        return "ohlc"
    if "volume" in text:
        return "volume"
    if _contains_any(text, ["tin tuc", "news", "headline", "bai bao"]):
        return "news"
    if _contains_any(text, ["gia hien tai", "current price", "quote", "last price"]):
        return "current_price"
    if _contains_any(text, ["close", "closing price", "gia dong cua", "gia"]):
        if _contains_any(text, ["cao nhat", "highest", "maximum", "max"]):
            return "highest_close"
        if _contains_any(text, ["thap nhat", "lowest", "minimum", "min"]):
            return "lowest_close"
        return "close_price"
    if _is_follow_up(text):
        return state.get("last_metric") or state.get("metric")
    return None


def _infer_time_window(text: str, state: dict[str, Any]) -> str | None:
    for pattern, unit in [
        (r"\b(\d{1,4})\s*(ngay|days?|d)\b", "days"),
        (r"\b(\d{1,2})\s*(thang|months?|mo)\b", "months"),
        (r"\b(\d{1,2})\s*(nam|years?|yrs?|y)\b", "years"),
    ]:
        match = re.search(pattern, text)
        if match:
            return f"{int(match.group(1))} {unit}"
    if "quy gan nhat" in text or "latest quarter" in text:
        return "latest_quarter"
    if _is_follow_up(text):
        return state.get("last_time_window") or state.get("time_window")
    return None


def _infer_grouping(text: str, state: dict[str, Any]) -> str | None:
    if _contains_any(text, ["tung thang", "theo thang", "monthly", "per month"]):
        return "month"
    if _contains_any(text, ["theo quy", "tung quy", "quy gan nhat", "quy truoc", "quarter"]):
        return "quarter"
    if _is_follow_up(text):
        return state.get("last_grouping") or state.get("grouping")
    return None


def _infer_comparison(text: str) -> str | None:
    if _contains_any(text, ["quy gan nhat voi quy truoc", "quy gan nhat so voi quy truoc", "latest quarter"]):
        return "latest_quarter_vs_previous"
    if _contains_any(text, ["so sanh", "compare"]):
        return "comparison"
    return None


def _task_title(intent: str, metric: str | None, grouping: str | None, comparison: str | None) -> str:
    if comparison == "latest_quarter_vs_previous":
        return "So sánh quý gần nhất với quý trước"
    if intent == "web_search":
        return "Tra cứu web"
    if grouping:
        return f"Phân tích theo {grouping}"
    if metric:
        return metric.replace("_", " ").title()
    return "Phân tích"


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _is_follow_up(text: str) -> bool:
    return _contains_any(
        text,
        [
            "cai do",
            "cai nay",
            "ket qua do",
            "ket qua tren",
            "cac ma do",
            "cac ma tren",
            "cung khoang thoi gian",
            "cung giai doan",
            "cung ky",
            "quy truoc",
            "same period",
            "same window",
            "that result",
            "previous",
        ],
    )


def _is_implicit_same_ticker_reference(text: str) -> bool:
    has_metric = _contains_any(
        text,
        [
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
        ],
    )
    has_specific_request = _contains_any(
        text,
        [
            "lay ",
            "cho toi",
            "hien thi",
            "so sanh",
            "vao ngay",
            "phien",
            "quy ",
            "thang ",
            "nam ",
        ],
    ) or bool(re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text))
    asks_universe = _contains_any(
        text,
        [
            "ticker nao",
            "ma nao",
            "co phieu nao",
            "cong ty nao",
            "top ",
            "xep hang",
            "tat ca ma",
            "cac ma",
            "cac ticker",
        ],
    )
    return has_metric and has_specific_request and not asks_universe


def _is_additive(text: str) -> bool:
    return _contains_any(text, ["them", "them ma", "cung voi ma", "so sanh voi ma", "add ", "include "])


def _merge(left: list[str], right: list[str]) -> list[str]:
    merged: list[str] = []
    for ticker in [*left, *right]:
        if ticker not in merged:
            merged.append(ticker)
    return merged


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(without_accents.replace("đ", "d").replace("Đ", "D").split())
