from datetime import datetime, timezone

from fintextsql.paths.news.service import _fallback_news_analysis


def test_fallback_news_analysis_uses_professional_source_labels() -> None:
    answer = _fallback_news_analysis(
        ticker="NVDA",
        articles=[
            {
                "ticker": "NVDA",
                "title": "Why Nvidia stock looks cheap ahead of its high-stakes earnings report this month",
                "publisher": "Yahoo Finance",
                "link": "https://example.com/nvda-earnings",
                "published_at": datetime(2026, 5, 14, tzinfo=timezone.utc),
                "summary": "Nvidia investors are watching earnings and valuation.",
                "source": "yahoo_finance_rss",
            },
            {
                "ticker": "NVDA",
                "title": "AI demand sends supplier profits higher",
                "publisher": "yahoo_finance_rss",
                "link": "https://example.com/ai-demand",
                "published_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
                "summary": "The article discusses artificial intelligence demand across hardware suppliers.",
                "source": "yahoo_finance_rss",
            },
        ],
    )

    assert "Tin từ yahoo_finance_rss" not in answer
    assert "[Yahoo Finance](https://example.com/ai-demand)" in answer
    assert "### Tin đáng chú ý" in answer
    assert "### Nhận định" in answer
