from fintextsql.text2sql.service import (
    _deterministic_exact_ohlc_explanation,
    _deterministic_high_close_explanation,
    _deterministic_latest_close_explanation,
    _deterministic_latest_volume_explanation,
    _deterministic_month_price_data_explanation,
    _deterministic_monthly_high_close_explanation,
    _deterministic_price_comparison_explanation,
    _deterministic_quarterly_close_explanation,
    _exact_ohlc_sql,
    _latest_close_sql,
    _latest_volume_sql,
    _lowest_volume_sql,
    _month_price_data_sql,
    _month_close_sql,
    _outperform_spy_lower_volatility_sql,
    _quarterly_close_comparison_sql,
    _return_volatility_sql,
)


def test_price_comparison_explanation_uses_all_rows() -> None:
    rows = [
        {"date": "2026-04-14", "aapl_close": 258.83, "msft_close": 393.11},
        {"date": "2026-04-15", "aapl_close": 266.43, "msft_close": 411.22},
        {"date": "2026-04-16", "aapl_close": 263.40, "msft_close": 420.26},
        {"date": "2026-04-17", "aapl_close": 270.23, "msft_close": 422.79},
    ]

    answer = _deterministic_price_comparison_explanation("So sánh close price của AAPL và MSFT", rows)

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

    answer = _deterministic_price_comparison_explanation("So sánh close price của AAPL, MSFT, NVDA", rows)

    assert answer is not None
    assert "6 dòng giá đóng cửa" in answer
    assert "AAPL: 258.83 → 298.87" in answer
    assert "MSFT: 393.11 → 405.21" in answer
    assert "NVDA: 196.51 → 225.83" in answer
    assert "Mã tăng mạnh nhất" in answer


def test_high_close_explanation_uses_all_returned_rows_not_only_head() -> None:
    rows = [
        {"ticker": "AAPL", "date": "2025-09-08", "close": 237.88},
        {"ticker": "AAPL", "date": "2025-09-17", "close": 238.99},
        {"ticker": "AAPL", "date": "2026-05-14", "close": 299.12},
    ]

    answer = _deterministic_high_close_explanation(
        "hiển thị giá đóng cửa cao nhất trong 8 tháng qua của AAPL",
        rows,
    )

    assert answer is not None
    assert "299.12" in answer
    assert "2026-05-14" in answer
    assert "2025-09-08 đến 2026-05-14" in answer


def test_monthly_high_close_explanation_groups_daily_rows_by_month() -> None:
    rows = [
        {"ticker": "AAPL", "date": "2025-09-08", "close": 237.88},
        {"ticker": "AAPL", "date": "2025-09-17", "close": 238.99},
        {"ticker": "AAPL", "date": "2025-10-03", "close": 245.20},
        {"ticker": "AAPL", "date": "2026-05-14", "close": 299.12},
    ]

    answer = _deterministic_monthly_high_close_explanation(
        "có chắc cái đó là cao nhất không, hãy liệt kê giá đóng cửa cao nhất trong từng tháng của 8 tháng qua",
        rows,
    )

    assert answer is not None
    assert "2025-09: close cao nhất 238.99 vào 2025-09-17" in answer
    assert "2025-10: close cao nhất 245.20 vào 2025-10-03" in answer
    assert "2026-05: close cao nhất 299.12 vào 2026-05-14" in answer
    assert "không chỉ 8 dòng đầu" in answer


def test_quarterly_close_comparison_sql_uses_only_explicit_ticker() -> None:
    sql = _quarterly_close_comparison_sql(
        "So sánh xu hướng giá đóng cửa của NVDA trong quý gần nhất với quý trước đó.\n\nContext tickers: NVDA",
        2000,
    )

    assert sql is not None
    assert "c.ticker IN ('NVDA')" in sql
    assert "DATE_TRUNC('quarter'" in sql
    assert "market_cap" not in sql


def test_quarterly_close_explanation_compares_latest_and_previous() -> None:
    rows = [
        {
            "ticker": "NVDA",
            "quarter": "2026-01-01",
            "start_date": "2026-01-02",
            "start_close": 190.0,
            "end_date": "2026-03-31",
            "end_close": 210.0,
            "pct_change": 10.53,
        },
        {
            "ticker": "NVDA",
            "quarter": "2026-04-01",
            "start_date": "2026-04-01",
            "start_close": 212.0,
            "end_date": "2026-05-14",
            "end_close": 225.0,
            "pct_change": 6.13,
        },
    ]

    answer = _deterministic_quarterly_close_explanation(
        "So sánh xu hướng giá đóng cửa của NVDA trong quý gần nhất với quý trước đó.",
        rows,
    )

    assert answer is not None
    assert "2026-01-01" in answer
    assert "2026-04-01" in answer
    assert "quý gần nhất yếu hơn" in answer


def test_exact_ohlc_sql_preserves_requested_date() -> None:
    sql = _exact_ohlc_sql("Cho tôi giá mở cửa, cao nhất, thấp nhất và đóng cửa của MSFT vào ngày 2024-01-15.", 500)

    assert sql is not None
    assert "p.date = DATE '2024-01-15'" in sql
    assert "p.open" in sql
    assert "p.high" in sql
    assert "p.low" in sql
    assert "p.close" in sql
    assert "highest_closes" not in sql


def test_ohlc_sql_preserves_requested_date_range() -> None:
    sql = _exact_ohlc_sql(
        "Cho tôi giá mở cửa, cao nhất, thấp nhất và đóng cửa của MSFT từ ngày 2024-01-25 đến ngày 2024-01-30.",
        500,
    )

    assert sql is not None
    assert "p.date >= DATE '2024-01-25'" in sql
    assert "p.date <= DATE '2024-01-30'" in sql
    assert "p.date = DATE '2024-01-25'" not in sql


def test_latest_volume_sql_selects_volume() -> None:
    sql = _latest_volume_sql("Volume giao dịch của TSLA trong 5 phiên gần nhất là bao nhiêu?", 500)

    assert sql is not None
    assert "p.volume" in sql
    assert "rn <= 5" in sql
    assert "p.close" not in sql


def test_lowest_volume_sql_uses_requested_year_and_volume() -> None:
    sql = _lowest_volume_sql("Ngày nào TSLA có volume giao dịch thấp nhất trong năm 2024?", 500)

    assert sql is not None
    assert "c.ticker IN ('TSLA')" in sql
    assert "p.date >= DATE '2024-01-01'" in sql
    assert "p.date < DATE '2025-01-01'" in sql
    assert "p.volume ASC" in sql


def test_month_price_data_sql_returns_full_price_columns() -> None:
    sql = _month_price_data_sql("Hiển thị toàn bộ dữ liệu của NVDA trong tháng 3 năm 2024.", 500)

    assert sql is not None
    assert "p.date >= DATE '2024-03-01'" in sql
    assert "p.date < DATE '2024-04-01'" in sql
    for column in ["p.open", "p.high", "p.low", "p.close", "p.adj_close", "p.volume"]:
        assert column in sql


def test_month_close_sql_uses_requested_month_not_recent_window() -> None:
    sql = _month_close_sql("Cho tôi giá đóng cửa của MSFT trong tháng 1 năm 2024.", 500)

    assert sql is not None
    assert "c.ticker IN ('MSFT')" in sql
    assert "p.date >= DATE '2024-01-01'" in sql
    assert "p.date < DATE '2024-02-01'" in sql
    assert "INTERVAL '366 days'" not in sql


def test_latest_close_sql_handles_recent_close_question() -> None:
    sql = _latest_close_sql("Cho tôi giá đóng cửa của AAPL trong 10 ngày gần nhất", 500)

    assert sql is not None
    assert "c.ticker IN ('AAPL')" in sql
    assert "p.close" in sql
    assert "INTERVAL '10 days'" in sql


def test_deterministic_explanations_do_not_emit_raw_pipe_tables() -> None:
    ohlc_answer = _deterministic_exact_ohlc_explanation(
        "Cho tôi giá mở cửa, cao nhất, thấp nhất và đóng cửa của MSFT vào ngày 2024-01-16.",
        [{"ticker": "MSFT", "date": "2024-01-16", "open": 393.66, "high": 394.03, "low": 387.62, "close": 390.27}],
    )
    volume_answer = _deterministic_latest_volume_explanation(
        "Volume giao dịch của TSLA trong 5 phiên gần nhất là bao nhiêu?",
        [{"ticker": "TSLA", "date": "2026-05-14", "volume": 12000000}],
    )
    month_answer = _deterministic_month_price_data_explanation(
        "Hiển thị toàn bộ dữ liệu của NVDA trong tháng 3 năm 2024.",
        [{"ticker": "NVDA", "date": "2024-03-01", "open": 80, "high": 82, "low": 79, "close": 82}],
    )
    close_answer = _deterministic_latest_close_explanation(
        "Cho tôi giá đóng cửa của AAPL trong 10 ngày gần nhất",
        [{"ticker": "AAPL", "date": "2026-05-14", "close": 297.54}],
    )

    for answer in [ohlc_answer, volume_answer, month_answer, close_answer]:
        assert answer is not None
        assert "|---|" not in answer


def test_return_volatility_sql_uses_daily_return_volatility() -> None:
    sql = _return_volatility_sql(
        "Ticker nào có tỷ lệ return/volatility tốt nhất trong nhóm AAPL, MSFT, NVDA, TSLA?",
        500,
    )

    assert sql is not None
    assert "STDDEV_SAMP(daily_return)" in sql
    assert "return_volatility_ratio" in sql
    assert "c.ticker IN ('AAPL', 'MSFT', 'NVDA', 'TSLA')" in sql


def test_outperform_spy_lower_volatility_sql_uses_prices_not_schema_message() -> None:
    sql = _outperform_spy_lower_volatility_sql(
        "Tìm cổ phiếu outperform SPY trong 1 năm gần nhất nhưng có volatility thấp hơn SPY.",
        500,
    )

    assert sql is not None
    assert "STDDEV_SAMP(daily_return)" in sql
    assert "spy_daily_volatility_pct" in sql
    assert "prices p" in sql
