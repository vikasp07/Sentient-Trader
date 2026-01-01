# test/unit/test_strategy.py

from tools.strategy_worker import decide_trade_single


def test_buy_condition():
    """Test BUY with positive sentiment + bullish technical indicators"""
    score = 0.5
    features = {
        "SMA_5": 151,
        "SMA_20": 150,
        "RSI_14": 50,
    }
    assert decide_trade_single(score, features) == "BUY"


def test_sell_condition():
    """Test SELL with negative sentiment + bearish technical indicators"""
    score = -0.5
    features = {
        "SMA_5": 140,
        "SMA_20": 150,
        "RSI_14": 60,
    }
    assert decide_trade_single(score, features) == "SELL"


def test_hold_condition_neutral():
    """Test HOLD with neutral sentiment"""
    score = 0.0
    features = {
        "SMA_5": 150,
        "SMA_20": 150,
        "RSI_14": 50,
    }
    assert decide_trade_single(score, features) == "HOLD"


def test_buy_on_strong_sentiment_only():
    """Test BUY with very strong positive sentiment (no features needed)"""
    score = 0.8  # Strong positive sentiment
    assert decide_trade_single(score, None) == "BUY"
    assert decide_trade_single(score, {}) == "BUY"


def test_sell_on_strong_sentiment_only():
    """Test SELL with very strong negative sentiment (no features needed)"""
    score = -0.7  # Strong negative sentiment
    assert decide_trade_single(score, None) == "SELL"
    assert decide_trade_single(score, {}) == "SELL"


def test_hold_when_sentiment_moderate_no_features():
    """Test HOLD with moderate sentiment and no features"""
    score = 0.5  # Moderate positive - not strong enough for sentiment-only BUY
    assert decide_trade_single(score, None) == "HOLD"
    assert decide_trade_single(score, {}) == "HOLD"


def test_hold_when_score_missing():
    """Test HOLD when sentiment score is missing"""
    features = {
        "SMA_5": 160,
        "SMA_20": 150,
        "RSI_14": 40,
    }
    assert decide_trade_single(None, features) == "HOLD"
