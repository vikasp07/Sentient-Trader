"""
Confidence Scoring Module with Learning Capabilities

Advanced confidence scoring that:
1. Combines multiple factors for initial scoring
2. Adjusts based on historical outcomes
3. Provides explainable confidence breakdowns
4. Learns from prediction accuracy over time
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class ConfidenceFactors:
    """Breakdown of confidence factors for explainability."""
    source_weight: float = 0.0
    frequency_weight: float = 0.0
    headline_boost: float = 0.0
    multi_method_bonus: float = 0.0
    context_bonus: float = 0.0
    historical_adjustment: float = 0.0
    sentiment_correlation: float = 0.0
    
    @property
    def total(self) -> float:
        return min(1.0, max(0.0,
            self.source_weight +
            self.frequency_weight +
            self.headline_boost +
            self.multi_method_bonus +
            self.context_bonus +
            self.historical_adjustment +
            self.sentiment_correlation
        ))
    
    def to_dict(self) -> dict:
        result = asdict(self)
        result['total'] = self.total
        return result


@dataclass
class OutcomeRecord:
    """Records the outcome of a ticker extraction decision."""
    ticker: str
    article_id: str
    extraction_confidence: float
    timestamp: str
    sentiment_score: Optional[float] = None
    price_change_1h: Optional[float] = None
    price_change_24h: Optional[float] = None
    was_relevant: Optional[bool] = None  # Did this ticker actually relate to article?
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'OutcomeRecord':
        return cls(**data)


class ConfidenceScorer:
    """
    Multi-factor confidence scoring with historical learning.
    
    Calculates confidence based on:
    - Extraction method reliability
    - Match frequency and location
    - Context relevance
    - Historical accuracy for this ticker
    - Sentiment correlation strength
    """
    
    # Base weights for extraction methods
    METHOD_WEIGHTS = {
        'explicit': 0.40,      # $AAPL or AAPL directly mentioned
        'company_name': 0.30,  # Company name matched
        'nlp_entity': 0.20,    # NLP extracted organization
    }
    
    # Maximum adjustment from historical learning
    MAX_HISTORICAL_ADJUSTMENT = 0.15
    
    def __init__(self, redis_client: Optional[redis.Redis] = None, outcomes_dir: str = None):
        """
        Initialize confidence scorer.
        
        Args:
            redis_client: Redis client for caching historical data
            outcomes_dir: Directory to store outcome records
        """
        self.redis = redis_client
        self.outcomes_dir = Path(outcomes_dir) if outcomes_dir else Path(__file__).parent.parent.parent / "data" / "outcomes"
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        
        # In-memory cache for ticker accuracy
        self._accuracy_cache: Dict[str, Dict] = {}
        
        # Load historical accuracy if available
        self._load_accuracy_cache()
    
    def calculate_confidence(
        self,
        ticker: str,
        methods_used: List[str],
        frequency: int,
        in_headline: bool,
        context_keywords_found: int,
        sentiment_strength: Optional[float] = None
    ) -> Tuple[float, ConfidenceFactors]:
        """
        Calculate confidence score with full factor breakdown.
        
        Args:
            ticker: The ticker symbol
            methods_used: List of extraction methods that found this ticker
            frequency: Number of times ticker was found in text
            in_headline: Whether ticker appears in headline
            context_keywords_found: Number of financial context keywords found
            sentiment_strength: Absolute value of sentiment score (0-1)
        
        Returns:
            Tuple of (confidence_score, ConfidenceFactors)
        """
        factors = ConfidenceFactors()
        
        # 1. Source weight - use best method's weight
        best_method = max(methods_used, key=lambda m: self.METHOD_WEIGHTS.get(m, 0))
        factors.source_weight = self.METHOD_WEIGHTS.get(best_method, 0.1)
        
        # 2. Frequency weight (0.05 per mention, max 0.20)
        factors.frequency_weight = min(0.20, frequency * 0.05)
        
        # 3. Headline boost
        factors.headline_boost = 0.15 if in_headline else 0.0
        
        # 4. Multi-method bonus
        unique_methods = len(set(methods_used))
        factors.multi_method_bonus = 0.10 if unique_methods >= 2 else 0.0
        
        # 5. Context bonus
        if context_keywords_found >= 3:
            factors.context_bonus = 0.10
        elif context_keywords_found >= 1:
            factors.context_bonus = 0.05
        
        # 6. Historical adjustment (from learning)
        factors.historical_adjustment = self._get_historical_adjustment(ticker)
        
        # 7. Sentiment correlation bonus
        if sentiment_strength is not None and sentiment_strength > 0.7:
            factors.sentiment_correlation = 0.05
        
        return factors.total, factors
    
    def _get_historical_adjustment(self, ticker: str) -> float:
        """
        Get historical accuracy adjustment for a ticker.
        
        Returns value between -MAX_HISTORICAL_ADJUSTMENT and +MAX_HISTORICAL_ADJUSTMENT
        """
        if ticker not in self._accuracy_cache:
            return 0.0
        
        stats = self._accuracy_cache[ticker]
        total = stats.get('total', 0)
        
        if total < 5:  # Need at least 5 samples
            return 0.0
        
        accuracy = stats.get('accuracy', 0.5)
        
        # Convert accuracy (0-1) to adjustment (-0.15 to +0.15)
        # accuracy 0.5 = no adjustment, 1.0 = +0.15, 0.0 = -0.15
        adjustment = (accuracy - 0.5) * 2 * self.MAX_HISTORICAL_ADJUSTMENT
        
        return round(adjustment, 4)
    
    def record_outcome(
        self,
        ticker: str,
        article_id: str,
        extraction_confidence: float,
        sentiment_score: Optional[float] = None,
        price_change_1h: Optional[float] = None,
        price_change_24h: Optional[float] = None,
        was_relevant: Optional[bool] = None
    ):
        """
        Record the outcome of a ticker extraction for learning.
        
        Args:
            ticker: The extracted ticker
            article_id: Unique identifier for the article
            extraction_confidence: Confidence score at extraction time
            sentiment_score: Sentiment analysis score (-1 to 1)
            price_change_1h: Price change 1 hour after article (%)
            price_change_24h: Price change 24 hours after article (%)
            was_relevant: Manual/heuristic indicator of relevance
        """
        record = OutcomeRecord(
            ticker=ticker,
            article_id=article_id,
            extraction_confidence=extraction_confidence,
            timestamp=datetime.utcnow().isoformat(),
            sentiment_score=sentiment_score,
            price_change_1h=price_change_1h,
            price_change_24h=price_change_24h,
            was_relevant=was_relevant
        )
        
        # Store to file
        self._store_outcome(record)
        
        # Update accuracy cache if relevance is known
        if was_relevant is not None:
            self._update_accuracy(ticker, was_relevant)
        elif sentiment_score is not None and price_change_1h is not None:
            # Heuristic: if sentiment direction matches price direction, consider relevant
            sentiment_direction = 1 if sentiment_score > 0 else -1
            price_direction = 1 if price_change_1h > 0 else -1
            was_relevant = sentiment_direction == price_direction
            self._update_accuracy(ticker, was_relevant)
        
        # Cache in Redis if available
        if self.redis:
            try:
                key = f"outcome:{ticker}:{article_id}"
                self.redis.setex(key, 86400 * 7, json.dumps(record.to_dict()))  # 7 day TTL
            except Exception as e:
                logger.warning(f"Failed to cache outcome in Redis: {e}")
    
    def _store_outcome(self, record: OutcomeRecord):
        """Store outcome record to file."""
        date_str = datetime.utcnow().strftime("%Y%m%d")
        filepath = self.outcomes_dir / f"outcomes_{date_str}.jsonl"
        
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to store outcome: {e}")
    
    def _update_accuracy(self, ticker: str, was_relevant: bool):
        """Update accuracy statistics for a ticker."""
        if ticker not in self._accuracy_cache:
            self._accuracy_cache[ticker] = {'total': 0, 'correct': 0, 'accuracy': 0.5}
        
        stats = self._accuracy_cache[ticker]
        stats['total'] += 1
        if was_relevant:
            stats['correct'] += 1
        stats['accuracy'] = stats['correct'] / stats['total']
        
        # Save updated cache
        self._save_accuracy_cache()
    
    def _load_accuracy_cache(self):
        """Load accuracy cache from file."""
        cache_file = self.outcomes_dir / "accuracy_cache.json"
        
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    self._accuracy_cache = json.load(f)
                logger.info(f"Loaded accuracy cache for {len(self._accuracy_cache)} tickers")
            except Exception as e:
                logger.error(f"Failed to load accuracy cache: {e}")
    
    def _save_accuracy_cache(self):
        """Save accuracy cache to file."""
        cache_file = self.outcomes_dir / "accuracy_cache.json"
        
        try:
            with open(cache_file, 'w') as f:
                json.dump(self._accuracy_cache, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save accuracy cache: {e}")
    
    def get_ticker_stats(self, ticker: str) -> Optional[Dict]:
        """Get accuracy statistics for a ticker."""
        return self._accuracy_cache.get(ticker)
    
    def get_all_stats(self) -> Dict[str, Dict]:
        """Get accuracy statistics for all tickers."""
        return self._accuracy_cache.copy()
    
    def analyze_outcomes(self, days: int = 30) -> Dict:
        """
        Analyze outcomes from the past N days.
        
        Returns summary statistics and insights.
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        all_outcomes: List[OutcomeRecord] = []
        
        # Load outcomes from files
        for filepath in self.outcomes_dir.glob("outcomes_*.jsonl"):
            try:
                # Extract date from filename
                date_str = filepath.stem.split('_')[1]
                file_date = datetime.strptime(date_str, "%Y%m%d")
                
                if file_date >= cutoff_date:
                    with open(filepath, 'r') as f:
                        for line in f:
                            if line.strip():
                                record = OutcomeRecord.from_dict(json.loads(line))
                                all_outcomes.append(record)
            except Exception as e:
                logger.warning(f"Failed to load outcomes from {filepath}: {e}")
        
        if not all_outcomes:
            return {"error": "No outcomes found", "days_analyzed": days}
        
        # Compute statistics
        ticker_stats = defaultdict(lambda: {'count': 0, 'relevant': 0, 'avg_confidence': 0})
        
        for outcome in all_outcomes:
            stats = ticker_stats[outcome.ticker]
            stats['count'] += 1
            stats['avg_confidence'] += outcome.extraction_confidence
            if outcome.was_relevant:
                stats['relevant'] += 1
        
        # Finalize averages
        for ticker, stats in ticker_stats.items():
            if stats['count'] > 0:
                stats['avg_confidence'] /= stats['count']
                stats['accuracy'] = stats['relevant'] / stats['count'] if stats['count'] > 0 else 0
        
        return {
            "days_analyzed": days,
            "total_outcomes": len(all_outcomes),
            "unique_tickers": len(ticker_stats),
            "ticker_breakdown": dict(ticker_stats),
            "top_accurate": sorted(
                [(k, v['accuracy']) for k, v in ticker_stats.items() if v['count'] >= 5],
                key=lambda x: x[1],
                reverse=True
            )[:10],
            "most_extracted": sorted(
                [(k, v['count']) for k, v in ticker_stats.items()],
                key=lambda x: x[1],
                reverse=True
            )[:10]
        }


class AdaptiveConfidenceScorer(ConfidenceScorer):
    """
    Extended confidence scorer that adapts weights based on performance.
    
    Periodically analyzes outcomes and adjusts method weights
    to improve extraction accuracy.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._method_accuracy: Dict[str, Dict] = {
            'explicit': {'total': 0, 'correct': 0},
            'company_name': {'total': 0, 'correct': 0},
            'nlp_entity': {'total': 0, 'correct': 0},
        }
        self._load_method_accuracy()
    
    def record_method_outcome(self, method: str, was_correct: bool):
        """Record outcome for a specific extraction method."""
        if method in self._method_accuracy:
            self._method_accuracy[method]['total'] += 1
            if was_correct:
                self._method_accuracy[method]['correct'] += 1
            self._save_method_accuracy()
    
    def adapt_weights(self, min_samples: int = 50):
        """
        Adapt method weights based on historical accuracy.
        
        Only adapts if we have enough samples for each method.
        """
        total_accuracy = 0
        method_accuracies = {}
        
        for method, stats in self._method_accuracy.items():
            if stats['total'] < min_samples:
                logger.info(f"Insufficient samples for {method}: {stats['total']}/{min_samples}")
                return  # Don't adapt until we have enough data
            
            accuracy = stats['correct'] / stats['total']
            method_accuracies[method] = accuracy
            total_accuracy += accuracy
        
        if total_accuracy == 0:
            return
        
        # Normalize and update weights
        for method, accuracy in method_accuracies.items():
            new_weight = accuracy / total_accuracy * 0.9  # Scale to leave room for variance
            old_weight = self.METHOD_WEIGHTS[method]
            
            # Gradual update (blend old and new)
            self.METHOD_WEIGHTS[method] = 0.7 * old_weight + 0.3 * new_weight
            
            logger.info(f"Adapted {method} weight: {old_weight:.3f} -> {self.METHOD_WEIGHTS[method]:.3f}")
    
    def _load_method_accuracy(self):
        """Load method accuracy from file."""
        filepath = self.outcomes_dir / "method_accuracy.json"
        
        if filepath.exists():
            try:
                with open(filepath, 'r') as f:
                    self._method_accuracy = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load method accuracy: {e}")
    
    def _save_method_accuracy(self):
        """Save method accuracy to file."""
        filepath = self.outcomes_dir / "method_accuracy.json"
        
        try:
            with open(filepath, 'w') as f:
                json.dump(self._method_accuracy, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save method accuracy: {e}")


if __name__ == "__main__":
    # Test the confidence scorer
    scorer = ConfidenceScorer()
    
    # Test confidence calculation
    confidence, factors = scorer.calculate_confidence(
        ticker="AAPL",
        methods_used=['explicit', 'company_name'],
        frequency=3,
        in_headline=True,
        context_keywords_found=2,
        sentiment_strength=0.85
    )
    
    print("\n=== Confidence Scoring Test ===\n")
    print(f"Ticker: AAPL")
    print(f"Total Confidence: {confidence:.3f}")
    print(f"\nFactor Breakdown:")
    for key, value in factors.to_dict().items():
        if key != 'total' and value > 0:
            print(f"  {key}: {value:.3f}")
    
    # Test outcome recording
    scorer.record_outcome(
        ticker="AAPL",
        article_id="test_001",
        extraction_confidence=confidence,
        sentiment_score=0.85,
        price_change_1h=1.2,
        was_relevant=True
    )
    
    print("\n=== Outcome Recorded ===")
    print(f"Stats for AAPL: {scorer.get_ticker_stats('AAPL')}")
