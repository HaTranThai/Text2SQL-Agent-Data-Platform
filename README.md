# FinTextSQL

Trợ lý phân tích dữ liệu tài chính bằng ngôn ngữ tự nhiên. Người dùng có thể hỏi bằng tiếng Việt hoặc tiếng Anh, hệ thống sẽ tự hiểu ý định, sinh SQL an toàn, chạy truy vấn trên Postgres, phân tích kết quả, vẽ biểu đồ và trả lời như một chatbot tài chính.

FinTextSQL kết hợp chat UI, Text-to-SQL, dữ liệu chứng khoán từ yfinance, phân tích tin tức, biểu đồ trực quan và Cloudflare Tunnel trong một sản phẩm Dockerized.

## Giá Trị Sản Phẩm

FinTextSQL giúp người dùng phân tích dữ liệu cổ phiếu mà không cần tự viết SQL.

Ví dụ câu hỏi:

```text
So sánh close price của AAPL và MSFT trong 30 ngày gần nhất
Vẽ chart so sánh giá đóng cửa AAPL, MSFT, NVDA trong 60 ngày gần nhất
% tăng/giảm 30 ngày của AAPL và MSFT
Có tin gì mới về MSFT không?
Cổ phiếu nào có market cap cao nhất trong database?
Giá hiện tại của AAPL
```

Hệ thống có thể:

- Tự phân loại ý định: hỏi dữ liệu, vẽ biểu đồ, xem tin tức, sync dữ liệu, hoặc tra cứu giá nhanh.
- Sinh SQL từ câu hỏi tự nhiên.
- Kiểm tra SQL để chỉ cho phép truy vấn `SELECT` an toàn.
- Chạy SQL trên Postgres và giải thích kết quả bằng tiếng Việt.
- Vẽ biểu đồ ngay trong chat cho dữ liệu phù hợp.
- Lấy giá, fundamentals và tin tức từ yfinance/RSS.
- Ghi nhớ ngữ cảnh hội thoại ngắn hạn, ví dụ hỏi tiếp “so sánh với NVDA nữa”.
- Hiển thị luồng xử lý để người dùng biết hệ thống đang chạy bước nào.
- Chạy local hoặc public qua Cloudflare Tunnel.

## Giao Diện Sản Phẩm

Frontend là một workspace phân tích tài chính dạng chat, gồm:

- Khung chat hỏi đáp
- Thinking trace hiển thị luồng xử lý
- SQL details cho các câu trả lời Text-to-SQL
- Bảng kết quả truy vấn
- Biểu đồ line, area, bar bằng Recharts
- Nút sync dữ liệu theo ticker
- Danh sách ticker đã có trong database
- Link nguồn tin tức hiển thị cạnh từng tin

## Luồng Xử Lý

Tất cả câu hỏi đi qua một API chat duy nhất.

```text
Người dùng nhập câu hỏi
  -> Intent Router
  -> Chọn luồng xử lý phù hợp
  -> Trả về answer + rows + SQL + chart spec + debug trace
```

Các luồng chính:

| Luồng | Mục đích |
| --- | --- |
| `text_to_sql` | Sinh SQL, validate, chạy query, repair nếu lỗi, giải thích kết quả |
| `visualization` | Sinh dữ liệu và chọn loại biểu đồ phù hợp |
| `news` | Lấy, lọc, dịch và phân tích tin tức tài chính |
| `ingestion` | Nạp dữ liệu giá, fundamentals, news vào Postgres |
| `simple_finance` | Tra cứu nhanh giá hiện tại, market cap, high/low |

## Kiến Trúc

```text
React/Vite Frontend
  -> /api proxy
  -> FastAPI Backend
      -> Intent Router
      -> Text-to-SQL Service
      -> News Service
      -> Simple Finance Service
      -> YFinance Ingestion
      -> Visualization Inference
  -> PostgreSQL
  -> OpenAI-compatible LLM endpoint
  -> yfinance / RSS providers
```

Các module quan trọng:

| Module | Vai trò |
| --- | --- |
| `fintextsql.api.main` | Khai báo API, quản lý session context, điều phối request |
| `fintextsql.core.intent` | Phân loại intent từ câu hỏi |
| `fintextsql.core.tickers` | Trích xuất ticker từ câu hỏi |
| `fintextsql.text2sql.service` | Pipeline Text-to-SQL, deterministic SQL, giải thích kết quả |
| `fintextsql.text2sql.sql_guard` | Validate SQL read-only và whitelist bảng |
| `fintextsql.text2sql.schema` | Mô tả schema và chọn bảng liên quan |
| `fintextsql.ingestion.yfinance_service` | Nạp dữ liệu từ yfinance |
| `fintextsql.paths.news.service` | Lấy tin tức, dịch headline, phân tích và gắn link nguồn |
| `fintextsql.paths.visualization.service` | Suy luận loại chart, trục X/Y, series |
| `fintextsql.paths.simple_finance.service` | Tra cứu giá nhanh |

## Mô Hình Dữ Liệu

Postgres lưu các bảng chính:

| Bảng | Mô tả |
| --- | --- |
| `companies` | Thông tin ticker, tên công ty, sector, currency |
| `prices` | Dữ liệu OHLCV theo ngày |
| `fundamentals` | Market cap, P/E, beta và các chỉ số cơ bản |
| `news_articles` | Tin tức đã chuẩn hóa, headline và URL nguồn |
| `ingestion_runs` | Log các lần nạp dữ liệu |

## An Toàn SQL

Vì SQL có thể được sinh bởi LLM, hệ thống có các giới hạn an toàn:

- Chỉ cho phép một câu `SELECT`.
- Chặn `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `COPY`.
- Parse và validate SQL bằng `sqlglot`.
- Chỉ cho phép truy vấn các bảng finance đã whitelist.
- Tự thêm hoặc giới hạn `LIMIT`.
- Một số case phổ biến dùng deterministic SQL thay vì để LLM tự đoán, ví dụ:
  - % tăng/giảm trong N ngày
  - market cap
  - chuỗi giá đóng cửa để vẽ chart

## Công Nghệ

| Lớp | Công nghệ |
| --- | --- |
| Frontend | React, TypeScript, Vite, Recharts, lucide-react |
| Backend | FastAPI, Pydantic, SQLAlchemy |
| Database | PostgreSQL 16 |
| Data Provider | yfinance, Yahoo Finance RSS, Google News RSS |
| LLM | OpenAI-compatible Chat Completions API |
| Deploy | Docker Compose, Cloudflare Tunnel |

## Chạy Nhanh

### 1. Tạo file môi trường

```bash
cp .env.example .env
```

Cấu hình tối thiểu:

```env
LLM_API_KEY=replace-with-your-key
LLM_MODEL=local-model
```

Nếu LLM server chạy trên máy host, backend trong Docker dùng:

```env
DOCKER_LLM_BASE_URL=http://host.docker.internal:20128/v1
```

### 2. Chạy toàn bộ stack

```bash
docker compose up -d --build
```

Mở các địa chỉ:

- Frontend: http://localhost:15173
- Backend docs: http://localhost:18000/docs
- Postgres: localhost:55432

### 3. Nạp dữ liệu mẫu

Có thể sync trong UI hoặc gọi API:

```bash
curl -X POST http://localhost:18000/ingest \
  -H "Content-Type: application/json" \
  -d "{\"tickers\":[\"AAPL\",\"MSFT\",\"NVDA\"],\"period\":\"1y\",\"interval\":\"1d\",\"include_fundamentals\":true,\"include_news\":true}"
```

### 4. Hỏi thử

```text
Vẽ chart so sánh giá đóng cửa AAPL, MSFT, NVDA trong 60 ngày gần nhất
```

Kết quả kỳ vọng:

- Intent: `visualization`
- SQL lấy `date`, `ticker`, `close`
- Chart có một đường riêng cho từng mã
- Câu trả lời tóm tắt biến động giá bằng tiếng Việt

## Biến Môi Trường

| Biến | Mặc định | Mô tả |
| --- | --- | --- |
| `DATABASE_URL` | local Postgres URL | URL database khi chạy backend local |
| `POSTGRES_DB` | `fintextsql` | Tên database trong Docker |
| `POSTGRES_USER` | `fintextsql` | User Postgres |
| `POSTGRES_PASSWORD` | `fintextsql` | Password Postgres |
| `POSTGRES_PORT` | `55432` | Port Postgres trên máy host |
| `LLM_BASE_URL` | `http://localhost:20128/v1` | LLM endpoint khi chạy backend local |
| `DOCKER_LLM_BASE_URL` | `http://host.docker.internal:20128/v1` | LLM endpoint khi backend chạy trong Docker |
| `LLM_API_KEY` | rỗng | API key cho OpenAI-compatible endpoint |
| `LLM_MODEL` | `local-model` | Model gửi vào LLM endpoint |
| `LLM_TIMEOUT_SECONDS` | `60` | Timeout cho request LLM |
| `MAX_SQL_ROWS` | `2000` | Số dòng tối đa cho query |
| `BACKEND_PORT` | `18000` | Port backend |
| `FRONTEND_PORT` | `15173` | Port frontend |
| `VITE_API_URL` | `/api` | API base URL của frontend |
| `CORS_ORIGINS` | localhost + domain tunnel | Danh sách origin được phép |
| `CLOUDFLARE_TUNNEL_TOKEN` | rỗng | Token Cloudflare Tunnel |

Không commit `.env` thật hoặc token thật lên git.

## Cloudflare Tunnel

`docker-compose.yml` đã có service `cloudflared`.

Chạy full stack:

```bash
docker compose up -d
```

Xem log tunnel:

```bash
docker compose logs --tail=40 cloudflared
```

Frontend gọi API qua `/api`, sau đó Vite proxy sang backend trong Docker. Cách này tránh lỗi CORS khi mở app qua domain Cloudflare.

## API Chính

### Health

```bash
curl http://localhost:18000/health
```

### Danh sách công ty

```bash
curl http://localhost:18000/companies
```

### Nạp dữ liệu

```bash
curl -X POST http://localhost:18000/ingest \
  -H "Content-Type: application/json" \
  -d "{\"tickers\":[\"AAPL\",\"MSFT\",\"NVDA\"],\"period\":\"1y\",\"interval\":\"1d\",\"include_fundamentals\":true,\"include_news\":true}"
```

### Xem intent trước khi chạy chat

```bash
curl -X POST http://localhost:18000/chat/route \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"Có tin gì mới về MSFT không?\",\"session_id\":\"demo\"}"
```

### Chat

```bash
curl -X POST http://localhost:18000/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"% tăng/giảm 30 ngày của AAPL và MSFT\",\"session_id\":\"demo\"}"
```

### Chạy trực tiếp Text-to-SQL

```bash
curl -X POST http://localhost:18000/query/sql \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"Top companies by market cap\"}"
```

## Development

### Backend local

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
uvicorn fintextsql.api.main:app --reload --port 18000
```

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn fintextsql.api.main:app --reload --port 18000
```

### Frontend local

```bash
cd frontend
npm install
npm run dev
```

### Test

```bash
pytest
```

Build frontend:

```bash
cd frontend
npm run build
```

## Vận Hành

Một số lệnh thường dùng:

```bash
docker compose ps
docker compose logs --tail=80 backend
docker compose logs --tail=80 frontend
docker compose logs --tail=80 cloudflared
docker compose restart backend frontend
docker compose down
```

Rebuild sau khi sửa code hoặc config:

```bash
docker compose up -d --build
```

Reset database local:

```bash
docker compose down -v
docker compose up -d --build
```

Lệnh này sẽ xóa dữ liệu Postgres local.

## Troubleshooting

### Chat không trả lời khi mở qua Cloudflare

Kiểm tra proxy `/api`:

```bash
curl http://localhost:15173/api/health
```

Nếu lỗi, xem log:

```bash
docker compose logs --tail=80 frontend
docker compose logs --tail=80 backend
```

### Browser báo lỗi CORS

Kiểm tra:

- `VITE_API_URL=/api`
- `CORS_ORIGINS` có domain Cloudflare
- frontend/backend đã rebuild sau khi sửa `.env`

```bash
docker compose up -d --build frontend backend
```

### News không có kết quả

Thử sync dữ liệu hoặc hỏi lại để service fetch RSS:

```text
Có tin gì mới về NVDA không?
```

Nếu provider lỗi hoặc mạng chậm, xem log backend.

### LLM trả lời chậm hoặc không trả lời

Kiểm tra:

- `LLM_BASE_URL`
- `DOCKER_LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL`
- LLM server local có đang chạy không

```bash
docker compose logs --tail=120 backend
```

### SQL không có dòng nào

Nạp dữ liệu trước:

```bash
curl -X POST http://localhost:18000/ingest \
  -H "Content-Type: application/json" \
  -d "{\"tickers\":[\"AAPL\",\"MSFT\",\"NVDA\"],\"period\":\"1y\",\"interval\":\"1d\",\"include_fundamentals\":true,\"include_news\":true}"
```

## Ghi Chú Bảo Mật

- Không commit `.env` thật.
- Rotate Cloudflare Tunnel token nếu token bị lộ.
- Không mở Postgres public ra internet.
- SQL sinh bởi LLM phải luôn đi qua guard.
- Nếu dùng cho production thật, cần thêm authentication, rate limit, logging và monitoring.

## Trạng Thái Hiện Tại

FinTextSQL hiện là một product-grade MVP cho phân tích dữ liệu tài chính:

- Chạy được bằng Docker Compose
- Có chat UI
- Có Text-to-SQL an toàn
- Có ingestion dữ liệu tài chính
- Có phân tích tin tức kèm link nguồn
- Có chart trong chat
- Có thinking trace
- Có Cloudflare Tunnel

Các bước tiếp theo nếu đưa lên production:

- Authentication
- Persist session/user history
- Observability
- Rate limiting
- Role-based access control
- Deployment hardening
