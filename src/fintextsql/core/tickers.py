from __future__ import annotations

import re
import unicodedata

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

ALIASES = {
    "aapl": "AAPL",
    "apple": "AAPL",
    "appl": "AAPL",
    "apply": "AAPL",
    "iphone": "AAPL",
    "msft": "MSFT",
    "microsoft": "MSFT",
    "nvda": "NVDA",
    "nvidia": "NVDA",
    "tsla": "TSLA",
    "tesla": "TSLA",
    "googl": "GOOGL",
    "goog": "GOOG",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "amzn": "AMZN",
    "amazon": "AMZN",
    "meta": "META",
    "facebook": "META",
    "amd": "AMD",
    "advanced micro devices": "AMD",
}


def extract_tickers(text: str) -> list[str]:
    seen: set[str] = set()
    tickers: list[str] = []
    for ticker in _ticker_candidates(text):
        if ticker in IGNORED_SYMBOLS or ticker in seen:
            continue
        seen.add(ticker)
        tickers.append(ticker)
    return tickers


def _ticker_candidates(text: str) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for match in re.finditer(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b", text):
        candidates.append((match.start(), match.group(0).upper()))

    normalized = _normalize_text(text)
    for alias, ticker in ALIASES.items():
        for match in re.finditer(rf"\b{re.escape(alias)}\b", normalized):
            candidates.append((match.start(), ticker))

    return [ticker for _, ticker in sorted(candidates, key=lambda item: item[0])]


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return without_accents.replace("đ", "d").replace("Đ", "D")
