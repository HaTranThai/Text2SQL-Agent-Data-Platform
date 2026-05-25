from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

FINANCE_TABLES = {
    "companies": [
        "id integer primary key",
        "ticker text unique, uppercase stock symbol such as AAPL",
        "name text",
        "exchange text",
        "sector text",
        "industry text",
        "currency text",
        "country text",
    ],
    "prices": [
        "id integer primary key",
        "company_id integer references companies(id)",
        "date date",
        "open double precision",
        "high double precision",
        "low double precision",
        "close double precision",
        "adj_close double precision",
        "volume integer",
    ],
    "fundamentals": [
        "id integer primary key",
        "company_id integer references companies(id)",
        "as_of_date date",
        "market_cap double precision",
        "trailing_pe double precision",
        "forward_pe double precision",
        "price_to_book double precision",
        "dividend_yield double precision",
        "beta double precision",
        "fifty_two_week_high double precision",
        "fifty_two_week_low double precision",
        "revenue_growth double precision",
        "gross_margins double precision",
        "profit_margins double precision",
        "total_revenue double precision",
        "ebitda double precision",
        "debt_to_equity double precision",
        "free_cashflow double precision",
    ],
    "news_articles": [
        "id integer primary key",
        "ticker text uppercase stock symbol",
        "title text",
        "publisher text",
        "link text",
        "published_at timestamp with time zone",
        "summary text",
        "source text",
    ],
    "ingestion_runs": [
        "id integer primary key",
        "source text",
        "status text",
        "tickers jsonb array of symbols",
        "started_at timestamp with time zone",
        "finished_at timestamp with time zone",
        "message text",
        "rows_loaded integer",
    ],
}


SCHEMA_RELATIONSHIPS = [
    "prices.company_id -> companies.id (a company has many daily price rows)",
    "fundamentals.company_id -> companies.id (point-in-time valuation rows per company)",
    "news_articles.ticker -> companies.ticker (join by uppercase ticker text, NOT by id)",
]

SCHEMA_RULES = [
    "Only these 5 tables and their listed columns exist. Never reference any other table or column.",
    "To filter a stock, join companies c and compare c.ticker to uppercase literals, e.g. c.ticker = 'AAPL'.",
    "prices has one row per company per trading day (open, high, low, close, adj_close, volume).",
    "fundamentals is point-in-time; pick the latest as_of_date when a current metric is asked.",
    "date and as_of_date are real DATE columns; use date filters like p.date >= CURRENT_DATE - INTERVAL '30 days'.",
    "news_articles.ticker is plain text; join news to companies on c.ticker = news_articles.ticker.",
]


@dataclass(slots=True)
class SelectedSchema:
    tables: list[str]
    schema_text: str


@lru_cache(maxsize=1)
def full_schema_text() -> str:
    return "\n\n".join(_format_table(table, columns) for table, columns in FINANCE_TABLES.items())


@lru_cache(maxsize=1)
def generation_schema_text() -> str:
    """Full schema plus relationships and rules, used when generating or repairing SQL."""
    relationships = "\n".join(f"  - {item}" for item in SCHEMA_RELATIONSHIPS)
    rules = "\n".join(f"  - {item}" for item in SCHEMA_RULES)
    return "\n\n".join(
        [
            full_schema_text(),
            f"relationships:\n{relationships}",
            f"rules:\n{rules}",
        ]
    )


@lru_cache(maxsize=256)
def select_schema(question: str) -> SelectedSchema:
    question_l = question.lower()
    tables: set[str] = {"companies"}

    if any(word in question_l for word in ["price", "close", "volume", "return", "giá", "khối lượng"]):
        tables.add("prices")
    if any(
        word in question_l
        for word in ["pe", "p/e", "market cap", "fundamental", "doanh thu", "biên", "beta", "định giá"]
    ):
        tables.add("fundamentals")
    if any(word in question_l for word in ["news", "headline", "tin tức", "bài báo"]):
        tables.add("news_articles")
    if any(word in question_l for word in ["ingest", "load", "sync", "import", "cập nhật", "tải"]):
        tables.add("ingestion_runs")
    if len(tables) == 1:
        tables.update({"prices", "fundamentals"})

    ordered = [table for table in FINANCE_TABLES if table in tables]
    return SelectedSchema(
        tables=ordered,
        schema_text="\n\n".join(_format_table(table, FINANCE_TABLES[table]) for table in ordered),
    )


def _format_table(table: str, columns: list[str]) -> str:
    cols = "\n".join(f"  - {column}" for column in columns)
    return f"table {table}:\n{cols}"

