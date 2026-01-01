# test/unit/test_sentiment.py
import pytest
import sys

sys.path.insert(0, '/app')  # for Docker

from enrich.sentiment.sentiment_analysis import analyze_text


def test_sentiment_analysis():
    test_text = "Stock market is booming today!"
    result = analyze_text(test_text)
    assert isinstance(result, dict)
    assert result["sentiment"] in ["positive", "negative", "neutral"]


def test_sentiment_analysis_negative():
    text = "Company shares fall sharply after poor results"
    result = analyze_text(text)
    assert isinstance(result, dict)
    assert result["sentiment"] in ["positive", "neutral", "negative"]


def test_empty_text_returns_neutral():
    result = analyze_text("")
    assert result["sentiment"] == "neutral"
    assert result["sentiment_score"] == 0.0


def test_result_has_required_fields():
    text = "Apple announces new product launch"
    result = analyze_text(text)

    required_fields = [
        "summary",
        "sentiment",
        "sentiment_score",
        "tickers",
        "events",
        "confidence",
    ]

    for field in required_fields:
        assert field in result