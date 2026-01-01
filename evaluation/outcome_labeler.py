# evaluation/outcome_labeler.py
"""
Automatic Outcome Labeling System

Tracks trade decisions and labels outcomes by:
- Fetching price at decision time (T0)
- Fetching price after configurable horizons (+30m, +1h, +1d)
- Computing returns and success labels
- Persisting outcomes for analysis

Integrates with existing decision logs and market data utilities.
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Any
from collections import defaultdict
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outcome_labeler")


# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
OUTCOMES_DIR = os.getenv("OUTCOMES_DIR", "data/outcomes")
DECISIONS_PATH = os.getenv("DECISIONS_PATH", "results/trade_decisions.jsonl")

# Default horizons in minutes
DEFAULT_HORIZONS = [30, 60, 1440]  # 30min, 1hour, 1day


@dataclass
class PendingOutcome:
    """A decision awaiting outcome labeling."""
    decision_id: str
    symbol: str
    decision: str  # BUY, SELL, HOLD
    price_t0: float
    timestamp: str
    sentiment_score: float
    ticker_confidence: float
    confidence_band: str
    extraction_methods: List[str]
    source_title: str
    horizons: Dict[int, Optional[float]] = field(default_factory=dict)  # horizon_min -> price
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PendingOutcome':
        return cls(**data)


@dataclass
class LabeledOutcome:
    """A fully labeled outcome with returns and success flag."""
    decision_id: str
    symbol: str
    decision: str
    price_t0: float
    timestamp: str
    sentiment_score: float
    ticker_confidence: float
    confidence_band: str
    extraction_methods: List[str]
    source_title: str
    
    # Outcome data
    price_30m: Optional[float] = None
    price_1h: Optional[float] = None
    price_1d: Optional[float] = None
    
    return_30m: Optional[float] = None
    return_1h: Optional[float] = None
    return_1d: Optional[float] = None
    
    success_30m: Optional[bool] = None
    success_1h: Optional[bool] = None
    success_1d: Optional[bool] = None
    
    labeled_at: str = ""
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'LabeledOutcome':
        # Handle missing fields gracefully
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class OutcomeLabeler:
    """
    Automatic outcome labeling for trade decisions.
    
    Features:
    - Tracks pending decisions awaiting price data
    - Fetches prices at configurable horizons
    - Computes returns and success labels
    - Persists labeled outcomes to JSONL and Redis
    - Background worker for automatic labeling
    """
    
    def __init__(
        self,
        outcomes_dir: str = None,
        redis_client: Optional[redis.Redis] = None,
        horizons: List[int] = None
    ):
        """
        Initialize the outcome labeler.
        
        Args:
            outcomes_dir: Directory to store outcome files
            redis_client: Redis client for caching
            horizons: List of horizon minutes to track [30, 60, 1440]
        """
        self.outcomes_dir = Path(outcomes_dir) if outcomes_dir else Path(OUTCOMES_DIR)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        
        self.horizons = horizons or DEFAULT_HORIZONS
        
        # Redis connection
        if redis_client:
            self.redis = redis_client
        else:
            try:
                self.redis = redis.from_url(REDIS_URL, decode_responses=True)
                self.redis.ping()
                logger.info("Connected to Redis for outcome tracking")
            except Exception as e:
                logger.warning(f"Redis unavailable: {e}. Using file-only storage.")
                self.redis = None
        
        # Pending outcomes awaiting price updates
        self._pending: Dict[str, PendingOutcome] = {}
        self._load_pending()
        
        # Statistics
        self._stats = {
            'total_labeled': 0,
            'pending_count': 0,
            'last_label_time': None
        }
        
        # Background worker
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_worker = threading.Event()
    
    def register_decision(
        self,
        decision_id: str,
        symbol: str,
        decision: str,
        price_t0: float,
        timestamp: str,
        sentiment_score: float = 0.0,
        ticker_confidence: float = 1.0,
        confidence_band: str = "unknown",
        extraction_methods: List[str] = None,
        source_title: str = ""
    ) -> PendingOutcome:
        """
        Register a new decision for outcome tracking.
        
        Args:
            decision_id: Unique identifier for the decision
            symbol: Stock ticker symbol
            decision: BUY, SELL, or HOLD
            price_t0: Price at decision time
            timestamp: ISO timestamp of decision
            sentiment_score: Effective sentiment score
            ticker_confidence: Confidence in ticker extraction
            confidence_band: Confidence band category
            extraction_methods: Methods used to extract ticker
            source_title: Title of source article
            
        Returns:
            PendingOutcome object
        """
        pending = PendingOutcome(
            decision_id=decision_id,
            symbol=symbol,
            decision=decision,
            price_t0=price_t0,
            timestamp=timestamp,
            sentiment_score=sentiment_score,
            ticker_confidence=ticker_confidence,
            confidence_band=confidence_band,
            extraction_methods=extraction_methods or [],
            source_title=source_title,
            horizons={h: None for h in self.horizons}
        )
        
        self._pending[decision_id] = pending
        self._save_pending(pending)
        
        # Cache in Redis
        if self.redis:
            try:
                key = f"pending_outcome:{decision_id}"
                self.redis.setex(key, 86400 * 2, json.dumps(pending.to_dict()))
            except Exception as e:
                logger.warning(f"Failed to cache pending outcome: {e}")
        
        logger.info(f"Registered decision {decision_id}: {symbol} {decision} @ ${price_t0:.2f}")
        return pending
    
    def update_price(
        self,
        decision_id: str,
        horizon_minutes: int,
        price: float
    ) -> Optional[LabeledOutcome]:
        """
        Update price for a pending outcome at a specific horizon.
        
        Args:
            decision_id: Decision identifier
            horizon_minutes: Horizon in minutes (30, 60, 1440)
            price: Price at this horizon
            
        Returns:
            LabeledOutcome if all horizons are filled, else None
        """
        if decision_id not in self._pending:
            logger.warning(f"Decision {decision_id} not found in pending")
            return None
        
        pending = self._pending[decision_id]
        pending.horizons[horizon_minutes] = price
        
        # Check if all horizons are filled
        if all(p is not None for p in pending.horizons.values()):
            return self._label_outcome(pending)
        
        return None
    
    def _label_outcome(self, pending: PendingOutcome) -> LabeledOutcome:
        """
        Convert pending outcome to fully labeled outcome.
        
        Args:
            pending: PendingOutcome with all price data
            
        Returns:
            LabeledOutcome with returns and success labels
        """
        price_t0 = pending.price_t0
        
        # Calculate returns (percentage)
        def calc_return(price_tx: Optional[float]) -> Optional[float]:
            if price_tx is None or price_t0 == 0:
                return None
            return ((price_tx - price_t0) / price_t0) * 100
        
        # Determine success based on decision
        def is_success(ret: Optional[float], decision: str) -> Optional[bool]:
            if ret is None:
                return None
            if decision == "BUY":
                return ret > 0  # Success if price went up
            elif decision == "SELL":
                return ret < 0  # Success if price went down
            else:  # HOLD
                return abs(ret) < 1.0  # Success if minimal movement
        
        # Get prices by horizon
        price_30m = pending.horizons.get(30)
        price_1h = pending.horizons.get(60)
        price_1d = pending.horizons.get(1440)
        
        # Calculate returns
        return_30m = calc_return(price_30m)
        return_1h = calc_return(price_1h)
        return_1d = calc_return(price_1d)
        
        # Determine success
        decision = pending.decision
        success_30m = is_success(return_30m, decision)
        success_1h = is_success(return_1h, decision)
        success_1d = is_success(return_1d, decision)
        
        labeled = LabeledOutcome(
            decision_id=pending.decision_id,
            symbol=pending.symbol,
            decision=pending.decision,
            price_t0=pending.price_t0,
            timestamp=pending.timestamp,
            sentiment_score=pending.sentiment_score,
            ticker_confidence=pending.ticker_confidence,
            confidence_band=pending.confidence_band,
            extraction_methods=pending.extraction_methods,
            source_title=pending.source_title,
            price_30m=price_30m,
            price_1h=price_1h,
            price_1d=price_1d,
            return_30m=round(return_30m, 4) if return_30m else None,
            return_1h=round(return_1h, 4) if return_1h else None,
            return_1d=round(return_1d, 4) if return_1d else None,
            success_30m=success_30m,
            success_1h=success_1h,
            success_1d=success_1d,
            labeled_at=datetime.utcnow().isoformat()
        )
        
        # Store labeled outcome
        self._store_outcome(labeled)
        
        # Remove from pending
        del self._pending[pending.decision_id]
        if self.redis:
            try:
                self.redis.delete(f"pending_outcome:{pending.decision_id}")
            except Exception:
                pass
        
        # Update stats
        self._stats['total_labeled'] += 1
        self._stats['last_label_time'] = datetime.utcnow().isoformat()
        
        logger.info(
            f"Labeled outcome {pending.decision_id}: {pending.symbol} {pending.decision} "
            f"return_1h={return_1h:.2f}% success={success_1h}"
        )
        
        return labeled
    
    def fetch_and_label_pending(self) -> List[LabeledOutcome]:
        """
        Fetch current prices and label any pending outcomes that are ready.
        
        Returns:
            List of newly labeled outcomes
        """
        if not self._pending:
            return []
        
        labeled = []
        now = datetime.utcnow()
        
        # Import price fetcher
        try:
            from tools.market_data_producer import get_real_prices, get_simulated_prices, HAS_YFINANCE
            price_fetcher = get_real_prices if HAS_YFINANCE else get_simulated_prices
        except ImportError:
            logger.warning("Could not import price fetcher, using fallback")
            price_fetcher = None
        
        # Get unique symbols from pending
        symbols = list(set(p.symbol for p in self._pending.values()))
        
        # Fetch current prices
        current_prices = {}
        if price_fetcher:
            try:
                ticks = price_fetcher(symbols)
                current_prices = {t['symbol']: t['price'] for t in ticks}
            except Exception as e:
                logger.error(f"Failed to fetch prices: {e}")
        
        # Process pending outcomes
        for decision_id, pending in list(self._pending.items()):
            try:
                decision_time = datetime.fromisoformat(pending.timestamp.replace('Z', '+00:00'))
                decision_time = decision_time.replace(tzinfo=None)  # Make naive for comparison
                elapsed_minutes = (now - decision_time).total_seconds() / 60
                
                current_price = current_prices.get(pending.symbol)
                if current_price is None:
                    continue
                
                # Update horizons that have elapsed
                for horizon in self.horizons:
                    if pending.horizons.get(horizon) is None and elapsed_minutes >= horizon:
                        pending.horizons[horizon] = current_price
                
                # Check if all horizons filled
                if all(p is not None for p in pending.horizons.values()):
                    outcome = self._label_outcome(pending)
                    labeled.append(outcome)
                    
            except Exception as e:
                logger.error(f"Error processing pending {decision_id}: {e}")
        
        return labeled
    
    def get_price_from_redis(self, symbol: str) -> Optional[float]:
        """
        Get latest price from Redis feature store.
        
        Args:
            symbol: Stock ticker symbol
            
        Returns:
            Latest price or None
        """
        if not self.redis:
            return None
        
        try:
            # Look for latest feature entry
            pattern = f"features:{symbol}:*"
            keys = self.redis.keys(pattern)
            if not keys:
                return None
            
            # Get most recent
            latest_key = sorted(keys)[-1]
            data = self.redis.get(latest_key)
            if data:
                features = json.loads(data)
                return features.get('price')
        except Exception as e:
            logger.error(f"Failed to get price from Redis: {e}")
        
        return None
    
    def _save_pending(self, pending: PendingOutcome):
        """Save pending outcome to file."""
        filepath = self.outcomes_dir / "pending.jsonl"
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(pending.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to save pending outcome: {e}")
    
    def _load_pending(self):
        """Load pending outcomes from file."""
        filepath = self.outcomes_dir / "pending.jsonl"
        if not filepath.exists():
            return
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        data = json.loads(line)
                        pending = PendingOutcome.from_dict(data)
                        # Only load if not already labeled (check horizons)
                        if not all(p is not None for p in pending.horizons.values()):
                            self._pending[pending.decision_id] = pending
            
            logger.info(f"Loaded {len(self._pending)} pending outcomes")
        except Exception as e:
            logger.error(f"Failed to load pending outcomes: {e}")
    
    def _store_outcome(self, outcome: LabeledOutcome):
        """Store labeled outcome to file and Redis."""
        # File storage
        date_str = datetime.utcnow().strftime("%Y%m%d")
        filepath = self.outcomes_dir / f"labeled_outcomes_{date_str}.jsonl"
        
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(outcome.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to store outcome: {e}")
        
        # Redis storage
        if self.redis:
            try:
                key = f"labeled_outcome:{outcome.decision_id}"
                self.redis.setex(key, 86400 * 30, json.dumps(outcome.to_dict()))
                
                # Add to symbol's outcome list
                list_key = f"outcomes:{outcome.symbol}"
                self.redis.lpush(list_key, json.dumps(outcome.to_dict()))
                self.redis.ltrim(list_key, 0, 999)  # Keep last 1000
            except Exception as e:
                logger.warning(f"Failed to cache outcome in Redis: {e}")
    
    def get_recent_outcomes(self, limit: int = 50) -> List[Dict]:
        """
        Get recent labeled outcomes.
        
        Args:
            limit: Maximum number to return
            
        Returns:
            List of outcome dicts
        """
        outcomes = []
        
        # Try Redis first
        if self.redis:
            try:
                keys = self.redis.keys("labeled_outcome:*")
                for key in sorted(keys, reverse=True)[:limit]:
                    data = self.redis.get(key)
                    if data:
                        outcomes.append(json.loads(data))
                if outcomes:
                    return outcomes
            except Exception:
                pass
        
        # Fall back to file
        try:
            files = sorted(self.outcomes_dir.glob("labeled_outcomes_*.jsonl"), reverse=True)
            for filepath in files:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            outcomes.append(json.loads(line.strip()))
                            if len(outcomes) >= limit:
                                return outcomes
        except Exception as e:
            logger.error(f"Failed to read outcomes: {e}")
        
        return outcomes
    
    def get_outcomes_for_symbol(self, symbol: str, limit: int = 100) -> List[Dict]:
        """
        Get outcomes for a specific symbol.
        
        Args:
            symbol: Stock ticker
            limit: Maximum number to return
            
        Returns:
            List of outcome dicts
        """
        outcomes = []
        
        # Try Redis
        if self.redis:
            try:
                list_key = f"outcomes:{symbol}"
                data_list = self.redis.lrange(list_key, 0, limit - 1)
                for data in data_list:
                    outcomes.append(json.loads(data))
                if outcomes:
                    return outcomes
            except Exception:
                pass
        
        # Fall back to file search
        try:
            files = sorted(self.outcomes_dir.glob("labeled_outcomes_*.jsonl"), reverse=True)
            for filepath in files:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            outcome = json.loads(line.strip())
                            if outcome.get('symbol') == symbol:
                                outcomes.append(outcome)
                                if len(outcomes) >= limit:
                                    return outcomes
        except Exception as e:
            logger.error(f"Failed to read outcomes for {symbol}: {e}")
        
        return outcomes
    
    def get_pending_count(self) -> int:
        """Get count of pending outcomes."""
        return len(self._pending)
    
    def get_stats(self) -> Dict:
        """Get labeler statistics."""
        return {
            'total_labeled': self._stats['total_labeled'],
            'pending_count': len(self._pending),
            'last_label_time': self._stats['last_label_time'],
            'horizons': self.horizons
        }
    
    def start_background_worker(self, interval_seconds: int = 300):
        """
        Start background worker to periodically label pending outcomes.
        
        Args:
            interval_seconds: Seconds between labeling runs
        """
        if self._worker_thread and self._worker_thread.is_alive():
            logger.warning("Background worker already running")
            return
        
        self._stop_worker.clear()
        
        def worker_loop():
            logger.info(f"Outcome labeling worker started (interval={interval_seconds}s)")
            while not self._stop_worker.is_set():
                try:
                    labeled = self.fetch_and_label_pending()
                    if labeled:
                        logger.info(f"Background labeled {len(labeled)} outcomes")
                except Exception as e:
                    logger.error(f"Background labeling error: {e}")
                
                self._stop_worker.wait(timeout=interval_seconds)
            
            logger.info("Outcome labeling worker stopped")
        
        self._worker_thread = threading.Thread(target=worker_loop, daemon=True)
        self._worker_thread.start()
    
    def stop_background_worker(self):
        """Stop the background worker."""
        if self._worker_thread:
            self._stop_worker.set()
            self._worker_thread.join(timeout=5)


# Singleton instance
_labeler_instance: Optional[OutcomeLabeler] = None


def get_labeler() -> OutcomeLabeler:
    """Get or create the global labeler instance."""
    global _labeler_instance
    if _labeler_instance is None:
        _labeler_instance = OutcomeLabeler()
    return _labeler_instance


def label_from_decision_log(decisions_path: str = None) -> int:
    """
    Process decisions from JSONL log and register for outcome tracking.
    
    Args:
        decisions_path: Path to trade_decisions.jsonl
        
    Returns:
        Number of decisions registered
    """
    path = Path(decisions_path) if decisions_path else Path(DECISIONS_PATH)
    if not path.exists():
        logger.warning(f"Decisions file not found: {path}")
        return 0
    
    labeler = get_labeler()
    registered = 0
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                
                try:
                    decision = json.loads(line.strip())
                    
                    # Skip if already registered
                    decision_id = f"{decision.get('symbol')}_{decision.get('ts', '')}"
                    if decision_id in labeler._pending:
                        continue
                    
                    # Get price from features
                    features = decision.get('features', {})
                    price_t0 = features.get('price')
                    if not price_t0:
                        indicators = decision.get('indicators', {})
                        price_t0 = indicators.get('price', 0)
                    
                    if not price_t0:
                        continue
                    
                    # Register decision
                    labeler.register_decision(
                        decision_id=decision_id,
                        symbol=decision.get('symbol', ''),
                        decision=decision.get('decision', 'HOLD'),
                        price_t0=float(price_t0),
                        timestamp=decision.get('ts', ''),
                        sentiment_score=decision.get('effective_sentiment', decision.get('sentiment_score', 0)),
                        ticker_confidence=decision.get('ticker_confidence', 1.0),
                        confidence_band=decision.get('confidence_band', 'unknown'),
                        extraction_methods=decision.get('extraction_reasons', []),
                        source_title=decision.get('source_title', '')
                    )
                    registered += 1
                    
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.warning(f"Failed to process decision: {e}")
    
    except Exception as e:
        logger.error(f"Failed to read decisions file: {e}")
    
    logger.info(f"Registered {registered} decisions for outcome tracking")
    return registered


if __name__ == "__main__":
    # Test the labeler
    labeler = OutcomeLabeler()
    
    # Register a test decision
    pending = labeler.register_decision(
        decision_id="test_001",
        symbol="AAPL",
        decision="BUY",
        price_t0=175.50,
        timestamp=datetime.utcnow().isoformat(),
        sentiment_score=0.65,
        ticker_confidence=0.85,
        confidence_band="high_0.8-0.9",
        extraction_methods=["explicit", "company_name"],
        source_title="Test article about Apple"
    )
    
    print(f"Registered: {pending}")
    
    # Simulate price updates
    labeler.update_price("test_001", 30, 176.20)
    labeler.update_price("test_001", 60, 177.00)
    outcome = labeler.update_price("test_001", 1440, 178.50)
    
    if outcome:
        print(f"\nLabeled outcome:")
        print(json.dumps(outcome.to_dict(), indent=2))
    
    # Get stats
    print(f"\nLabeler stats: {labeler.get_stats()}")
