from __future__ import annotations

import unicodedata
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
        text = _normalize_text(message)
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
            ],
        ):
            return RouteDecision("ingestion", "high", tickers, "Data ingestion keyword detected")

        if _contains(
            text,
            ["chart", "plot", "visualize", "graph", "ve bieu do", "ve chart", "bieu do", "do thi"],
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
                "market cap",
            ],
        ):
            return RouteDecision("simple_finance", "medium", tickers, "Simple finance lookup detected")

        if _is_general_chat(text, tickers=tickers):
            return RouteDecision("general", "high", tickers, "General assistant/help question detected")

        # Low-information input (no ticker and no analytical signal, e.g. "a", "asdf"):
        # do not fabricate a SQL query — ask the user to be more specific.
        if not tickers and not _contains(text, _ANALYTICAL_SIGNALS):
            return RouteDecision(
                "general",
                "low",
                tickers,
                "No ticker or analytical signal detected; needs a clearer question",
            )

        return RouteDecision("text_to_sql", "medium", tickers, "Default analytical path")


def _contains(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _is_general_chat(text: str, *, tickers: list[str]) -> bool:
    text = " ".join(text.split())
    if tickers or _contains(text, _ANALYTICAL_SIGNALS):
        return False
    general_phrases = [
        "ban co the lam duoc nhung gi",
        "ban lam duoc gi",
        "ban co the lam gi",
        "huong dan",
        "cach dung",
        "help",
        "what can you do",
        "how to use",
        "xin chao",
        "hello",
        "hi",
    ]
    if any(phrase in text for phrase in general_phrases):
        return True
    return text in {"?", "help me", "tro giup"}


_ANALYTICAL_SIGNALS = [
    "gia",
    "close",
    "closing price",
    "volume",
    "market cap",
    "pe",
    "p/e",
    "beta",
    "doanh thu",
    "loi nhuan",
    "gia dong cua",
    "cao nhat",
    "thap nhat",
    "so sanh",
    "top",
    "xep hang",
    "tang giam",
    "phan tram",
    "chart",
    "bieu do",
    "tin tuc",
    "tin moi",
    "co tin",
    "drawdown",
    "volatility",
    "bien dong",
    "correlation",
    "tuong quan",
    "return",
    "loi suat",
    "rui ro",
    "ohlc",
    "ma20",
    "ma50",
    "ma200",
    "von hoa",
    "phien",
    "co phieu",
    "ticker",
    "ma nao",
]


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(without_accents.replace("đ", "d").replace("Đ", "D").split())
