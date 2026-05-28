"""Unified web search path covering both news headlines and company facts
(CEO, founder, headquarters, website, business summary).

Pattern: user question → Tavily.search → LLM summarizes snippets in Vietnamese.
For news-style questions the search uses `topic="news"` with a fresh time range;
for company-fact questions it uses `topic="general"`.

Falls back to raw Tavily AI answer + snippet list when the LLM is unavailable,
with a banner warning the user that summary may need verification.
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

# Keywords that should bias the search toward news (fresh results, time-bound).
_NEWS_HINTS = [
    "news", "headline", "tin tuc", "tin moi", "tin gi",
    "co tin", "bai bao", "thong cao",
]

# Recency cues — even on general questions these favor a fresh news search.
_RECENCY_HINTS = [
    "hien tai", "hien nay", "current", "now", "moi nhat", "moi day",
    "vua qua", "trong nam", "thang nay", "today", "hom nay",
]


class WebSearchService:
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

        flavour = _detect_flavour(question)  # "news" | "fact"
        query = _build_query(question, tickers, flavour)
        use_news_topic = flavour == "news" or _is_recency_question(question)
        try:
            payload = await _tavily_search(
                api_key=self.settings.tavily_api_key,
                query=query,
                timeout=self.settings.tavily_timeout_seconds,
                topic="news" if use_news_topic else "general",
                time_range="month" if use_news_topic else None,
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
                "published_at": r.get("published_date"),
            }
            for r in results[:6]
        ]

        if self.llm:
            try:
                summary = await self._summarize_with_llm(
                    question=question,
                    query=query,
                    tickers=tickers,
                    flavour=flavour,
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
        flavour: str,
        tavily_answer: str,
        sources: list[dict[str, Any]],
    ) -> str:
        snippets_block = "\n\n".join(
            f"[{i+1}] {s.get('title') or ''}\nURL: {s.get('url')}\n{s.get('snippet')}"
            for i, s in enumerate(sources)
        )
        today = datetime.now(timezone.utc).date().isoformat()

        if flavour == "news":
            system_prompt = "\n".join(
                [
                    "Bạn là chuyên gia phân tích tin tức tài chính, trả lời bằng tiếng Việt tự nhiên.",
                    f"Hôm nay là {today}.",
                    "CHỈ dùng nội dung từ snippet được cung cấp; KHÔNG bịa giá, rating hay sự kiện không có trong bài.",
                    "Với mỗi tin nổi bật, tự đánh giá sentiment: 📈 tích cực / 📉 tiêu cực / ➖ trung tính.",
                    "Dịch tiêu đề sang tiếng Việt.",
                    "Trả lời ĐÚNG theo cấu trúc Markdown sau:",
                    "**Tổng quan:** 1-2 câu kèm sentiment tổng thể.",
                    "### Tin nổi bật",
                    "- <icon> **<tựa tiếng Việt>** [<publisher>](<url>) — <takeaway cụ thể> (<sentiment>)",
                    "### Tác động tiềm năng",
                    "- 2-3 bullet ngắn về ảnh hưởng tới mã/ngành.",
                    "### Lưu ý",
                    "- Đây là phân tích từ tiêu đề/snippet RSS, không phải khuyến nghị mua bán.",
                ]
            )
        else:  # fact lookup
            system_prompt = "\n".join(
                [
                    "Bạn là trợ lý tra cứu thông tin doanh nghiệp, trả lời bằng tiếng Việt tự nhiên.",
                    f"Hôm nay là {today}. Khi snippet đề cập sự kiện trong tương lai",
                    "  (ví dụ 'sẽ trở thành CEO từ DD/MM/YYYY'), PHẢI so sánh ngày đó với hôm nay",
                    "  để xác định ai là người đương nhiệm THỰC SỰ hiện tại, và làm rõ cả chuyển giao",
                    "  đã công bố (nếu có).",
                    "CHỈ dùng thông tin từ snippet; KHÔNG bịa.",
                    "Khi snippet mâu thuẫn, ưu tiên nguồn mới nhất hoặc nguồn chính thống",
                    "  (trang chủ công ty, Reuters, Bloomberg, Apple newsroom...).",
                    "Trả lời tiếng Việt theo cấu trúc:",
                    "**Trả lời:** <1-2 câu súc tích đáp đúng câu hỏi.>",
                    "**Chi tiết:** <2-4 gạch đầu dòng bổ sung.>",
                    "**Nguồn:** `[1]`, `[2]` … khớp với snippet đã dùng.",
                    "Nếu snippet không đủ thông tin, ghi rõ 'không tìm thấy thông tin chắc chắn'.",
                ]
            )

        user_prompt = "\n\n".join(
            [
                f"Câu hỏi gốc của người dùng: {question}",
                f"Mã/công ty được nhắc tới: {', '.join(tickers) if tickers else '(không có)'}",
                f"Loại câu hỏi: {'tin tức' if flavour == 'news' else 'tra cứu fact'}",
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


# -------------------- helpers --------------------

def _detect_flavour(question: str) -> str:
    """Return "news" if question is about news/headlines, else "fact"."""
    normalized = _normalize_text(question)
    if any(hint in normalized for hint in _NEWS_HINTS):
        return "news"
    return "fact"


def _build_query(question: str, tickers: list[str], flavour: str) -> str:
    """Augment the question with company names + current year so search stays fresh.

    For news flavour, prepend "Latest news:" so Tavily ranks recent articles higher.
    """
    today = datetime.now(timezone.utc).date()
    year_hint = str(today.year)
    company_hint = ""
    if tickers:
        names = " OR ".join(_TICKER_TO_NAME.get(t, t) for t in tickers)
        company_hint = f" ({names})"
    prefix = "Latest news: " if flavour == "news" else ""
    return f"{prefix}{question}{company_hint} {year_hint}".strip()


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
    """Fallback rendering when LLM is unavailable: show snippet list with a warning."""
    today = datetime.now(timezone.utc).date().isoformat()
    lines: list[str] = [
        f"_Hôm nay: {today}. LLM nội bộ đang offline — hiển thị raw search results, có thể cần verify._",
        "",
    ]
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
