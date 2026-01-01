# feature_store/feature_worker.py
"""
Feature worker (HARDENED): consumes market-ticks Kafka topic, computes indicators, saves features to Redis.
Run: python -m feature_store.feature_worker

INDICATOR WARMUP METADATA:
Technical indicators need historical data to be meaningful:
- SMA(20) needs 20 bars, otherwise computed on partial data
- RSI(14) needs 14 bars for avg gain/loss calculation
- MACD needs 26 bars for slow EMA + 9 for signal = 35 total

Each feature set includes `_meta` with warmup status:
{
  "indicator_count": X,        # Number of valid (non-None) indicators
  "ready": true/false,         # True if features are usable for trading
  "warmup_remaining": N,       # Bars until full warmup (50)
  "is_warmed_up": true/false,  # True if num_bars >= 50
  "trend_indicators_ready": M  # Count of ready trend indicators (SMA, EMA, MACD)
}

PERSISTENCE CONDITION:
Features saved to Redis only when:
- At least one trend indicator exists (sma_5, sma_20, ema_12, ema_26, macd), OR
- Price history length >= 5 (MIN_BARS_TO_PERSIST)

HARDENING:
- Ignores MISSING data_quality ticks
- Indicators return None (NOT 0.0) until warmup complete
- Price validation prevents NaN/Inf/negative values
- Worker NEVER crashes - all exceptions caught and logged
"""

import os
import json
import logging
import math
from time import sleep
from typing import Any, Dict, Optional
import pandas as pd
from kafka import KafkaConsumer
from kafka.errors import KafkaError

from feature_store.feature_store import compute_indicators, save_online_feature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("feature_worker")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
INPUT_TOPIC = os.getenv("INPUT_TOPIC", "market-ticks")
GROUP_ID = os.getenv("GROUP_ID", "feature-worker-group")
MAX_BUFFER = int(os.getenv("FEATURE_BUFFER_SIZE", "5000"))  # per-symbol

# ============================================================================
# WARMUP LOGIC: Technical indicators need historical data to compute correctly.
# 
# Why warmup matters:
# - SMA(20) needs 20 bars minimum, otherwise it's computed on partial data
# - RSI(14) needs 14 bars to calculate average gains/losses
# - MACD needs 26 bars for slow EMA + 9 for signal line = 35 total
# 
# CRITICAL: Indicators return None (NOT 0.0) until warmup is complete.
# Returning 0.0 would create false signals (e.g., RSI=0 implies extreme oversold)
# ============================================================================
WARMUP_MIN_BARS = int(os.getenv("WARMUP_MIN_BARS", "50"))  # Global warmup threshold
MIN_BARS_TO_PERSIST = int(os.getenv("MIN_BARS_TO_PERSIST", "5"))  # Minimum bars before saving

# Per-indicator warmup requirements (bars needed before indicator is valid)
WARMUP_INDICATOR_MAP = {
    # Trend indicators (moving averages)
    "sma_5": 5, "sma_20": 20, "sma_50": 50,
    "ema_12": 12, "ema_26": 26,
    # Momentum indicators
    "rsi_14": 14,  # RSI needs 14 periods for avg gain/loss
    "macd": 26,     # MACD = EMA(12) - EMA(26)
    "macd_signal": 35,  # Signal = EMA(9) of MACD, so 26 + 9 = 35
    "stoch_k": 14, "stoch_d": 17,  # Stochastic needs 14 + 3 smoothing
    "momentum_10": 10, "roc_10": 10,
    # Volatility indicators  
    "atr_14": 14,  # ATR needs 14 periods
    "boll_upper": 20, "boll_lower": 20, "boll_mid": 20,  # Bollinger uses SMA(20)
    "cci_20": 20,  # CCI uses 20 period typical price
    # Trend strength
    "adx": 14,  # ADX needs 14 periods for DI calculation
}

# Trend indicators used to determine if features are "ready"
TREND_INDICATORS = ["sma_5", "sma_20", "ema_12", "ema_26", "macd"]

# Data quality values (must match market_data_producer)
DATA_QUALITY_GOOD = "GOOD"
DATA_QUALITY_PARTIAL = "PARTIAL"
DATA_QUALITY_STALE = "STALE"
DATA_QUALITY_MISSING = "MISSING"

# Simple in-memory buffers: dict symbol -> list of ticks (dict)
buffers: Dict[str, list] = {}


def make_consumer():
    return KafkaConsumer(
        INPUT_TOPIC,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        auto_offset_reset="earliest",
        group_id=GROUP_ID,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")) if isinstance(v, (bytes, bytearray)) else json.loads(v),
        enable_auto_commit=True,
    )


def is_valid_price(price: Any) -> bool:
    """Check if a price value is valid (not None, NaN, or <= 0)."""
    if price is None:
        return False
    try:
        p = float(price)
        if math.isnan(p) or math.isinf(p):
            return False
        if p <= 0:
            return False
        return True
    except (ValueError, TypeError):
        return False


def compute_feature_completeness(features: Dict, num_bars: int) -> Dict:
    """
    Compute feature completeness metadata.
    
    WARMUP LOGIC:
    - Each indicator has a minimum bar requirement (see WARMUP_INDICATOR_MAP)
    - Indicators return None (NOT 0.0) until enough data exists
    - 'ready' = True when at least one trend indicator is valid
    - 'warmup_remaining' = bars needed until full warmup
    
    Returns dict with:
    - indicator_count: number of valid (non-None) indicators
    - ready: True if features are usable for trading decisions
    - warmup_remaining: bars until global warmup complete
    - is_warmed_up: True if num_bars >= WARMUP_MIN_BARS
    - completeness_pct: percentage of indicators that are valid
    """
    total_indicators = 0
    valid_indicators = 0
    not_ready_indicators = []
    trend_indicators_ready = 0
    
    for indicator, min_bars in WARMUP_INDICATOR_MAP.items():
        total_indicators += 1
        value = features.get(indicator)
        
        if num_bars < min_bars:
            # WARMUP: Not enough bars yet - indicator will be None
            not_ready_indicators.append(f"{indicator}(need {min_bars}, have {num_bars})")
        elif value is not None:
            valid_indicators += 1
            # Track trend indicator readiness
            if indicator in TREND_INDICATORS:
                trend_indicators_ready += 1
        else:
            # Indicator returned NaN despite having enough bars (edge case)
            not_ready_indicators.append(f"{indicator}(NaN)")
    
    completeness_pct = (valid_indicators / total_indicators * 100) if total_indicators > 0 else 0
    is_warmed_up = num_bars >= WARMUP_MIN_BARS
    warmup_remaining = max(0, WARMUP_MIN_BARS - num_bars)
    
    # READY: Features are usable if we have at least one trend indicator
    # OR we have minimum price history (MIN_BARS_TO_PERSIST)
    ready = (trend_indicators_ready > 0) or (num_bars >= MIN_BARS_TO_PERSIST)
    
    return {
        # Standard fields
        "num_bars": num_bars,
        "is_warmed_up": is_warmed_up,
        "completeness_pct": round(completeness_pct, 1),
        "valid_indicators": valid_indicators,
        "total_indicators": total_indicators,
        "not_ready": not_ready_indicators[:5] if not_ready_indicators else [],
        # New fields per requirements
        "indicator_count": valid_indicators,  # Alias for clarity
        "ready": ready,  # True if features are usable
        "warmup_remaining": warmup_remaining,  # Bars until full warmup
        "trend_indicators_ready": trend_indicators_ready,
    }


def process_tick(tick: dict):
    """
    Process a market tick and update features.
    
    tick: {"symbol":"AAPL", "ts":"2025-11-26T14:30:00Z", "price":277.95, "volume":100, "data_quality":"GOOD"}
    
    HARDENING:
    - Ignores ticks with data_quality=MISSING
    - Validates price before processing
    - Tracks warmup status per symbol
    """
    symbol = tick.get("symbol")
    if not symbol:
        logger.warning("tick missing symbol: %s", tick)
        return
    
    # HARDENING: Skip index symbols (^DJI, ^GSPC, etc.)
    if symbol.startswith("^"):
        logger.debug(f"Skipping index symbol: {symbol}")
        return

    # HARDENING: Check data_quality
    data_quality = tick.get("data_quality", DATA_QUALITY_GOOD)  # Default to GOOD for backwards compat
    if data_quality == DATA_QUALITY_MISSING:
        logger.debug(f"Ignoring MISSING quality tick for {symbol}")
        return
    
    # HARDENING: Validate price
    price = tick.get("price")
    if not is_valid_price(price):
        logger.warning(f"Invalid price for {symbol}: {price}, skipping tick")
        return
    
    price = float(price)

    ts = tick.get("ts")
    volume = tick.get("volume", 0.0)
    try:
        volume = float(volume) if volume else 0.0
    except (ValueError, TypeError):
        volume = 0.0

    buf = buffers.setdefault(symbol, [])
    buf.append({"ts": ts, "price": price, "volume": volume, "data_quality": data_quality})

    # trim buffer
    if len(buf) > MAX_BUFFER:
        buf[:] = buf[-MAX_BUFFER:]

    df = pd.DataFrame(buf)
    if df.empty:
        return

    # normalize ts column
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)
    
    num_bars = len(df)

    # compute indicators on whole buffer, pick last row
    df_ind = compute_indicators(df)
    last_row = df_ind.iloc[-1]

    # build features dict (safe conversion)
    def safe(v):
        if pd.isna(v) or v is None:
            return None
        return float(v)

    features = {
        "symbol": symbol,
        "ts": last_row["ts"].isoformat(),
        "price": safe(last_row.get("price")),
        # SMA indicators
        "SMA_5": safe(last_row.get("sma_5")),
        "SMA_20": safe(last_row.get("sma_20")),
        "SMA_50": safe(last_row.get("sma_50")),
        "sma_5": safe(last_row.get("sma_5")),
        "sma_20": safe(last_row.get("sma_20")),
        "sma_50": safe(last_row.get("sma_50")),
        # EMA indicators
        "ema_12": safe(last_row.get("ema_12")),
        "ema_26": safe(last_row.get("ema_26")),
        # MACD indicators
        "macd": safe(last_row.get("macd")),
        "macd_signal": safe(last_row.get("macd_signal")),
        "macd_hist": safe(last_row.get("macd_hist")),
        # RSI
        "RSI_14": safe(last_row.get("rsi_14")),
        "rsi_14": safe(last_row.get("rsi_14")),
        # Bollinger Bands
        "boll_upper": safe(last_row.get("boll_upper")),
        "boll_lower": safe(last_row.get("boll_lower")),
        "boll_mid": safe(last_row.get("boll_mid")),
        # Volume indicators
        "vwap": safe(last_row.get("vwap")),
        "obv": safe(last_row.get("obv")),
        # ATR - Average True Range (volatility)
        "atr_14": safe(last_row.get("atr_14")),
        # Stochastic Oscillator
        "stoch_k": safe(last_row.get("stoch_k")),
        "stoch_d": safe(last_row.get("stoch_d")),
        # Williams %R
        "williams_r": safe(last_row.get("williams_r")),
        # CCI - Commodity Channel Index
        "cci_20": safe(last_row.get("cci_20")),
        # ADX - Average Directional Index
        "adx": safe(last_row.get("adx")),
        "plus_di": safe(last_row.get("plus_di")),
        "minus_di": safe(last_row.get("minus_di")),
        # Momentum indicators
        "momentum_10": safe(last_row.get("momentum_10")),
        "roc_10": safe(last_row.get("roc_10")),
        # Price change
        "price_change": safe(last_row.get("price_change")),
        "price_change_pct": safe(last_row.get("price_change_pct")),
    }
    
    # HARDENING: Add feature completeness metadata for downstream consumers
    # This allows strategy_worker to detect if features are ready for trading
    completeness = compute_feature_completeness(features, num_bars)
    features["_meta"] = {
        # Data quality from source tick
        "data_quality": data_quality,
        "num_bars": num_bars,
        # Warmup status fields
        "is_warmed_up": completeness["is_warmed_up"],
        "completeness_pct": completeness["completeness_pct"],
        "valid_indicators": completeness["valid_indicators"],
        "total_indicators": completeness["total_indicators"],
        # ================================================================
        # KEY FIELDS FOR DOWNSTREAM CONSUMERS (strategy_worker):
        # - indicator_count: how many indicators have valid values
        # - ready: True if features are usable (has trend indicators OR bars >= 5)
        # - warmup_remaining: bars until global warmup (50) complete
        # - trend_indicators_ready: count of valid trend indicators (0-5)
        # ================================================================
        "indicator_count": completeness["indicator_count"],
        "ready": completeness["ready"],
        "warmup_remaining": completeness["warmup_remaining"],
        "trend_indicators_ready": completeness["trend_indicators_ready"],
    }

    # =========================================================================
    # PERSISTENCE CONDITION: Only save features when meaningful data exists
    # 
    # Save when:
    # - At least one trend indicator is ready (e.g., SMA_5 after 5 bars), OR
    # - Price history >= MIN_BARS_TO_PERSIST (default 5)
    #
    # This prevents polluting Redis with empty/useless feature sets
    # =========================================================================
    if not completeness["ready"]:
        logger.debug(f"Skipping save for {symbol}: not ready (bars={num_bars}, need {MIN_BARS_TO_PERSIST})")
        return
    
    # Save to Redis
    save_online_feature(symbol, features["ts"], features)
    
    # Log with warmup status
    if completeness["is_warmed_up"]:
        warmup_status = "READY"
    else:
        warmup_status = f"WARMUP({num_bars}/{WARMUP_MIN_BARS}, {completeness['warmup_remaining']} remaining)"
    
    logger.info(f"Saved features for {symbol} @ {features['ts']} [{warmup_status}] "
                f"quality={data_quality} indicators={completeness['indicator_count']}/{completeness['total_indicators']}")


def main():
    logger.info("Starting feature worker — connecting to %s", KAFKA_BOOTSTRAP)
    consumer = None
    for attempt in range(20):
        try:
            consumer = make_consumer()
            logger.info("Connected to Kafka; consuming %s", INPUT_TOPIC)
            break
        except KafkaError as e:
            logger.warning("Kafka connect failed (attempt %d): %s", attempt + 1, e)
            sleep(2)
    if consumer is None:
        logger.error("Failed to connect to Kafka; exiting")
        return

    try:
        for msg in consumer:
            try:
                # SAFETY: Wrap all processing in try/except to ensure worker NEVER crashes
                tick_data = msg.value
                
                # SAFETY: Handle malformed messages
                if tick_data is None:
                    logger.warning("Received None message, skipping")
                    continue
                if not isinstance(tick_data, dict):
                    logger.warning(f"Received non-dict message: {type(tick_data)}, skipping")
                    continue
                
                process_tick(tick_data)
                
            except json.JSONDecodeError as e:
                # SAFETY: Handle malformed JSON without crashing
                logger.warning(f"Malformed JSON in message: {e}")
                continue
            except KeyError as e:
                # SAFETY: Handle missing required fields
                logger.warning(f"Missing required field in tick: {e}")
                continue
            except Exception:
                # SAFETY: Catch-all to ensure worker continues
                logger.exception("Failed to process tick; continuing")
                continue
    except KeyboardInterrupt:
        logger.info("Feature worker interrupted")
    finally:
        if consumer:
            consumer.close()
        logger.info("Feature worker shutdown complete")


if __name__ == "__main__":
    main()
