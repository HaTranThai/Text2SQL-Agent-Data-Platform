import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  ChevronDown,
  Database,
  Loader2,
  RefreshCw,
  Send,
  Sparkles,
  Table2,
  TerminalSquare,
  User,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { format as formatSqlRaw } from "sql-formatter";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function formatSql(sql: string): string {
  try {
    return formatSqlRaw(sql, { language: "postgresql", keywordCase: "preserve", tabWidth: 2 });
  } catch {
    return sql;
  }
}
const TRACE_STEP_DELAY_MS = 1700;

type IntentName = "general" | "text_to_sql" | "visualization" | "news" | "ingestion" | "simple_finance";

type VisualizationSpec = {
  type: "line" | "bar" | "area" | "scatter";
  x?: string | null;
  y?: string | null;
  y_series?: string[] | null;
  series?: string | null;
  title?: string | null;
};

type ChatResponse = {
  intent: IntentName;
  answer: string;
  sql?: string | null;
  rows: Record<string, unknown>[];
  columns: string[];
  visualization?: VisualizationSpec | null;
  sources: Record<string, unknown>[];
  debug: Record<string, unknown>;
  sub_results?: TaskResult[];
};

type TaskResult = {
  intent: IntentName;
  title: string;
  answer: string;
  sql?: string | null;
  rows: Record<string, unknown>[];
  columns: string[];
  visualization?: VisualizationSpec | null;
  debug: Record<string, unknown>;
};

type ResultPayload = ChatResponse | TaskResult;

type RoutePreviewResponse = {
  intent: IntentName;
  tickers: string[];
  reason: string;
  pipeline: string[];
  router: Record<string, unknown>;
};

type Company = {
  ticker: string;
  name?: string | null;
  sector?: string | null;
  currency?: string | null;
};

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  response?: ChatResponse;
};

type ProgressStep = {
  key: string;
  label: string;
  detail: string;
};

const starterMessages: ChatMessage[] = [
  {
    role: "assistant",
    content:
      "Mình sẵn sàng phân tích dữ liệu finance. Sync vài ticker, rồi hỏi tự nhiên; nếu kết quả có thể vẽ chart, mình sẽ vẽ ngay trong câu trả lời.",
  },
];

const samplePrompts = [
  "So sánh close price của AAPL và MSFT trong 30 ngày gần nhất",
  "Tính MA20 và MA50 của AAPL trong 100 ngày gần nhất",
  "Drawdown lớn nhất của NVDA năm 2024",
  "Tính beta của NVDA so với SPY trong 2 năm gần nhất",
  "Tương quan lợi suất ngày giữa AAPL và MSFT trong 1 năm gần nhất",
  "Top 5 phiên có volume cao nhất của TSLA",
  "Top companies by market cap",
  "Giá hiện tại của AAPL MSFT NVDA",
  "% tăng/giảm 30 ngày của AAPL và MSFT",
  "Tin mới có thể ảnh hưởng tới giá NVDA",
];

export default function App() {
  const sessionId = useMemo(() => makeSessionId(), []);
  const [messages, setMessages] = useState<ChatMessage[]>(starterMessages);
  const [input, setInput] = useState("");
  const [showSuggestions, setShowSuggestions] = useState(true);
  const [loading, setLoading] = useState(false);
  const [activeProgress, setActiveProgress] = useState<{ steps: ProgressStep[]; current: number } | null>(null);
  const [ingesting, setIngesting] = useState(false);
  const [tickers, setTickers] = useState("AAPL, MSFT, NVDA");
  const [period, setPeriod] = useState("1y");
  const [companies, setCompanies] = useState<Company[]>([]);
  const [health, setHealth] = useState<"checking" | "ok" | "down">("checking");
  const conversationRef = useRef<HTMLDivElement | null>(null);

  const latestResponse = useMemo(
    () => [...messages].reverse().find((message) => message.response)?.response,
    [messages],
  );
  const loadedTickers = useMemo(() => new Set(companies.map((company) => company.ticker)), [companies]);

  useEffect(() => {
    void refreshMetadata();
  }, []);

  useEffect(() => {
    conversationRef.current?.scrollTo({
      top: conversationRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, loading, activeProgress]);

  useEffect(() => {
    if (!loading || !activeProgress) return undefined;
    const intervalId = window.setInterval(() => {
      setActiveProgress((current) => {
        if (!current) return current;
        return {
          ...current,
          current: Math.min(current.current + 1, current.steps.length - 1),
        };
      });
    }, TRACE_STEP_DELAY_MS);
    return () => window.clearInterval(intervalId);
  }, [loading, activeProgress?.steps.length]);

  async function refreshMetadata() {
    try {
      const [healthResponse, companiesResponse] = await Promise.all([
        fetch(`${API_URL}/health`),
        fetch(`${API_URL}/companies`),
      ]);
      setHealth(healthResponse.ok ? "ok" : "down");
      if (companiesResponse.ok) {
        setCompanies(await companiesResponse.json());
      }
    } catch {
      setHealth("down");
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || loading) return;

    setMessages((current) => [...current, { role: "user", content: text }]);
    setInput("");
    setActiveProgress({ steps: progressStepsForMessage(text), current: 0 });
    setLoading(true);
    try {
      try {
        const route = await postJson<RoutePreviewResponse>("/chat/route", { message: text, session_id: sessionId });
        const steps = progressStepsForRoute(route);
        setActiveProgress({ steps, current: Math.min(1, steps.length - 1) });
      } catch {
        // The main chat request still owns the final answer if route preview fails.
      }
      const response = await postJson<ChatResponse>("/chat", { message: text, session_id: sessionId });
      setMessages((current) => [...current, { role: "assistant", content: response.answer, response }]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        { role: "assistant", content: error instanceof Error ? error.message : "Request failed." },
      ]);
    } finally {
      setActiveProgress(null);
      setLoading(false);
    }
  }

  async function runIngestion(event: FormEvent) {
    event.preventDefault();
    await ingestSymbols(
      tickers
        .split(",")
        .map((ticker) => ticker.trim())
        .filter(Boolean),
      period,
    );
  }

  async function ingestSymbols(symbols: string[], selectedPeriod = period) {
    if (!symbols.length || ingesting) return;
    setTickers(symbols.join(", "));
    setIngesting(true);
    try {
      const response = await postJson<{
        run_id: number;
        status: string;
        tickers: string[];
        rows_loaded: number;
        message?: string | null;
      }>("/ingest", {
        tickers: symbols,
        period: selectedPeriod,
        interval: "1d",
        include_fundamentals: true,
        include_news: true,
      });
      const chatResponse: ChatResponse = {
        intent: "ingestion",
        answer: response.message ?? "Ingestion finished.",
        rows: [
          {
            run_id: response.run_id,
            status: response.status,
            tickers: response.tickers.join(", "),
            rows_loaded: response.rows_loaded,
          },
        ],
        columns: ["run_id", "status", "tickers", "rows_loaded"],
        sources: [],
        debug: {},
      };
      setMessages((current) => [
        ...current,
        { role: "assistant", content: chatResponse.answer, response: chatResponse },
      ]);
      await refreshMetadata();
    } catch (error) {
      setMessages((current) => [
        ...current,
        { role: "assistant", content: error instanceof Error ? error.message : "Ingestion failed." },
      ]);
    } finally {
      setIngesting(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-row">
          <Database size={22} aria-hidden="true" />
          <div>
            <h1>FinTextSQL</h1>
            <p>Finance Text-to-SQL assistant</p>
          </div>
        </div>
        <div className="topbar-actions">
          <StatusPill icon={<Table2 size={16} />} label={`${latestResponse?.rows.length ?? 0} rows`} />
          <StatusPill icon={<Database size={16} />} label={`${companies.length} symbols`} />
          <div className={`status-pill ${health}`}>
            <Activity size={16} aria-hidden="true" />
            {health === "ok" ? "Backend online" : health === "down" ? "Backend offline" : "Checking"}
          </div>
        </div>
      </header>

      <main className="chat-shell">
        <form className="data-strip" onSubmit={runIngestion}>
          <div className="data-strip-title">
            <Database size={18} aria-hidden="true" />
            <span>Data</span>
          </div>
          <input value={tickers} onChange={(event) => setTickers(event.target.value)} aria-label="Tickers" />
          <select value={period} onChange={(event) => setPeriod(event.target.value)} aria-label="Period">
            <option value="1mo">1mo</option>
            <option value="3mo">3mo</option>
            <option value="6mo">6mo</option>
            <option value="1y">1y</option>
            <option value="2y">2y</option>
            <option value="5y">5y</option>
          </select>
          <button type="submit" disabled={ingesting || !tickers.trim()}>
            {ingesting ? <Loader2 size={16} aria-hidden="true" /> : <RefreshCw size={16} aria-hidden="true" />}
            Sync
          </button>
          <button className="icon-button" type="button" onClick={() => void refreshMetadata()} title="Refresh">
            <RefreshCw size={17} aria-hidden="true" />
          </button>
        </form>

        <div className="universe-row" aria-label="Loaded symbols">
          {companies.length ? (
            companies.slice(0, 12).map((company) => (
              <span key={company.ticker}>
                {company.ticker}
                <small>{company.currency ?? company.sector ?? "loaded"}</small>
              </span>
            ))
          ) : (
            <span>
              No symbols loaded
              <small>sync first</small>
            </span>
          )}
        </div>

        <section className="conversation" ref={conversationRef} aria-label="Conversation">
          {messages.map((message, index) => (
            <ChatBubble
              key={`${message.role}-${index}`}
              message={message}
              loadedTickers={loadedTickers}
              onSyncMissing={(symbols) => void ingestSymbols(symbols, "3mo")}
              ingesting={ingesting}
            />
          ))}
          {loading ? <LoadingBubble progress={activeProgress} /> : null}
        </section>

        <div className="suggestions">
          <button
            type="button"
            className="suggestions-label"
            onClick={() => setShowSuggestions((value) => !value)}
            aria-expanded={showSuggestions}
          >
            <Sparkles size={13} aria-hidden="true" />
            Gợi ý câu hỏi
            <ChevronDown
              size={15}
              className={`suggestions-chevron${showSuggestions ? " open" : ""}`}
              aria-hidden="true"
            />
          </button>
          {showSuggestions ? (
            <div className="suggestion-row">
              {samplePrompts.map((prompt) => (
                <button type="button" key={prompt} onClick={() => setInput(prompt)} title={prompt}>
                  {prompt}
                </button>
              ))}
            </div>
          ) : null}
        </div>

        <form className="composer" onSubmit={sendMessage}>
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }}
            placeholder="Hỏi bất cứ điều gì..."
            aria-label="Ô nhập câu hỏi"
          />
          <button type="submit" disabled={loading || !input.trim()} title="Gửi (Enter)">
            {loading ? <Loader2 size={18} aria-hidden="true" /> : <Send size={18} aria-hidden="true" />}
          </button>
        </form>
      </main>
    </div>
  );
}

function StatusPill({ icon, label }: { icon: JSX.Element; label: string }) {
  return (
    <div className="status-pill muted">
      {icon}
      {label}
    </div>
  );
}

function ChatBubble({
  message,
  loadedTickers,
  onSyncMissing,
  ingesting,
}: {
  message: ChatMessage;
  loadedTickers: Set<string>;
  onSyncMissing: (symbols: string[]) => void;
  ingesting: boolean;
}) {
  const response = message.response;
  const visualization = response ? getVisualization(response) : null;
  const hasSubResults = Boolean(response?.sub_results?.length);
  const shouldShowTable = Boolean(response && !hasSubResults && response.intent !== "news" && response.rows.length);
  const missingTickers = response
    ? getRequestedTickers(response).filter((ticker) => !loadedTickers.has(ticker))
    : [];

  return (
    <article className={`chat-message ${message.role}`}>
      <div className="avatar" aria-hidden="true">
        {message.role === "user" ? <User size={17} /> : <Bot size={17} />}
      </div>
      <div className="bubble-body">
        <MessageContent content={message.content} />

        {response && !response.rows.length ? (
          <EmptyInline
            missingTickers={missingTickers}
            onSyncMissing={() => onSyncMissing(missingTickers)}
            ingesting={ingesting}
          />
        ) : null}

        {response ? <ProgressTrace response={response} mode="complete" /> : null}

        {response?.sub_results?.length ? <SubResults results={response.sub_results} /> : null}

        {response && !hasSubResults && visualization && response.rows.length ? (
          <ArtifactCard title={chartTitle(visualization)} icon={<BarChart3 size={16} />}>
            <ResultChart response={response} visualization={visualization} />
          </ArtifactCard>
        ) : null}

        {response && shouldShowTable ? (
          <ArtifactCard title={`${response.rows.length} rows`} icon={<Table2 size={16} />}>
            <ResultTable response={response} />
          </ArtifactCard>
        ) : null}

        {response?.sql && !hasSubResults ? (
          <details className="sql-details">
            <summary>
              <TerminalSquare size={14} aria-hidden="true" />
              SQL
            </summary>
            <pre className="sql-block">{formatSql(response.sql)}</pre>
          </details>
        ) : null}
      </div>
    </article>
  );
}

function SubResults({ results }: { results: TaskResult[] }) {
  return (
    <div className="sub-results">
      {results.map((result, index) => {
        const visualization = getVisualization(result);
        const showTable = result.intent !== "news" && result.rows.length > 0;
        return (
          <section className="sub-result" key={`${result.title}-${index}`}>
            <div className="sub-result-title">{result.title}</div>
            <MessageContent content={result.answer} />
            {visualization && result.rows.length ? (
              <ArtifactCard title={chartTitle(visualization)} icon={<BarChart3 size={16} />}>
                <ResultChart response={result} visualization={visualization} />
              </ArtifactCard>
            ) : null}
            {showTable ? (
              <ArtifactCard title={`${result.rows.length} rows`} icon={<Table2 size={16} />}>
                <ResultTable response={result} />
              </ArtifactCard>
            ) : null}
            {result.sql ? (
              <details className="sql-details">
                <summary>
                  <TerminalSquare size={14} aria-hidden="true" />
                  SQL
                </summary>
                <pre className="sql-block">{result.sql ? formatSql(result.sql) : ""}</pre>
              </details>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}

function ProgressTrace({
  response,
  mode,
}: {
  response: ChatResponse;
  mode: "complete";
}) {
  const steps = completedStepsFromResponse(response);
  if (!steps.length) return null;

  return (
    <details className="thinking-card">
      <summary>
        <Activity size={14} aria-hidden="true" />
        Thinking trace
      </summary>
      <ol className="thinking-steps">
        {steps.map((step) => (
          <li className="done" key={step.key}>
            <span className="step-dot" aria-hidden="true" />
            <div>
              <strong>{step.label}</strong>
              <small>{step.detail}</small>
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function MessageContent({ content }: { content: string }) {
  const blocks = parseMessageBlocks(content);
  return (
    <div className="message-content">
      {blocks.map((block, index) => {
        if (block.type === "heading") {
          const HeadingTag = `h${block.level}` as "h3" | "h4";
          return <HeadingTag key={index}>{renderInlineMarkdown(block.text)}</HeadingTag>;
        }
        if (block.type === "list") {
          const ListTag = block.ordered ? "ol" : "ul";
          return (
            <ListTag key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{renderInlineMarkdown(item)}</li>
              ))}
            </ListTag>
          );
        }
        if (block.type === "code") {
          return (
            <pre className="answer-code" key={index}>
              {block.text}
            </pre>
          );
        }
        if (block.type === "table") {
          return <MarkdownTable key={index} headers={block.headers} rows={block.rows} />;
        }
        return <p key={index}>{renderInlineMarkdown(block.text)}</p>;
      })}
    </div>
  );
}

type MessageBlock =
  | { type: "paragraph"; text: string }
  | { type: "heading"; level: 3 | 4; text: string }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "code"; text: string }
  | { type: "table"; headers: string[]; rows: string[][] };

function parseMessageBlocks(content: string): MessageBlock[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: MessageBlock[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let code: string[] | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ type: "paragraph", text: paragraph.join(" ") });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      blocks.push({ type: "list", ordered: list.ordered, items: list.items });
      list = null;
    }
  };

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const rawLine = lines[lineIndex];
    const line = rawLine.trim();
    if (line.startsWith("```")) {
      if (code) {
        blocks.push({ type: "code", text: code.join("\n") });
        code = null;
      } else {
        flushParagraph();
        flushList();
        code = [];
      }
      continue;
    }
    if (code) {
      code.push(rawLine);
      continue;
    }
    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }

    if (isMarkdownTableStart(lines, lineIndex)) {
      flushParagraph();
      flushList();
      const tableLines: string[] = [line];
      let cursor = lineIndex + 2;
      while (cursor < lines.length && isMarkdownTableRow(lines[cursor].trim())) {
        tableLines.push(lines[cursor].trim());
        cursor += 1;
      }
      const parsed = parseMarkdownTable(tableLines);
      if (parsed) {
        blocks.push(parsed);
        lineIndex = cursor - 1;
        continue;
      }
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({
        type: "heading",
        level: heading[1].length >= 3 ? 3 : 4,
        text: heading[2],
      });
      continue;
    }

    const unordered = line.match(/^[-*]\s+(.+)$/);
    const ordered = line.match(/^\d+\.\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const isOrdered = Boolean(ordered);
      const item = (ordered?.[1] ?? unordered?.[1] ?? "").trim();
      if (!list || list.ordered !== isOrdered) {
        flushList();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push(item);
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  if (code) blocks.push({ type: "code", text: code.join("\n") });
  return blocks;
}

function MarkdownTable({ headers, rows }: { headers: string[]; rows: string[][] }) {
  return (
    <div className="markdown-table-wrap">
      <table>
        <thead>
          <tr>
            {headers.map((header, index) => (
              <th key={`${header}-${index}`}>{renderInlineMarkdown(header)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {headers.map((_, columnIndex) => (
                <td key={columnIndex}>{renderInlineMarkdown(row[columnIndex] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function isMarkdownTableStart(lines: string[], index: number): boolean {
  return isMarkdownTableRow(lines[index]?.trim() ?? "") && isMarkdownTableDivider(lines[index + 1]?.trim() ?? "");
}

function isMarkdownTableRow(line: string): boolean {
  return line.startsWith("|") && line.endsWith("|") && line.split("|").length >= 3;
}

function isMarkdownTableDivider(line: string): boolean {
  return isMarkdownTableRow(line) && line
    .slice(1, -1)
    .split("|")
    .every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function parseMarkdownTable(lines: string[]): MessageBlock | null {
  if (!lines.length) return null;
  const headers = splitMarkdownTableRow(lines[0]);
  const rows = lines.slice(1).map(splitMarkdownTableRow).filter((row) => row.length);
  if (!headers.length || !rows.length) return null;
  return { type: "table", headers, rows };
}

function splitMarkdownTableRow(line: string): string[] {
  return line
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const parts = text.split(/(\[[^\]]+\]\(https?:\/\/[^)\s]+\)|\*\*[^*]+\*\*)/g);
  return parts.map((part, index) => {
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
    if (link) {
      return (
        <a className="source-link-pill" href={link[2]} key={index} rel="noreferrer" target="_blank">
          {link[1]}
        </a>
      );
    }
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    return part;
  });
}

function LoadingBubble({ progress }: { progress: { steps: ProgressStep[]; current: number } | null }) {
  return (
    <article className="chat-message assistant">
      <div className="avatar" aria-hidden="true">
        <Bot size={17} />
      </div>
      <div className="bubble-body">
        <p className="inline-loader">
          <Loader2 size={16} aria-hidden="true" /> Đang xử lý
        </p>
        {progress ? <LiveProgressTrace progress={progress} /> : null}
      </div>
    </article>
  );
}

function LiveProgressTrace({ progress }: { progress: { steps: ProgressStep[]; current: number } }) {
  const visibleSteps = progress.steps.slice(0, progress.current + 1);
  return (
    <div className="thinking-card live">
      <div className="thinking-title">
        <Activity size={14} aria-hidden="true" />
        Luồng đang chạy
      </div>
      <ol className="thinking-steps">
        {visibleSteps.map((step, index) => {
          const status = index < visibleSteps.length - 1 ? "done" : "active";
          return (
            <li className={status} key={step.key}>
              <span className="step-dot" aria-hidden="true" />
              <div>
                <strong>{step.label}</strong>
                <small>{step.detail}</small>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function ArtifactCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: JSX.Element;
  children: ReactNode;
}) {
  return (
    <section className="artifact-card">
      <div className="artifact-header">
        {icon}
        <span>{title}</span>
      </div>
      {children}
    </section>
  );
}

function EmptyInline({
  missingTickers,
  onSyncMissing,
  ingesting,
}: {
  missingTickers: string[];
  onSyncMissing: () => void;
  ingesting: boolean;
}) {
  return (
    <div className="empty-inline">
      <AlertTriangle size={16} aria-hidden="true" />
      <span>
        {missingTickers.length
          ? `Thiếu dữ liệu cho ${missingTickers.join(", ")}.`
          : "Chưa có dòng dữ liệu phù hợp."}
      </span>
      {missingTickers.length ? (
        <button type="button" onClick={onSyncMissing} disabled={ingesting}>
          {ingesting ? <Loader2 size={14} aria-hidden="true" /> : <RefreshCw size={14} aria-hidden="true" />}
          Sync missing
        </button>
      ) : null}
    </div>
  );
}

function ResultChart({
  response,
  visualization,
}: {
  response: ResultPayload;
  visualization: VisualizationSpec;
}) {
  const xKey = visualization.x ?? response.columns[0];
  const yKey =
    visualization.y ?? response.columns.find((column) => isNumericColumn(response.rows, column));
  if (!xKey || !yKey || !isNumericColumn(response.rows, yKey)) return null;

  const { data, seriesKeys } = buildChartData(
    response,
    xKey,
    yKey,
    visualization.series ?? null,
    visualization.type,
    visualization.y_series ?? null,
  );
  if (!data.length || !seriesKeys.length) return null;

  const Chart = visualization.type === "bar" ? BarChart : AreaChart;
  const isMoneyChart = seriesKeys.some(isMoneyMetric);
  const yDomain = visualization.type === "bar" ? undefined : priceAwareDomain(data, seriesKeys, isMoneyChart);
  return (
    <div className="chart-box">
      <ResponsiveContainer width="100%" height={330}>
        <Chart data={data} margin={{ top: 8, right: 28, bottom: 8, left: 4 }}>
          {visualization.type !== "bar" ? (
            <defs>
              {seriesKeys.map((key, index) => (
                <linearGradient key={key} id={chartGradientId(key)} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={chartColors[index % chartColors.length]} stopOpacity={0.28} />
                  <stop offset="94%" stopColor={chartColors[index % chartColors.length]} stopOpacity={0.03} />
                </linearGradient>
              ))}
            </defs>
          ) : null}
          <CartesianGrid strokeDasharray="4 4" stroke="#dfe6ee" vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={{ fontSize: 12, fill: "#334155" }}
            axisLine={{ stroke: "#334155" }}
            tickLine={false}
            minTickGap={28}
          />
          <YAxis
            tick={{ fontSize: 12, fill: "#334155" }}
            axisLine={{ stroke: "#334155" }}
            tickLine={false}
            tickFormatter={(value) => formatAxisValue(value, { currency: isMoneyChart })}
            width={64}
            domain={yDomain}
          />
          <Tooltip
            formatter={(value, name) => [
              formatTooltipValue(value, { currency: isMoneyMetric(String(name)) }),
              labelize(String(name)),
            ]}
            labelFormatter={(label) => `${labelize(xKey)}: ${label}`}
            cursor={{ stroke: "#94a3b8", strokeDasharray: "4 4" }}
            contentStyle={{
              borderRadius: 8,
              borderColor: "#dce2e8",
              boxShadow: "0 10px 30px rgba(15, 23, 42, 0.12)",
              fontSize: 12,
            }}
          />
          <Legend iconType="circle" wrapperStyle={{ fontSize: 12, paddingTop: 8 }} />
          {visualization.type === "bar"
            ? seriesKeys.map((key, index) => (
                <Bar
                  key={key}
                  dataKey={key}
                  fill={chartColors[index % chartColors.length]}
                  radius={[4, 4, 0, 0]}
                />
              ))
            : seriesKeys.map((key, index) => (
                <Area
                  key={key}
                  dataKey={key}
                  fill={`url(#${chartGradientId(key)})`}
                  stroke={chartColors[index % chartColors.length]}
                  strokeWidth={2.5}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2 }}
                  connectNulls
                  type="monotone"
                />
              ))}
        </Chart>
      </ResponsiveContainer>
    </div>
  );
}

function ResultTable({ response }: { response: ResultPayload }) {
  const columns = response.columns.length ? response.columns : Object.keys(response.rows[0]);
  const rows = orderRowsForTable(response.rows, columns);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column) => (
                <td key={column}>{formatCell(row[column], column)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function orderRowsForTable(rows: Record<string, unknown>[], columns: string[]) {
  const dateColumn = first(columns, ["date", "as_of_date", "published_at"]);
  if (!dateColumn) return rows;

  return [...rows].sort((a, b) => String(b[dateColumn]).localeCompare(String(a[dateColumn])));
}

async function postJson<T>(path: string, payload: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    throw new Error(errorBody.detail ?? `Request failed with ${response.status}`);
  }
  return response.json();
}

const chartColors = ["#2f7f6f", "#315f9f", "#b76e2b", "#6f5aa8", "#b74763"];

function chartTitle(visualization: VisualizationSpec): string {
  if (visualization.x && visualization.y) {
    return `${labelize(visualization.y)} by ${labelize(visualization.x)}`;
  }
  return visualization.title ?? "Chart";
}

function labelize(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatAxisValue(value: unknown, options: { currency?: boolean } = {}): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "");

  const absolute = Math.abs(number);
  const prefix = options.currency ? "$" : "";
  if (absolute >= 1_000_000_000_000) return `${prefix}${trimNumber(number / 1_000_000_000_000)}T`;
  if (absolute >= 1_000_000_000) return `${prefix}${trimNumber(number / 1_000_000_000)}B`;
  if (absolute >= 1_000_000) return `${prefix}${trimNumber(number / 1_000_000)}M`;
  if (absolute >= 1_000) return `${prefix}${trimNumber(number / 1_000)}K`;
  if (absolute > 0 && absolute < 1) return `${prefix}${number.toPrecision(2)}`;
  return `${prefix}${trimNumber(number)}`;
}

function formatTooltipValue(value: unknown, options: { currency?: boolean } = {}): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "");
  const prefix = options.currency ? "$" : "";
  if (Math.abs(number) >= 1_000_000) {
    return `${formatAxisValue(number, options)} (${prefix}${number.toLocaleString(undefined, { maximumFractionDigits: 2 })})`;
  }
  return `${prefix}${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function trimNumber(value: number): string {
  return value.toLocaleString(undefined, {
    maximumFractionDigits: Math.abs(value) >= 10 ? 1 : 2,
  });
}

function chartGradientId(key: string): string {
  return `chart-gradient-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function isMoneyMetric(key: string): boolean {
  const normalized = key.toLowerCase();
  return [
    "price",
    "close",
    "open",
    "high",
    "low",
    "adj_close",
    "market_cap",
    "last_price",
  ].some((metric) => normalized === metric || normalized.endsWith(`_${metric}`));
}

function buildChartData(
  response: ResultPayload,
  xKey: string,
  yKey: string,
  seriesKey: string | null,
  chartType: VisualizationSpec["type"],
  explicitSeries: string[] | null = null,
) {
  const numericExplicit = (explicitSeries ?? []).filter((key) => isNumericColumn(response.rows, key));
  if (numericExplicit.length || !seriesKey) {
    const yKeys = numericExplicit.length
      ? numericExplicit
      : wideMetricColumns(response, xKey, yKey, chartType);
    return {
      data: response.rows
        .map((row) => {
          const normalized = { ...row };
          for (const key of yKeys) {
            normalized[key] = toFiniteNumber(row[key]);
          }
          return normalized;
        })
        .filter(
          (row) =>
            row[xKey] !== null &&
            row[xKey] !== undefined &&
            yKeys.some((key) => row[key] !== null),
        )
        .sort((a, b) => String(a[xKey]).localeCompare(String(b[xKey]))),
      seriesKeys: yKeys,
    };
  }

  const grouped = new Map<string, Record<string, unknown>>();
  const series = new Set<string>();
  for (const row of response.rows) {
    const xValue = String(row[xKey] ?? "");
    const key = String(row[seriesKey] ?? yKey);
    const yValue = toFiniteNumber(row[yKey]);
    if (!xValue || yValue === null) continue;
    series.add(key);
    const bucket = grouped.get(xValue) ?? { [xKey]: xValue };
    bucket[key] = yValue;
    grouped.set(xValue, bucket);
  }

  return {
    data: [...grouped.values()].sort((a, b) => String(a[xKey]).localeCompare(String(b[xKey]))),
    seriesKeys: [...series],
  };
}

function priceAwareDomain(data: Record<string, unknown>[], seriesKeys: string[], isMoneyChart: boolean) {
  if (!isMoneyChart) return undefined;
  const values = data.flatMap((row) =>
    seriesKeys
      .map((key) => toFiniteNumber(row[key]))
      .filter((value): value is number => value !== null),
  );
  if (!values.length) return undefined;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = Math.max(max - min, Math.abs(max) * 0.02, 1);
  return [Math.max(0, min - spread * 0.15), max + spread * 0.15];
}

function wideMetricColumns(
  response: ResultPayload,
  xKey: string,
  yKey: string,
  chartType: VisualizationSpec["type"],
): string[] {
  if (chartType === "bar" || xKey === "ticker" || !yKey.includes("_")) return [yKey];

  const yKeyParts = yKey.toLowerCase().split("_");
  const metricSuffix = yKeyParts[yKeyParts.length - 1];
  if (!metricSuffix) return [yKey];

  const candidates = response.columns.filter(
    (column) =>
      column !== xKey &&
      !["id", "company_id"].includes(column) &&
      column.toLowerCase().endsWith(`_${metricSuffix}`) &&
      isNumericColumn(response.rows, column),
  );
  return candidates.length > 1 ? candidates : [yKey];
}

function getVisualization(response: ResultPayload): VisualizationSpec | null {
  if (!response.rows.length) return null;
  if (
    response.visualization?.x &&
    response.visualization?.y &&
    isNumericColumn(response.rows, response.visualization.y)
  ) {
    return response.visualization;
  }

  const columns = response.columns.length ? response.columns : Object.keys(response.rows[0]);
  const x = first(columns, ["date", "as_of_date", "published_at", "ticker"]);
  const y =
    first(columns, ["last_price", "close", "adj_close", "volume", "market_cap", "trailing_pe"]) ??
    columns.find((column) => isNumericColumn(response.rows, column)) ??
    null;
  if (!x || !y) return null;
  return {
    type: x === "ticker" ? "bar" : "line",
    x,
    y,
    series: columns.includes("ticker") && x !== "ticker" ? "ticker" : null,
    title: "Chart",
  };
}

function first(columns: string[], preferred: string[]): string | null {
  for (const column of preferred) {
    if (columns.includes(column)) return column;
  }
  return null;
}

function getRequestedTickers(response: ChatResponse): string[] {
  const router = response.debug?.router;
  if (!router || typeof router !== "object" || !("tickers" in router)) return [];
  const tickers = (router as { tickers?: unknown }).tickers;
  return Array.isArray(tickers) ? tickers.filter((ticker): ticker is string => typeof ticker === "string") : [];
}

function progressStepsForMessage(_message: string): ProgressStep[] {
  return [
    step("receive", "Nhận câu hỏi", "Đưa request vào /chat"),
    step("route", "Intent Router", "Phân loại câu hỏi và trích xuất ticker"),
  ];
}

function progressStepsForRoute(route: RoutePreviewResponse): ProgressStep[] {
  const steps = [
    step("receive", "Nhận câu hỏi", "Request đã vào /chat"),
    step("route", `Intent Router: ${route.intent}`, route.reason || "Đã xác định intent"),
  ];
  const pipeline = Array.isArray(route.pipeline)
    ? route.pipeline.filter((item): item is string => typeof item === "string")
    : [];
  steps.push(...pipeline.map(pipelineStep));
  steps.push(step("result", "Response", "Đang chờ backend trả answer, table và chart nếu có"));
  return steps;
}

function completedStepsFromResponse(response: ChatResponse): ProgressStep[] {
  const router = response.debug?.router;
  const routerIntent =
    router && typeof router === "object" && "intent" in router ? String((router as { intent?: unknown }).intent) : response.intent;
  const routerReason =
    router && typeof router === "object" && "reason" in router ? String((router as { reason?: unknown }).reason) : "Completed";
  const steps = [
    step("receive", "Nhận câu hỏi", "Request đã được xử lý bởi /chat"),
    step("route", `Intent Router: ${routerIntent}`, routerReason),
  ];
  const pipeline = Array.isArray(response.debug?.pipeline)
    ? response.debug.pipeline.filter((item): item is string => typeof item === "string")
    : [];
  if (pipeline.length) {
    steps.push(...pipeline.map(pipelineStep));
  } else {
    steps.push(step("service", labelize(response.intent), "Completed"));
  }
  if (response.sql) steps.push(step("sql", "SQL", response.sql.replace(/\s+/g, " ").slice(0, 140)));
  steps.push(step("result", "Response", `${response.rows.length} rows, ${response.columns.length} columns`));
  return steps;
}

function step(key: string, label: string, detail: string): ProgressStep {
  return { key, label, detail };
}

function pipelineStep(name: string): ProgressStep {
  const catalog: Record<string, ProgressStep> = {
    route_intent: step("route_intent", "Intent Router", "Chọn nhánh xử lý cho request"),
    task_planner: step("task_planner", "Task planner", "Tách câu hỏi thành một hoặc nhiều tác vụ"),
    execute_tasks: step("execute_tasks", "Task executor", "Chạy từng tác vụ theo kế hoạch"),
    synthesize_response: step("synthesize_response", "Synthesize response", "Ghép kết quả các tác vụ thành câu trả lời"),
    parse_ingestion_request: step("parse_ingestion_request", "Parse ingestion request", "Lấy ticker, period và interval"),
    fetch_yfinance: step("fetch_yfinance", "YFinance ingestion", "Tải giá, fundamentals và news nếu bật"),
    upsert_postgres: step("upsert_postgres", "Upsert Postgres", "Ghi dữ liệu vào database"),
    load_news: step("load_news", "News service", "Đọc tin từ Postgres hoặc provider"),
    analyze_news: step("analyze_news", "Analyze news", "Tóm tắt theme, tác động và caveat"),
    format_sources: step("format_sources", "Format sources", "Chuẩn hóa metadata nguồn tin"),
    quote_lookup: step("quote_lookup", "Quote lookup", "Lấy giá nhanh cho ticker"),
    fallback_postgres_if_needed: step("fallback_postgres_if_needed", "Fallback Postgres", "Dùng dữ liệu local nếu provider không trả"),
    infer_visualization: step("infer_visualization", "Infer visualization", "Chọn chart phù hợp với rows/columns"),
    load_schema: step("load_schema", "Load schema", "Nạp schema finance có thể truy vấn"),
    schema_selector: step("schema_selector", "Schema selector", "Chọn bảng liên quan đến câu hỏi"),
    deterministic_sql: step("deterministic_sql", "Deterministic SQL", "Dùng template SQL cho case đã nhận diện rõ"),
    planner: step("planner", "Planner", "Lập kế hoạch truy vấn"),
    sql_generator: step("sql_generator", "SQL generator", "Sinh SQL SELECT"),
    sql_guard: step("sql_guard", "SQL guard", "Validate read-only, whitelist bảng, thêm LIMIT"),
    execute_sql: step("execute_sql", "Execute SQL", "Chạy query trong Postgres"),
    repair_empty_result: step("repair_empty_result", "Repair empty result", "Sửa query khi kết quả rỗng"),
    repair_sql_error: step("repair_sql_error", "Repair SQL error", "Sửa query khi guard/database báo lỗi"),
    explainer: step("explainer", "Explainer", "Tóm tắt kết quả"),
  };
  return catalog[name] ?? step(name, labelize(name), "Completed");
}

function formatCell(value: unknown, column = ""): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") {
    const normalizedColumn = column.toLowerCase();
    if (Number.isInteger(value) || normalizedColumn.includes("volume") || normalizedColumn.endsWith("_count")) {
      return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
    }
    if (
      ["open", "high", "low", "close", "adj_close", "start_close", "end_close", "price", "last_price"].some(
        (metric) => normalizedColumn === metric || normalizedColumn.endsWith(`_${metric}`),
      )
    ) {
      return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) < 10 ? 4 : 2 });
  }
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function isNumericColumn(rows: Record<string, unknown>[], column: string): boolean {
  return rows.slice(0, 20).some((row) => toFiniteNumber(row[column]) !== null);
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function makeSessionId(): string {
  const storageKey = "fintextsql.sessionId";
  try {
    const existing = window.localStorage.getItem(storageKey);
    if (existing) return existing;
  } catch {
    // localStorage may be unavailable in restricted browser modes.
  }

  let sessionId: string;
  if ("crypto" in window && "randomUUID" in window.crypto) {
    sessionId = window.crypto.randomUUID();
  } else {
    sessionId = `session-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  try {
    window.localStorage.setItem(storageKey, sessionId);
  } catch {
    // The in-memory session id still works for the current page lifetime.
  }
  return sessionId;
}
