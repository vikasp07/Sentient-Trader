# evaluation/metrics.py
"""
Strategy Evaluation Metrics

Provides lightweight analytics for strategy performance:
- Overall win rate
- Average return per trade
- Win rate by sentiment bucket, confidence bucket, indicator group
- False positive rate for BUY/SELL signals
- JSON/CSV-friendly outputs

Works with both backtest results and live paper-trading decisions.
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict
import csv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("strategy_metrics")


# Configuration
OUTCOMES_DIR = os.getenv("OUTCOMES_DIR", "data/outcomes")
DECISIONS_PATH = os.getenv("DECISIONS_PATH", "results/trade_decisions.jsonl")
METRICS_OUTPUT_DIR = os.getenv("METRICS_OUTPUT_DIR", "results/metrics")


@dataclass
class BucketStats:
    """Statistics for a single bucket (sentiment, confidence, etc.)."""
    bucket_name: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    total_return: float = 0.0
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0
    
    @property
    def avg_return(self) -> float:
        return self.total_return / self.total if self.total > 0 else 0.0
    
    def to_dict(self) -> dict:
        return {
            'bucket': self.bucket_name,
            'total': self.total,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': round(self.win_rate, 4),
            'avg_return': round(self.avg_return, 4)
        }


@dataclass
class SignalStats:
    """Statistics for a signal type (BUY/SELL)."""
    signal: str
    total: int = 0
    true_positives: int = 0  # Correct predictions
    false_positives: int = 0  # Incorrect predictions
    total_return: float = 0.0
    
    @property
    def accuracy(self) -> float:
        return self.true_positives / self.total if self.total > 0 else 0.0
    
    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / self.total if self.total > 0 else 0.0
    
    @property
    def avg_return(self) -> float:
        return self.total_return / self.total if self.total > 0 else 0.0
    
    def to_dict(self) -> dict:
        return {
            'signal': self.signal,
            'total': self.total,
            'true_positives': self.true_positives,
            'false_positives': self.false_positives,
            'accuracy': round(self.accuracy, 4),
            'false_positive_rate': round(self.false_positive_rate, 4),
            'avg_return': round(self.avg_return, 4)
        }


class StrategyMetrics:
    """
    Comprehensive strategy evaluation metrics.
    
    Computes:
    - Overall performance (win rate, avg return)
    - Performance by sentiment bucket
    - Performance by confidence bucket
    - Performance by indicator group
    - False positive rates by signal type
    """
    
    # Sentiment buckets
    SENTIMENT_BUCKETS = {
        'very_negative': (-1.0, -0.6),
        'negative': (-0.6, -0.2),
        'neutral': (-0.2, 0.2),
        'positive': (0.2, 0.6),
        'very_positive': (0.6, 1.0)
    }
    
    # Confidence buckets
    CONFIDENCE_BUCKETS = {
        'low': (0.0, 0.5),
        'medium_low': (0.5, 0.6),
        'medium': (0.6, 0.7),
        'medium_high': (0.7, 0.8),
        'high': (0.8, 0.9),
        'very_high': (0.9, 1.0)
    }
    
    # Indicator groups
    INDICATOR_GROUPS = {
        'trend': ['sma_5', 'sma_20', 'sma_50', 'ema_12', 'ema_26', 'adx', 'plus_di', 'minus_di'],
        'momentum': ['macd', 'macd_signal', 'macd_hist', 'rsi_14', 'stoch_k', 'stoch_d', 
                    'williams_r', 'cci_20', 'momentum_10', 'roc_10'],
        'volatility': ['boll_upper', 'boll_lower', 'boll_mid', 'atr_14'],
        'volume': ['vwap', 'obv']
    }
    
    def __init__(
        self,
        outcomes_dir: str = None,
        output_dir: str = None
    ):
        """
        Initialize metrics calculator.
        
        Args:
            outcomes_dir: Directory containing labeled outcomes
            output_dir: Directory for metric outputs
        """
        self.outcomes_dir = Path(outcomes_dir) if outcomes_dir else Path(OUTCOMES_DIR)
        self.output_dir = Path(output_dir) if output_dir else Path(METRICS_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Statistics containers
        self._reset_stats()
    
    def _reset_stats(self):
        """Reset all statistics."""
        self.total_trades = 0
        self.total_wins = 0
        self.total_return = 0.0
        
        # By sentiment bucket
        self.sentiment_stats: Dict[str, BucketStats] = {
            bucket: BucketStats(bucket_name=bucket)
            for bucket in self.SENTIMENT_BUCKETS
        }
        
        # By confidence bucket
        self.confidence_stats: Dict[str, BucketStats] = {
            bucket: BucketStats(bucket_name=bucket)
            for bucket in self.CONFIDENCE_BUCKETS
        }
        
        # By indicator group
        self.indicator_stats: Dict[str, BucketStats] = {
            group: BucketStats(bucket_name=group)
            for group in self.INDICATOR_GROUPS
        }
        
        # By signal type
        self.signal_stats: Dict[str, SignalStats] = {
            'BUY': SignalStats(signal='BUY'),
            'SELL': SignalStats(signal='SELL'),
            'HOLD': SignalStats(signal='HOLD')
        }
        
        # By symbol
        self.symbol_stats: Dict[str, BucketStats] = defaultdict(
            lambda: BucketStats(bucket_name="")
        )
    
    def _get_sentiment_bucket(self, score: float) -> str:
        """Get sentiment bucket for a score."""
        for bucket, (low, high) in self.SENTIMENT_BUCKETS.items():
            if low <= score < high or (bucket == 'very_positive' and score >= high - 0.01):
                return bucket
        return 'neutral'
    
    def _get_confidence_bucket(self, confidence: float) -> str:
        """Get confidence bucket for a value."""
        for bucket, (low, high) in self.CONFIDENCE_BUCKETS.items():
            if low <= confidence < high or (bucket == 'very_high' and confidence >= high - 0.01):
                return bucket
        return 'medium'
    
    def _get_active_indicator_groups(self, indicators: Dict) -> List[str]:
        """Determine which indicator groups were active in a decision."""
        active = []
        for group, group_indicators in self.INDICATOR_GROUPS.items():
            # Check if any indicator from this group has a non-null value
            for ind in group_indicators:
                if indicators.get(ind) is not None:
                    active.append(group)
                    break
        return active
    
    def process_outcome(self, outcome: Dict):
        """
        Process a single labeled outcome.
        
        Args:
            outcome: Labeled outcome dict with returns and success flags
        """
        # Use 1-hour return as primary metric (or 1-day if not available)
        return_pct = outcome.get('return_1h')
        if return_pct is None:
            return_pct = outcome.get('return_1d', 0)
        
        success = outcome.get('success_1h')
        if success is None:
            success = outcome.get('success_1d', False)
        
        decision = outcome.get('decision', 'HOLD')
        sentiment_score = outcome.get('sentiment_score', 0)
        confidence = outcome.get('ticker_confidence', 1.0)
        symbol = outcome.get('symbol', 'UNKNOWN')
        
        # Skip HOLD for win/loss calculations (informational only)
        is_actionable = decision in ('BUY', 'SELL')
        
        # Overall stats
        if is_actionable:
            self.total_trades += 1
            self.total_return += return_pct or 0
            if success:
                self.total_wins += 1
        
        # Sentiment bucket stats
        sentiment_bucket = self._get_sentiment_bucket(sentiment_score)
        if is_actionable:
            stats = self.sentiment_stats[sentiment_bucket]
            stats.total += 1
            stats.total_return += return_pct or 0
            if success:
                stats.wins += 1
            else:
                stats.losses += 1
        
        # Confidence bucket stats
        confidence_bucket = self._get_confidence_bucket(confidence)
        if is_actionable:
            stats = self.confidence_stats[confidence_bucket]
            stats.total += 1
            stats.total_return += return_pct or 0
            if success:
                stats.wins += 1
            else:
                stats.losses += 1
        
        # Signal stats
        signal_stat = self.signal_stats.get(decision)
        if signal_stat:
            signal_stat.total += 1
            signal_stat.total_return += return_pct or 0
            if success:
                signal_stat.true_positives += 1
            else:
                signal_stat.false_positives += 1
        
        # Symbol stats
        if is_actionable:
            sym_stats = self.symbol_stats[symbol]
            sym_stats.bucket_name = symbol
            sym_stats.total += 1
            sym_stats.total_return += return_pct or 0
            if success:
                sym_stats.wins += 1
            else:
                sym_stats.losses += 1
        
        # Indicator group stats (based on what was used)
        indicators = outcome.get('indicators', {})
        if not indicators:
            indicators = outcome.get('features', {})
        
        active_groups = self._get_active_indicator_groups(indicators)
        for group in active_groups:
            if is_actionable:
                stats = self.indicator_stats[group]
                stats.total += 1
                stats.total_return += return_pct or 0
                if success:
                    stats.wins += 1
                else:
                    stats.losses += 1
    
    def compute_from_outcomes(self, outcomes: List[Dict]) -> Dict:
        """
        Compute metrics from a list of outcomes.
        
        Args:
            outcomes: List of labeled outcome dicts
            
        Returns:
            Metrics summary dict
        """
        self._reset_stats()
        
        for outcome in outcomes:
            self.process_outcome(outcome)
        
        return self.generate_summary()
    
    def compute_from_files(self, pattern: str = "labeled_outcomes_*.jsonl") -> Dict:
        """
        Compute metrics from outcome files.
        
        Args:
            pattern: Glob pattern for outcome files
            
        Returns:
            Metrics summary dict
        """
        self._reset_stats()
        
        files = list(self.outcomes_dir.glob(pattern))
        if not files:
            logger.warning(f"No outcome files found matching {pattern}")
            return self.generate_summary()
        
        for filepath in files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            outcome = json.loads(line.strip())
                            self.process_outcome(outcome)
            except Exception as e:
                logger.error(f"Failed to process {filepath}: {e}")
        
        return self.generate_summary()
    
    def generate_summary(self) -> Dict:
        """
        Generate comprehensive metrics summary.
        
        Returns:
            Dict with all metrics
        """
        summary = {
            'computed_at': datetime.utcnow().isoformat(),
            'overall': {
                'total_trades': self.total_trades,
                'total_wins': self.total_wins,
                'total_losses': self.total_trades - self.total_wins,
                'win_rate': round(self.total_wins / self.total_trades, 4) if self.total_trades > 0 else 0,
                'total_return': round(self.total_return, 4),
                'avg_return': round(self.total_return / self.total_trades, 4) if self.total_trades > 0 else 0
            },
            'by_sentiment': {
                bucket: stats.to_dict()
                for bucket, stats in self.sentiment_stats.items()
                if stats.total > 0
            },
            'by_confidence': {
                bucket: stats.to_dict()
                for bucket, stats in self.confidence_stats.items()
                if stats.total > 0
            },
            'by_indicator_group': {
                group: stats.to_dict()
                for group, stats in self.indicator_stats.items()
                if stats.total > 0
            },
            'by_signal': {
                signal: stats.to_dict()
                for signal, stats in self.signal_stats.items()
                if stats.total > 0
            },
            'by_symbol': {
                symbol: stats.to_dict()
                for symbol, stats in self.symbol_stats.items()
                if stats.total > 0
            },
            'false_positive_analysis': self._compute_false_positive_analysis()
        }
        
        return summary
    
    def _compute_false_positive_analysis(self) -> Dict:
        """Compute detailed false positive analysis."""
        buy_stats = self.signal_stats['BUY']
        sell_stats = self.signal_stats['SELL']
        
        return {
            'buy_signals': {
                'total': buy_stats.total,
                'correct': buy_stats.true_positives,
                'incorrect': buy_stats.false_positives,
                'false_positive_rate': round(buy_stats.false_positive_rate, 4),
                'interpretation': 'Percentage of BUY signals where price actually decreased'
            },
            'sell_signals': {
                'total': sell_stats.total,
                'correct': sell_stats.true_positives,
                'incorrect': sell_stats.false_positives,
                'false_positive_rate': round(sell_stats.false_positive_rate, 4),
                'interpretation': 'Percentage of SELL signals where price actually increased'
            }
        }
    
    def save_json_report(self, summary: Dict = None, filename: str = None) -> str:
        """
        Save metrics summary as JSON.
        
        Args:
            summary: Metrics summary (computes if not provided)
            filename: Output filename
            
        Returns:
            Path to saved file
        """
        if summary is None:
            summary = self.generate_summary()
        
        if filename is None:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"metrics_summary_{timestamp}.json"
        
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)
        
        logger.info(f"Saved JSON report to {filepath}")
        return str(filepath)
    
    def save_csv_report(self, summary: Dict = None, prefix: str = None) -> List[str]:
        """
        Save metrics as CSV files for offline analysis.
        
        Args:
            summary: Metrics summary (computes if not provided)
            prefix: Filename prefix
            
        Returns:
            List of paths to saved files
        """
        if summary is None:
            summary = self.generate_summary()
        
        if prefix is None:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            prefix = f"metrics_{timestamp}"
        
        saved_files = []
        
        # Overall summary CSV
        overall_path = self.output_dir / f"{prefix}_overall.csv"
        with open(overall_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['metric', 'value'])
            for key, value in summary['overall'].items():
                writer.writerow([key, value])
        saved_files.append(str(overall_path))
        
        # By sentiment CSV
        sentiment_path = self.output_dir / f"{prefix}_by_sentiment.csv"
        self._write_bucket_csv(sentiment_path, summary.get('by_sentiment', {}))
        saved_files.append(str(sentiment_path))
        
        # By confidence CSV
        confidence_path = self.output_dir / f"{prefix}_by_confidence.csv"
        self._write_bucket_csv(confidence_path, summary.get('by_confidence', {}))
        saved_files.append(str(confidence_path))
        
        # By indicator group CSV
        indicator_path = self.output_dir / f"{prefix}_by_indicator.csv"
        self._write_bucket_csv(indicator_path, summary.get('by_indicator_group', {}))
        saved_files.append(str(indicator_path))
        
        # By signal CSV
        signal_path = self.output_dir / f"{prefix}_by_signal.csv"
        signal_data = summary.get('by_signal', {})
        with open(signal_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['signal', 'total', 'true_positives', 'false_positives', 
                           'accuracy', 'false_positive_rate', 'avg_return'])
            for signal, stats in signal_data.items():
                writer.writerow([
                    stats.get('signal', signal),
                    stats.get('total', 0),
                    stats.get('true_positives', 0),
                    stats.get('false_positives', 0),
                    stats.get('accuracy', 0),
                    stats.get('false_positive_rate', 0),
                    stats.get('avg_return', 0)
                ])
        saved_files.append(str(signal_path))
        
        # By symbol CSV
        symbol_path = self.output_dir / f"{prefix}_by_symbol.csv"
        self._write_bucket_csv(symbol_path, summary.get('by_symbol', {}))
        saved_files.append(str(symbol_path))
        
        logger.info(f"Saved {len(saved_files)} CSV reports")
        return saved_files
    
    def _write_bucket_csv(self, path: Path, bucket_data: Dict):
        """Write bucket stats to CSV."""
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['bucket', 'total', 'wins', 'losses', 'win_rate', 'avg_return'])
            for bucket, stats in bucket_data.items():
                writer.writerow([
                    stats.get('bucket', bucket),
                    stats.get('total', 0),
                    stats.get('wins', 0),
                    stats.get('losses', 0),
                    stats.get('win_rate', 0),
                    stats.get('avg_return', 0)
                ])


# Singleton instance
_metrics_instance: Optional[StrategyMetrics] = None


def get_metrics() -> StrategyMetrics:
    """Get or create the global metrics instance."""
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = StrategyMetrics()
    return _metrics_instance


def generate_backtest_metrics(backtest_results: List[Dict]) -> Dict:
    """
    Generate metrics from backtest results.
    
    Args:
        backtest_results: List of trade dicts from backtester
        
    Returns:
        Metrics summary
    """
    metrics = StrategyMetrics()
    
    # Convert backtest format to outcome format
    outcomes = []
    for trade in backtest_results:
        outcome = {
            'symbol': trade.get('symbol'),
            'decision': trade.get('side', '').upper().replace('BUY', 'BUY').replace('SELL', 'SELL'),
            'return_1h': trade.get('pnl_pct', trade.get('pnl', 0)),
            'success_1h': trade.get('pnl', 0) > 0 if trade.get('side') == 'buy' else trade.get('pnl', 0) < 0,
            'sentiment_score': trade.get('sentiment_score', 0),
            'ticker_confidence': trade.get('ticker_confidence', 1.0),
            'indicators': trade.get('indicators', {})
        }
        outcomes.append(outcome)
    
    return metrics.compute_from_outcomes(outcomes)


if __name__ == "__main__":
    # Demo/test
    metrics = StrategyMetrics()
    
    # Sample outcomes for testing
    sample_outcomes = [
        {
            'decision': 'BUY',
            'symbol': 'AAPL',
            'sentiment_score': 0.75,
            'ticker_confidence': 0.85,
            'return_1h': 1.5,
            'success_1h': True,
            'indicators': {'sma_5': 175, 'rsi_14': 55}
        },
        {
            'decision': 'BUY',
            'symbol': 'MSFT',
            'sentiment_score': 0.45,
            'ticker_confidence': 0.72,
            'return_1h': -0.8,
            'success_1h': False,
            'indicators': {'sma_5': 380, 'macd': 1.2}
        },
        {
            'decision': 'SELL',
            'symbol': 'TSLA',
            'sentiment_score': -0.55,
            'ticker_confidence': 0.91,
            'return_1h': -2.1,
            'success_1h': True,
            'indicators': {'adx': 28, 'rsi_14': 72}
        },
        {
            'decision': 'SELL',
            'symbol': 'NVDA',
            'sentiment_score': -0.32,
            'ticker_confidence': 0.65,
            'return_1h': 0.5,
            'success_1h': False,
            'indicators': {'boll_upper': 500, 'atr_14': 15}
        }
    ]
    
    summary = metrics.compute_from_outcomes(sample_outcomes)
    
    print("\n=== Strategy Metrics Summary ===\n")
    print(json.dumps(summary, indent=2))
    
    # Save reports
    metrics.save_json_report(summary)
    metrics.save_csv_report(summary)
