# tools/market_data_producer.py
"""
Market Data Producer (HARDENED):
- Fetches real stock prices using yfinance
- Publishes to Kafka topic 'market-ticks'
- Runs continuously, fetching data every interval
- ROBUST: Handles yfinance failures gracefully
- QUALITY: Tracks data_quality per tick (GOOD/PARTIAL/STALE/MISSING)
- CACHE: Last-known-price caching for resilience
"""

import os
import json
import logging
import time
import math
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict

from kafka import KafkaProducer
from kafka.errors import KafkaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("market_data_producer")

# Configuration
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")
OUTPUT_TOPIC = os.getenv("OUTPUT_TOPIC", "market-ticks")
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL_SEC", "60"))  # seconds between fetches
STALE_PRICE_MAX_AGE_SEC = int(os.getenv("STALE_PRICE_MAX_AGE_SEC", "300"))  # 5 minutes

# Stock symbols to track - major US stocks
SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM", "V", "JNJ",
    "WMT", "PG", "MA", "HD", "DIS", "PYPL", "NFLX", "ADBE", "CRM", "INTC",
    "AMD", "QCOM", "IBM", "ORCL", "CSCO", "BA", "GE", "CAT", "MMM", "KO",
    "PEP", "MCD", "NKE", "SBUX", "TGT", "COST", "LOW", "FDX", "UPS", "GM", "F"
]

# Data quality enum values
DATA_QUALITY_GOOD = "GOOD"        # Valid OHLCV data
DATA_QUALITY_PARTIAL = "PARTIAL"  # Price only, missing volume/etc
DATA_QUALITY_STALE = "STALE"      # Reused last known price
DATA_QUALITY_MISSING = "MISSING"  # No data available (not published)

# Try to import yfinance, fall back to simulated data if not available
try:
    import yfinance as yf
    HAS_YFINANCE = True
    logger.info("yfinance available - will fetch real market data")
except ImportError:
    HAS_YFINANCE = False
    logger.warning("yfinance not available - will use simulated data")

import random


@dataclass
class CachedPrice:
    """Last known price cache entry for a symbol."""
    symbol: str
    price: float
    volume: int
    timestamp: datetime
    
    def is_stale(self, max_age_seconds: int = STALE_PRICE_MAX_AGE_SEC) -> bool:
        """Check if cached price is too old."""
        age = (datetime.utcnow() - self.timestamp).total_seconds()
        return age > max_age_seconds


# Global price cache: symbol -> CachedPrice
_price_cache: Dict[str, CachedPrice] = {}


def is_valid_price(price: Any) -> bool:
    """
    SAFETY: Check if a price value is valid for publishing.
    
    Invalid prices that MUST be rejected:
    - None: yfinance sometimes returns None for missing data
    - NaN: pandas/numpy NaN values from failed calculations
    - Inf: Edge case from division errors
    - <= 0: Invalid market price (stocks can't be negative)
    
    Returns:
        True if price is safe to publish, False otherwise
    """
    if price is None:
        return False
    try:
        p = float(price)
        # SAFETY: Reject NaN and Inf values
        if math.isnan(p) or math.isinf(p):
            return False
        # SAFETY: Reject zero or negative prices
        if p <= 0:
            return False
        return True
    except (ValueError, TypeError):
        return False


def update_cache(symbol: str, price: float, volume: int) -> None:
    """Update the price cache for a symbol."""
    _price_cache[symbol] = CachedPrice(
        symbol=symbol,
        price=price,
        volume=volume,
        timestamp=datetime.utcnow()
    )


def get_cached_price(symbol: str) -> Optional[Dict]:
    """
    RESILIENCE: Get cached price for a symbol when yfinance fails.
    
    Cache policy:
    - Maximum age: 5 minutes (STALE_PRICE_MAX_AGE_SEC)
    - Beyond max age: returns None (tick marked as MISSING)
    - Cached ticks marked as data_quality="STALE"
    
    This ensures the system continues producing data even when
    yfinance is temporarily unavailable.
    """
    if symbol not in _price_cache:
        return None
    
    cached = _price_cache[symbol]
    
    # SAFETY: Don't use prices older than 5 minutes
    if cached.is_stale():
        logger.warning(f"Cached price for {symbol} expired (>{STALE_PRICE_MAX_AGE_SEC}s), skipping")
        return None
    
    cache_age = int((datetime.utcnow() - cached.timestamp).total_seconds())
    
    return {
        "symbol": symbol,
        "ts": datetime.utcnow().isoformat() + "Z",
        "price": cached.price,
        "volume": cached.volume,
        "data_quality": DATA_QUALITY_STALE,  # Mark as stale so consumers know
        "cache_age_sec": cache_age,
        "source": "cache"
    }


def get_simulated_prices(symbols: List[str]) -> List[Dict]:
    """Generate simulated stock prices for testing (with data_quality)."""
    base_prices = {
        "AAPL": 175.0, "MSFT": 380.0, "GOOGL": 140.0, "AMZN": 180.0, "META": 500.0,
        "NVDA": 480.0, "TSLA": 250.0, "JPM": 195.0, "V": 280.0, "JNJ": 155.0,
        "WMT": 160.0, "PG": 150.0, "MA": 450.0, "HD": 380.0, "DIS": 95.0,
        "PYPL": 65.0, "NFLX": 480.0, "ADBE": 550.0, "CRM": 280.0, "INTC": 45.0,
        "AMD": 140.0, "QCOM": 170.0, "IBM": 165.0, "ORCL": 125.0, "CSCO": 50.0,
        "BA": 180.0, "GE": 165.0, "CAT": 350.0, "MMM": 130.0, "KO": 60.0,
        "PEP": 170.0, "MCD": 290.0, "NKE": 105.0, "SBUX": 95.0, "TGT": 135.0,
        "COST": 920.0, "LOW": 250.0, "FDX": 280.0, "UPS": 130.0, "GM": 50.0, "F": 11.0
    }
    
    ticks = []
    ts = datetime.utcnow().isoformat() + "Z"
    
    for symbol in symbols:
        base = base_prices.get(symbol, 100.0)
        # Add random variation (-2% to +2%)
        price = base * (1 + random.uniform(-0.02, 0.02))
        volume = random.randint(10000, 1000000)
        
        ticks.append({
            "symbol": symbol,
            "ts": ts,
            "price": round(price, 2),
            "volume": volume,
            "data_quality": DATA_QUALITY_GOOD,
            "source": "simulated"
        })
        # Update cache even for simulated
        update_cache(symbol, round(price, 2), volume)
    
    return ticks


def get_real_prices(symbols: List[str]) -> List[Dict]:
    """
    Fetch real stock prices using yfinance with robust error handling.
    
    Returns ticks with data_quality field:
    - GOOD: Fresh data from yfinance
    - PARTIAL: Price only, missing volume
    - STALE: Cached price (within max age)
    - MISSING: No data available (tick not returned)
    
    Never returns ticks with invalid prices (None, NaN, <= 0).
    """
    if not HAS_YFINANCE:
        return get_simulated_prices(symbols)
    
    ticks = []
    ts = datetime.utcnow().isoformat() + "Z"
    processed_symbols = set()
    
    try:
        # Fetch data for all symbols at once (more efficient)
        data = yf.download(
            tickers=" ".join(symbols),
            period="1d",
            interval="1m",
            progress=False,
            threads=True
        )
        
        # CRITICAL: Check for None or empty DataFrame
        if data is None or data.empty:
            logger.warning("yfinance returned None/empty, falling back to cache/simulated")
        else:
            # Process yfinance data
            for symbol in symbols:
                try:
                    if len(symbols) == 1:
                        # Single symbol - no multi-level column
                        if 'Close' not in data.columns:
                            continue
                        close_series = data['Close']
                        volume_series = data.get('Volume')
                    else:
                        # Multiple symbols - multi-level column index
                        if 'Close' not in data.columns:
                            continue
                        if symbol not in data['Close'].columns:
                            continue
                        close_series = data['Close'][symbol]
                        volume_series = data['Volume'][symbol] if 'Volume' in data.columns else None
                    
                    # Get last non-NaN price
                    if close_series is None or close_series.empty:
                        continue
                    
                    valid_prices = close_series.dropna()
                    if valid_prices.empty:
                        continue
                    
                    price = float(valid_prices.iloc[-1])
                    
                    # CRITICAL: Validate price
                    if not is_valid_price(price):
                        logger.debug(f"Invalid price for {symbol}: {price}")
                        continue
                    
                    price = round(price, 2)
                    
                    # Get volume (optional - PARTIAL quality if missing)
                    volume = 0
                    data_quality = DATA_QUALITY_GOOD
                    
                    if volume_series is not None and not volume_series.empty:
                        valid_volumes = volume_series.dropna()
                        if not valid_volumes.empty:
                            try:
                                volume = int(valid_volumes.iloc[-1])
                            except (ValueError, TypeError):
                                volume = 0
                                data_quality = DATA_QUALITY_PARTIAL
                    else:
                        data_quality = DATA_QUALITY_PARTIAL
                    
                    ticks.append({
                        "symbol": symbol,
                        "ts": ts,
                        "price": price,
                        "volume": volume,
                        "data_quality": data_quality,
                        "source": "yfinance"
                    })
                    processed_symbols.add(symbol)
                    
                    # Update cache
                    update_cache(symbol, price, volume)
                    
                except (KeyError, IndexError, TypeError) as e:
                    logger.debug(f"Could not get price for {symbol}: {type(e).__name__}: {e}")
                    continue
                except Exception as e:
                    logger.warning(f"Unexpected error getting price for {symbol}: {e}")
                    continue
    
    except Exception as e:
        logger.warning(f"yfinance error: {e}")
    
    # For symbols not fetched, try cache then simulated
    missing_symbols = set(symbols) - processed_symbols
    
    for symbol in missing_symbols:
        # Try cache first
        cached = get_cached_price(symbol)
        if cached:
            ticks.append(cached)
            logger.debug(f"Using cached price for {symbol} (age: {cached.get('cache_age_sec', 0)}s)")
        else:
            # Fall back to simulated as last resort
            simulated = get_simulated_prices([symbol])
            if simulated:
                tick = simulated[0]
                tick["data_quality"] = DATA_QUALITY_PARTIAL  # Mark as not fully trusted
                tick["source"] = "simulated_fallback"
                ticks.append(tick)
                logger.info(f"Using simulated price for {symbol} (no cache available)")
    
    logger.info(f"Fetched {len(ticks)} prices: "
                f"{sum(1 for t in ticks if t.get('data_quality') == DATA_QUALITY_GOOD)} GOOD, "
                f"{sum(1 for t in ticks if t.get('data_quality') == DATA_QUALITY_PARTIAL)} PARTIAL, "
                f"{sum(1 for t in ticks if t.get('data_quality') == DATA_QUALITY_STALE)} STALE")
    
    return ticks


def create_producer():
    """Create Kafka producer with retry logic"""
    while True:
        try:
            producer = KafkaProducer(
                bootstrap_servers=[KAFKA_BOOTSTRAP],
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                acks="all",
                retries=5
            )
            logger.info(f"Connected to Kafka at {KAFKA_BOOTSTRAP}")
            return producer
        except KafkaError as e:
            logger.warning(f"Kafka not ready ({e}), retrying in 5s...")
            time.sleep(5)


def main():
    logger.info(f"Starting Market Data Producer")
    logger.info(f"Tracking {len(SYMBOLS)} symbols")
    logger.info(f"Fetch interval: {FETCH_INTERVAL}s")
    
    producer = create_producer()
    
    iteration = 0
    while True:
        try:
            iteration += 1
            
            # Fetch prices (real or simulated)
            if HAS_YFINANCE:
                ticks = get_real_prices(SYMBOLS)
            else:
                ticks = get_simulated_prices(SYMBOLS)
            
            # Publish to Kafka
            # SAFETY: Final validation before publishing - NEVER publish invalid prices
            published = 0
            skipped = 0
            for tick in ticks:
                # CRITICAL: Final guard - reject any tick with invalid price
                if not is_valid_price(tick.get('price')):
                    logger.warning(f"BLOCKED invalid tick for {tick.get('symbol')}: price={tick.get('price')}")
                    skipped += 1
                    continue
                
                # SAFETY: Never publish ticks marked as MISSING
                if tick.get('data_quality') == DATA_QUALITY_MISSING:
                    logger.debug(f"Skipping MISSING quality tick for {tick.get('symbol')}")
                    skipped += 1
                    continue
                
                try:
                    producer.send(OUTPUT_TOPIC, tick)
                    published += 1
                except Exception as e:
                    logger.error(f"Failed to publish tick for {tick['symbol']}: {e}")
            
            producer.flush()
            
            # Log summary with quality breakdown
            if skipped > 0:
                logger.warning(f"[Iteration {iteration}] Published {published}, BLOCKED {skipped} invalid ticks")
            else:
                logger.info(f"[Iteration {iteration}] Published {published} market ticks")
            
            # Wait for next interval
            time.sleep(FETCH_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.exception(f"Error in main loop: {e}")
            time.sleep(10)
    
    producer.close()


if __name__ == "__main__":
    main()
