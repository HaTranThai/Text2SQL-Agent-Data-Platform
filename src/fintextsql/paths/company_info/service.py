"""Answer fact-lookup questions (CEO, leadership, website, headquarters, founders…)
using Tavily web search as the live source, then LLM summarization in Vietnamese.

These facts don't live in the local Postgres schema, so the path is:
    user question  →  Tavily.search(query)  →  LLM summarizes snippets  →  answer + sources

Falls back gracefully if TAVILY_API_KEY is missing or the request fails.
"""

from __future__ import annotations

import asyncio
import unicodedata
from datetime import datetime, timezone
from typing import Any

import httpx

from fintextsql.core.config import Settings
from fintextsql.llm.client import LLMClient, LLMMessage

TAVILY_ENDPOINT = "https://api.tavily.com/search"

# Phrases that indicate a *current/now* question — these favor fresh news search.
_RECENCY_HINTS = [
    "hien tai", "hien nay", "current", "now", "moi nhat", "moi day",
    "vua qua", "trong nam", "thang nay", "today",
]


class CompanyInfoService:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm

    async def answer(
        self, question: str, tickers: list[str]
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self.settings.tavily_api_key:
            return (
                "Chưa cấu hình `TAVILY_API_KEY` trong `.env` nên không tra cứu web được. "
                "Đăng ký miễn phí tại https://app.tavily.com/home, dán key vào `.env` rồi restart backend.",
                [],
            )

        query = _build_query(question, tickers)
        is_recent = _is_recency_question(question)
        try:
            payload = await _tavily_search(
                api_key=self.settings.tavily_api_key,
                query=query,
                timeout=self.settings.tavily_timeout_seconds,
                topic="news" if is_recent else "general",
                time_range="month" if is_recent else None,
            )
        except Exception as exc:
            return (f"Không gọi được Tavily search: {exc}. Hãy kiểm tra API key hoặc kết nối mạng.", [])

        results = payload.get("results") or []
        tavily_answer = (payload.get("answer") or "").strip()

        if not results and not tavily_answer:
            return (f"Không tìm thấy kết quả cho: \"{query}\".", [])

        sources = [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": (r.get("content") or "").strip(),
                "score": r.get("score"),
            }
            for r in results[:6]
        ]

        # Prefer an LLM Vietnamese summary if the LLM is available; otherwise return
        # the Tavily-built answer + top snippets directly.
        if self.llm:
            try:
                summary = await self._summarize_with_llm(
                    question=question,
                    query=query,
                    tickers=tickers,
                    tavily_answer=tavily_answer,
                    sources=sources,
                )
                if summary:
                    return summary, sources
            except Exception:
                pass

        return _format_fallback(query, tavily_answer, sources), sources

    async def _summarize_with_llm(
        self,
        *,
        question: str,
        query: str,
        tickers: list[str],
        tavily_answer: str,
        sources: list[dict[str, Any]],
    ) -> str:
        snippets_block = "\n\n".join(
            f"[{i+1}] {s.get('title') or ''}\nURL: {s.get('url')}\n{s.get('snippet')}"
            for i, s in enumerate(sources)
        )
        today = datetime.now(timezone.utc).date().isoformat()
        system_prompt = "\n".join(
            [
                "Bạn là trợ lý tra cứu thông tin doanh nghiệp, trả lời bằng tiếng Việt tự nhiên.",
                f"Hôm nay là {today}. Khi snippet đề cập sự kiện trong tương lai (ví dụ 'sẽ trở thành CEO từ DD/MM/YYYY'),",
                "  PHẢI so sánh ngày đó với hôm nay để xác định ai là người đương nhiệm THỰC SỰ hiện tại,",
                "  và làm rõ cả chuyển giao đã công bố (nếu có).",
                "CHỈ dùng thông tin từ snippet được cung cấp; KHÔNG bịa.",
                "Nếu snippet mâu thuẫn, ưu tiên nguồn mới nhất (theo ngày) hoặc nguồn chính thống (trang chủ công ty, Reuters, Bloomberg, Apple newsroom…).",
                "Trả lời tiếng Việt theo cấu trúc:",
                "**Trả lời:** <1-2 câu súc tích, đáp đúng câu hỏi. Nếu có chuyển giao đã công bố nhưng CHƯA có hiệu lực tại ngày hôm nay, ghi rõ ai đang đương nhiệm + ai sẽ kế nhiệm và từ ngày nào.>",
                "**Chi tiết:** <2-4 gạch đầu dòng bổ sung: ngày công bố, ngày có hiệu lực, vai trò liên quan, v.v.>",
                "**Nguồn:** ghi `[1]`, `[2]` … khớp với snippet đã dùng.",
                "Nếu snippet không đủ thông tin để khẳng định, nói rõ 'không tìm thấy thông tin chắc chắn'.",
            ]
        )
        user_prompt = "\n\n".join(
            [
                f"Câu hỏi gốc của người dùng: {question}",
                f"Mã/công ty được nhắc tới: {', '.join(tickers) if tickers else '(không có)'}",
                f"Search query đã dùng: {query}",
                f"Tavily AI answer (tham khảo): {tavily_answer or '(trống)'}",
                "Snippet từ Tavily:",
                snippets_block or "(trống)",
            ]
        )
        text = await asyncio.wait_for(
            self.llm.chat(
                [LLMMessage("system", system_prompt), LLMMessage("user", user_prompt)],
                temperature=0.2,
            ),
            timeout=self.settings.llm_timeout_seconds,
        )
        return (text or "").strip()


def _build_query(question: str, tickers: list[str]) -> str:
    """Augment the question with company names + current year so search stays fresh."""
    today = datetime.now(timezone.utc).date()
    year_hint = str(today.year)
    if not tickers:
        return f"{question} {year_hint}"
    company_hint = " OR ".join(_TICKER_TO_NAME.get(t, t) for t in tickers)
    return f"{question} ({company_hint}) {year_hint}"


def _is_recency_question(question: str) -> bool:
    normalized = _normalize_text(question)
    return any(hint in normalized for hint in _RECENCY_HINTS)


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return " ".join(without.replace("đ", "d").split())


# Minimal hint map — Tavily handles unknown tickers fine, but full names help relevance.
_TICKER_TO_NAME = {
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft",
    "GOOG": "Alphabet Google",
    "GOOGL": "Alphabet Google",
    "AMZN": "Amazon",
    "META": "Meta Platforms Facebook",
    "TSLA": "Tesla",
    "NVDA": "NVIDIA",
    "NFLX": "Netflix",
    "ORCL": "Oracle",
    "AMD": "AMD",
    "INTC": "Intel",
    "AVGO": "Broadcom",
    "ADBE": "Adobe",
    "CRM": "Salesforce",
}


async def _tavily_search(
    *,
    api_key: str,
    query: str,
    timeout: float,
    topic: str = "general",
    time_range: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        # "advanced" uses Tavily's LLM to synthesize a higher-quality answer
        # from the top results — worth the extra cost for fact-lookup questions.
        "search_depth": "advanced",
        "include_answer": "advanced",
        "max_results": 8,
        "topic": topic,
    }
    if time_range:
        body["time_range"] = time_range
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(TAVILY_ENDPOINT, json=body)
        resp.raise_for_status()
        return resp.json()


def _format_fallback(query: str, tavily_answer: str, sources: list[dict[str, Any]]) -> str:
    """Fallback rendering when LLM is unavailable.

    Tavily's `include_answer` can hallucinate (especially for time-sensitive
    questions where "announced for September 2026" gets restated as past-tense
    "took over on September 2026"). Without LLM to cross-check against today's
    date, we show the AI answer as a *draft* and surface raw snippets so the
    user can verify with their own eyes.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    lines: list[str] = [f"_Hôm nay: {today}. LLM nội bộ đang offline — chưa tóm tắt lại được, hiển thị raw search results._", ""]
    if tavily_answer:
        lines.append(f"**Tavily AI tóm tắt (có thể cần verify):** {tavily_answer}")
        lines.append("")
    if sources:
        lines.append("**Trích đoạn từ các nguồn:**")
        for i, s in enumerate(sources, 1):
            title = s.get("title") or s.get("url")
            snippet = (s.get("snippet") or "").strip()
            if snippet:
                snippet = snippet.replace("\n", " ")
                if len(snippet) > 280:
                    snippet = snippet[:277].rsplit(" ", 1)[0] + "…"
            lines.append(f"- [{i}] [{title}]({s.get('url')})")
            if snippet:
                lines.append(f"  > {snippet}")
    if len(lines) <= 2:
        lines.append(f"Không có kết quả cho query: {query}")
    return "\n".join(lines)
