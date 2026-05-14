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
