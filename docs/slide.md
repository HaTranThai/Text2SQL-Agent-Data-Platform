# FinTextSQL — Slide Báo Cáo Đồ Án

> Outline 22 slide cho buổi báo cáo ~20 phút (mỗi slide ~50 giây)
> Mỗi slide ghi rõ: **Title** + **Nội dung chính** + **Ảnh nhúng** (nếu có) + **Speaker notes**
> Path ảnh dùng relative: `picture/<filename>`

---

## Slide 1 — Bìa

### Title
**FinTextSQL — Hệ thống Hỏi đáp Dữ liệu Tài chính bằng Ngôn ngữ Tự nhiên**

### Nội dung
- Đồ án cuối kỳ — môn Data Platform
- Khoa Công nghệ Thông tin — Trường ĐH Công nghiệp TP.HCM
- Nhóm 4 thành viên
- GVHD: ThS. Nguyễn Hữu Vũ
- Tháng 5/2026

### Speaker notes
- Chào thầy cô, giới thiệu tên đề tài (5s)
- Giới thiệu nhanh các thành viên (10s)
- "Hôm nay nhóm sẽ trình bày một hệ thống cho phép hỏi đáp dữ liệu chứng khoán bằng tiếng Việt tự nhiên..."

---

## Slide 2 — Thành viên nhóm

### Title
**Thành viên nhóm và phân chia công việc**

### Nội dung — bảng

| STT | Họ tên | Vai trò |
|---|---|---|
| 1 | Trần Thái Hà | Team Lead + Backend (LangGraph pipeline, LLM router, SSE streaming) |
| 2 | Trịnh Dương Hoan | Frontend + UX (React chat UI, Recharts, theme) |
| 3 | Nguyễn Thị Mỹ Duyên | Data + Testing (130 câu test, benchmark, ingest NASDAQ-100) |
| 4 | Đoàn Vũ Thiên Ban | DevOps + Documentation (Docker Compose, Cloudflare, README, báo cáo) |

### Speaker notes
- Mỗi thành viên giới thiệu vai trò 5-10s
- Skip nhanh nếu thời gian hạn chế

---

## Slide 3 — Vấn đề

### Title
**4 rào cản truy cập dữ liệu tài chính**

### Nội dung
1. **Người không biết SQL** không tự truy vấn được, phải chờ dev hoặc dùng dashboard cố định
2. **Dashboard cố định** chỉ trả lời câu hỏi đã pre-define; câu hỏi mới = đợi build chart mới
3. **LLM tổng quát** (ChatGPT, Gemini) trả lời sai dữ liệu cụ thể vì không có kết nối DB real-time
4. **Câu hỏi tài chính đa dạng** — có dạng truy vấn DB, dạng tra fact (CEO, trụ sở), tin tức, biểu đồ — mỗi loại cần pipeline khác

### Speaker notes
- "Hãy tưởng tượng một sinh viên tài chính muốn so sánh AAPL và MSFT trong 30 ngày qua..."
- Nhấn mạnh: 4 rào cản này tồn tại đồng thời, không tool nào hiện tại giải quyết được trọn vẹn

---

## Slide 4 — Mục tiêu

### Title
**Mục tiêu đề tài**

### Nội dung
Xây dựng hệ thống chat tài chính có thể:
- **Hiểu câu hỏi tiếng Việt/Anh tự nhiên** (kể cả viết tắt, sai chính tả ticker)
- **Tự phân loại 5 intent** không bắt user chọn tab/menu
- **Sinh SQL an toàn** — chỉ SELECT, whitelist bảng, chống injection
- **Trả lời tiếng Việt** kèm bảng + biểu đồ tự động
- **Nhớ ngữ cảnh đa lượt** — "còn TSLA thì sao", "2 năm này"
- **Đóng gói chạy local** — không cần OpenAI API key, Docker Compose

### Speaker notes
- Đây là 6 mục tiêu cốt lõi
- Phân biệt với tool thông thường: mục tiêu 1, 2, 5 là khác biệt lớn

---

## Slide 5 — Giải pháp đề xuất

### Title
**Kiến trúc Multi-path Agent — 3 kỹ thuật cốt lõi**

### Nội dung
1. **LLM-first Intent Router** — gọi LLM phân loại câu hỏi vào 1 trong 5 luồng, fallback rule-based khi LLM down
2. **LangGraph Text-to-SQL Pipeline** — orchestrate sinh SQL → guard → execute → repair → explain theo state machine
3. **Tavily Web Search Integration** — tra cứu tin tức/fact ngoài database bằng web search engine chuyên cho AI

### Speaker notes
- "Đây là 3 kỹ thuật xương sống của hệ thống. Phần kiến trúc sau sẽ giải thích chi tiết cách 3 cái này hoạt động cùng nhau."

---

## Slide 6 — Kiến trúc tổng quan

### Title
**Kiến trúc hệ thống**

### Ảnh
![Kiến trúc tổng quan](picture/architecture.jpg)

### Nội dung
- **Frontend (React)** → HTTP/SSE → **Backend FastAPI**
- Backend xử lý qua **5 bước**: PolicyGuard → Follow-up rewrite → LLM Intent Router → TaskPlanner → Branch
- **5 path service**: text_to_sql / visualization / web_search / ingestion / general
- **PostgreSQL** chỉ kết nối với text_to_sql (đọc) + ingestion (ghi)
- **Scheduler** cron 10 phút trong giờ NASDAQ
- **External**: yfinance + Tavily Search API

### Speaker notes
- Đi từ trái sang phải, giải thích 30s
- Nhấn: "PostgreSQL không nối với ChatResponse — mỗi path tự assemble response"
- Nhấn: "Scheduler là sidecar — chỉ trigger ingestion, không chạy luồng user request"

---

## Slide 7 — 5 Intent Path

### Title
**5 luồng xử lý chuyên biệt**

### Nội dung — bảng

| Intent | Trigger | Ví dụ câu hỏi |
|---|---|---|
| **text_to_sql** | Câu hỏi cần truy vấn DB | "Giá AAPL năm 2024 cao nhất bao nhiêu?" |
| **visualization** | Câu hỏi cần vẽ chart | "Vẽ chart AAPL 60 ngày qua" |
| **web_search** | CEO, founder, news, tin tức | "CEO Apple hiện tại là ai?" |
| **ingestion** | Yêu cầu sync data | "Ingest dữ liệu mới cho NVDA" |
| **general** | Kiến thức tài chính phổ thông | "P/E là gì? Warren Buffett là ai?" |

### Speaker notes
- Mỗi path đọc 2-3s
- Nhấn: "Đây là khác biệt với tool one-shot — chúng tôi chia rõ data ở đâu (DB / yfinance / web / LLM kiến thức)"

---

## Slide 8 — Pipeline Text2SQL

### Title
**Pipeline Text2SQL điều phối bằng LangGraph**

### Ảnh
![Text2SQL Pipeline](picture/text2sql.jpg)

### Nội dung
- **Build SQL phase** (4 node rule-based): Load Schema → Schema Cache → Knowledge Extractor → Schema Selector
- **SQL Generator** (LLM call #1) → **SQL Guard** (sqlglot AST) → **Execute SQL** (PostgreSQL)
- **result_after_execute** (diamond) rẽ 4 nhánh:
  - success → **Explainer** (deterministic VN HOẶC LLM stream)
  - fail/empty → **Repair Agent** (LLM call #2, fan out 3 candidate, retry max 2)
  - LLM down → **Fail Handler**
- **Repair Agent là true agent duy nhất** trong hệ thống (có feedback loop)

### Speaker notes
- 1 phút cho slide này — đây là core kỹ thuật
- Nhấn: "Có 3 LLM call max — Router + Generator + Repair hoặc Explainer"
- Nhấn: "Repair Agent là 'agent' theo nghĩa chặt — có feedback loop từ SQL error → tự sửa → retry"

---

## Slide 9 — LLM Call Budget

### Title
**Chi phí LLM call theo scenario**

### Nội dung — bảng

| Scenario | Số LLM call | Chi tiết |
|---|---|---|
| Best case | **2 calls** | Intent Router + SQL Generator |
| Common case | **3 calls** | + Explainer stream |
| Worst case | **5 calls** | + Repair (×2) → SQL Generator x3 → Explainer |

### Lưu ý
- 4 node rule-based (Load/Cache/Knowledge/Selector) **KHÔNG gọi LLM** → chạy <10ms
- Explainer có deterministic VN formatter cho 12+ pattern → skip LLM khi match

### Speaker notes
- "Đây là tối ưu quan trọng: chúng tôi cố gắng tối thiểu LLM call vì latency cao"
- "Bằng cách deterministic 4 step đầu + có explainer fallback, best case chỉ 2 LLM call"

---

## Slide 10 — Demo: Trang chủ

### Title
**Demo 1 — Giao diện trang chủ**

### Ảnh
![Home Screen](picture/01_home.png)

### Nội dung
- **Sidebar trái**: 102 mã NASDAQ-100, nút "Bộ nhớ Q→SQL", panel sync data (1y/2y/5y/10y)
- **Chat area**: greeting + 10 gợi ý câu hỏi mẫu
- **Composer dưới**: ô "Hỏi bất cứ điều gì..."
- Theme sáng/tối, hide scrollbars

### Speaker notes
- "Đây là giao diện đầu tiên user thấy. Phong cách ChatGPT-like nhưng có sidebar chuyên cho tài chính."
- Click vào 1 mã trong sidebar → tự fill vào composer "Hỏi về AAPL..."

---

## Slide 11 — Demo: text_to_sql

### Title
**Demo 2 — Luồng text_to_sql với MA20/MA50**

### Ảnh
![Text2SQL Result](picture/02_text_to_sql.png)

### Câu hỏi mẫu
> "Tính MA20 và MA50 của AAPL trong 100 ngày gần nhất"

### Nội dung — Kết quả
- **Giá trị mới nhất** (2026-05-28): Close 312.51, MA20 295.51, MA50 274.04
- **Nhận xét kỹ thuật**: MA20 > MA50 → xu hướng ngắn hạn mạnh hơn trung hạn
- **Lưu ý**: 19 phiên đầu chưa đủ data tính MA20 chính xác
- Cuối có chart "Close by Date" + gợi ý 2 follow-up

### Speaker notes
- "Đây là câu hỏi đặc thù cho text_to_sql — cần tính moving average trên data thực, không thể trả lời bằng web search."
- "Hệ thống tự sinh SQL với CTE + window function, không cần user biết SQL"

---

## Slide 12 — Demo: web_search

### Title
**Demo 3 — Luồng web_search với tin tức NVDA**

### Ảnh
![Web Search Result](picture/02_web_search.png)

### Câu hỏi mẫu
> "Tin tức gần đây về NVDA"

### Nội dung — Kết quả
- **Tổng quan**: tình hình NVDA trong tuần qua
- **Tin nổi bật**: bullets với sentiment tags (📈 / 📉 / ➖)
  - VD: "NVDA hợp tác với MIRA nâng cấp..." (📈 tích cực)
  - VD: "Trí tuệ Mỹ Erik Prince muốn NVDA cấp phép..." (➖ trung tính)
- **Tác động tiềm năng** + cite nguồn URL

### Speaker notes
- "Đây là khả năng quan trọng — LLM thuần không thể trả lời tin tức cập nhật"
- "Tavily search + LLM tóm tắt → user thấy summary VN với sentiment + nguồn để verify"

---

## Slide 13 — Demo: general (LLM chat)

### Title
**Demo 4 — Luồng general — Giải thích kiến thức tài chính**

### Ảnh
![General Path Result](picture/02_general.png)

### Câu hỏi mẫu
> "P/E ratio là gì? Cách tính như thế nào?"

### Nội dung — Kết quả
- **Định nghĩa**: P/E là chỉ số định giá doanh nghiệp
- **Công thức**: `P/E = Giá cổ phiếu / EPS`
- **Ví dụ**: Giá 50.000đ, EPS 5.000đ → P/E = 10
- **Cách hiểu nhanh**: P/E cao = kỳ vọng tăng trưởng, P/E thấp = định giá rẻ tương đối
- **Lưu ý**: không nên dùng P/E đơn lẻ, phải so sánh với ngành/đối thủ/lịch sử

### Speaker notes
- "Đây là kiến thức phổ thông — không cần database, không cần web search"
- "LLM persona FinTextSQL được prompt KHÔNG khuyến nghị mua/bán → tránh rủi ro pháp lý"

---

## Slide 14 — Demo: Multi-turn (lượt 1)

### Title
**Demo 5a — Hội thoại đa lượt: Lượt 1**

### Ảnh
![Multi-turn Q1](picture/02_multi_turn_1.png)

### Câu hỏi
> "So sánh close price của AAPL và MSFT trong 30 ngày gần nhất"

### Nội dung — Kết quả
- **Diễn biến từng mã**: AAPL từ 270.17 → 312.51 (+15.67%), MSFT từ 424.46 → 426.99 (+0.60%)
- **Nhận xét chính**: AAPL tăng mạnh hơn rõ rệt, MSFT đi ngang nhưng giá tuyệt đối cao hơn
- **Kết luận**: chênh lệch giá cuối kỳ = 114.48 USD

### Speaker notes
- "Câu hỏi này đơn giản, nhưng quan trọng để setup context cho slide tiếp theo"
- "Hệ thống nhớ AAPL, MSFT, 30 ngày, close_price vào SESSION_STATE"

---

## Slide 15 — Demo: Multi-turn (lượt 2)

### Title
**Demo 5b — Follow-up tự động hiểu ngữ cảnh**

### Ảnh
![Multi-turn Q2](picture/02_multi_turn_2.png)

### Câu hỏi follow-up
> "Vậy nếu so với TSLA thì như thế nào"

### Nội dung — Hệ thống tự động:
1. Đọc SESSION_STATE: tickers=[AAPL, MSFT], window=30d, metric=close
2. Merge TSLA vào → tickers=[AAPL, MSFT, TSLA]
3. Rewrite: "So sánh close price của AAPL, MSFT, TSLA trong 30 ngày gần nhất"
4. Re-execute pipeline → vẽ chart 3 mã

### Kết quả
- TSLA tăng mạnh nhất (+18.59%), AAPL (+15.67%), MSFT (+0.60%)
- Cuối kỳ: TSLA 442.10 > MSFT 426.99 > AAPL 312.51
- **Chart "Close Price by Date"** vẽ 3 đường overlay

### Speaker notes
- "Đây là tính năng phân biệt FinTextSQL với tool one-shot"
- "User không phải lặp lại 30 ngày, close price, so sánh — hệ thống tự hiểu từ context"
- "Đặc biệt câu hỏi rất ngắn 'còn TSLA thì sao' — đa số tool không xử lý được"

---

## Slide 16 — Công nghệ sử dụng

### Title
**Tech Stack**

### Nội dung — Backend

| Component | Technology |
|---|---|
| Framework | FastAPI + Uvicorn (Python 3.11) |
| Pipeline orchestrator | LangGraph |
| SQL validator | sqlglot |
| ORM | SQLAlchemy + psycopg |
| LLM client | httpx (chat + chat_stream SSE) |
| Database | PostgreSQL 16 |
| LLM | OpenAI-compatible (cx/gpt-5.4) |
| Web search | Tavily Search API |
| Data source | yfinance |

### Frontend
React 18 + TypeScript + Vite + Recharts + sql-formatter

### DevOps
Docker Compose + Nginx + Cloudflare Tunnel

### Speaker notes
- Skim nhanh 30s
- Nhấn: "Stack hoàn toàn local trừ Tavily — phù hợp yêu cầu chủ quyền dữ liệu"

---

## Slide 17 — Database Schema

### Title
**Cơ sở dữ liệu PostgreSQL 16**

### Nội dung — 5 bảng

| Bảng | Cột chính | Mục đích |
|---|---|---|
| **companies** | id, ticker, name, sector, industry | Thông tin công ty cố định |
| **prices** | id, company_id, date, open, high, low, close, volume (BIGINT) | Giá lịch sử daily |
| **fundamentals** | id, company_id, as_of_date, market_cap, pe, beta | Chỉ số tài chính cơ bản |
| **qa_examples** | id, question, sql, embedding (JSONB 256-d), use_count | Cross-session memory |
| **ingestion_runs** | id, source, tickers, status, rows_loaded | Audit log |

### Số liệu thực tế
- **101 mã** NASDAQ-100 ingested
- **235.802 dòng giá** (2016-05 → 2026-05)
- **~85 MB** dung lượng DB

### Speaker notes
- "Volume column là BIGINT vì NVDA có ngày volume > 2.14 tỷ — int4 overflow"
- "qa_examples dùng JSONB lưu embedding 256-d thay vì pgvector → đơn giản hơn"

---

## Slide 18 — Cross-session Memory

### Title
**Cross-session Memory với Feature-hash Embedding**

### Nội dung
- Mỗi câu **Q → SQL thành công** lưu vào bảng `qa_examples` với:
  - `question` (text gốc)
  - `sql` (câu SQL đã chạy được)
  - `embedding` (feature-hash MD5 256-d, JSONB)
- Khi câu hỏi mới đến:
  1. Tính embedding feature-hash của câu mới
  2. Tính cosine similarity với toàn bộ memory
  3. Lấy **top-3** với threshold ≥ 0.18
  4. Inject làm few-shot vào prompt SQL Generator

### Tại sao feature-hash thay vì sentence-transformer?
- **Lightweight**: không cần model embedding nặng (sentence-transformer ~400MB)
- **Fast**: tính embedding < 1ms
- **Đủ dùng**: cho domain hẹp (tài chính) với vocabulary < 1000 từ

### Speaker notes
- "Đây là kỹ thuật tự học của hệ thống — càng dùng nhiều, accuracy càng tăng"
- "Trade-off: feature-hash không bắt được rephrase ngữ nghĩa tương đồng, nhưng đủ cho production demo"

---

## Slide 19 — SSE Streaming

### Title
**SSE Streaming — Trải nghiệm như ChatGPT**

### Nội dung
- Endpoint `/chat/stream` dùng **FastAPI StreamingResponse + Server-Sent Events**
- Gửi từng event về client:
  - `routing` (intent + tickers)
  - `sql` (câu SQL đã sinh)
  - `rows` (kết quả query)
  - `token` (từng chunk text answer)
  - `done` (kết thúc)
- Frontend consume bằng `fetch + ReadableStream`, append tokens vào message đang tăng dần

### Hiệu quả
| Metric | Non-streaming | Streaming |
|---|---|---|
| Time to first content | ~8s | **~3s** |
| Perceived latency | 100% | **30-40%** (giảm 60-70%) |
| Total latency | ~8s | ~8s (không đổi) |

### Speaker notes
- "User cảm thấy nhanh hơn dù tổng thời gian không đổi"
- "Đặc biệt với câu hỏi dài, SSE giúp UX khác biệt rất rõ rệt"

---

## Slide 20 — Kết quả Benchmark

### Title
**Đánh giá hiệu năng — Intent Router + Text2SQL End-to-End**

---

### 🟢 Benchmark 1 — Text2SQL End-to-End Accuracy (quan trọng nhất)

Cách đo: với mỗi câu hỏi, viết gold SQL bằng tay → chạy trên DB lấy gold result; chạy pipeline → so kết quả thực tế với gold (Execution Accuracy metric, chuẩn Spider/BIRD).

#### Test set
**60 câu** text_to_sql, chia theo độ khó:
- **Easy** (22 câu): filter cơ bản, MAX/MIN/AVG, đơn-giá, đơn-ngày, COUNT
- **Medium** (23 câu): top-N, GROUP BY, comparison, percentage change, multi-ticker
- **Hard** (15 câu): window functions (LAG/AVG OVER), drawdown, MA20/MA50, volatility (STDDEV), correlation, 52-week high/low, streak

#### Kết quả

| Chỉ số | Giá trị |
|---|---|
| **SQL Generation Rate** | 100% (60/60) — luôn sinh được SQL |
| **SQL Execution Rate** | 100% (60/60) — SQL luôn chạy được, không lỗi cú pháp |
| **🎯 Result Match Rate** | **80% (48/60)** — **metric chính (Execution Accuracy)** |

#### Per-difficulty breakdown

| Difficulty | Total | Matched | Accuracy |
|---|---|---|---|
| **easy** | 22 | 21 | **95.45%** |
| **medium** | 23 | 17 | **73.91%** |
| **hard** | 15 | 10 | **66.67%** |

#### Latency end-to-end (gọi `/chat`, gồm cả LLM time)
- Mean: 11.2 s | Median: 9.7 s | P95: 24.3 s

#### Phân tích 12 câu sai
- **1 easy**: NVDA max high 2024 (system trả ticker thay vì giá)
- **6 medium**: liệt kê cuối tháng (12 → vài rows), top-3 volume tickers (system filter sai), AVG theo quý (đánh nhãn quarter sai)
- **5 hard**: drawdown lớn nhất NVDA (output sai shape), ngày tăng/giảm % mạnh nhất (giá trị % sai 0.01), MA20 60 ngày (42 vs 60 rows), MA50 (rounding diff)
- **Đặc điểm chung**: SQL chạy được nhưng business logic khác gold ở chi tiết (LIMIT, time window, output shape, rounding)
- Hệ thống **không "sai SQL"** — vẫn chạy được — nhưng chưa khớp gold 100%

---

### 🟦 Benchmark 2 — Intent Router Accuracy

#### Test set
- **174 câu hỏi tiếng Việt** trải đều 5 intent
- Bao gồm: truy vấn cơ bản, aggregation, window function, beta/correlation, drawdown, ranking, multi-step reasoning, câu mơ hồ, SQL injection, news/CEO, ingestion

#### Kết quả tổng thể

| Chỉ số | Giá trị |
|---|---|
| **Routing accuracy** | **98.85%** (172/174) |
| **Macro F1** | **99.18%** |
| **Lỗi mạng** | 0 |

#### Per-intent metrics

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| **general** | 31 | 96.77% | 96.77% | **96.77%** |
| **ingestion** | 3 | 100.00% | 100.00% | **100%** ✓ |
| **text_to_sql** | 115 | 99.13% | 99.13% | **99.13%** |
| **visualization** | 8 | 100.00% | 100.00% | **100%** ✓ |
| **web_search** | 17 | 100.00% | 100.00% | **100%** ✓ |

#### Latency router (gọi `/chat/route`)
- Median (P50): 3.2 s | P95: 8.0 s | Min (rule-based catch): 3 ms

#### Điểm nổi bật
- **3/5 intent đạt F1 = 100%** (ingestion, visualization, web_search) — không nhầm câu nào
- **SQL Injection: 6/6 chặn đúng** (DROP/UPDATE/leak password đều route sang `general` để refuse)
- **Câu mơ hồ ("tốt nhất", "ngon nhất"): 100% route sang `general`** để hỏi lại — không generate SQL bừa
- Rule-based fast path catch một số case trong **< 5 ms** (giảm chi phí LLM)

---

### 📁 Reproducibility

| Loại | File |
|---|---|
| Gold SQL + question set Text2SQL | `benchmark/gold_sql.csv` |
| Kết quả Text2SQL chi tiết | `benchmark/text2sql_results.csv` + `.summary.json` |
| Script benchmark Text2SQL | `scripts/benchmark_text2sql.py` |
| Question set Intent Router | `test.csv` (174 câu) |
| Kết quả Intent Router | `benchmark/benchmark_results.csv` + `.summary.json` |
| Script benchmark Router | `scripts/benchmark_router.py` |

### Speaker notes
- "**Text2SQL E2E 80% match rate** mới là metric quan trọng nhất — đây là **Execution Accuracy** chuẩn Spider/BIRD"
- "100% câu hỏi sinh được SQL và chạy được — pipeline không crash"
- "Easy 100%, Medium 82%, Hard 50% — đúng kỳ vọng: window function, drawdown vẫn còn khó"
- "Intent router 98.85% giúp giảm tải LLM cho text_to_sql — câu mơ hồ/SQL injection chặn ngay từ vòng router"

---

## Slide 21 — Hạn chế và Hướng phát triển

### Title
**Hạn chế + Hướng phát triển**

### Hạn chế hiện tại
1. Phụ thuộc LLM endpoint — LLM down thì system báo lỗi
2. Universe cố định NASDAQ-100 (101 mã) — chưa có thị trường Việt
3. Chỉ daily bar — không có intraday
4. In-memory session — restart mất context
5. Feature-hash memory — không bắt được rephrase ngữ nghĩa
6. Chưa có auth + rate limit

### Hướng phát triển
- Thêm bảng `prices_intraday` (bar 1m/5m/15m)
- Thay feature-hash bằng sentence-transformer (bge-small / multilingual-e5)
- Fine-tune Qwen 7B / Llama 3 8B trên 5k cặp (Q, SQL) → chạy 100% local
- Multi-database: HOSE, HNX, UPCOM cho thị trường Việt
- Forecasting path (Prophet/ARIMA) với disclaimer
- JWT auth + Redis session + caching
- Monitoring (Prometheus + Grafana, Sentry)
- Export CSV/Excel/PDF, share permalink

### Speaker notes
- Mỗi hạn chế ghép với 1 hướng phát triển tương ứng
- Nhấn: "Đây là demo cho đồ án — production cần thêm auth, monitoring, mở rộng universe"

---

## Slide 22 — Kết luận & Q&A

### Title
**Kết luận & Cảm ơn**

### Đã hoàn thành
- ✅ Hệ thống FinTextSQL chạy end-to-end với 5 service Docker
- ✅ Multi-path Agent với 5 intent + LangGraph pipeline + repair loop
- ✅ Routing accuracy ~96%, SQL execution rate ~95% trên 130 câu test
- ✅ Cross-session memory, SSE streaming, multi-turn follow-up
- ✅ Frontend ChatGPT-like với 102 mã NASDAQ-100

### Source code + Demo
- GitHub: `https://github.com/<repo>`
- Public demo: `https://data-platform.mrworld.id.vn` (qua Cloudflare Tunnel)

### Q&A
**Sẵn sàng nhận câu hỏi từ thầy cô và các bạn**

### Speaker notes
- 5s mở Q&A
- Chuẩn bị 5-10 câu hỏi dự kiến:
  1. Tại sao không dùng pgvector mà dùng feature-hash?
  2. Khi LLM down thì system hoạt động thế nào?
  3. Latency 5-8s có chấp nhận được không?
  4. Bảo mật SQL injection cụ thể ra sao?
  5. Mở rộng cho thị trường Việt phải làm gì?
  6. Có thể fine-tune model nhỏ thay LLM cloud không?
  7. Cross-session memory có vấn đề privacy không?

---

## 📋 Tóm tắt phân bổ thời gian

| Slides | Nội dung | Thời gian |
|---|---|---|
| 1-2 | Bìa + thành viên | 1 phút |
| 3-5 | Vấn đề + mục tiêu + giải pháp | 2 phút |
| 6-9 | Kiến trúc + Text2SQL + LLM budget | 4 phút |
| 10-15 | Demo 5 intent + multi-turn | 6 phút |
| 16-19 | Tech stack + DB + memory + SSE | 3 phút |
| 20 | Benchmark | 1.5 phút |
| 21 | Hạn chế + hướng phát triển | 1.5 phút |
| 22 | Q&A | 5 phút (mở) |
| **Tổng** | | **~20 phút thuyết trình + Q&A** |

---

## 🎨 Gợi ý style slide

### Khi convert sang PowerPoint/Google Slides
- **Font**: Title 32pt, Body 18pt, Code 14pt
- **Màu**: Background trắng, text đen, accent xanh `#2563EB`
- **Layout**: 16:9
- **Ảnh**: chiếm ~50-60% slide khi có (slide 6, 8, 10-15)
- **Bullet**: tối đa 5 bullet/slide
- **Code/SQL**: dùng monospace font (Courier / Consolas)

### Khi convert sang Marp/Reveal.js
```bash
# Marp CLI
npm install -g @marp-team/marp-cli
marp slide.md --pdf --output presentation.pdf
marp slide.md --pptx --output presentation.pptx

# Reveal.js
# Mở slide.md trong VSCode + extension "Markdown Slides Preview"
```

### Khi convert sang Google Slides
- Copy từng section sang Google Slides thủ công
- Tải ảnh từ `picture/` folder
- Apply theme "Simple Light" hoặc "Material Light"

---

## 📂 Cấu trúc file ảnh

```
docs/
├── slide.md                  # File này
└── picture/
    ├── architecture.jpg      # Slide 6
    ├── text2sql.jpg          # Slide 8
    ├── 01_home.png           # Slide 10
    ├── 02_text_to_sql.png    # Slide 11
    ├── 02_web_search.png     # Slide 12
    ├── 02_general.png        # Slide 13
    ├── 02_multi_turn_1.png   # Slide 14
    └── 02_multi_turn_2.png   # Slide 15
```

8 ảnh được dùng cho 7 slide có ảnh (slide 14-15 dùng chung concept multi-turn).
