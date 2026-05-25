from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from fintextsql.core.tickers import extract_tickers

GLOSSARY: dict[str, tuple[list[str], str]] = {
    "moving_average": (
        ["ma20", "ma50", "ma200", "ma ", "moving average", "duong trung binh", "trung binh dong"],
        "MA(n) = AVG(close) over the last n trading days, e.g. AVG(close) OVER (ORDER BY date ROWS BETWEEN n-1 PRECEDING AND CURRENT ROW).",
    ),
    "return": (
        ["return", "loi suat", "loi nhuan", "tang phan tram", "% tang", "muc tang"],
        "return over a window = (end_close - start_close) / start_close, expressed as a percentage.",
    ),
    "daily_return": (
        ["loi suat ngay", "daily return", "lợi suất hằng ngày", "loi suat hang ngay"],
        "daily return = (close - prev_close) / prev_close using LAG(close) OVER (PARTITION BY ticker ORDER BY date).",
    ),
    "volatility": (
        ["volatility", "bien dong", "do lech chuan", "rui ro", "standard deviation"],
        "volatility = STDDEV_SAMP of daily returns; higher means riskier.",
    ),
    "drawdown": (
        ["drawdown", "giam tu dinh", "sut giam"],
        "drawdown = (close - running_peak) / running_peak where running_peak = MAX(close) up to current row; max drawdown is the most negative value.",
    ),
    "beta": (
        ["beta"],
        "beta = COVAR_POP(stock_return, benchmark_return) / VAR_POP(benchmark_return); benchmark is usually SPY or QQQ; compute from daily returns over the window.",
    ),
    "correlation": (
        ["correlation", "tuong quan"],
        "correlation = CORR(daily_return_a, daily_return_b) of two tickers joined by date.",
    ),
    "fifty_two_week": (
        ["52 tuan", "52 week", "52-week", "dinh 52", "52w"],
        "52-week high/low = MAX/MIN over roughly the last 252 trading days.",
    ),
    "up_session": (
        ["phien tang", "phien giam", "tang gia so voi", "cao hon mo cua", "dong cua cao hon"],
        "an up session = close > prev_close (or close > open); count with a CASE/SUM over the window.",
    ),
    "market_cap": (
        ["market cap", "von hoa"],
        "market_cap comes from the fundamentals table; pick the latest as_of_date per company.",
    ),
    "pe_ratio": (
        ["pe", "p/e", "dinh gia"],
        "P/E ratios (trailing_pe, forward_pe) live in the fundamentals table; pick the latest as_of_date.",
    ),
}

_TIME_PATTERNS = [
    (r"\b(\d{1,4})\s*(ngay|days?|d)\b", "days"),
    (r"\b(\d{1,2})\s*(thang|months?|mo)\b", "months"),
    (r"\b(\d{1,2})\s*(nam|years?|yrs?|y)\b", "years"),
]


@dataclass(slots=True)
class Knowledge:
    tickers: list[str] = field(default_factory=list)
    time_window: str | None = None
    glossary: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        lines: list[str] = []
        if self.tickers:
            lines.append(f"tickers: {', '.join(self.tickers)}")
        if self.time_window:
            lines.append(f"time_window: {self.time_window}")
        if self.glossary:
            lines.append("domain definitions:")
            lines.extend(f"  - {item}" for item in self.glossary)
        return "\n".join(lines)


def extract_knowledge(question: str) -> Knowledge:
    normalized = _normalize_text(question.lower())
    tickers = extract_tickers(question)

    time_window: str | None = None
    for pattern, unit in _TIME_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            time_window = f"{int(match.group(1))} {unit}"
            break

    glossary: list[str] = []
    seen: set[str] = set()
    for _name, (keywords, definition) in GLOSSARY.items():
        if definition in seen:
            continue
        if any(keyword in normalized for keyword in keywords):
            glossary.append(definition)
            seen.add(definition)

    return Knowledge(tickers=tickers, time_window=time_window, glossary=glossary)


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return " ".join(without_accents.replace("đ", "d").replace("Đ", "D").split())
