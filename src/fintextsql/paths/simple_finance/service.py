from __future__ import annotations

from typing import Any

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from fintextsql.db.models import Company, Fundamental, Price


class SimpleFinanceService:
    def __init__(self, db: Session):
        self.db = db

    async def answer(self, question: str, tickers: list[str]) -> tuple[str, list[dict[str, Any]]]:
        if not tickers:
            return "Bạn muốn xem nhanh mã nào? Ví dụ: AAPL, MSFT, NVDA.", []

        rows: list[dict[str, Any]] = []
        for ticker in tickers[:5]:
            rows.append(self._live_snapshot(ticker) or self._db_snapshot(ticker) or {"ticker": ticker})

        parts = []
        for row in rows:
            price = row.get("last_price") or row.get("close")
            currency = row.get("currency") or ""
            if price is not None:
                parts.append(f"{row['ticker']}: {price:,.2f} {currency}".strip())
            else:
                parts.append(f"{row['ticker']}: chưa có giá")
        return "Giá nhanh: " + "; ".join(parts), rows

    def _live_snapshot(self, ticker: str) -> dict[str, Any] | None:
        try:
            ticker_obj = yf.Ticker(ticker)
            fast = ticker_obj.fast_info
            return {
                "ticker": ticker,
                "last_price": _json_safe(_read_fast_info(fast, "last_price")),
                "previous_close": _json_safe(_read_fast_info(fast, "previous_close")),
                "day_high": _json_safe(_read_fast_info(fast, "day_high")),
                "day_low": _json_safe(_read_fast_info(fast, "day_low")),
                "market_cap": _json_safe(_read_fast_info(fast, "market_cap")),
                "currency": _json_safe(_read_fast_info(fast, "currency")),
                "source": "yfinance_fast_info",
            }
        except Exception:
            return None

    def _db_snapshot(self, ticker: str) -> dict[str, Any] | None:
        stmt = (
            select(Company, Price, Fundamental)
            .join(Price, Price.company_id == Company.id)
            .outerjoin(Fundamental, Fundamental.company_id == Company.id)
            .where(Company.ticker == ticker)
            .order_by(Price.date.desc(), Fundamental.as_of_date.desc().nulls_last())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if not row:
            return None
        company, price, fundamental = row
        return {
            "ticker": company.ticker,
            "name": company.name,
            "close": price.close,
            "date": price.date.isoformat(),
            "volume": price.volume,
            "market_cap": fundamental.market_cap if fundamental else None,
            "trailing_pe": fundamental.trailing_pe if fundamental else None,
            "currency": company.currency,
            "source": "postgres",
        }


def _read_fast_info(fast: Any, key: str) -> Any:
    try:
        value = fast[key]
    except Exception:
        value = getattr(fast, key, None)
    return value


def _json_safe(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value
