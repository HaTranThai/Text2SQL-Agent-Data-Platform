from fintextsql.core.intent import IntentRouter


def test_routes_visualization_vietnamese() -> None:
    decision = IntentRouter().route("So sánh close price của AAPL và MSFT trong 30 ngày")

    assert decision.intent == "text_to_sql"
    assert decision.tickers == ["AAPL", "MSFT"]


def test_routes_ingestion() -> None:
    decision = IntentRouter().route("ingest AAPL MSFT 1y")

    assert decision.intent == "ingestion"
    assert decision.tickers == ["AAPL", "MSFT"]


def test_defaults_to_text_to_sql() -> None:
    decision = IntentRouter().route("top 5 companies by market cap")

    assert decision.intent == "text_to_sql"


def test_routes_misspelled_apple_news_to_aapl() -> None:
    decision = IntentRouter().route("các tin tức có thể ảnh hưởng đến giá của apply")

    assert decision.intent == "news"
    assert decision.tickers == ["AAPL"]


def test_routes_capability_question_to_general() -> None:
    decision = IntentRouter().route("Bạn có thể làm được những gì ?")

    assert decision.intent == "general"
    assert decision.tickers == []


def test_routes_how_to_use_to_general() -> None:
    decision = IntentRouter().route("hướng dẫn tôi cách dùng hệ thống này")

    assert decision.intent == "general"


def test_help_word_does_not_override_finance_news_intent() -> None:
    decision = IntentRouter().route("help tôi xem tin tức ảnh hưởng đến giá apple")

    assert decision.intent == "news"
    assert decision.tickers == ["AAPL"]


def test_capability_wording_with_chart_and_ticker_stays_visualization() -> None:
    decision = IntentRouter().route("bạn có thể vẽ biểu đồ giá đóng cửa AAPL không")

    assert decision.intent == "visualization"
    assert decision.tickers == ["AAPL"]


def test_explicit_nvda_quarterly_close_question_stays_text_to_sql() -> None:
    decision = IntentRouter().route("So sánh xu hướng giá đóng cửa của NVDA trong quý gần nhất với quý trước đó.")

    assert decision.intent == "text_to_sql"
    assert decision.tickers == ["NVDA"]
