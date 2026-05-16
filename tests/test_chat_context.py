from fintextsql.api.main import (
    SESSION_HISTORY,
    SESSION_STATE,
    SESSION_TICKERS,
    _conversation_context_text,
    _is_follow_up_ticker_reference,
    _message_with_context_tickers,
    _remember_session_turn,
    _resolve_context_tickers,
)
from fintextsql.api.schemas import ChatResponse
from fintextsql.core.intent import RouteDecision
from fintextsql.core.tickers import extract_tickers


def test_follow_up_ticker_reference_reuses_session_tickers() -> None:
    session_id = "test-session"
    SESSION_TICKERS.pop(session_id, None)

    assert _resolve_context_tickers(session_id, "So sanh AAPL va MSFT", ["AAPL", "MSFT"]) == [
        "AAPL",
        "MSFT",
    ]
    assert _resolve_context_tickers(session_id, "% tang giam 30 ngay cua 2 ma tren", []) == [
        "AAPL",
        "MSFT",
    ]


def test_context_tickers_are_added_to_effective_message() -> None:
    message = _message_with_context_tickers("% tang giam 30 ngay cua 2 ma tren", ["AAPL", "MSFT"], [])

    assert "AAPL, MSFT" in message
    assert "from" not in message.lower()


def test_additive_ticker_reference_merges_previous_and_new_tickers() -> None:
    session_id = "test-additive-session"
    SESSION_TICKERS.pop(session_id, None)

    _resolve_context_tickers(session_id, "So sanh close price cua AAPL va MSFT", ["AAPL", "MSFT"])
    tickers = _resolve_context_tickers(session_id, "so sanh voi ma NVDA nua cho toi", ["NVDA"])

    assert tickers == ["AAPL", "MSFT", "NVDA"]
    message = _message_with_context_tickers(
        "so sanh voi ma NVDA nua cho toi",
        tickers,
        ["NVDA"],
        conversation_context="turn 1 user question: So sanh close price cua AAPL va MSFT trong 30 ngay gan nhat",
    )
    assert "AAPL, MSFT, NVDA" in message
    assert "30 ngay" in message


def test_ticker_extraction_ignores_vietnamese_helper_word_cho() -> None:
    assert extract_tickers("so sanh voi ma NVDA nua cho toi") == ["NVDA"]


def test_ticker_extraction_accepts_common_company_aliases_and_typos() -> None:
    assert extract_tickers("tin tức ảnh hưởng đến giá của apply") == ["AAPL"]
    assert extract_tickers("so sánh apple với microsoft và nvidia") == ["AAPL", "MSFT", "NVDA"]


def test_conversation_context_is_included_for_follow_up_messages() -> None:
    session_id = "test-context-window"
    SESSION_HISTORY.pop(session_id, None)
    SESSION_STATE.pop(session_id, None)
    SESSION_TICKERS.pop(session_id, None)

    response = ChatResponse(
        intent="text_to_sql",
        answer="Compared AAPL and MSFT close prices.",
        sql="SELECT date, aapl_close, msft_close FROM prices",
        rows=[{"date": "2026-04-14", "aapl_close": 258.83, "msft_close": 393.11}],
        columns=["date", "aapl_close", "msft_close"],
    )
    _remember_session_turn(
        session_id,
        "So sanh close price cua AAPL va MSFT trong 30 ngay gan nhat",
        response,
        RouteDecision("text_to_sql", "medium", ["AAPL", "MSFT"], "test"),
    )

    context = _conversation_context_text(session_id)
    tickers = _resolve_context_tickers(session_id, "so sanh voi ma NVDA nua cho toi", ["NVDA"])
    message = _message_with_context_tickers(
        "so sanh voi ma NVDA nua cho toi",
        tickers,
        ["NVDA"],
        conversation_context=context,
    )

    assert tickers == ["AAPL", "MSFT", "NVDA"]
    assert "AAPL, MSFT, NVDA" in message
    assert "30 ngay" in message
    assert "aapl_close, msft_close" in message


def test_ticker_extraction_only_reads_uppercase_symbols_from_context() -> None:
    assert extract_tickers("recent conversation user asked from context AAPL MSFT") == ["AAPL", "MSFT"]


def test_verification_follow_up_reuses_previous_single_ticker() -> None:
    session_id = "test-aapl-verification-followup"
    SESSION_HISTORY.pop(session_id, None)
    SESSION_TICKERS.pop(session_id, None)

    _remember_session_turn(
        session_id,
        "hiển thị giá đóng cửa cao nhất trong 8 tháng qua của AAPL",
        ChatResponse(
            intent="text_to_sql",
            answer="Giá đóng cửa cao nhất của AAPL là 250.",
            sql="SELECT date, close FROM prices WHERE ticker = 'AAPL' ORDER BY close DESC LIMIT 1",
            rows=[{"date": "2026-05-01", "close": 250}],
            columns=["date", "close"],
        ),
        RouteDecision("text_to_sql", "medium", ["AAPL"], "test"),
    )

    follow_up = "có chắc cái đó là cao nhất không, hãy liệt kê giá đóng cửa cao nhất trong từng tháng của 8 tháng qua"
    tickers = _resolve_context_tickers(session_id, follow_up, [])
    message = _message_with_context_tickers(
        follow_up,
        tickers,
        [],
        conversation_context=_conversation_context_text(session_id),
    )

    assert _is_follow_up_ticker_reference(follow_up)
    assert tickers == ["AAPL"]
    assert "Context tickers: AAPL" in message
    assert "8 tháng" in message


def test_structured_state_is_added_to_follow_up_context() -> None:
    session_id = "test-structured-state"
    SESSION_HISTORY.pop(session_id, None)
    SESSION_STATE.pop(session_id, None)
    SESSION_TICKERS.pop(session_id, None)

    _remember_session_turn(
        session_id,
        "hiển thị giá đóng cửa cao nhất trong 8 tháng qua của AAPL",
        ChatResponse(
            intent="text_to_sql",
            answer="Giá đóng cửa cao nhất của AAPL là 298.87.",
            sql="SELECT ticker, date, close FROM highest_closes",
            rows=[{"ticker": "AAPL", "date": "2026-05-13", "close": 298.87}],
            columns=["ticker", "date", "close"],
        ),
        RouteDecision("text_to_sql", "medium", ["AAPL"], "test"),
    )

    context = _conversation_context_text(session_id)
    follow_up = "có chắc cái đó không, liệt kê từng tháng"
    message = _message_with_context_tickers(
        follow_up,
        ["AAPL"],
        [],
        conversation_context=context,
    )

    assert "current structured state" in context
    assert "metric=highest_close" in context
    assert "time_window=8 months" in context
    assert "Structured follow-up contract" in message
    assert "Use only these tickers: AAPL" in message
