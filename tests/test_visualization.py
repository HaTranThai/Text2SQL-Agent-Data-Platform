from fintextsql.paths.visualization.service import infer_visualization


def test_current_price_prefers_last_price_over_market_cap() -> None:
    rows = [
        {"ticker": "AAPL", "last_price": 298.87, "market_cap": 4_389_609_930_752.0},
        {"ticker": "MSFT", "last_price": 405.21, "market_cap": 3_010_076_082_176.0},
    ]

    viz = infer_visualization("Giá hiện tại của AAPL MSFT", ["ticker", "last_price", "market_cap"], rows)

    assert viz is not None
    assert viz.type == "bar"
    assert viz.x == "ticker"
    assert viz.y == "last_price"


def test_market_cap_prefers_market_cap() -> None:
    rows = [
        {"ticker": "AAPL", "last_price": 298.87, "market_cap": 4_389_609_930_752.0},
        {"ticker": "MSFT", "last_price": 405.21, "market_cap": 3_010_076_082_176.0},
    ]

    viz = infer_visualization("Top companies by market cap", ["ticker", "last_price", "market_cap"], rows)

    assert viz is not None
    assert viz.type == "bar"
    assert viz.x == "ticker"
    assert viz.y == "market_cap"


def test_does_not_use_text_column_as_y_axis() -> None:
    rows = [{"ticker": "AAPL", "title": "Apple headline"}]

    viz = infer_visualization("Tin mới về AAPL", ["ticker", "title"], rows)

    assert viz is None
