from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fintextsql.db.models import Company, Fundamental, IngestionRun, Price
from fintextsql.paths.news.service import NewsService


class YFinanceIngestionService:
    def __init__(self, db: Session):
        self.db = db

    async def ingest(
        self,
        *,
        tickers: list[str],
        period: str,
        interval: str,
        include_fundamentals: bool,
        include_news: bool,
    ) -> IngestionRun:
        normalized = [_normalize_ticker(ticker) for ticker in tickers if ticker.strip()]
        run = IngestionRun(source="yfinance", status="running", tickers=normalized, rows_loaded=0)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        rows_loaded = 0
        messages: list[str] = []
        try:
            for ticker in normalized:
                loaded = await self._ingest_ticker(
                    ticker=ticker,
                    period=period,
                    interval=interval,
                    include_fundamentals=include_fundamentals,
                    include_news=include_news,
                )
                rows_loaded += loaded
            run.status = "success"
            run.message = f"Loaded {rows_loaded} rows for {', '.join(normalized)}."
        except Exception as exc:
            run.status = "failed"
            messages.append(str(exc))
            run.message = "; ".join(messages)
        finally:
            run.rows_loaded = rows_loaded
            run.finished_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(run)
        return run

    async def _ingest_ticker(
        self,
        *,
        ticker: str,
        period: str,
        interval: str,
        include_fundamentals: bool,
        include_news: bool,
    ) -> int:
        ticker_obj = yf.Ticker(ticker)
        info = _safe_info(ticker_obj)
        company = self._upsert_company(ticker, info)
        rows_loaded = self._upsert_prices(company.id, ticker_obj, period, interval)

        if include_fundamentals:
            self._upsert_fundamentals(company.id, info)
            rows_loaded += 1

        if include_news:
            try:
                articles = await NewsService(self.db).fetch_and_store(ticker=ticker, limit=10)
                rows_loaded += len(articles)
            except Exception:
                pass

        self.db.commit()
        return rows_loaded

    def _upsert_company(self, ticker: str, info: dict[str, Any]) -> Company:
        values = {
            "ticker": ticker,
            "name": _clean_text(info.get("longName") or info.get("shortName")),
            "exchange": _clean_text(info.get("exchange")),
            "sector": _clean_text(info.get("sector")),
            "industry": _clean_text(info.get("industry")),
            "currency": _clean_text(info.get("currency")),
            "country": _clean_text(info.get("country")),
        }
        stmt = insert(Company).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Company.ticker],
            set_={key: getattr(stmt.excluded, key) for key in values if key != "ticker"},
        )
        self.db.execute(stmt)
        self.db.commit()
        return self.db.execute(select(Company).where(Company.ticker == ticker)).scalar_one()

    def _upsert_prices(self, company_id: int, ticker_obj: yf.Ticker, period: str, interval: str) -> int:
        history = ticker_obj.history(period=period, interval=interval, auto_adjust=False)
        if history.empty:
            return 0

        loaded = 0
        for idx, row in history.iterrows():
            price_date = pd.Timestamp(idx).date()
            values = {
                "company_id": company_id,
                "date": price_date,
                "open": _clean_float(row.get("Open")),
                "high": _clean_float(row.get("High")),
                "low": _clean_float(row.get("Low")),
                "close": _clean_float(row.get("Close")),
                "adj_close": _clean_float(row.get("Adj Close")),
                "volume": _clean_int(row.get("Volume")),
            }
            stmt = insert(Price).values(**values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_prices_company_date",
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "adj_close": stmt.excluded.adj_close,
                    "volume": stmt.excluded.volume,
                },
            )
            self.db.execute(stmt)
            loaded += 1
        return loaded

    def _upsert_fundamentals(self, company_id: int, info: dict[str, Any]) -> None:
        values = {
            "company_id": company_id,
            "as_of_date": datetime.now(timezone.utc).date(),
            "market_cap": _clean_float(info.get("marketCap")),
            "trailing_pe": _clean_float(info.get("trailingPE")),
            "forward_pe": _clean_float(info.get("forwardPE")),
            "price_to_book": _clean_float(info.get("priceToBook")),
            "dividend_yield": _clean_float(info.get("dividendYield")),
            "beta": _clean_float(info.get("beta")),
            "fifty_two_week_high": _clean_float(info.get("fiftyTwoWeekHigh")),
            "fifty_two_week_low": _clean_float(info.get("fiftyTwoWeekLow")),
            "revenue_growth": _clean_float(info.get("revenueGrowth")),
            "gross_margins": _clean_float(info.get("grossMargins")),
            "profit_margins": _clean_float(info.get("profitMargins")),
            "total_revenue": _clean_float(info.get("totalRevenue")),
            "ebitda": _clean_float(info.get("ebitda")),
            "debt_to_equity": _clean_float(info.get("debtToEquity")),
            "free_cashflow": _clean_float(info.get("freeCashflow")),
            "raw": _clean_raw_info(info),
        }
        stmt = insert(Fundamental).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fund_company_as_of",
            set_={key: getattr(stmt.excluded, key) for key in values if key not in {"company_id", "as_of_date"}},
        )
        self.db.execute(stmt)


def _safe_info(ticker_obj: yf.Ticker) -> dict[str, Any]:
    try:
        return dict(ticker_obj.info or {})
    except Exception:
        return {}


def _normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def _clean_text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _clean_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_raw_info(info: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "longName",
        "shortName",
        "quoteType",
        "website",
        "longBusinessSummary",
        "marketCap",
        "trailingPE",
        "forwardPE",
        "priceToBook",
        "dividendYield",
        "beta",
        "totalRevenue",
        "ebitda",
        "freeCashflow",
    }
    cleaned: dict[str, Any] = {}
    for key in allowed:
        value = info.get(key)
        if isinstance(value, float) and pd.isna(value):
            value = None
        if isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value
    return cleaned
