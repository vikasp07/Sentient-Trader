"""
Decision Outcome Tracker

Tracks and analyzes extraction decisions for continuous learning:
- Stores every decision with outcome
- Analyzes historical accuracy by method, ticker, source
- Dynamically adjusts confidence weights based on performance
- Provides statistical tuning recommendations

This implements the "maturity requirement" - system learns over time
without requiring reinforcement learning.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class DecisionRecord:
    """A single extraction decision with outcome tracking."""
    symbol: str
    confidence: float
    decision: str  # BUY, SELL, HOLD
    extraction_methods: List[str]  # explicit, company_name, nlp_entity
    reasons: List[str]
    article_id: str
    headline: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    # Outcome fields (filled later)
    return_1h: Optional[float] = None
    return_24h: Optional[float] = None
    was_correct: Optional[bool] = None  # Did prediction match price movement?
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'DecisionRecord':
        return cls(**data)


@dataclass 
class MethodStats:
    """Statistics for an extraction method."""
    total: int = 0
    correct: int = 0
    total_confidence: float = 0.0
    avg_return: float = 0.0
    
    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total > 0 else 0.5
    
    @property
    def avg_confidence(self) -> float:
        return self.total_confidence / self.total if self.total > 0 else 0.0


class DecisionOutcomeTracker:
    """
    Tracks extraction decisions and their outcomes for learning.
    
    Key features:
    - Store every decision with full context
    - Track outcomes (price changes)
    - Analyze accuracy by method, ticker, confidence level
    - Recommend weight adjustments
    """
    
    # Default method weights (will be adjusted based on performance)
    DEFAULT_METHOD_WEIGHTS = {
        'explicit': 0.5,
        'company_name': 0.3,
        'nlp_entity': 0.2,
    }
    
    def __init__(
        self,
        outcomes_dir: str = None,
        redis_client: Optional[redis.Redis] = None,
        min_samples_for_tuning: int = 20
    ):
        """
        Initialize the tracker.
        
        Args:
            outcomes_dir: Directory to store outcome files
            redis_client: Redis for caching
            min_samples_for_tuning: Minimum samples before adjusting weights
        """
        self.outcomes_dir = Path(outcomes_dir) if outcomes_dir else Path(__file__).parent.parent.parent / "data" / "outcomes"
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        
        self.redis = redis_client
        self.min_samples = min_samples_for_tuning
        
        # In-memory statistics
        self._method_stats: Dict[str, MethodStats] = {
            'explicit': MethodStats(),
            'company_name': MethodStats(),
            'nlp_entity': MethodStats(),
        }
        
        self._ticker_stats: Dict[str, MethodStats] = defaultdict(MethodStats)
        self._confidence_bands: Dict[str, MethodStats] = {
            'low_0.5-0.6': MethodStats(),
            'medium_0.6-0.7': MethodStats(),
            'high_0.7-0.8': MethodStats(),
            'very_high_0.8-1.0': MethodStats(),
        }
        
        # Current optimized weights
        self._optimized_weights: Dict[str, float] = self.DEFAULT_METHOD_WEIGHTS.copy()
        self._confidence_threshold: float = 0.5
        
        # Load existing statistics
        self._load_stats()
    
    def record_decision(
        self,
        symbol: str,
        confidence: float,
        decision: str,
        extraction_methods: List[str],
        reasons: List[str],
        article_id: str,
        headline: str
    ) -> DecisionRecord:
        """
        Record a new extraction decision.
        
        Args:
            symbol: Stock ticker
            confidence: Confidence score (0-1)
            decision: BUY/SELL/HOLD
            extraction_methods: Methods that extracted this symbol
            reasons: Reasoning for extraction
            article_id: Article identifier
            headline: Article headline
        
        Returns:
            DecisionRecord object
        """
        record = DecisionRecord(
            symbol=symbol,
            confidence=confidence,
            decision=decision,
            extraction_methods=extraction_methods,
            reasons=reasons,
            article_id=article_id,
            headline=headline
        )
        
        # Store to file
        self._store_decision(record)
        
        # Cache in Redis if available
        if self.redis:
            try:
                key = f"decision:{symbol}:{article_id}"
                self.redis.setex(key, 86400 * 7, json.dumps(record.to_dict()))
            except Exception as e:
                logger.warning(f"Failed to cache decision: {e}")
        
        logger.info(f"Recorded decision: {symbol} {decision} conf={confidence:.2f}")
        
        return record
    
    def record_outcome(
        self,
        symbol: str,
        article_id: str,
        return_1h: float,
        return_24h: float
    ):
        """
        Record the outcome of a previous decision.
        
        Args:
            symbol: Stock ticker
            article_id: Article identifier
            return_1h: 1-hour price return (%)
            return_24h: 24-hour price return (%)
        """
        # Try to find the original decision in Redis
        decision_data = None
        if self.redis:
            try:
                key = f"decision:{symbol}:{article_id}"
                cached = self.redis.get(key)
                if cached:
                    decision_data = json.loads(cached)
            except Exception:
                pass
        
        if not decision_data:
            # Create minimal outcome record
            decision_data = {
                'symbol': symbol,
                'article_id': article_id,
                'extraction_methods': ['unknown'],
                'confidence': 0.5,
                'decision': 'UNKNOWN'
            }
        
        # Determine if prediction was correct
        # BUY is correct if price went up, SELL if price went down
        decision = decision_data.get('decision', 'HOLD')
        was_correct = False
        
        if decision == 'BUY' and return_24h > 0:
            was_correct = True
        elif decision == 'SELL' and return_24h < 0:
            was_correct = True
        elif decision == 'HOLD' and abs(return_24h) < 1.0:
            was_correct = True
        
        # Update statistics
        methods = decision_data.get('extraction_methods', [])
        confidence = decision_data.get('confidence', 0.5)
        
        for method in methods:
            if method in self._method_stats:
                stats = self._method_stats[method]
                stats.total += 1
                stats.total_confidence += confidence
                stats.avg_return = (stats.avg_return * (stats.total - 1) + return_24h) / stats.total
                if was_correct:
                    stats.correct += 1
        
        # Update ticker stats
        ticker_stats = self._ticker_stats[symbol]
        ticker_stats.total += 1
        ticker_stats.total_confidence += confidence
        ticker_stats.avg_return = (ticker_stats.avg_return * (ticker_stats.total - 1) + return_24h) / ticker_stats.total
        if was_correct:
            ticker_stats.correct += 1
        
        # Update confidence band stats
        band = self._get_confidence_band(confidence)
        band_stats = self._confidence_bands[band]
        band_stats.total += 1
        band_stats.total_confidence += confidence
        if was_correct:
            band_stats.correct += 1
        
        # Store outcome
        outcome_record = {
            'symbol': symbol,
            'article_id': article_id,
            'confidence': confidence,
            'decision': decision,
            'return_1h': return_1h,
            'return_24h': return_24h,
            'was_correct': was_correct,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        self._store_outcome(outcome_record)
        self._save_stats()
        
        # Check if we should retune weights
        total_samples = sum(s.total for s in self._method_stats.values())
        if total_samples > 0 and total_samples % 50 == 0:
            self._retune_weights()
        
        logger.info(f"Outcome recorded: {symbol} return_24h={return_24h:.2f}% correct={was_correct}")
    
    def _get_confidence_band(self, confidence: float) -> str:
        """Get confidence band for a score."""
        if confidence < 0.6:
            return 'low_0.5-0.6'
        elif confidence < 0.7:
            return 'medium_0.6-0.7'
        elif confidence < 0.8:
            return 'high_0.7-0.8'
        else:
            return 'very_high_0.8-1.0'
    
    def _store_decision(self, record: DecisionRecord):
        """Store decision to file."""
        date_str = datetime.utcnow().strftime("%Y%m%d")
        filepath = self.outcomes_dir / f"decisions_{date_str}.jsonl"
        
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to store decision: {e}")
    
    def _store_outcome(self, outcome: dict):
        """Store outcome to file."""
        date_str = datetime.utcnow().strftime("%Y%m%d")
        filepath = self.outcomes_dir / f"outcomes_{date_str}.jsonl"
        
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(outcome) + '\n')
        except Exception as e:
            logger.error(f"Failed to store outcome: {e}")
    
    def _load_stats(self):
        """Load statistics from file."""
        stats_file = self.outcomes_dir / "stats.json"
        
        if stats_file.exists():
            try:
                with open(stats_file, 'r') as f:
                    data = json.load(f)
                
                # Load method stats
                for method, stats_data in data.get('method_stats', {}).items():
                    if method in self._method_stats:
                        self._method_stats[method] = MethodStats(**stats_data)
                
                # Load optimized weights
                self._optimized_weights = data.get('optimized_weights', self.DEFAULT_METHOD_WEIGHTS.copy())
                self._confidence_threshold = data.get('confidence_threshold', 0.5)
                
                logger.info(f"Loaded statistics from {stats_file}")
                
            except Exception as e:
                logger.error(f"Failed to load stats: {e}")
    
    def _save_stats(self):
        """Save statistics to file."""
        stats_file = self.outcomes_dir / "stats.json"
        
        try:
            data = {
                'method_stats': {
                    method: asdict(stats) 
                    for method, stats in self._method_stats.items()
                },
                'confidence_bands': {
                    band: asdict(stats)
                    for band, stats in self._confidence_bands.items()
                },
                'optimized_weights': self._optimized_weights,
                'confidence_threshold': self._confidence_threshold,
                'last_updated': datetime.utcnow().isoformat()
            }
            
            with open(stats_file, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save stats: {e}")
    
    def _retune_weights(self):
        """
        Retune method weights based on historical accuracy.
        
        Uses statistical weight tuning (no RL):
        - Calculate accuracy per method
        - Adjust weights proportionally
        - Raise threshold if low-confidence decisions underperform
        """
        logger.info("Retuning extraction weights based on outcomes...")
        
        # Check if we have enough samples
        total_samples = sum(s.total for s in self._method_stats.values())
        if total_samples < self.min_samples:
            logger.info(f"Insufficient samples for tuning: {total_samples}/{self.min_samples}")
            return
        
        # Calculate accuracy-weighted adjustments
        method_accuracies = {}
        for method, stats in self._method_stats.items():
            if stats.total >= 5:  # Need minimum samples per method
                method_accuracies[method] = stats.accuracy
        
        if not method_accuracies:
            return
        
        # Normalize weights based on accuracy
        total_accuracy = sum(method_accuracies.values())
        if total_accuracy > 0:
            for method in method_accuracies:
                # Blend current weight with accuracy-based weight
                accuracy_weight = method_accuracies[method] / total_accuracy
                old_weight = self._optimized_weights.get(method, 0.3)
                
                # Gradual adjustment (70% old, 30% new)
                new_weight = 0.7 * old_weight + 0.3 * accuracy_weight
                
                # Clamp to reasonable range
                self._optimized_weights[method] = max(0.1, min(0.6, new_weight))
                
                logger.info(f"Method {method}: accuracy={method_accuracies[method]:.2f}, "
                          f"weight {old_weight:.3f} → {self._optimized_weights[method]:.3f}")
        
        # Adjust confidence threshold based on low-band performance
        low_band = self._confidence_bands['low_0.5-0.6']
        if low_band.total >= 10:
            low_accuracy = low_band.accuracy
            
            if low_accuracy < 0.4:
                # Low confidence decisions are underperforming, raise threshold
                self._confidence_threshold = min(0.7, self._confidence_threshold + 0.05)
                logger.info(f"Raising confidence threshold to {self._confidence_threshold:.2f} "
                          f"(low-band accuracy: {low_accuracy:.2f})")
            elif low_accuracy > 0.6:
                # Low confidence decisions are good, can lower threshold
                self._confidence_threshold = max(0.4, self._confidence_threshold - 0.02)
                logger.info(f"Lowering confidence threshold to {self._confidence_threshold:.2f} "
                          f"(low-band accuracy: {low_accuracy:.2f})")
        
        self._save_stats()
    
    def get_optimized_weights(self) -> Dict[str, float]:
        """Get current optimized method weights."""
        return self._optimized_weights.copy()
    
    def get_confidence_threshold(self) -> float:
        """Get current confidence threshold."""
        return self._confidence_threshold
    
    def get_method_stats(self) -> Dict[str, dict]:
        """Get statistics for all extraction methods."""
        return {
            method: {
                'total': stats.total,
                'correct': stats.correct,
                'accuracy': round(stats.accuracy, 3),
                'avg_confidence': round(stats.avg_confidence, 3),
                'avg_return': round(stats.avg_return, 2)
            }
            for method, stats in self._method_stats.items()
        }
    
    def get_confidence_band_stats(self) -> Dict[str, dict]:
        """Get statistics by confidence band."""
        return {
            band: {
                'total': stats.total,
                'correct': stats.correct,
                'accuracy': round(stats.accuracy, 3) if stats.total > 0 else 0
            }
            for band, stats in self._confidence_bands.items()
        }
    
    def get_ticker_stats(self, ticker: str) -> Optional[dict]:
        """Get statistics for a specific ticker."""
        if ticker not in self._ticker_stats:
            return None
        
        stats = self._ticker_stats[ticker]
        return {
            'total': stats.total,
            'correct': stats.correct,
            'accuracy': round(stats.accuracy, 3),
            'avg_return': round(stats.avg_return, 2)
        }
    
    def generate_report(self) -> dict:
        """Generate a comprehensive learning report."""
        return {
            'summary': {
                'total_decisions': sum(s.total for s in self._method_stats.values()),
                'overall_accuracy': round(
                    sum(s.correct for s in self._method_stats.values()) / 
                    max(1, sum(s.total for s in self._method_stats.values())),
                    3
                ),
                'current_threshold': self._confidence_threshold
            },
            'method_performance': self.get_method_stats(),
            'confidence_bands': self.get_confidence_band_stats(),
            'optimized_weights': self._optimized_weights,
            'recommendations': self._generate_recommendations()
        }
    
    def _generate_recommendations(self) -> List[str]:
        """Generate recommendations based on analysis."""
        recommendations = []
        
        # Check method performance
        for method, stats in self._method_stats.items():
            if stats.total >= 10:
                if stats.accuracy < 0.4:
                    recommendations.append(
                        f"Consider reducing weight for '{method}' - accuracy only {stats.accuracy:.1%}"
                    )
                elif stats.accuracy > 0.7:
                    recommendations.append(
                        f"'{method}' performing well ({stats.accuracy:.1%}) - consider increasing weight"
                    )
        
        # Check confidence bands
        low_band = self._confidence_bands['low_0.5-0.6']
        if low_band.total >= 10 and low_band.accuracy < 0.45:
            recommendations.append(
                f"Low-confidence decisions underperforming ({low_band.accuracy:.1%}) - "
                f"consider raising threshold above {self._confidence_threshold:.2f}"
            )
        
        high_band = self._confidence_bands['very_high_0.8-1.0']
        if high_band.total >= 10 and high_band.accuracy < 0.6:
            recommendations.append(
                "Even high-confidence extractions underperforming - review extraction logic"
            )
        
        if not recommendations:
            recommendations.append("System performing within expected parameters")
        
        return recommendations


# Singleton instance for global access
_tracker_instance: Optional[DecisionOutcomeTracker] = None


def get_tracker() -> DecisionOutcomeTracker:
    """Get or create the global tracker instance."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = DecisionOutcomeTracker()
    return _tracker_instance


if __name__ == "__main__":
    # Test the tracker
    tracker = DecisionOutcomeTracker()
    
    # Record some test decisions
    tracker.record_decision(
        symbol="AAPL",
        confidence=0.85,
        decision="BUY",
        extraction_methods=["explicit", "company_name"],
        reasons=["company_name_match", "headline_match"],
        article_id="test_001",
        headline="Apple stock rises after strong iPhone demand"
    )
    
    tracker.record_decision(
        symbol="TSLA",
        confidence=0.65,
        decision="SELL",
        extraction_methods=["company_name"],
        reasons=["company_name_match", "negative_context"],
        article_id="test_002",
        headline="Tesla faces production challenges"
    )
    
    # Record outcomes
    tracker.record_outcome("AAPL", "test_001", return_1h=0.5, return_24h=1.3)
    tracker.record_outcome("TSLA", "test_002", return_1h=-0.3, return_24h=-2.1)
    
    # Generate report
    report = tracker.generate_report()
    
    print("\n=== Decision Outcome Tracker Report ===\n")
    print(json.dumps(report, indent=2))
