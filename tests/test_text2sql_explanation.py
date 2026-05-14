from fintextsql.text2sql.service import _deterministic_price_comparison_explanation


def test_price_comparison_explanation_uses_all_rows() -> None:
    rows = [
        {"date": "2026-04-14", "aapl_close": 258.83, "msft_close": 393.11},
        {"date": "2026-04-15", "aapl_close": 266.43, "msft_close": 411.22},
        {"date": "2026-04-16", "aapl_close": 263.40, "msft_close": 420.26},
        {"date": "2026-04-17", "aapl_close": 270.23, "msft_close": 422.79},
    ]

    answer = _deterministic_price_comparison_explanation(rows)

    assert answer is not None
    assert "4 phiên giao dịch" in answer
    assert "2026-04-14" in answer
    assert "2026-04-17" in answer
    assert "MSFT luôn có close price cao hơn AAPL" in answer
    assert "AAPL: từ 258.83 lên 270.23" in answer
    assert "MSFT: từ 393.11 lên 422.79" in answer


def test_long_price_explanation_calculates_pct_change_by_ticker() -> None:
    rows = [
        {"ticker": "AAPL", "date": "2026-04-14", "close": 258.83},
        {"ticker": "MSFT", "date": "2026-04-14", "close": 393.11},
        {"ticker": "NVDA", "date": "2026-04-14", "close": 196.51},
        {"ticker": "AAPL", "date": "2026-05-13", "close": 298.87},
        {"ticker": "MSFT", "date": "2026-05-13", "close": 405.21},
        {"ticker": "NVDA", "date": "2026-05-13", "close": 225.83},
    ]

    answer = _deterministic_price_comparison_explanation(rows)

    assert answer is not None
    assert "6 dòng giá đóng cửa" in answer
    assert "AAPL: 258.83 → 298.87" in answer
    assert "MSFT: 393.11 → 405.21" in answer
    assert "NVDA: 196.51 → 225.83" in answer
    assert "Mã tăng mạnh nhất" in answer
