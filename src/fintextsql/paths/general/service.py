"""General conversational path — answers free-form questions with an LLM
that has been given a FinTextSQL persona.

This path handles questions that don't require DB access (e.g.
"P/E là gì?", "Warren Buffett là ai?", "thị trường gấu là gì?", "kể tôi nghe
về Tesla", greetings, capability questions). For data-bound questions the
LLM is instructed to gently steer the user toward asking text_to_sql /
visualization / web_search style queries.

Falls back to a deterministic capability blurb if the LLM is unavailable.
"""

from __future__ import annotations

import asyncio

from fintextsql.core.config import Settings
from fintextsql.llm.client import LLMClient, LLMError, LLMMessage


_SYSTEM_PROMPT = "\n".join(
    [
        "Bạn là FinTextSQL — trợ lý phân tích dữ liệu tài chính bằng ngôn ngữ tự nhiên.",
        "Bạn nói chuyện thân thiện, ngắn gọn, tự nhiên bằng tiếng Việt (chỉ chuyển sang tiếng Anh khi user gõ tiếng Anh trước).",
        "",
        "Mục tiêu của bạn ở luồng general:",
        "- Trả lời các câu hỏi kiến thức tài chính phổ thông: P/E, EBITDA, MA20, drawdown, beta, ROE, ROA, thị trường gấu/bò, IPO, v.v.",
        "- Trả lời câu hỏi conversational (chào hỏi, hỏi 'bạn làm được gì', tự giới thiệu).",
        "- Giải thích khái niệm, lịch sử công ty/nhân vật nổi tiếng (Warren Buffett, Steve Jobs, ...) — chỉ ở mức kiến thức chung, không dữ liệu chính xác cập nhật.",
        "",
        "Ranh giới:",
        "- KHÔNG đưa khuyến nghị mua/bán cụ thể (không nói 'nên mua mã X').",
        "- KHÔNG bịa giá / volume / market cap / số liệu tài chính cụ thể của một mã. Khi user hỏi loại này, gợi ý: 'Bạn hỏi cụ thể như... mình sẽ truy vấn database thật'.",
        "- KHÔNG bịa tin tức / thông cáo. Khi user hỏi tin tức, gợi ý: 'Bạn hỏi `tin tức mới nhất của X` để mình search web thật'.",
        "- KHÔNG bịa thông tin CEO / lãnh đạo / website. Gợi ý: 'Bạn hỏi `CEO của X hiện tại là ai` để mình tra cứu web thật'.",
        "",
        "Khi user nhập rất mơ hồ (vd 'a', 'asdf', '?'):",
        "- Hỏi lại lịch sự, gợi ý 3-4 câu ví dụ ngắn mà hệ thống làm tốt.",
        "",
        "Phong cách output:",
        "- Trả lời 1 ý chính + 2-4 bullet bổ trợ nếu cần. Không lan man.",
        "- Dùng Markdown nhẹ (bold, bullet); không viết heading H1/H2 trừ khi cần phân mục dài.",
        "- Khi liệt kê công thức, dùng inline code `MA20 = AVG(close, 20)`.",
    ]
)


_CAPABILITY_FALLBACK = "\n".join(
    [
        "Chào bạn. Mình là **FinTextSQL** — trợ lý phân tích dữ liệu tài chính bằng câu hỏi tự nhiên.",
        "",
        "### Mình làm tốt các việc này",
        "- Truy vấn database giá đóng cửa, volume, market cap, P/E, beta, fundamentals của ~100 mã NASDAQ-100.",
        "- Vẽ chart (line / bar / scatter) cho 1 hoặc nhiều mã.",
        "- Tra cứu tin tức và thông tin công ty (CEO, founder, website) qua web search.",
        "- Trả lời câu hỏi kiến thức tài chính phổ thông (P/E là gì, drawdown là gì, MA20 tính thế nào...).",
        "- Nhớ ngữ cảnh hội thoại: 'còn TSLA thì sao', 'cùng khoảng thời gian đó'.",
        "",
        "### Ví dụ bạn có thể hỏi",
        "- `Giá đóng cửa cao nhất của AAPL trong năm 2024 là bao nhiêu?`",
        "- `So sánh AAPL, MSFT, NVDA trong 180 ngày gần nhất`",
        "- `Vẽ chart giá đóng cửa của AAPL và MSFT trong 60 ngày`",
        "- `Tin tức mới nhất về Tesla`",
        "- `CEO của Apple hiện tại là ai?`",
        "- `P/E ratio là gì? Cách tính như thế nào?`",
        "",
        "_(LLM nội bộ đang offline nên đây là câu trả lời mặc định. Khi LLM khôi phục bạn sẽ nhận được câu trả lời sinh động hơn.)_",
    ]
)


class GeneralService:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm

    async def answer(self, question: str) -> str:
        if not self.llm:
            return _CAPABILITY_FALLBACK
        try:
            text = await asyncio.wait_for(
                self.llm.chat(
                    [
                        LLMMessage("system", _SYSTEM_PROMPT),
                        LLMMessage("user", question),
                    ],
                    temperature=0.5,
                    max_tokens=800,
                ),
                timeout=self.settings.llm_timeout_seconds,
            )
            return (text or "").strip() or _CAPABILITY_FALLBACK
        except (LLMError, asyncio.TimeoutError, Exception):
            return _CAPABILITY_FALLBACK
