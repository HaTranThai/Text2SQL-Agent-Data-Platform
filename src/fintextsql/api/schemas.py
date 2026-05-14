from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


IntentName = Literal["text_to_sql", "visualization", "news", "ingestion", "simple_finance"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class VisualizationSpec(BaseModel):
    type: Literal["line", "bar", "area", "scatter"] = "line"
    x: str | None = None
    y: str | None = None
    series: str | None = None
    title: str | None = None


class ChatResponse(BaseModel):
    intent: IntentName
    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    visualization: VisualizationSpec | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    debug: dict[str, Any] = Field(default_factory=dict)


class RoutePreviewResponse(BaseModel):
    intent: IntentName
    tickers: list[str] = Field(default_factory=list)
    reason: str
    pipeline: list[str] = Field(default_factory=list)
    router: dict[str, Any] = Field(default_factory=dict)


class IngestionRequest(BaseModel):
    tickers: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"])
    period: str = "1y"
    interval: str = "1d"
    include_fundamentals: bool = True
    include_news: bool = True


class IngestionResponse(BaseModel):
    run_id: int
    status: str
    tickers: list[str]
    rows_loaded: int
    message: str | None = None


class CompanyResponse(BaseModel):
    ticker: str
    name: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
