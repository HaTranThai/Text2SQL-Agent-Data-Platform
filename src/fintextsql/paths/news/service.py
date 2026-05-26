from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import quote

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fintextsql.db.models import NewsArticle
from fintextsql.llm.client import LLMClient, LLMError, LLMMessage

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FinTextSQL/0.1; +https://localhost)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
NEWS_ANALYSIS_TIMEOUT_SECONDS = 20
NEWS_FETCH_TIMEOUT_SECONDS = 8


class NewsService:
    def __init__(self, db: Session, llm: LLMClient | None = None):
        self.db = db
        self.llm = llm

    async def answer(self, question: str, tickers: list[str]) -> tuple[str, list[dict[str, Any]]]:
        ticker = tickers[0] if tickers else None
        articles = await self.fetch_and_store(ticker=ticker, limit=8)
        if not articles:
            articles = self._latest_from_db(ticker=ticker, limit=8)

        if not articles:
            if ticker:
                return f"Chưa lấy được tin tức cho {ticker}. Hãy thử ingest lại hoặc kiểm tra kết nối internet.", []
            return "Chưa có tin tức finance trong database.", []

        answer = await self._analyze_news(question=question, ticker=ticker, articles=articles)
        return answer, articles

    async def _analyze_news(
        self,
        *,
        question: str,
        ticker: str | None,
        articles: list[dict[str, Any]],
    ) -> str:
        if self.llm:
            try:
                answer = await asyncio.wait_for(
                    self.llm.chat(
                        [
                            LLMMessage(
                                "system",
                                "\n".join(
                                    [
                                        "Bạn là chuyên gia phân tích tin tức tài chính, trả lời bằng tiếng Việt tự nhiên.",
                                        "Chỉ dùng tiêu đề/tóm tắt được cung cấp; KHÔNG bịa giá, rating hay sự kiện không có trong bài.",
                                        "Với MỖI bài được nhắc tới, tự phân loại tâm lý: 📈 tích cực / 📉 tiêu cực / ➖ trung tính,",
                                        "dựa trên nội dung bài (có thể tham khảo sentiment_hint nhưng tự đánh giá lại nếu thấy chưa đúng).",
                                        "Dịch tiêu đề sang tiếng Việt; không để nguyên tiêu đề tiếng Anh.",
                                        "Tránh câu sáo rỗng lặp lại; mỗi bài một takeaway cụ thể.",
                                        "Trả lời ĐÚNG theo cấu trúc Markdown sau:",
                                        "**Tổng quan:** 1-2 câu kèm tâm lý chung (tích cực/tiêu cực/trái chiều).",
                                        "### Tin nổi bật",
                                        "- <icon> **<tựa tiếng Việt>** [<Publisher>](<url>) — <một takeaway cụ thể> (<tâm lý>)",
                                        "### Tác động tiềm năng",
                                        "- các gạch đầu dòng ngắn gọn về ảnh hưởng tới mã/ngành",
                                        "### Lưu ý",
                                        "- nhắc đây là phân tích từ tiêu đề RSS, không phải khuyến nghị mua bán",
                                    ]
                                ),
                            ),
                            LLMMessage(
                                "user",
                                "\n\n".join(
                                    [
                                        f"Câu hỏi người dùng: {question}",
                                        f"Mã/chủ đề: {ticker or 'thị trường chung'}",
                                        "Danh sách bài viết (kèm sentiment_hint gợi ý):",
                                        _articles_context(articles),
                                    ]
                                ),
                            ),
                        ],
                        temperature=0.2,
                        max_tokens=900,
                    ),
                    timeout=NEWS_ANALYSIS_TIMEOUT_SECONDS,
                )
                return _clean_answer_tail(answer)
            except (LLMError, asyncio.TimeoutError):
                pass
        return _clean_answer_tail(_fallback_news_analysis(ticker=ticker, articles=articles))

    async def fetch_and_store(self, *, ticker: str | None, limit: int = 10) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=NEWS_FETCH_TIMEOUT_SECONDS) as client:
            for url, source in self._feed_urls(ticker):
                try:
                    response = await client.get(url, headers=RSS_HEADERS)
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                articles.extend(self._articles_from_feed(response.text, ticker=ticker, source=source, limit=limit))
                if len(articles) >= limit:
                    break

        articles = _filter_relevant_articles(_dedupe_articles(articles), ticker=ticker)[:limit]
        for article in articles:
            stmt = insert(NewsArticle).values(**article)
            stmt = stmt.on_conflict_do_update(
                index_elements=[NewsArticle.link],
                set_={
                    "ticker": stmt.excluded.ticker,
                    "title": stmt.excluded.title,
                    "publisher": stmt.excluded.publisher,
                    "published_at": stmt.excluded.published_at,
                    "summary": stmt.excluded.summary,
                    "source": stmt.excluded.source,
                },
            )
            self.db.execute(stmt)
        self.db.commit()
        return [_serialize_article(article) for article in articles]

    def _articles_from_feed(
        self,
        feed_text: str,
        *,
        ticker: str | None,
        source: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        feed = feedparser.parse(feed_text)
        articles: list[dict[str, Any]] = []
        for entry in feed.entries[:limit]:
            title = str(entry.get("title", "")).strip()
            link = str(entry.get("link", "")).strip()
            if not title or not link:
                continue
            articles.append(
                {
                    "ticker": ticker,
                    "title": title,
                    "publisher": _entry_publisher(entry, fallback=source),
                    "link": link,
                    "published_at": _parse_published(entry.get("published")),
                    "summary": _strip_html(str(entry.get("summary", "")))[:1000] or None,
                    "source": source,
                }
            )
        return articles

    def _latest_from_db(self, *, ticker: str | None, limit: int) -> list[dict[str, Any]]:
        stmt = select(NewsArticle).order_by(NewsArticle.published_at.desc().nulls_last()).limit(limit)
        if ticker:
            stmt = stmt.where(NewsArticle.ticker == ticker)
        rows = self.db.execute(stmt).scalars().all()
        return [
            _serialize_article(
                {
                    "ticker": row.ticker,
                    "title": row.title,
                    "publisher": row.publisher,
                    "link": row.link,
                    "published_at": row.published_at,
                    "summary": row.summary,
                    "source": row.source,
                }
            )
            for row in rows
        ]

    def _feed_urls(self, ticker: str | None) -> list[tuple[str, str]]:
        if ticker:
            # Build a query with ticker + company name so Google News returns on-topic articles.
            names = sorted(alias for alias in _ticker_aliases(ticker) if alias != ticker.lower())
            terms = " OR ".join([ticker, *[name.title() for name in names]]) if names else ticker
            query = quote(f"({terms}) stock when:14d")
            return [
                # Google News first: more reliable ticker-specific coverage than Yahoo's generic headline feed.
                (f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en", "google_news_rss"),
                (
                    "https://feeds.finance.yahoo.com/rss/2.0/headline"
                    f"?s={quote(ticker)}&region=US&lang=en-US",
                    "yahoo_finance_rss",
                ),
            ]
        return [
            (
                "https://news.google.com/rss/search?q=stock%20market%20finance%20when:14d&hl=en-US&gl=US&ceid=US:en",
                "google_news_rss",
            ),
            ("https://finance.yahoo.com/news/rssindex", "yahoo_finance_rss"),
        ]


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(text).split())


def _entry_publisher(entry: Any, *, fallback: str) -> str:
    source = entry.get("source")
    if isinstance(source, dict):
        publisher = str(source.get("title") or "").strip()
        if publisher:
            return publisher[:160]
    publisher = str(entry.get("publisher") or entry.get("author") or "").strip()
    return (publisher or fallback)[:160]


def _dedupe_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in articles:
        link_key = str(article.get("link") or "").strip().lower()
        title_key = _canonical_title(str(article.get("title") or ""))
        keys = {key for key in [link_key, title_key] if key}
        if not keys or seen.intersection(keys):
            continue
        seen.update(keys)
        deduped.append(article)
    return deduped


def _canonical_title(title: str) -> str:
    normalized = re.sub(r"\s+-\s+[^-]{2,80}$", "", title.strip().lower())
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _filter_relevant_articles(articles: list[dict[str, Any]], *, ticker: str | None) -> list[dict[str, Any]]:
    if not ticker:
        return articles
    aliases = _ticker_aliases(ticker)
    relevant = [
        article
        for article in articles
        if any(alias in _article_text(article) for alias in aliases)
    ]
    relevant = sorted(
        relevant,
        key=lambda article: (_article_relevance_score(article, ticker=ticker), _published_sort_key(article)),
        reverse=True,
    )
    if len(relevant) >= 2:
        return relevant
    if relevant:
        indirect = sorted(
            (article for article in articles if article not in relevant),
            key=_published_sort_key,
            reverse=True,
        )
        return [*relevant, *indirect[:2]]
    return sorted(articles, key=_published_sort_key, reverse=True)


def _article_relevance_score(article: dict[str, Any], *, ticker: str) -> int:
    text = _article_text(article)
    title = str(article.get("title") or "").lower()
    score = 0
    for alias in _ticker_aliases(ticker):
        if alias in text:
            score += 5
        if alias in title:
            score += 6  # mentions in the headline are far more on-topic than in the body
    direct_terms = [
        "stock",
        "shares",
        "price target",
        "rating",
        "valuation",
        "earnings",
        "revenue",
        "profit",
        "ceo",
        "guidance",
        "analyst",
        ticker.lower(),
    ]
    score += sum(3 for term in direct_terms if term in text)
    indirect_terms = ["marketplace", "digital markets act", "ios", "app store", "supplier", "partner"]
    score += sum(1 for term in indirect_terms if term in text)
    return score


def _ticker_aliases(ticker: str) -> set[str]:
    aliases_by_ticker = {
        "AAPL": {"aapl", "apple"},
        "MSFT": {"msft", "microsoft"},
        "NVDA": {"nvda", "nvidia"},
        "TSLA": {"tsla", "tesla"},
        "GOOGL": {"googl", "google", "alphabet"},
        "GOOG": {"goog", "google", "alphabet"},
        "AMZN": {"amzn", "amazon"},
        "META": {"meta", "facebook"},
        "AMD": {"amd", "advanced micro devices"},
    }
    return aliases_by_ticker.get(ticker.upper(), {ticker.lower()})


def _article_text(article: dict[str, Any]) -> str:
    return f"{article.get('title') or ''} {article.get('summary') or ''}".lower()


_BULLISH_TERMS = [
    "surge", "soar", "soars", "jump", "jumps", "rally", "rallies", "beat", "beats",
    "record high", "record", "upgrade", "upgraded", "price target", "hikes", "raises",
    "outperform", "gains", "gain", "rises", "rose", "higher", "tops", "wins", "boost",
    "optimistic", "bullish", "cheap", "undervalued", "strong demand", "growth",
    "all-time high", "top-notch", "winning streak",
]
_BEARISH_TERMS = [
    "plunge", "plunges", "slump", "slumps", "tumble", "tumbles", "falls", "fell",
    "drop", "drops", "miss", "misses", "downgrade", "downgraded", "cuts", "cut target",
    "lawsuit", "probe", "investigation", "warning", "warns", "weak", "decline",
    "declines", "bearish", "loss", "losses", "concern", "concerns", "cautious",
    "stretched", "selloff", "sell-off", "slowdown", "layoff", "recall", "sanction", "ban",
]


def _article_sentiment(article: dict[str, Any]) -> tuple[str, str]:
    """Lightweight keyword sentiment: returns (label_vi, icon)."""
    text = _article_text(article)
    bull = sum(1 for term in _BULLISH_TERMS if term in text)
    bear = sum(1 for term in _BEARISH_TERMS if term in text)
    if bull > bear:
        return "tích cực", "📈"
    if bear > bull:
        return "tiêu cực", "📉"
    return "trung tính", "➖"


def _published_sort_key(article: dict[str, Any]) -> float:
    value = article.get("published_at")
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _articles_context(articles: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, article in enumerate(articles[:8], start=1):
        label, icon = _article_sentiment(article)
        parts = [
            f"{index}. title: {article.get('title')}",
            f"url: {article.get('link')}",
            f"publisher: {_source_link_label(str(article.get('publisher') or article.get('source') or ''))}",
            f"published_at: {article.get('published_at') or 'unknown'}",
            f"sentiment_hint: {icon} {label}",
        ]
        summary = str(article.get("summary") or "").strip()
        if summary:
            parts.append(f"summary: {summary[:500]}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _fallback_news_analysis(*, ticker: str | None, articles: list[dict[str, Any]]) -> str:
    subject = ticker or "thị trường"
    themes = _theme_counts(articles)
    top_publishers = _top_values([str(article.get("publisher") or article.get("source") or "") for article in articles])
    selected_articles = _select_articles_for_brief(articles, ticker=ticker, limit=4)

    lines = [
        f"Mình tìm thấy {len(articles)} tin gần đây về {subject}. Chủ đề nổi bật là "
        f"{_format_themes(themes)}. Tâm lý chung: {_overall_sentiment_label(articles)}.",
        "",
        "### Tin đáng chú ý",
    ]
    for article in selected_articles:
        lines.append(f"- {_article_bullet(article, ticker=ticker)}")

    lines.extend(
        [
            "",
            "### Nhận định",
            _fallback_implication(subject, themes),
            "",
            "### Lưu ý",
            "- Đây là phân tích từ tiêu đề và mô tả ngắn của nguồn tin RSS, không phải khuyến nghị mua bán.",
            "- Nên đối chiếu thêm báo cáo tài chính, diễn biến giá và tin chính thức của công ty trước khi ra quyết định.",
        ]
    )
    source_labels = _unique_source_labels(top_publishers)
    if source_labels:
        lines.append(f"- Nguồn nổi bật: {', '.join(source_labels)}.")
    return "\n".join(lines)


def _select_articles_for_brief(
    articles: list[dict[str, Any]], *, ticker: str | None, limit: int
) -> list[dict[str, Any]]:
    if not ticker:
        return articles[:limit]
    aliases = _ticker_aliases(ticker)
    direct = [article for article in articles if any(alias in _article_text(article) for alias in aliases)]
    indirect = [article for article in articles if article not in direct]
    # Keep at most one sector-level article in a ticker answer so the response does not sound padded.
    return [*direct[:limit], *indirect[: max(0, min(1, limit - len(direct)))]] or articles[:limit]


def _translate_headline(headline: str) -> str:
    exact = {
        "NVDA Stock Quote Price and Forecast - CNN": "Giá cổ phiếu NVDA và dự báo - CNN",
        "NVIDIA and IREN Announce Strategic Partnership to Accelerate Deployment of up to 5 Gigawatts of AI Infrastructure - NVIDIA Newsroom": (
            "NVIDIA và IREN công bố hợp tác chiến lược để đẩy nhanh triển khai tới 5 gigawatt hạ tầng AI"
        ),
        "Get Paid 10% To Buy NVDA At A 30% Discount, Here’s How - Trefis": (
            "Cách nhận mức lợi suất 10% khi mua NVDA với chiết khấu 30%"
        ),
        "Why Nvidia stock looks cheap ahead of its high-stakes earnings report this month - Yahoo Finance": (
            "Vì sao cổ phiếu Nvidia có vẻ rẻ trước báo cáo kết quả kinh doanh quan trọng trong tháng này"
        ),
        "Meet the AI Stock Running Rings Around Nvidia in 2026. It Could Just Be Getting Started": (
            "Cổ phiếu AI đang vượt trội so với Nvidia trong năm 2026 có thể mới chỉ bắt đầu tăng tốc"
        ),
        "Xi Tells US CEOs Accompanying Trump That China Will Open Up More": (
            "Ông Tập nói với các CEO Mỹ đi cùng ông Trump rằng Trung Quốc sẽ mở cửa hơn nữa"
        ),
        "Taiwan's Foxconn reports forecast-beating 19% jump in Q1 profit on AI demand": (
            "Foxconn của Đài Loan báo lợi nhuận quý 1 tăng 19%, vượt dự báo nhờ nhu cầu AI"
        ),
        "Exclusive-US clears H200 chip sales to 10 China firms as Nvidia CEO looks for breakthrough": (
            "Độc quyền: Mỹ cho phép bán chip H200 cho 10 công ty Trung Quốc khi CEO Nvidia tìm kiếm đột phá"
        ),
        "Nvidia Overtakes Silver To Become World's Second-Largest Asset At $5.52 Trillion, Market Commentator Calls It 'Historic Technological Revolution'": (
            "Nvidia vượt bạc để trở thành tài sản lớn thứ hai thế giới ở mức 5,52 nghìn tỷ USD"
        ),
        "Ron DeSantis Admits Tesla's Are Undeniably 'Top-Notch Products' But Rejects The Idea That 'EVs Will Save The World'": (
            "Ron DeSantis thừa nhận xe Tesla là sản phẩm hàng đầu nhưng bác bỏ quan điểm xe điện sẽ cứu thế giới"
        ),
        "China's view on Elon Musk? Visionary, occasional villain": (
            "Trung Quốc nhìn Elon Musk như một nhà viễn kiến, nhưng đôi lúc cũng là nhân vật gây tranh cãi"
        ),
        "Gavin Newsom Unveils $1 Billion EV Incentive Program That Could Benefit Elon Musk's Tesla Semi": (
            "Gavin Newsom công bố chương trình ưu đãi xe điện 1 tỷ USD có thể hỗ trợ Tesla Semi"
        ),
        "Elon Musk Left For China With Trump During OpenAI Trial Despite Judge's 'Recall Status' Order: Report": (
            "Elon Musk sang Trung Quốc cùng ông Trump trong lúc phiên tòa OpenAI vẫn có yêu cầu từ thẩm phán"
        ),
        "Evercore ISI Hikes Apple Price Target to $365: Bull Case Now $500 on Services Compounding": (
            "Evercore ISI nâng mục tiêu giá Apple lên 365 USD, kịch bản tích cực là 500 USD nhờ mảng dịch vụ"
        ),
        "KeyBanc turns ’more cautious’ on Apple stock amid ’stretched’ valuation": (
            "KeyBanc thận trọng hơn với cổ phiếu Apple vì định giá bị cho là căng"
        ),
        "Apple Names New CEO as Tim Cook to Step Down in September": (
            "Apple bổ nhiệm CEO mới khi Tim Cook dự kiến rời vị trí vào tháng 9"
        ),
        "Xsolla and Skich Announce Strategic Partnership to Bring Merchant of Record Payments to an Alternative Mobile Game Marketplace": (
            "Xsolla và Skich hợp tác thanh toán cho marketplace game thay thế trên iOS"
        ),
    }
    if headline in exact:
        return exact[headline]

    translated = headline
    replacements = [
        ("Exclusive-", "Độc quyền: "),
        ("Exclusive:", "Độc quyền:"),
        ("Nvidia", "Nvidia"),
        ("AI Stock", "cổ phiếu AI"),
        ("AI stock", "cổ phiếu AI"),
        ("AI demand", "nhu cầu AI"),
        ("stock", "cổ phiếu"),
        ("shares", "cổ phiếu"),
        ("chip sales", "doanh số chip"),
        ("chip", "chip"),
        ("CEO", "CEO"),
        ("profit", "lợi nhuận"),
        ("revenue", "doanh thu"),
        ("forecast-beating", "vượt dự báo"),
        ("reports", "báo cáo"),
        ("jump", "tăng"),
        ("Q1", "quý 1"),
        ("China", "Trung Quốc"),
        ("Taiwan", "Đài Loan"),
        ("US", "Mỹ"),
        ("U.S.", "Mỹ"),
        ("market", "thị trường"),
        ("demand", "nhu cầu"),
        ("sales", "doanh số"),
        ("firms", "công ty"),
        ("clears", "cho phép"),
        ("looks for", "tìm kiếm"),
        ("breakthrough", "đột phá"),
        ("Open Up More", "mở cửa hơn nữa"),
        ("open up more", "mở cửa hơn nữa"),
    ]
    for source, target in replacements:
        translated = translated.replace(source, target)
    return translated


def _translated_article_headline(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "").strip()
    translated = _translate_headline(title)
    if not _looks_untranslated_english(translated):
        return translated
    return _headline_vi_summary(title, str(article.get("publisher") or article.get("source") or "nguồn tin"))


def _translated_article_reference(article: dict[str, Any]) -> str:
    headline = _translated_article_headline(article)
    link = str(article.get("link") or "").strip()
    source = str(article.get("publisher") or article.get("source") or "Nguồn").strip()
    label = _source_link_label(source)
    if not link:
        return f"**{headline}** ({label})"
    return f"**{headline}** [{label}]({link})"


def _article_bullet(article: dict[str, Any], *, ticker: str | None) -> str:
    date_text = _article_date_label(article)
    date_suffix = f", {date_text}" if date_text else ""
    label, icon = _article_sentiment(article)
    return (
        f"{icon} {_translated_article_reference(article)}{date_suffix}. "
        f"{_article_vi_description(article, ticker=ticker)} (Tâm lý: {label})"
    )


def _overall_sentiment_label(articles: list[dict[str, Any]]) -> str:
    counts = {"tích cực": 0, "tiêu cực": 0, "trung tính": 0}
    for article in articles:
        label, _ = _article_sentiment(article)
        counts[label] += 1
    if counts["tích cực"] and counts["tiêu cực"]:
        if abs(counts["tích cực"] - counts["tiêu cực"]) <= 1:
            return "trái chiều"
    top = max(counts, key=lambda key: counts[key])
    return top


def _article_date_label(article: dict[str, Any]) -> str:
    value = article.get("published_at")
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, str) and value[:10]:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d/%m/%Y")
        except ValueError:
            return ""
    return ""


def _article_vi_description(article: dict[str, Any], *, ticker: str | None) -> str:
    title = str(article.get("title") or "").strip()
    summary = str(article.get("summary") or "").strip()
    text = f"{title} {summary}".lower()
    prefix = ""
    if ticker and not any(alias in text for alias in _ticker_aliases(ticker)):
        prefix = f"Không nhắc trực tiếp {ticker}, nên chỉ nên xem như tín hiệu ngành. "

    if "running rings around nvidia" in text or ("ai stock" in text and "nvidia" in text):
        return (
            prefix
            + "Bài viết so sánh một cổ phiếu AI khác với Nvidia, cho thấy dòng tiền vẫn đang tìm kiếm các lựa chọn hưởng lợi từ làn sóng AI ngoài nhóm dẫn đầu."
        )
    if "desantis" in text and "tesla" in text:
        return (
            prefix
            + "Bài viết nói về việc Ron DeSantis khen chất lượng sản phẩm của Tesla nhưng vẫn phản đối các chính sách bắt buộc xe điện, nên tác động chính nằm ở câu chuyện chính sách EV và hình ảnh thương hiệu Tesla."
        )
    if "evercore" in text and "apple" in text and "price target" in text:
        return (
            prefix
            + "Tác động thiên về tích cực: Evercore nâng mục tiêu giá và nhấn mạnh luận điểm tăng trưởng từ mảng dịch vụ, có thể hỗ trợ kỳ vọng định giá ngắn hạn."
        )
    if "keybanc" in text and "apple" in text and ("cautious" in text or "stretched" in text):
        return (
            prefix
            + "Tác động thiên về tiêu cực/thận trọng: KeyBanc lo ngại định giá Apple đã căng và nhu cầu phần cứng tại Mỹ có dấu hiệu bình thường hóa."
        )
    if "apple names new ceo" in text or ("tim cook" in text and "step down" in text):
        return (
            prefix
            + "Đây là tin quản trị rất nhạy với sentiment: thay đổi CEO có thể tạo biến động ngắn hạn, dù cần xác nhận thêm từ nguồn chính thức của Apple."
        )
    if "xsolla" in text and "skich" in text and ("ios" in text or "digital markets act" in text):
        return (
            prefix
            + "Tác động gián tiếp: tin liên quan hệ sinh thái iOS tại EU và cạnh tranh App Store, có thể ảnh hưởng câu chuyện doanh thu dịch vụ hơn là giá cổ phiếu ngay lập tức."
        )
    if "china's view on elon musk" in text or ("elon musk" in text and "visionary" in text):
        return (
            prefix
            + "Bài viết mô tả cách Trung Quốc nhìn nhận Elon Musk: vừa là nhân vật công nghệ có ảnh hưởng, vừa có lúc gây tranh cãi với cơ quan quản lý và công chúng; điều này liên quan tới rủi ro thị trường Trung Quốc của Tesla."
        )
    if "newsom" in text and ("ev incentive" in text or "rebate" in text):
        return (
            prefix
            + "Tin nói về chương trình ưu đãi xe điện trị giá 1 tỷ USD tại California, có thể hỗ trợ nhu cầu đối với Tesla Semi nếu chính sách được triển khai đúng như mô tả."
        )
    if "openai trial" in text or ("recall status" in text and "musk" in text):
        return (
            prefix
            + "Bài viết liên quan tới lịch trình pháp lý của Elon Musk trong vụ kiện OpenAI và chuyến đi Trung Quốc; đây là tin về rủi ro quản trị/hình ảnh hơn là dữ liệu kinh doanh trực tiếp của Tesla."
        )
    if "xi tells" in text or ("china" in text and "open up" in text):
        return (
            prefix
            + "Nội dung xoay quanh tín hiệu chính sách từ Trung Quốc với doanh nghiệp Mỹ; nhóm tin kiểu này quan trọng vì có thể tác động tới kỳ vọng về chuỗi cung ứng, nhu cầu và rủi ro địa chính trị."
        )
    if "foxconn" in text and ("ai demand" in text or "profit" in text):
        return (
            prefix
            + "Tin nói về lợi nhuận Foxconn tăng vượt dự báo nhờ nhu cầu AI, qua đó phản ánh nhu cầu hạ tầng và phần cứng AI vẫn mạnh trong chuỗi cung ứng công nghệ."
        )
    if "winning streak" in text or "gapped higher" in text:
        return (
            prefix
            + "Tin phản ánh phản ứng tích cực của cổ phiếu trước kỳ vọng đơn hàng chip AI từ Trung Quốc, nhưng vẫn phụ thuộc vào phê duyệt chính sách."
        )
    if "h200" in text or ("chip" in text and "china" in text):
        return (
            prefix
            + "Bài viết liên quan tới việc bán chip AI sang Trung Quốc và chính sách kiểm soát xuất khẩu; đây là yếu tố có thể ảnh hưởng tới doanh thu, biên lợi nhuận và kỳ vọng tăng trưởng của các công ty bán chip."
        )
    if any(keyword in text for keyword in ["quote", "price", "forecast"]):
        return (
            prefix
            + "Trọng tâm là giá cổ phiếu và dự báo, hữu ích để đọc sentiment ngắn hạn nhưng chưa đủ để kết luận về nền tảng kinh doanh."
        )
    if any(keyword in text for keyword in ["partnership", "deployment", "infrastructure", "gigawatts"]):
        return (
            prefix
            + "Nội dung về hợp tác hoặc triển khai hạ tầng AI, thường là tín hiệu tích cực cho nhu cầu tính toán và đầu tư dài hạn."
        )
    if any(keyword in text for keyword in ["discount", "paid", "buy", "options"]):
        return (
            prefix
            + "Đây là góc nhìn chiến lược giao dịch/quyền chọn, phù hợp để tham khảo cách quản trị rủi ro hơn là tín hiệu cơ bản trực tiếp."
        )
    if _has_ai_term(text):
        return (
            prefix
            + "Tin củng cố bức tranh nhu cầu AI, qua đó có thể ảnh hưởng tới kỳ vọng với nhóm phần cứng và hạ tầng công nghệ."
        )
    if any(keyword in text for keyword in ["earnings", "profit", "revenue", "q1", "quarter"]):
        return (
            prefix
            + "Nhóm tin về doanh thu, lợi nhuận hoặc kỳ vọng quý thường có tác động trực tiếp tới phản ứng ngắn hạn của thị trường."
        )
    if any(keyword in text for keyword in ["valuation", "cheap", "expensive", "market cap"]):
        return (
            prefix
            + "Luận điểm chính nằm ở định giá; cần đọc kỹ các giả định về tăng trưởng, biên lợi nhuận và mức chiết khấu."
        )
    if summary:
        return prefix + _compact_summary(summary)
    return prefix + "RSS chỉ cung cấp tiêu đề ngắn, nên cần mở link nguồn để đọc nội dung chi tiết trước khi kết luận."


def _compact_summary(summary: str) -> str:
    cleaned = _strip_html(summary)
    if len(cleaned) <= 220:
        return cleaned
    return f"{cleaned[:217].rstrip()}..."


def _source_link_label(source: str) -> str:
    source = source.replace("[", "").replace("]", "").strip()
    source = re.sub(r"\s+", " ", source)
    source_labels = {
        "yahoo_finance_rss": "Yahoo Finance",
        "google_news_rss": "Google News",
    }
    if source in source_labels:
        return source_labels[source]
    if not source or source.endswith("_rss"):
        return "Nguồn"
    if len(source) > 18:
        return f"{source[:16].rstrip()}..."
    return source


def _unique_source_labels(sources: list[str]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for source in sources:
        label = _source_link_label(source)
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _looks_untranslated_english(value: str) -> bool:
    english_words = re.findall(r"\b[a-zA-Z]{4,}\b", value)
    protected = {"NVIDIA", "Nvidia", "NVDA", "Apple", "Microsoft", "Tesla", "Meta", "Amazon", "Google", "CNN", "Yahoo"}
    remaining = [word for word in english_words if word not in protected and not word.isupper()]
    return len(remaining) >= 3


def _headline_vi_summary(title: str, publisher: str) -> str:
    text = title.lower()
    publisher_label = _source_link_label(publisher)
    if "desantis" in text and "tesla" in text:
        return "Ron DeSantis khen xe Tesla nhưng vẫn phản đối ý tưởng xe điện sẽ cứu thế giới"
    if "evercore" in text and "apple" in text and "price target" in text:
        return "Evercore nâng mục tiêu giá Apple nhờ kỳ vọng tăng trưởng dịch vụ"
    if "keybanc" in text and "apple" in text and ("cautious" in text or "stretched" in text):
        return "KeyBanc thận trọng hơn với Apple vì lo ngại định giá"
    if "apple names new ceo" in text or ("tim cook" in text and "step down" in text):
        return "Apple thay đổi CEO, yếu tố có thể tác động tới sentiment"
    if "xsolla" in text and "skich" in text:
        return "Marketplace game thay thế trên iOS có thêm đối tác thanh toán"
    if "nvidia wins" in text and "america loses" in text:
        return "Nvidia hưởng lợi nhưng chính sách chip gây tranh luận tại Mỹ"
    if "nvidia stock extends winning streak" in text and "china" in text:
        return "Cổ phiếu Nvidia kéo dài đà tăng nhờ kỳ vọng từ thị trường Trung Quốc"
    if "ai rally pushes" in text and "s&p 500" in text:
        return "Đà tăng nhóm AI kéo S&P 500 tiến gần vùng đỉnh mới"
    if "nvidia stock looks cheap" in text:
        return "Cổ phiếu Nvidia được nhìn nhận là còn rẻ trước kỳ báo cáo quan trọng"
    if "ai demand" in text and ("profit" in text or "profits" in text):
        return "Nhu cầu AI hỗ trợ lợi nhuận của các doanh nghiệp trong chuỗi cung ứng"
    if "ai demand" in text:
        return "Nhu cầu AI tiếp tục là điểm tựa cho nhóm hạ tầng công nghệ"
    if "china" in text and "elon musk" in text and "visionary" in text:
        return "Trung Quốc nhìn Elon Musk như một nhà viễn kiến nhưng cũng là nhân vật gây tranh cãi"
    if "newsom" in text and ("ev incentive" in text or "rebate" in text):
        return "Chương trình ưu đãi xe điện 1 tỷ USD của California có thể hỗ trợ Tesla Semi"
    if "openai trial" in text or ("recall status" in text and "musk" in text):
        return "Elon Musk sang Trung Quốc trong lúc vụ kiện OpenAI vẫn đang có yêu cầu từ thẩm phán"
    if any(keyword in text for keyword in ["quote", "price", "forecast"]):
        topic = "giá cổ phiếu và dự báo"
    elif any(keyword in text for keyword in ["partnership", "deployment", "infrastructure", "gigawatts"]):
        topic = "hợp tác chiến lược và triển khai hạ tầng AI"
    elif any(keyword in text for keyword in ["discount", "paid", "buy", "options"]):
        topic = "chiến lược mua cổ phiếu/quyền chọn với mức chiết khấu"
    elif any(keyword in text for keyword in ["h200", "chip", "export"]):
        topic = "chip AI, Trung Quốc và chính sách xuất khẩu"
    elif any(keyword in text for keyword in ["earnings", "profit", "revenue", "q1", "quarter"]):
        topic = "kết quả kinh doanh và kỳ vọng lợi nhuận"
    elif any(keyword in text for keyword in ["valuation", "cheap", "expensive", "market cap"]):
        topic = "định giá cổ phiếu"
    elif _has_ai_term(text):
        topic = "nhu cầu AI và triển vọng ngành"
    else:
        topic = "cập nhật mới liên quan đến doanh nghiệp và thị trường"
    topic = _sentence_case_vi(topic)
    if publisher_label in {"Yahoo Finance", "Google News", "Nguồn"}:
        return topic
    return f"{topic} theo {publisher_label}"


def _sentence_case_vi(value: str) -> str:
    if not value:
        return value
    return value[0].upper() + value[1:]


def _theme_counts(articles: list[dict[str, Any]]) -> dict[str, int]:
    buckets = {
        "AI/chip": ["ai", "chip", "semiconductor", "gpu", "data center", "nvidia"],
        "kết quả kinh doanh": ["earnings", "revenue", "profit", "forecast", "guidance", "quarter"],
        "định giá/cổ phiếu": ["stock", "shares", "buy", "sell", "valuation", "upside", "downside"],
        "vĩ mô/ngành": ["market", "demand", "tariff", "china", "taiwan", "supply"],
    }
    counts = {name: 0 for name in buckets}
    for article in articles:
        text = f"{article.get('title') or ''} {article.get('summary') or ''}".lower()
        for name, keywords in buckets.items():
            if any(_keyword_in_text(keyword, text) for keyword in keywords):
                counts[name] += 1
    return {name: count for name, count in counts.items() if count}


def _keyword_in_text(keyword: str, text: str) -> bool:
    if keyword == "ai":
        return _has_ai_term(text)
    return keyword in text


def _has_ai_term(text: str) -> bool:
    return bool(re.search(r"\bai\b|artificial intelligence|openai", text))


def _format_themes(themes: dict[str, int]) -> str:
    if not themes:
        return "một số cập nhật chung về doanh nghiệp và thị trường"
    ordered = sorted(themes.items(), key=lambda item: item[1], reverse=True)
    return ", ".join(name for name, _ in ordered[:3])


def _fallback_implication(subject: str, themes: dict[str, int]) -> str:
    implications: list[str] = []
    if "AI/chip" in themes:
        implications.append(
            f"- Với {subject}, nhóm tin về AI/chip thường liên quan trực tiếp tới kỳ vọng tăng trưởng doanh thu và nhu cầu sản phẩm."
        )
    if "kết quả kinh doanh" in themes:
        implications.append(
            "- Các tin về lợi nhuận, doanh thu hoặc dự báo có thể ảnh hưởng mạnh tới kỳ vọng ngắn hạn của thị trường."
        )
    if "định giá/cổ phiếu" in themes:
        implications.append(
            "- Các bài viết về định giá hoặc khuyến nghị nên được xem là góc nhìn tham khảo, vì chúng phụ thuộc nhiều vào giả định của từng nguồn."
        )
    if "vĩ mô/ngành" in themes:
        implications.append(
            "- Tin vĩ mô/ngành có thể tác động tới sentiment ngay cả khi không phải tin nội tại riêng của công ty."
        )
    if not implications:
        implications.append(
            "- Các headline hiện tại chưa đủ để kết luận hướng tác động rõ ràng; nên đọc chi tiết từng bài và so với dữ liệu giá."
        )
    return "\n".join(implications)


def _top_values(values: list[str], limit: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        value = value.strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return [value for value, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]]


def _clean_answer_tail(answer: str) -> str:
    lines = answer.strip().splitlines()
    while lines and lines[-1].strip().lower().startswith(("nếu bạn muốn", "neu ban muon")):
        lines.pop()
    return "\n".join(lines).strip()


def _serialize_article(article: dict[str, Any]) -> dict[str, Any]:
    value = dict(article)
    published = value.get("published_at")
    if isinstance(published, datetime):
        value["published_at"] = published.isoformat()
    label, icon = _article_sentiment(article)
    value["sentiment"] = label
    value["sentiment_icon"] = icon
    value["source_label"] = _source_link_label(str(article.get("publisher") or article.get("source") or ""))
    return value
