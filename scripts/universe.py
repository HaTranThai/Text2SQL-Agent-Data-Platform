"""Reference universe for ingestion and scheduled refresh.

NASDAQ-100 constituents (as of 2026-05) plus a few legacy tickers already
seeded so the existing demo flows keep working.
"""

from __future__ import annotations

NASDAQ_100: list[str] = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AXON", "AZN", "BIIB", "BKNG",
    "BKR", "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD",
    "CSCO", "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC",
    "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR",
    "MCHP", "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MU", "NFLX",
    "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP",
    "PLTR", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS",
    "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

# Extra tickers that are not part of NASDAQ-100 but were seeded in earlier demos.
EXTRA_TICKERS: list[str] = []


def universe() -> list[str]:
    """Return de-duplicated ticker universe in canonical order."""
    seen: set[str] = set()
    result: list[str] = []
    for ticker in NASDAQ_100 + EXTRA_TICKERS:
        symbol = ticker.upper().strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


if __name__ == "__main__":
    tickers = universe()
    print(f"Universe size: {len(tickers)}")
    print(", ".join(tickers))
