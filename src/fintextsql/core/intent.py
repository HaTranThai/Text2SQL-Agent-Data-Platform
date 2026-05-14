from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fintextsql.api.schemas import IntentName
from fintextsql.core.tickers import extract_tickers

RouteConfidence = Literal["high", "medium", "low"]


@dataclass(slots=True)
class RouteDecision:
    intent: IntentName
    confidence: RouteConfidence
    tickers: list[str]
    reason: str


class IntentRouter:
    def route(self, message: str) -> RouteDecision:
        text = message.lower()
        tickers = extract_tickers(message)

        if _contains(
            text,
            [
                "ingest",
                "import",
                "sync",
                "load data",
                "cap nhat du lieu",
                "tai du lieu",
                "nap du lieu",
                "cập nhật dữ liệu",
                "tải dữ liệu",
                "nạp dữ liệu",
            ],
        ):
            return RouteDecision("ingestion", "high", tickers, "Data ingestion keyword detected")

        if _contains(
            text,
            ["chart", "plot", "visualize", "graph", "ve bieu do", "ve chart", "bieu do", "do thi", "vẽ", "biểu đồ", "đồ thị"],
        ):
            return RouteDecision("visualization", "high", tickers, "Visualization keyword detected")

        if _contains(
            text,
            [
                "news",
                "headline",
                "tin tuc",
                "tin moi",
                "tin gi",
                "co tin",
                "bai bao",
                "tin tức",
                "tin mới",
                "tin gì",
                "có tin",
                "bài báo",
            ],
        ):
            return RouteDecision("news", "high", tickers, "News keyword detected")

        if tickers and _contains(
            text,
            [
                "current price",
                "quote",
                "last price",
                "gia hien tai",
                "gia nhanh",
                "gia hom nay",
                "giá hiện tại",
                "giá nhanh",
                "giá hôm nay",
                "market cap",
            ],
        ):
            return RouteDecision("simple_finance", "medium", tickers, "Simple finance lookup detected")

        return RouteDecision("text_to_sql", "medium", tickers, "Default analytical path")


def _contains(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)
