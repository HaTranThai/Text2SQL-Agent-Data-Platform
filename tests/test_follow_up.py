from fintextsql.api import main as M


def _seed(session_id: str, message: str) -> None:
    M.SESSION_MESSAGES[session_id] = message


def test_compare_follow_up_merges_tickers() -> None:
    _seed("c1", "Giá đóng cửa của AAPL trong 30 ngày gần nhất")
    out = M._rewrite_follow_up("c1", "nếu so nó với GOOG thì sao")
    assert out is not None
    assert "AAPL" in out and "GOOG" in out
    assert "30 ngày" in out


def test_continuation_follow_up_swaps_ticker_keeps_analysis() -> None:
    _seed("c2", "Drawdown lớn nhất của NVDA năm 2024")
    out = M._rewrite_follow_up("c2", "còn TSLA thì sao")
    assert out is not None
    assert "TSLA" in out and "NVDA" not in out
    assert "Drawdown" in out and "2024" in out


def test_metric_swap_follow_up_keeps_ticker_and_window() -> None:
    _seed("c3", "Giá đóng cửa của TSLA trong 90 ngày gần nhất")
    out = M._rewrite_follow_up("c3", "thế còn volume thì sao")
    assert out is not None
    assert "volume" in out.lower()
    assert "TSLA" in out
    assert "90 ngày" in out


def test_pronoun_follow_up_reuses_previous_question() -> None:
    _seed("c4", "Giá đóng cửa của AAPL trong 30 ngày gần nhất")
    assert M._rewrite_follow_up("c4", "tương tự thì sao") == "Giá đóng cửa của AAPL trong 30 ngày gần nhất"


def test_self_sufficient_question_is_not_rewritten() -> None:
    _seed("c5", "Giá đóng cửa của AAPL trong 30 ngày gần nhất")
    assert M._rewrite_follow_up("c5", "So sánh giá AAPL và GOOG") is None


def test_no_session_or_no_history_returns_none() -> None:
    assert M._rewrite_follow_up(None, "còn TSLA thì sao") is None
    assert M._rewrite_follow_up("fresh-session-xyz", "còn TSLA thì sao") is None


def test_result_reference_constrains_to_previous_dates() -> None:
    _seed("c6", "Top 5 phiên có volume cao nhất của NVDA")
    M.SESSION_RESULT["c6"] = {"tickers": ["NVDA"], "dates": ["2024-09-20", "2024-06-21"]}
    out = M._rewrite_follow_up("c6", "trong số đó phiên nào giá đóng cửa cao nhất?")
    assert out is not None
    assert "2024-09-20" in out and "NVDA" in out


def test_result_reference_constrains_to_previous_tickers() -> None:
    _seed("c7", "Tìm các mã có giá đóng cửa mới nhất cao hơn MA20 và MA50")
    M.SESSION_RESULT["c7"] = {"tickers": ["AAPL", "MSFT", "NVDA"], "dates": []}
    out = M._rewrite_follow_up("c7", "trong nhóm đó mã nào có volatility thấp nhất?")
    assert out is not None
    assert "AAPL" in out and "MSFT" in out and "NVDA" in out
