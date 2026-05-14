# Architecture

## Backend

FastAPI exposes five paths behind one chat API:

- `text_to_sql`: schema selection, planning, SQL generation, execution, repair, explanation.
- `visualization`: runs text-to-SQL, then returns a chart spec for the frontend.
- `news`: fetches Yahoo Finance RSS headlines and stores them in Postgres.
- `ingestion`: loads price history and fundamentals from `yfinance`.
- `simple_finance`: reads quick quotes from `yfinance.fast_info`, falling back to Postgres.

Core modules:

- `fintextsql.core.intent`: heuristic intent router.
- `fintextsql.text2sql.schema`: finance schema text and table selector.
- `fintextsql.text2sql.sql_guard`: read-only PostgreSQL validation using `sqlglot`.
- `fintextsql.text2sql.service`: full text-to-SQL pipeline.
- `fintextsql.ingestion.yfinance_service`: upsert-based ingestion.
- `fintextsql.paths.*`: route-specific services.

## Database

Postgres tables:

- `companies`: ticker metadata.
- `prices`: OHLCV time series keyed by company/date.
- `fundamentals`: point-in-time valuation and financial metrics.
- `news_articles`: normalized finance headlines.
- `ingestion_runs`: audit trail for data syncs.

## LLM

The client calls an OpenAI-compatible endpoint:

```text
POST {LLM_BASE_URL}/chat/completions
Authorization: Bearer {LLM_API_KEY}
```

The app expects the endpoint at `http://localhost:20128/v1` for local backend runs. In Docker on Linux, Compose maps the host service through `host.docker.internal`.

## Frontend

React/Vite provides a single operational workspace:

- chat stream
- SQL preview
- yfinance ingestion controls
- loaded ticker universe
- table results
- Recharts visualization for chartable responses

## Safety Boundaries

- SQL execution is restricted to one `SELECT` statement.
- DDL/DML expressions are rejected.
- Only known finance tables are allowed.
- Generated SQL is capped with `LIMIT`.
- Secrets are read from environment variables and excluded from git by `.gitignore`.

