from __future__ import annotations

import re

IGNORED_SYMBOLS = {
    "API",
    "BY",
    "C",
    "CAP",
    "CEO",
    "CFO",
    "CHO",
    "CLOSE",
    "CSV",
    "DB",
    "EPS",
    "ETF",
    "HTTP",
    "JSON",
    "LLM",
    "NASDAQ",
    "NYSE",
    "PE",
    "P",
    "PRICE",
    "SANH",
    "SO",
    "SQL",
    "THEO",
    "TOP",
    "TRONG",
    "USD",
    "V",
    "VE",
    "VOI",
}


def extract_tickers(text: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b", text)
    seen: set[str] = set()
    tickers: list[str] = []
    for candidate in candidates:
        candidate = candidate.upper()
        if candidate in IGNORED_SYMBOLS or candidate in seen:
            continue
        seen.add(candidate)
        tickers.append(candidate)
    return tickers
