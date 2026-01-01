"""
Ticker Extraction Module

Robust, explainable multi-ticker extraction with confidence scoring.

Pipeline placement:
    News Fetcher
    → Ticker Extraction + Confidence Scoring (THIS MODULE)
    → Sentiment Model (FinBERT)
    → Signal Aggregation
    → Strategy Engine
"""

from .extractor import TickerExtractor, ExtractionResult, extract_tickers
from .confidence import ConfidenceScorer, AdaptiveConfidenceScorer
from .outcome_tracker import DecisionOutcomeTracker, get_tracker
from .band_analytics import ConfidenceBandAnalytics, get_analytics

__all__ = [
    'TickerExtractor',
    'ExtractionResult',
    'extract_tickers',
    'ConfidenceScorer',
    'AdaptiveConfidenceScorer',
    'DecisionOutcomeTracker',
    'get_tracker',
    'ConfidenceBandAnalytics',
    'get_analytics',
]
