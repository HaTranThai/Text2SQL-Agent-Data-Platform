from fintextsql.core.planner import TaskPlanner


def test_explicit_ticker_overrides_previous_context() -> None:
    plan = TaskPlanner(
        {
            "active_tickers": ["AAPL", "MSFT"],
            "last_metric": "market_cap",
            "last_time_window": "8 months",
        }
    ).plan("So sánh xu hướng giá đóng cửa của NVDA trong quý gần nhất với quý trước đó.")

    task = plan.tasks[0]
    assert task.tickers == ["NVDA"]
    assert task.metric == "close_price"
    assert task.grouping == "quarter"
    assert task.comparison == "latest_quarter_vs_previous"


def test_additive_ticker_merges_only_for_additive_language() -> None:
    plan = TaskPlanner({"active_tickers": ["AAPL"]}).plan("thêm MSFT vào so sánh")

    assert plan.tasks[0].tickers == ["AAPL", "MSFT"]


def test_news_impact_with_help_word_stays_news() -> None:
    plan = TaskPlanner().plan("help tôi xem tin tức ảnh hưởng đến giá apple")

    assert plan.tasks[0].intent == "news"
    assert plan.tasks[0].tickers == ["AAPL"]
