# evaluation/__init__.py
"""
Strategy Evaluation Module

Provides:
- Outcome labeling with price tracking
- Strategy performance metrics
- Analytics and reporting
"""

try:
    from evaluation.outcome_labeler import OutcomeLabeler, get_labeler
    from evaluation.metrics import StrategyMetrics, get_metrics
except ImportError:
    # Handle case where module is imported before all dependencies available
    OutcomeLabeler = None
    get_labeler = None
    StrategyMetrics = None
    get_metrics = None

__all__ = [
    'OutcomeLabeler',
    'get_labeler',
    'StrategyMetrics', 
    'get_metrics',
]
