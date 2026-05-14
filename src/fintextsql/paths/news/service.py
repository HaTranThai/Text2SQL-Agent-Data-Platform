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
NEWS_ANALYSIS_TIMEOUT_SECONDS = 12
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
                                (
                                    "You are a finance news analyst. Answer in Vietnamese like a normal chatbot. "
                                    "Use only the provided article titles/summaries. Do not invent prices, ratings, "
                                    "or facts not present in the articles. Translate article titles into Vietnamese "
                                    "when you mention them. When mentioning a specific article, write the Vietnamese "
                                    "title as plain text, then put a short Markdown source link immediately after it. "
                                    "Be concise but analytical."
                                ),
                            ),
                            LLMMessage(
                                "user",
                                "\n\n".join(
                                    [
                                        f"User question: {question}",
                                        f"Ticker/topic: {ticker or 'market'}",
                                        "Articles:",
                                        _articles_context(articles),
                                        (
                                            "Write an answer with: 1) short overview, 2) key themes, "
                                            "3) likely implications for the ticker/topic, 4) caveats. "
                                            "Mention the most relevant publishers briefly. Do not output English "
                                            "headlines verbatim; translate them into Vietnamese first. In the key "
                                            "article list, use this format: Vietnamese title [Source](article URL)."
                                        ),
                                    ]
                                ),
                            ),
                        ],
                        temperature=0.2,
                        max_tokens=700,
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
            query = quote(f"{ticker} stock when:14d")
            return [
                (
                    "https://feeds.finance.yahoo.com/rss/2.0/headline"
                    f"?s={quote(ticker)}&region=US&lang=en-US",
                    "yahoo_finance_rss",
                ),
                (f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en", "google_news_rss"),
            ]
        return [
            ("https://finance.yahoo.com/news/rssindex", "yahoo_finance_rss"),
            (
                "https://news.google.com/rss/search?q=stock%20market%20finance%20when:14d&hl=en-US&gl=US&ceid=US:en",
                "google_news_rss",
            ),
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
        key = str(article.get("link") or article.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(article)
    return deduped


def _filter_relevant_articles(articles: list[dict[str, Any]], *, ticker: str | None) -> list[dict[str, Any]]:
    if not ticker:
        return articles
    aliases = _ticker_aliases(ticker)
    relevant = [
        article
        for article in articles
        if any(alias in _article_text(article) for alias in aliases)
    ]
    return relevant if len(relevant) >= 3 else articles


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


def _articles_context(articles: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, article in enumerate(articles[:8], start=1):
        parts = [
            f"{index}. title: {article.get('title')}",
            f"url: {article.get('link')}",
            f"publisher: {article.get('publisher') or article.get('source')}",
            f"published_at: {article.get('published_at') or 'unknown'}",
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
    top_articles = [_article_bullet(article, ticker=ticker) for article in articles[:4] if article.get("title")]

    lines = [
        f"Mình lấy được {len(articles)} tin gần đây về {subject}. Nhìn nhanh, các tin đang xoay quanh "
        f"{_format_themes(themes)}.",
        "",
        "### Điểm chính",
    ]
    for article_line in top_articles:
        lines.append(f"- {article_line}")

    lines.extend(
        [
            "",
            "### Diễn giải",
            _fallback_implication(subject, themes),
            "",
            "### Lưu ý",
            "- Đây là phân tích từ tiêu đề và mô tả ngắn của nguồn tin RSS, không phải khuyến nghị mua bán.",
            "- Nên đối chiếu thêm báo cáo tài chính, diễn biến giá và tin chính thức của công ty trước khi ra quyết định.",
        ]
    )
    if top_publishers:
        lines.append(f"- Nguồn nổi bật: {', '.join(top_publishers)}.")
    return "\n".join(lines)


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
    if not link:
        return headline
    source = str(article.get("publisher") or article.get("source") or "Nguồn").strip()
    return f"{headline} [{_source_link_label(source)}]({link})"


def _article_bullet(article: dict[str, Any], *, ticker: str | None) -> str:
    return f"{_translated_article_reference(article)}: {_article_vi_description(article, ticker=ticker)}"


def _article_vi_description(article: dict[str, Any], *, ticker: str | None) -> str:
    title = str(article.get("title") or "").strip()
    summary = str(article.get("summary") or "").strip()
    text = f"{title} {summary}".lower()
    prefix = ""
    if ticker and not any(alias in text for alias in _ticker_aliases(ticker)):
        prefix = f"Tin này không nhắc trực tiếp tới {ticker}, nhưng có thể ảnh hưởng tới sentiment ngành. "

    if "running rings around nvidia" in text or ("ai stock" in text and "nvidia" in text):
        return (
            prefix
            + "Bài viết so sánh một cổ phiếu AI khác với Nvidia, hàm ý nhà đầu tư đang tìm kiếm các lựa chọn hưởng lợi từ làn sóng AI ngoài nhóm dẫn đầu quen thuộc."
        )
    if "desantis" in text and "tesla" in text:
        return (
            prefix
            + "Bài viết nói về việc Ron DeSantis khen chất lượng sản phẩm của Tesla nhưng vẫn phản đối các chính sách bắt buộc xe điện, nên tác động chính nằm ở câu chuyện chính sách EV và hình ảnh thương hiệu Tesla."
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
    if "h200" in text or ("chip" in text and "china" in text):
        return (
            prefix
            + "Bài viết liên quan tới việc bán chip AI sang Trung Quốc và chính sách kiểm soát xuất khẩu; đây là yếu tố có thể ảnh hưởng tới doanh thu, biên lợi nhuận và kỳ vọng tăng trưởng của các công ty bán chip."
        )
    if any(keyword in text for keyword in ["quote", "price", "forecast"]):
        return (
            prefix
            + "Bài viết tập trung vào giá cổ phiếu và dự báo, phù hợp để tham khảo sentiment thị trường nhưng cần đối chiếu với dữ liệu giá và báo cáo tài chính."
        )
    if any(keyword in text for keyword in ["partnership", "deployment", "infrastructure", "gigawatts"]):
        return (
            prefix
            + "Tin nói về hợp tác hoặc triển khai hạ tầng AI, thường phản ánh kế hoạch mở rộng năng lực tính toán và nhu cầu đầu tư dài hạn."
        )
    if any(keyword in text for keyword in ["discount", "paid", "buy", "options"]):
        return (
            prefix
            + "Nội dung có thiên hướng chiến lược giao dịch hoặc quyền chọn, nên xem là góc nhìn tham khảo về cách tiếp cận rủi ro/lợi nhuận hơn là tín hiệu cơ bản trực tiếp."
        )
    if any(keyword in text for keyword in ["earnings", "profit", "revenue", "q1", "quarter"]):
        return (
            prefix
            + "Bài viết liên quan tới kết quả kinh doanh, doanh thu hoặc lợi nhuận; đây là nhóm tin có thể ảnh hưởng trực tiếp tới kỳ vọng ngắn hạn của thị trường."
        )
    if any(keyword in text for keyword in ["valuation", "cheap", "expensive", "market cap"]):
        return (
            prefix
            + "Tin tập trung vào định giá cổ phiếu, thường dựa trên giả định tăng trưởng và lợi nhuận tương lai nên cần đọc kỹ luận điểm phía sau."
        )
    if _has_ai_term(text):
        return (
            prefix
            + "Bài viết xoay quanh nhu cầu AI hoặc triển vọng ngành công nghệ, có thể tác động tới nhóm cổ phiếu hưởng lợi từ hạ tầng AI."
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
    if not source or source.endswith("_rss"):
        return "Nguồn"
    if len(source) > 18:
        return f"{source[:16].rstrip()}..."
    return source


def _looks_untranslated_english(value: str) -> bool:
    english_words = re.findall(r"\b[a-zA-Z]{4,}\b", value)
    protected = {"NVIDIA", "Nvidia", "NVDA", "Apple", "Microsoft", "Tesla", "Meta", "Amazon", "Google", "CNN", "Yahoo"}
    remaining = [word for word in english_words if word not in protected and not word.isupper()]
    return len(remaining) >= 3


def _headline_vi_summary(title: str, publisher: str) -> str:
    text = title.lower()
    if "desantis" in text and "tesla" in text:
        return "Ron DeSantis khen xe Tesla nhưng vẫn phản đối ý tưởng xe điện sẽ cứu thế giới"
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
    elif any(keyword in text for keyword in ["earnings", "profit", "revenue", "q1", "quarter"]):
        topic = "kết quả kinh doanh và kỳ vọng lợi nhuận"
    elif any(keyword in text for keyword in ["h200", "chip", "export"]):
        topic = "chip AI, Trung Quốc và chính sách xuất khẩu"
    elif any(keyword in text for keyword in ["valuation", "cheap", "expensive", "market cap"]):
        topic = "định giá cổ phiếu"
    elif _has_ai_term(text):
        topic = "nhu cầu AI và triển vọng ngành"
    else:
        topic = "cập nhật mới liên quan đến doanh nghiệp và thị trường"
    return f"Tin từ {publisher}: {topic}"


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
    return value
