"""LLM-first intent router.

Replaces the rule-based router as the default classifier. Sends the user message
to the LLM with a structured prompt asking it to classify into one of 5 intents
(general, text_to_sql, visualization, web_search, ingestion) and return JSON.

When the LLM call fails (down, timeout, unparseable JSON), falls back to
RuleBasedRouter so the system stays usable. The LLM also extracts tickers,
which is more robust than regex (handles company names, paraphrases, typos).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from fintextsql.api.schemas import IntentName
from fintextsql.core.config import Settings
from fintextsql.core.intent import RouteDecision, RuleBasedRouter
from fintextsql.core.tickers import extract_tickers
from fintextsql.llm.client import LLMClient, LLMError, LLMMessage


_ROUTER_TIMEOUT_SECONDS = 8
_VALID_INTENTS = {"general", "text_to_sql", "visualization", "web_search", "ingestion"}


_SYSTEM_PROMPT = "\n".join(
    [
        "Bạn là Intent Router của FinTextSQL — một trợ lý phân tích dữ liệu tài chính.",
        "Nhiệm vụ: phân loại câu hỏi của người dùng vào ĐÚNG MỘT trong 5 intent dưới đây.",
        "",
        "5 INTENT:",
        "",
        "1. text_to_sql — câu hỏi truy vấn dữ liệu thật trong database (giá, volume,",
        "   market cap, return, P/E của một mã cụ thể, so sánh giữa các mã, top/lowest,",
        "   theo năm/tháng/quý, drawdown, correlation, MA, volatility, v.v.).",
        "   Yêu cầu: phải có mã cổ phiếu cụ thể HOẶC tín hiệu phân tích cụ thể.",
        "",
        "2. visualization — câu hỏi yêu cầu vẽ chart (chart, biểu đồ, đồ thị,",
        "   plot, scatter, vẽ ...). Cũng cần dữ liệu thật từ DB.",
        "",
        "3. web_search — câu hỏi cần tra cứu thông tin BÊN NGOÀI database:",
        "   - Tin tức (news, headline, tin tức, có tin gì)",
        "   - Thông tin công ty: CEO, founder, lãnh đạo, trụ sở, website, ngành nghề,",
        "     giới thiệu công ty, công ty này làm gì",
        "   Path này dùng Tavily web search.",
        "",
        "4. ingestion — câu yêu cầu sync/cập nhật/ingest dữ liệu mới từ yfinance",
        "   (ingest AAPL, sync 3 mã, cập nhật dữ liệu, tải dữ liệu).",
        "",
        "5. general — mọi trường hợp khác:",
        "   - Câu hỏi kiến thức tài chính phổ thông KHÔNG cần dữ liệu cụ thể",
        "     (P/E là gì, drawdown nghĩa là gì, MA20 tính thế nào, Warren Buffett là ai,",
        "     thị trường gấu là gì, IPO là gì).",
        "   - Câu chào hỏi, hỏi 'bạn làm được gì'.",
        "   - Câu mơ hồ quá ngắn ('a', 'hello', 'help').",
        "",
        "QUY TẮC QUAN TRỌNG:",
        "- 'P/E của AAPL' → text_to_sql (cần data thật).",
        "- 'P/E là gì' → general (kiến thức phổ thông).",
        "- 'CEO của Apple' → web_search (thông tin ngoài DB).",
        "- 'giá AAPL hiện tại' → text_to_sql (data thật từ bảng prices).",
        "- 'tin tức Tesla' → web_search.",
        "- 'vẽ chart AAPL' → visualization.",
        "- Nếu vừa muốn data vừa muốn chart → ưu tiên visualization.",
        "",
        "Cũng trích xuất danh sách MÃ CỔ PHIẾU NIÊM YẾT (uppercase symbol như AAPL, MSFT,",
        "TSLA, NVDA, GOOG, META, AMZN, NFLX). Tự convert tên công ty sang ticker:",
        "Apple→AAPL, Microsoft→MSFT, Tesla→TSLA, Nvidia→NVDA, Google/Alphabet→GOOG,",
        "Meta/Facebook→META, Amazon→AMZN, Netflix→NFLX, Adobe→ADBE, AMD→AMD, Intel→INTC.",
        "Nếu user gõ sai chính tả nhẹ (apple, appl, applle) vẫn nhận ra → AAPL.",
        "",
        "OUTPUT: trả về DUY NHẤT một JSON object với 3 field:",
        "  {",
        '    "intent": "<một trong: general|text_to_sql|visualization|web_search|ingestion>",',
        '    "tickers": ["AAPL", "MSFT", ...],   // array các ticker liên quan, có thể rỗng',
        '    "reason": "<1 câu ngắn giải thích tại sao chọn intent này>"',
        "  }",
        "KHÔNG được bọc trong code fence, KHÔNG có text khác ngoài JSON.",
    ]
)


@dataclass(slots=True)
class LLMIntentRouter:
    settings: Settings
    llm: LLMClient
    fallback: RuleBasedRouter = RuleBasedRouter()

    async def route(self, message: str) -> RouteDecision:
        try:
            raw = await asyncio.wait_for(
                self.llm.chat(
                    [LLMMessage("system", _SYSTEM_PROMPT), LLMMessage("user", message)],
                    temperature=0,
                    max_tokens=200,
                ),
                timeout=_ROUTER_TIMEOUT_SECONDS,
            )
        except (LLMError, asyncio.TimeoutError, Exception):
            return self.fallback.route(message)

        parsed = _parse_router_json(raw)
        if not parsed:
            return self.fallback.route(message)

        intent = parsed.get("intent")
        if intent not in _VALID_INTENTS:
            return self.fallback.route(message)

        # Trust LLM tickers if non-empty, otherwise back-fill with regex extraction
        # so downstream features (context tickers, planner) still get something.
        llm_tickers = [str(t).upper().strip() for t in (parsed.get("tickers") or []) if str(t).strip()]
        regex_tickers = extract_tickers(message)
        tickers = llm_tickers or regex_tickers
        reason = str(parsed.get("reason") or "LLM-classified intent")[:200]

        return RouteDecision(intent, "high", tickers, f"LLM router: {reason}")


def _parse_router_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    # Strip code fences if the LLM disobeyed instructions.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Some LLMs add prose; pull the first {...} block.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


# Sync wrapper used by code that hasn't been made async yet (e.g. /chat/route preview).
def route_sync(router: LLMIntentRouter, message: str) -> RouteDecision:
    return asyncio.get_event_loop().run_until_complete(router.route(message))
