"""
Confidence Band Analytics

Tracks false trade rates per confidence band and auto-adjusts thresholds.

This is the "one dashboard metric" that matters most:
- False trades per confidence band

Example output:
    Confidence Band  |  Loss Rate
    ----------------+------------
    0.9–1.0         |  12%
    0.7–0.8         |  31%
    0.5–0.6         |  58% ❌

Auto-adjustment rule:
    if band_loss_rate > 40%:
        raise threshold
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class BandStats:
    """Statistics for a confidence band."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    
    @property
    def loss_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.losing_trades / self.total_trades
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades
    
    @property
    def avg_pnl(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades


class ConfidenceBandAnalytics:
    """
    Tracks and analyzes trade performance by confidence band.
    
    Confidence bands:
    - very_high: 0.9-1.0
    - high: 0.8-0.9
    - medium_high: 0.7-0.8
    - medium: 0.6-0.7
    - low: 0.5-0.6
    """
    
    # Loss rate threshold for auto-adjustment
    LOSS_THRESHOLD = 0.40  # 40% loss rate triggers adjustment
    
    # Minimum trades required before adjusting
    MIN_TRADES_FOR_ADJUSTMENT = 10
    
    BANDS = [
        ("very_high_0.9-1.0", 0.9, 1.0),
        ("high_0.8-0.9", 0.8, 0.9),
        ("medium_high_0.7-0.8", 0.7, 0.8),
        ("medium_0.6-0.7", 0.6, 0.7),
        ("low_0.5-0.6", 0.5, 0.6),
    ]
    
    def __init__(self, data_dir: str = None):
        """
        Initialize analytics.
        
        Args:
            data_dir: Directory to store analytics data
        """
        self.data_dir = Path(data_dir) if data_dir else Path(__file__).parent.parent.parent / "data" / "analytics"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # Statistics per band
        self._band_stats: Dict[str, BandStats] = {
            band[0]: BandStats() for band in self.BANDS
        }
        
        # Current recommended threshold
        self._recommended_threshold: float = 0.7  # Default
        
        # Load existing data
        self._load_stats()
    
    def get_band_for_confidence(self, confidence: float) -> str:
        """Get band name for a confidence score."""
        for band_name, low, high in self.BANDS:
            if low <= confidence < high or (high == 1.0 and confidence == 1.0):
                return band_name
        return "low_0.5-0.6"  # Default to low band
    
    def record_trade_outcome(
        self,
        symbol: str,
        confidence: float,
        decision: str,
        pnl: float,
        return_pct: float = None
    ):
        """
        Record a trade outcome for analytics.
        
        Args:
            symbol: Stock symbol
            confidence: Ticker confidence at decision time
            decision: BUY or SELL
            pnl: Profit/Loss in dollars
            return_pct: Return percentage (optional)
        """
        band = self.get_band_for_confidence(confidence)
        stats = self._band_stats[band]
        
        stats.total_trades += 1
        stats.total_pnl += pnl
        
        if pnl > 0:
            stats.winning_trades += 1
        else:
            stats.losing_trades += 1
        
        # Store individual trade
        trade_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "confidence": confidence,
            "band": band,
            "decision": decision,
            "pnl": pnl,
            "return_pct": return_pct,
            "is_win": pnl > 0
        }
        
        self._store_trade(trade_record)
        self._save_stats()
        
        # Check if we need to adjust threshold
        self._check_and_adjust_threshold()
    
    def _store_trade(self, trade: dict):
        """Store trade record to file."""
        date_str = datetime.utcnow().strftime("%Y%m%d")
        filepath = self.data_dir / f"trades_{date_str}.jsonl"
        
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(trade) + '\n')
        except Exception as e:
            logger.error(f"Failed to store trade: {e}")
    
    def _load_stats(self):
        """Load statistics from file."""
        stats_file = self.data_dir / "band_stats.json"
        
        if stats_file.exists():
            try:
                with open(stats_file, 'r') as f:
                    data = json.load(f)
                
                for band, stats_data in data.get('band_stats', {}).items():
                    if band in self._band_stats:
                        self._band_stats[band] = BandStats(**stats_data)
                
                self._recommended_threshold = data.get('recommended_threshold', 0.7)
                
                logger.info(f"Loaded band stats from {stats_file}")
                
            except Exception as e:
                logger.error(f"Failed to load band stats: {e}")
    
    def _save_stats(self):
        """Save statistics to file."""
        stats_file = self.data_dir / "band_stats.json"
        
        try:
            data = {
                'band_stats': {
                    band: asdict(stats) for band, stats in self._band_stats.items()
                },
                'recommended_threshold': self._recommended_threshold,
                'last_updated': datetime.utcnow().isoformat()
            }
            
            with open(stats_file, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save band stats: {e}")
    
    def _check_and_adjust_threshold(self):
        """
        Check if confidence threshold should be adjusted.
        
        Rule: if band_loss_rate > 40%, raise threshold
        """
        # Check each band from lowest to highest
        for band_name, low, high in reversed(self.BANDS):
            stats = self._band_stats[band_name]
            
            if stats.total_trades < self.MIN_TRADES_FOR_ADJUSTMENT:
                continue
            
            loss_rate = stats.loss_rate
            
            if loss_rate > self.LOSS_THRESHOLD:
                # This band is underperforming
                new_threshold = high  # Raise threshold above this band
                
                if new_threshold > self._recommended_threshold:
                    old_threshold = self._recommended_threshold
                    self._recommended_threshold = new_threshold
                    
                    logger.warning(
                        f"AUTO-ADJUSTMENT: Raising threshold {old_threshold:.2f} → {new_threshold:.2f} "
                        f"(band {band_name} has {loss_rate:.1%} loss rate)"
                    )
                    
                    self._save_stats()
                break
    
    def get_dashboard_report(self) -> dict:
        """
        Generate dashboard report showing loss rate per confidence band.
        
        Returns:
            Dict with band statistics and recommendations
        """
        bands_report = []
        
        for band_name, low, high in self.BANDS:
            stats = self._band_stats[band_name]
            
            status = "✓" if stats.loss_rate < 0.30 else "⚠️" if stats.loss_rate < 0.40 else "❌"
            
            bands_report.append({
                "band": band_name,
                "range": f"{low:.1f}-{high:.1f}",
                "total_trades": stats.total_trades,
                "win_rate": f"{stats.win_rate:.1%}",
                "loss_rate": f"{stats.loss_rate:.1%}",
                "avg_pnl": f"${stats.avg_pnl:.2f}",
                "status": status
            })
        
        return {
            "title": "FALSE TRADES PER CONFIDENCE BAND",
            "bands": bands_report,
            "current_threshold": self._recommended_threshold,
            "recommendation": self._generate_recommendation()
        }
    
    def _generate_recommendation(self) -> str:
        """Generate a recommendation based on current stats."""
        problem_bands = []
        
        for band_name, low, high in self.BANDS:
            stats = self._band_stats[band_name]
            if stats.total_trades >= self.MIN_TRADES_FOR_ADJUSTMENT:
                if stats.loss_rate > self.LOSS_THRESHOLD:
                    problem_bands.append((band_name, stats.loss_rate))
        
        if not problem_bands:
            return "System performing well. Current threshold is appropriate."
        
        worst_band, worst_rate = max(problem_bands, key=lambda x: x[1])
        return f"Consider raising threshold. Band '{worst_band}' has {worst_rate:.1%} loss rate."
    
    def print_dashboard(self):
        """Print dashboard to console."""
        report = self.get_dashboard_report()
        
        print("\n" + "=" * 60)
        print(f"  {report['title']}")
        print("=" * 60)
        print(f"{'Band':<20} {'Trades':<8} {'Win%':<8} {'Loss%':<8} {'Avg P&L':<10} {'Status'}")
        print("-" * 60)
        
        for band in report['bands']:
            print(f"{band['range']:<20} {band['total_trades']:<8} {band['win_rate']:<8} {band['loss_rate']:<8} {band['avg_pnl']:<10} {band['status']}")
        
        print("-" * 60)
        print(f"Current Threshold: {report['current_threshold']:.2f}")
        print(f"Recommendation: {report['recommendation']}")
        print("=" * 60 + "\n")
    
    def get_recommended_threshold(self) -> float:
        """Get the current recommended confidence threshold."""
        return self._recommended_threshold


# Singleton instance
_analytics_instance: Optional[ConfidenceBandAnalytics] = None


def get_analytics() -> ConfidenceBandAnalytics:
    """Get or create the global analytics instance."""
    global _analytics_instance
    if _analytics_instance is None:
        _analytics_instance = ConfidenceBandAnalytics()
    return _analytics_instance


if __name__ == "__main__":
    # Test the analytics
    analytics = ConfidenceBandAnalytics()
    
    # Simulate some trades
    test_trades = [
        # High confidence trades (0.9-1.0) - mostly wins
        ("AAPL", 0.95, "BUY", 150.0),
        ("MSFT", 0.92, "BUY", 200.0),
        ("GOOGL", 0.98, "BUY", -50.0),  # One loss
        ("NVDA", 0.91, "BUY", 180.0),
        
        # Medium-high confidence (0.7-0.8) - mixed
        ("TSLA", 0.75, "BUY", 100.0),
        ("AMD", 0.72, "SELL", -80.0),
        ("INTC", 0.78, "BUY", 50.0),
        ("QCOM", 0.71, "SELL", -120.0),
        
        # Low confidence (0.5-0.6) - mostly losses
        ("META", 0.55, "BUY", -100.0),
        ("NFLX", 0.52, "SELL", -80.0),
        ("PYPL", 0.58, "BUY", 30.0),
        ("DIS", 0.54, "SELL", -90.0),
        ("AMZN", 0.59, "BUY", -60.0),
    ]
    
    print("Recording test trades...")
    for symbol, conf, decision, pnl in test_trades:
        analytics.record_trade_outcome(symbol, conf, decision, pnl)
    
    # Print dashboard
    analytics.print_dashboard()
    
    print(f"\nRecommended threshold: {analytics.get_recommended_threshold():.2f}")
