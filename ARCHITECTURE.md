# FinTextSQL — Architecture

Tài liệu này mô tả kiến trúc hiện tại của hệ thống. Bản cập nhật: 2026-05.

---

## 1. Tổng quan hệ thống

FinTextSQL là một trợ lý phân tích dữ liệu tài chính bằng ngôn ngữ tự nhiên (Việt/Anh). Người dùng đặt câu hỏi qua chat UI; hệ thống tự nhận diện ý định, chọn luồng xử lý phù hợp, sinh SQL an toàn hoặc gọi web search, trả về câu trả lời tiếng Việt kèm bảng kết quả và biểu đồ.

Toàn bộ stack đóng gói bằng Docker Compose gồm 5 service: `postgres`, `backend`, `scheduler`, `frontend`, `cloudflared`.

```
┌──────────────┐    HTTPS     ┌─────────────┐    /api    ┌──────────────────────────┐
│  Browser     │ ───────────► │  Cloudflare │ ─────────► │  Frontend (React/Vite)   │
│  (chat UI)   │              │  Tunnel     │            │  Nginx reverse-proxy     │
└──────────────┘              └─────────────┘            └─────────────┬────────────┘
                                                                       │ /api/*
                                                                       ▼
                                                          ┌──────────────────────────┐
                                                          │  Backend (FastAPI)       │
                                                          │  - Intent Router         │
                                                          │  - Task Planner          │
                                                          │  - 7 path services       │
                                                          └──┬───────┬───────┬───────┘
                                                             │       │       │
                                              ┌──────────────┘       │       └────────────────┐
                                              ▼                      ▼                        ▼
                                      ┌───────────────┐    ┌──────────────────┐    ┌──────────────────┐
                                      │  PostgreSQL   │    │  LLM endpoint    │    │  External APIs   │
                                      │  16-alpine    │    │  (OpenAI-compat) │    │  yfinance, RSS,  │
                                      │               │    │                  │    │  Tavily          │
                                      └───────▲───────┘    └──────────────────┘    └──────────────────┘
                                              │
                                      ┌───────┴───────┐
                                      │  Scheduler    │
                                      │  (worker-only,│
                                      │   no port)    │
                                      └───────────────┘
```

---

## 2. Backend — `src/fintextsql/`

### 2.1 Entry point — `api/main.py`

FastAPI exposes các endpoint sau:

| Endpoint | Method | Mô tả |
|---|---|---|
| `/health` | GET | Liveness probe |
| `/schema` | GET | Trả về schema text dùng cho prompt |
| `/companies` | GET | Danh sách ticker đã ingest |
| `/memory` | GET/DELETE | Quản lý Q→SQL memory (few-shot store) |
| `/ingest` | POST | Nạp dữ liệu yfinance |
| `/chat` | POST | **Endpoint chính** — pipeline đầy đủ |
| `/chat/route` | POST | Preview intent + pipeline (không chạy) |
| `/query/sql` | POST | Chạy thẳng Text-to-SQL, bỏ qua router |

Backend giữ session state **in-memory** qua các dict module-level:

```python
SESSION_TICKERS: dict[str, list[str]]      # ticker đã dùng gần nhất
SESSION_MESSAGES: dict[str, str]           # câu hỏi trước
SESSION_HISTORY: dict[str, list[dict]]     # 8 turn gần nhất
SESSION_STATE: dict[str, dict[str, Any]]   # metric, time_window, grouping
SESSION_RESULT: dict[str, dict[str, Any]]  # ticker/date của kết quả gần nhất
```

State này dùng cho follow-up resolution (vd "còn TSLA thì sao", "cùng khoảng thời gian đó", "2 năm này"). Hạn chế: restart backend = mất hết — đây là điểm cần Redis nếu scale.

### 2.2 Intent Router — `core/intent.py`

Heuristic-based, 7 intent. Thứ tự ưu tiên:

```
1. ingestion       — keyword: ingest, sync, "cập nhật dữ liệu"
2. visualization   — keyword: chart, plot, "biểu đồ", "đồ thị" (word-boundary match)
3. company_info    — ticker + keyword: ceo, founder, "trụ sở", "ai sáng lập"...
4. news            — keyword: news, "tin tức", "có tin gì"
5. simple_finance  — ticker + "current price", "giá hiện tại", "market cap"
6. general         — chào hỏi / help / câu không có tín hiệu phân tích
7. text_to_sql     — default fallback khi có ticker hoặc analytical signal
```

Hàm `_contains_word()` (regex `(?<!\w)needle(?!\w)`) tránh false positive như "cái đó thì..." (sau strip dấu → "cai do thi...") match nhầm `"do thi"` (đồ thị).

### 2.3 Task Planner — `core/planner.py`

Sinh `AgentPlan` gồm 1-N `PlannedTask`. Một câu hỏi có thể spawn nhiều task:

```
"So sánh AAPL và MSFT và vẽ chart và có tin gì không"
  → Task 1: text_to_sql (so sánh)
  → Task 2: visualization (chart)
  → Task 3: news (tin tức)
```

Planner tự suy luận `metric`, `time_window`, `grouping`, `comparison` từ câu hỏi; nếu là follow-up (`"cùng khoảng thời gian đó"`) thì reuse từ `SESSION_STATE`.

### 2.4 Policy Guard — `core/policy.py`

Chặn các câu hỏi out-of-scope (vd chính trị, y tế, code injection) trước khi route. Trả về `PolicyDecision` với câu trả lời mặc định và lý do.

### 2.5 Path Services — `paths/`

| Path | File | Vai trò |
|---|---|---|
| `text_to_sql` | `text2sql/service.py` | LangGraph pipeline (mục 3) |
| `visualization` | `paths/visualization/service.py` | Chạy text_to_sql + suy luận `VisualizationSpec` |
| `news` | `paths/news/service.py` | Yahoo + Google RSS, dịch headline, LLM phân tích |
| `simple_finance` | `paths/simple_finance/service.py` | `yfinance.fast_info` cho quote nhanh, fallback Postgres |
| `ingestion` | `ingestion/yfinance_service.py` | Upsert prices/fundamentals/news từ yfinance |
| `company_info` | `paths/company_info/service.py` | **Tavily web search** + LLM tóm tắt tiếng Việt |
| `general` | inline trong `api/main.py` | Greeting / help message |

---

## 3. Text-to-SQL Pipeline (LangGraph)

`text2sql/service.py` — module trung tâm, dài 3355 dòng. Cấu trúc `StateGraph`:

```
       START
         │
         ▼
   ┌───────────┐
   │ build_sql │ ──► deterministic match? ──Yes──┐
   └─────┬─────┘                                 │
         │ No                                    │
         ▼                                       │
   knowledge_extractor                           │
         │                                       │
         ▼                                       │
   schema_selector                               │
         │                                       │
         ▼                                       │
   planner (LLM, prompt ngắn)                    │
         │                                       │
         ▼                                       │
   candidate_generator (LLM, 3 SQL/lần)          │
         │                                       │
         ▼                                       │
   sql_guard (sqlglot validate)                  │
         │                                       │
         ▼                                       │
         ◄───────────────────────────────────────┘
         │
         ▼
   ┌───────────┐
   │  execute  │ ──► execute-based scoring (chọn candidate tốt nhất)
   └─────┬─────┘
         │
         ├─ error & attempt<1 ──► repair_error ──► execute
         ├─ empty & attempt<1 ──► repair_empty ──► execute
         ├─ error & attempt>=1 ─► fail (friendly message)
         └─ ok                ─► explain (LLM tiếng Việt) ──► END
```

### 3.1 Deterministic SQL Builders

[`_deterministic_sql()`](src/fintextsql/text2sql/service.py) chứa ~32 builder hard-code cho các pattern phổ biến. Khi câu hỏi match, hệ thống **bỏ qua hoàn toàn LLM**, dùng SQL có sẵn → nhanh hơn và chính xác hơn.

Các pattern đang support:
- OHLC theo ngày / khoảng ngày / ngày gần nhất
- AVG close theo quý / năm
- Volume so sánh ngày cụ thể vs max của quý
- Top volume / lowest volume theo năm
- Up sessions count, longest down streak
- MA20/MA50/MA200 (screen + series + compare)
- Year-vs-year return, year return, max drawdown
- Correlation (price + volume)
- Recovery after biggest drop
- Month price data, month close
- Outperform SPY + lower volatility ranking
- Growth + stability ranking (mới)
- All-tickers month builders (mới)
- Latest close / monthly high / high close
- Market cap, price change %, quarterly close, price series

### 3.2 Candidate Generation + Selection

Khi không match deterministic, LLM được yêu cầu sinh **3 candidate SQL khác nhau** trong **một lần gọi** (multi-block ```sql```). Sau đó `execute()` chạy lần lượt; chọn candidate:
1. Có rows trả về (ưu tiên), VÀ
2. SQL ngắn hơn (proxy cho gọn gàng).

Score function: `(2 if rows else 1, -len(sql))`.

### 3.3 SQL Guard — `text2sql/sql_guard.py`

Mọi SQL trước khi execute phải qua guard:
- Parse bằng `sqlglot` (dialect postgres) — không chỉ regex.
- Chỉ cho phép **một** statement.
- Reject `Alter`, `Create`, `Delete`, `Drop`, `Insert`, `Merge`, `Update`, `TruncateTable`.
- Whitelist 5 bảng: `companies`, `prices`, `fundamentals`, `news_articles`, `ingestion_runs` (CTE alias được cho phép).
- Auto-add `LIMIT` nếu thiếu (`MAX_SQL_ROWS=5000`).

### 3.4 Schema Selector — `text2sql/schema.py`

`select_schema(question)` chỉ đưa các bảng liên quan vào prompt (giảm token, tránh confuse LLM). Cache bằng `@lru_cache(maxsize=256)` — câu hỏi giống nhau không re-compute.

### 3.5 Knowledge Extractor — `text2sql/knowledge.py`

Trích từ câu hỏi: `tickers`, `time_window`, `glossary` (định nghĩa MA, return, drawdown, beta, correlation, P/E...). Block knowledge này nối vào prompt LLM để giảm hallucination về công thức tài chính.

### 3.6 Cross-session Memory (Few-shot)

`text2sql/few_shot.py`:
- Mỗi query thành công lưu vào bảng `qa_examples` (question, sql, embedding, use_count).
- Embedding: **feature-hash MD5 256-dim** (không cần pgvector / external model).
- Tokenize + bigram, hash bucket, L2-normalize → cosine in-Python.
- Threshold cosine ≥ 0.18 → top 3 retrieved, đưa vào prompt như few-shot.
- Soft cap 2000 row, prune theo `use_count` ASC + `created_at` ASC.

### 3.7 Explainer

Hơn 20 **deterministic explainer** chuyên cho từng pattern result (vd `_deterministic_year_return_explanation`, `_deterministic_drawdown_explanation`...). Nếu không match → fallback LLM với prompt yêu cầu trả lời tiếng Việt + numerically careful + không claim "query failed" khi có rows.

---

## 4. Database Schema (PostgreSQL 16)

ORM: SQLAlchemy 2.0 declarative. Init lúc startup qua `init_db()`.

| Bảng | Mục đích | Constraint quan trọng |
|---|---|---|
| `companies` | Ticker metadata | `ticker` unique + index |
| `prices` | OHLCV daily | `UQ(company_id, date)`, FK cascade |
| `fundamentals` | Market cap, P/E, beta, 52w high/low, EBITDA, debt/equity... | `UQ(company_id, as_of_date)` |
| `news_articles` | RSS đã chuẩn hóa | `link` unique |
| `qa_examples` | Cross-session few-shot memory | `question_key` unique, `embedding` JSONB |
| `ingestion_runs` | Audit log cho mỗi lần sync | — |

---

## 5. LLM Client — `llm/client.py`

OpenAI-compatible (Chat Completions API):

```
POST {LLM_BASE_URL}/chat/completions
Authorization: Bearer {LLM_API_KEY}
```

- Default: `http://localhost:20128/v1` (LLM server local).
- Trong Docker: `http://host.docker.internal:20128/v1`.
- Timeout: 60s (configurable qua `LLM_TIMEOUT_SECONDS`).
- Exception: `LLMError` → service fallback sang heuristic SQL.

---

## 6. External Integrations

### 6.1 yfinance

`ingestion/yfinance_service.py`:
- Upsert prices (OHLCV) theo period (default 1y).
- Fundamentals từ `Ticker.info` (market cap, P/E, beta...).
- News từ `Ticker.news` (Yahoo Finance RSS proxy).
- Mỗi lần ingest → ghi `ingestion_runs` row.

### 6.2 News RSS — `paths/news/service.py`

- Yahoo Finance RSS: `https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>`
- Google News RSS: `https://news.google.com/rss/search?q=<TICKER>`
- Dịch headline sang tiếng Việt qua LLM.
- LLM phân tích "có thể ảnh hưởng tới giá thế nào".

### 6.3 Tavily Web Search — `paths/company_info/service.py`

Dùng cho intent `company_info` (CEO, founder, headquarters, website, ngành nghề...).

```
POST https://api.tavily.com/search
{
  "api_key": TAVILY_API_KEY,
  "query": "<question> (<company name>) <year>",
  "search_depth": "advanced",
  "include_answer": "advanced",
  "max_results": 8,
  "topic": "news" if recency else "general",
  "time_range": "month" if recency else None
}
```

LLM tóm tắt snippet với prompt **time-aware**: so sánh ngày sự kiện trong snippet với hôm nay để xác định đúng người đương nhiệm (tránh hallucination "X đã thành CEO" cho sự kiện tương lai).

Fallback graceful khi `TAVILY_API_KEY` rỗng hoặc Tavily timeout.

---

## 7. Scheduler — `scripts/scheduler.py`

Long-lived worker service trong Docker Compose (không expose port).

```
Vòng lặp mỗi INTERVAL_SECONDS (default 600s):
  1. Check US market open?
     - Weekday & 13:00–22:30 UTC (DST union, errs on side of running)
     - SKIP_MARKET_HOURS=true để bypass
  2. Nếu mở → POST /ingest cho từng batch (default 20 ticker)
     - period=5d, interval=1d
     - fundamentals/news=false (rarely changes intraday)
  3. SIGTERM-aware: sleep theo bước 5s để exit nhanh
```

Universe = NASDAQ-100 (`scripts/universe.py`).

Bulk historical ingest riêng qua `scripts/ingest_universe.py`:
```bash
python -m scripts.ingest_universe --period 10y --batch-size 5
```

---

## 8. Frontend — `frontend/src/App.tsx`

Single-file React SPA (1452 dòng). Stack: Vite + TypeScript + Recharts + lucide-react + sql-formatter.

Tính năng chính:
- Chat stream với "thinking trace" (pipeline step animation delay 1700ms/step).
- SQL preview (format bằng `sql-formatter`).
- Bảng kết quả + chart Recharts (line/bar/area/scatter).
- Sidebar: list ticker đã có + nút ingest + modal xem Q→SQL memory.
- Dark/light mode.

Deploy: Vite build → static files → Nginx serve + `/api` proxy về backend.

---

## 9. Safety Boundaries

- **SQL**: chỉ `SELECT`, một statement, whitelist 5 bảng, auto-LIMIT.
- **Stack trace**: không leak ra response, chỉ log server-side.
- **Secrets**: `.env` excluded từ git; `.env.example` cho template.
- **Postgres**: không expose public port (chỉ `localhost:55432` qua Docker port mapping).
- **CORS**: whitelist origin qua `CORS_ORIGINS` env.
- **Rate limit / auth**: **chưa có** — production cần bổ sung (FastAPI middleware + JWT/API key).

---

## 10. Tech Stack Summary

| Layer | Technology |
|---|---|
| Frontend | React 18, TypeScript, Vite, Recharts, TailwindCSS-like inline styles |
| Backend | FastAPI 0.115+, Pydantic v2, SQLAlchemy 2.0, LangGraph 0.2+, sqlglot 25+ |
| Database | PostgreSQL 16-alpine |
| LLM | OpenAI-compatible endpoint (local llama.cpp / vLLM / hosted) |
| Data sources | yfinance 0.2.40+, feedparser 6+, Tavily API |
| Deploy | Docker Compose, Nginx, Cloudflare Tunnel |
| Python | 3.11+ |

---

## 11. Known Limitations

1. **In-memory session state** → không scale horizontal. Cần Redis hoặc DB-backed session store.
2. **No auth / rate limit** → endpoint công khai. Cần FastAPI middleware + JWT.
3. **`text2sql/service.py` 3355 dòng** → khó maintain. Nên tách thành `deterministic_sql/`, `explainers/`, `repair/`.
4. **Frontend monolith** (`App.tsx` 1452 dòng) → tách component theo concern.
5. **Scheduler DST handling** dùng UTC union khoảng → có thể refresh ngoài giờ market thật. Dùng `zoneinfo` (US/Eastern) sẽ chính xác hơn.
6. **`App.tsx` chưa handle intent `company_info`** trong TypeScript type union — type mismatch BE/FE.
