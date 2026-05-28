from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal


PolicyCategory = Literal[
    "unsafe_sql",
    "secret_request",
    "investment_advice",
    "certain_prediction",
    "ambiguous_screen",
    "unsupported_fundamental",
    "forecast_request",
]


@dataclass(slots=True)
class PolicyDecision:
    triggered: bool
    category: PolicyCategory | None = None
    answer: str = ""

    def to_debug(self) -> dict[str, object]:
        return asdict(self)


class PolicyGuard:
    def check(self, message: str) -> PolicyDecision:
        text = _normalize_text(message)

        if _contains_any(
            text,
            [
                "drop table",
                "delete from",
                "truncate",
                "alter table",
                "update ",
                "insert into",
                "create table",
                "xoa bang",
                "xoa toan bo",
                "cap nhat gia",
                "tao bang",
            ],
        ):
            return PolicyDecision(
                True,
                "unsafe_sql",
                "Mình không thể chạy hoặc hỗ trợ lệnh thay đổi/phá hoại database. Hệ thống này chỉ cho phép truy vấn tài chính dạng read-only.",
            )

        if _contains_any(text, ["mat khau", "password", "secret", "api key", "connection string", "database credential"]):
            return PolicyDecision(
                True,
                "secret_request",
                "Mình không thể hiển thị mật khẩu, secret, API key hoặc thông tin kết nối database. Bạn có thể hỏi dữ liệu tài chính read-only như giá, volume, fundamentals hoặc tin tức.",
            )

        if _contains_any(
            text,
            [
                "nen mua",
                "nen ban",
                "mua ma nao",
                "ma nao de mua",
                "ma nao nen mua",
                "ngon nhat de mua",
                "ma ngon",
                "chon ma nao de mua",
                "mua ma gi",
                "nen dau tu vao ma",
                "khuyen nghi mua",
                "buy recommendation",
                "should i buy",
                "what should i buy",
            ],
        ):
            return PolicyDecision(
                True,
                "investment_advice",
                "Mình không thể đưa khuyến nghị mua/bán trực tiếp hoặc chọn mã để mua hôm nay. Mình có thể giúp phân tích theo tiêu chí rõ ràng như return, volatility, drawdown, volume, P/E, beta hoặc fundamentals để bạn tự đánh giá.",
            )

        if _contains_any(text, ["chac chan tang", "chac chan giam", "du doan chac chan", "bao dam tang", "dam bao tang"]):
            return PolicyDecision(
                True,
                "certain_prediction",
                "Không thể khẳng định chắc chắn mã nào sẽ tăng/giảm trong tương lai. Mình có thể phân tích dữ liệu lịch sử, momentum, volatility, drawdown hoặc volume bất thường để đánh giá kịch bản một cách thận trọng.",
            )

        if _contains_any(text, ["du bao gia", "forecast", "price prediction", "du doan gia"]):
            return PolicyDecision(
                True,
                "forecast_request",
                "Mình không có mô hình dự báo giá tương lai nên không thể đưa ra một mức giá tháng sau. Mình có thể phân tích xu hướng lịch sử, MA/EMA, volatility và các kịch bản thận trọng nếu bạn muốn đánh giá rủi ro.",
            )

        if _contains_any(text, ["co phieu nao tot nhat", "co phieu tot nhat hien tai", "co phieu nao tot nhat hien tai"]):
            return PolicyDecision(
                True,
                "ambiguous_screen",
                "“Tốt nhất” còn mơ hồ. Bạn hãy chọn tiêu chí như return cao, volatility thấp, drawdown thấp, momentum mạnh, định giá P/E hấp dẫn hoặc fundamentals tốt; mình sẽ xếp hạng theo tiêu chí đó.",
            )

        return PolicyDecision(False)


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(without_accents.replace("đ", "d").replace("Đ", "D").split())
