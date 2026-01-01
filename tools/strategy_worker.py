# tools/strategy_worker.py
"""
Strategy worker (HARDENED):
- consumes news-sentiment Kafka topic
- reads latest features from Redis (or feature-api fallback)
- applies deterministic rules (decide_trade_single)
- uses symbol confidence to weight decisions
- publishes trade-decisions to Kafka topic 'trade-decisions'
- appends decisions to results/trade_decisions.jsonl
- tracks outcomes for learning

Pipeline:
  news-sentiment → strategy-worker → trade-decisions
  
Key Features:
- Symbol confidence filtering (>= 0.7 for trading)
- Effective sentiment = sentiment_score * symbol_confidence
- Outcome tracking for system maturity

HARDENING:
- Index filtering (skip ^symbols)
- News-only decisions when indicators not ready (is_warmed_up=False)
- Signal fusion with explicit weights
- Decision explanations for transparency
- Feature completeness awareness
"""

import os
import json
import time
import logging
from datetime import datetime
from typing import Any, Dict, Optional, List

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError
import requests

try:
    import redis as redis_lib
except Exception:
    redis_lib = None

# Import outcome tracker for learning
try:
    from enrich.ticker_extraction.outcome_tracker import get_tracker, DecisionOutcomeTracker
    OUTCOME_TRACKING_ENABLED = True
except ImportError:
    OUTCOME_TRACKING_ENABLED = False
    get_tracker = None

# Import band analytics for dashboard metric
try:
    from enrich.ticker_extraction.band_analytics import get_analytics, ConfidenceBandAnalytics
    BAND_ANALYTICS_ENABLED = True
except ImportError:
    BAND_ANALYTICS_ENABLED = False
    get_analytics = None

# Import outcome labeler for automatic outcome tracking
try:
    from evaluation.outcome_labeler import get_labeler
    OUTCOME_LABELING_ENABLED = True
except ImportError:
    OUTCOME_LABELING_ENABLED = False
    get_labeler = None

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("strategy_worker")

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")
INPUT_TOPIC = os.getenv("INPUT_TOPIC", "news-sentiment")
OUTPUT_TOPIC = os.getenv("OUTPUT_TOPIC", "trade-decisions")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
FEATURE_API = os.getenv("FEATURE_API_URL", "http://feature-api:8100")

SENTIMENT_BUY = float(os.getenv("SENTIMENT_BUY", "0.4"))
SENTIMENT_SELL = float(os.getenv("SENTIMENT_SELL", "-0.3"))

# Confidence thresholds
MIN_CONFIDENCE_FOR_TRADING = float(os.getenv("MIN_CONFIDENCE", "0.7"))
IGNORE_CONFIDENCE_BELOW = float(os.getenv("IGNORE_CONFIDENCE", "0.5"))

RESULTS_PATH = os.getenv("RESULTS_PATH", "results/trade_decisions.jsonl")

# Signal fusion weights (HARDENING)
TECHNICAL_WEIGHT = float(os.getenv("TECHNICAL_WEIGHT", "0.6"))  # 60% technical
SENTIMENT_WEIGHT = float(os.getenv("SENTIMENT_WEIGHT", "0.4"))  # 40% sentiment

# ACTIONABLE TRADE THRESHOLDS - Lowered for real-time trading
# News-only mode (when no technical indicators available)
SENTIMENT_STRONG_BUY = float(os.getenv("SENTIMENT_STRONG_BUY", "0.70"))    # Lowered from 0.75
SENTIMENT_STRONG_SELL = float(os.getenv("SENTIMENT_STRONG_SELL", "-0.65")) # Keep at -0.65

# Partial mode (when some indicators available but not fully warmed)
PARTIAL_BUY_THRESHOLD = float(os.getenv("PARTIAL_BUY_THRESHOLD", "0.55"))   # Combined score threshold
PARTIAL_SELL_THRESHOLD = float(os.getenv("PARTIAL_SELL_THRESHOLD", "-0.55"))
MIN_PARTIAL_INDICATORS = int(os.getenv("MIN_PARTIAL_INDICATORS", "2"))  # Min indicators for partial mode

# Full mode threshold
FULL_MODE_SIGNAL_RATIO = float(os.getenv("FULL_MODE_SIGNAL_RATIO", "0.45"))  # Lowered from 0.50

# Index symbols to skip (no tradeable decisions)
INDEX_PREFIXES = ["^"]  # ^DJI, ^GSPC, ^IXIC, etc.

# -------------------------------------------------------------------
# Redis client (optional)
# -------------------------------------------------------------------
r: Optional[Any] = None
if redis_lib:
    try:
        r = redis_lib.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        logger.info("Redis connected for feature lookup (%s)", REDIS_URL)
    except Exception:
        logger.warning("Redis unavailable, will use Feature API fallback")
        r = None
else:
    logger.info("redis library not available; will use Feature API fallback")

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def is_index_symbol(symbol: str) -> bool:
    """Check if symbol is an index (non-tradeable)."""
    if not symbol:
        return False
    return any(symbol.startswith(prefix) for prefix in INDEX_PREFIXES)


def _bootstrap_list(bootstrap: str) -> List[str]:
    # Accept "host:9092" or "host1:9092,host2:9092"
    if not bootstrap:
        return ["redpanda:9092"]
    return [s.strip() for s in bootstrap.split(",") if s.strip()]

def _deserialize_message(v):
    if v is None:
        return {}
    if isinstance(v, (bytes, bytearray)):
        try:
            s = v.decode("utf-8")
        except Exception:
            s = v.decode("latin-1", errors="ignore")
    else:
        s = v
    if isinstance(s, str):
        try:
            return json.loads(s)
        except Exception:
            # not JSON, return as-is
            return s
    return s

def append_result(record: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(RESULTS_PATH) or ".", exist_ok=True)
    try:
        with open(RESULTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to append result to %s", RESULTS_PATH)

# -------------------------------------------------------------------
# Feature fetching
# -------------------------------------------------------------------
def _fetch_features_from_redis_latest(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Find latest Redis key for features:{symbol}:* and return parsed JSON.
    """
    if not r:
        return None
    try:
        pattern = f"features:{symbol}:*"
        keys = r.keys(pattern)
        if not keys:
            return None
        # sort keys lexicographically; timestamps in ISO format should sort fine
        keys_sorted = sorted(keys)
        best = keys_sorted[-1]
        raw = r.get(best)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            logger.exception("Failed to parse JSON from Redis key %s", best)
            return None
    except Exception:
        logger.exception("Redis feature lookup failed for %s", symbol)
        return None

def _fetch_features_from_api(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Query feature API; prefer /features/{symbol}/latest if available.
    """
    try:
        # Try the /latest endpoint first
        url_latest = f"{FEATURE_API}/features/{symbol}/latest"
        resp = requests.get(url_latest, timeout=4)
        if resp.status_code == 200:
            return resp.json()
        # Fallback to query param style
        url_q = f"{FEATURE_API}/indicators?symbol={symbol}"
        resp = requests.get(url_q, timeout=4)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        logger.debug("Feature API fetch failed for %s", symbol, exc_info=True)
    return None

def get_features(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch features from Redis or Feature API; return None if not available."""
    # Try Redis latest
    if r:
        f = _fetch_features_from_redis_latest(symbol)
        if f:
            return f
    # Try feature API
    f = _fetch_features_from_api(symbol)
    return f

# -------------------------------------------------------------------
# Strategy logic
# -------------------------------------------------------------------
def _get_float_from_features(features: Dict[str, Any], *keys, default=None) -> Optional[float]:
    """
    Try several casing variants for keys.
    """
    for k in keys:
        if k in features:
            try:
                v = features[k]
                if v is None:
                    continue
                return float(v)
            except Exception:
                continue
    return default

def decide_trade_single(score: Optional[float], features: Optional[Dict[str, Any]], 
                        return_explanation: bool = True) -> Dict[str, Any]:
    """
    Advanced trading decision logic using multiple technical indicators.
    Returns: Dict with decision and explanation
    
    HARDENING:
    - News-only mode when indicators not ready (is_warmed_up=False)
    - Signal fusion with explicit weights (60% technical, 40% sentiment)
    - Full decision explanation for transparency
    - Conservative defaults when data is missing
    
    Strategy combines:
    1. Sentiment analysis score
    2. Moving average crossover (SMA_5 vs SMA_20)
    3. RSI (overbought/oversold)
    4. MACD histogram (momentum)
    5. Stochastic oscillator (momentum)
    6. Williams %R (overbought/oversold)
    7. ADX (trend strength)
    8. CCI (trend identification)
    """
    explanation = {
        "signals": [],
        "mode": "unknown",
        "buy_score": 0.0,
        "sell_score": 0.0,
        "signal_count": 0,
        "feature_completeness": 0.0,
        "is_warmed_up": False,
    }
    
    if score is None:
        explanation["mode"] = "no_sentiment"
        explanation["reason"] = "No sentiment score available"
        result = {"decision": "HOLD", "explanation": explanation}
        return result if return_explanation else "HOLD"

    # Check if features are warmed up
    is_warmed_up = False
    feature_completeness = 0.0
    if features:
        meta = features.get("_meta", {})
        is_warmed_up = meta.get("is_warmed_up", False)
        feature_completeness = meta.get("completeness_pct", 0.0)
    
    explanation["is_warmed_up"] = is_warmed_up
    explanation["feature_completeness"] = feature_completeness

    # Extract all technical indicators
    sma5 = sma20 = sma50 = rsi14 = None
    macd_val = macd_hist = macd_signal = None
    stoch_k = stoch_d = williams = None
    cci_val = adx_val = plus_di = minus_di = None
    momentum_val = roc_val = None
    boll_upper = boll_lower = price = None
    
    has_basic_features = False
    has_advanced_features = False
    
    if features and is_warmed_up:
        try:
            # Basic indicators
            sma5 = _get_float_from_features(features, "SMA_5", "sma_5", "sma5", "SMA5")
            sma20 = _get_float_from_features(features, "SMA_20", "sma_20", "sma20", "SMA20")
            sma50 = _get_float_from_features(features, "SMA_50", "sma_50", "sma50", "SMA50")
            rsi14 = _get_float_from_features(features, "RSI_14", "rsi_14", "RSI14", "rsi14")
            price = _get_float_from_features(features, "price", "Price", "PRICE")
            
            # MACD
            macd_val = _get_float_from_features(features, "macd", "MACD")
            macd_hist = _get_float_from_features(features, "macd_hist", "MACD_hist", "macd_histogram")
            macd_signal = _get_float_from_features(features, "macd_signal", "MACD_signal")
            
            # Bollinger Bands
            boll_upper = _get_float_from_features(features, "boll_upper", "BOLL_upper")
            boll_lower = _get_float_from_features(features, "boll_lower", "BOLL_lower")
            
            # Advanced indicators
            stoch_k = _get_float_from_features(features, "stoch_k", "STOCH_K", "stochastic_k")
            stoch_d = _get_float_from_features(features, "stoch_d", "STOCH_D", "stochastic_d")
            williams = _get_float_from_features(features, "williams_r", "WILLIAMS_R", "williams_percent_r")
            cci_val = _get_float_from_features(features, "cci_20", "CCI_20", "cci")
            adx_val = _get_float_from_features(features, "adx", "ADX")
            plus_di = _get_float_from_features(features, "plus_di", "PLUS_DI", "+DI")
            minus_di = _get_float_from_features(features, "minus_di", "MINUS_DI", "-DI")
            momentum_val = _get_float_from_features(features, "momentum_10", "MOMENTUM_10", "momentum")
            roc_val = _get_float_from_features(features, "roc_10", "ROC_10", "roc")
            
            has_basic_features = all(x is not None for x in [sma5, sma20, rsi14])
            has_advanced_features = all(x is not None for x in [stoch_k, williams, adx_val])
        except Exception:
            has_basic_features = False
            has_advanced_features = False
    
    # ============================================================
    # COUNT AVAILABLE PARTIAL INDICATORS
    # ============================================================
    available_indicators = {}
    partial_buy = 0.0
    partial_sell = 0.0
    partial_count = 0
    
    if features:
        # Check each indicator even if not fully warmed up
        try:
            _sma5 = _get_float_from_features(features, "SMA_5", "sma_5", "sma5", "SMA5")
            _sma20 = _get_float_from_features(features, "SMA_20", "sma_20", "sma20", "SMA20")
            _rsi14 = _get_float_from_features(features, "RSI_14", "rsi_14", "RSI14", "rsi14")
            _macd_hist = _get_float_from_features(features, "macd_hist", "MACD_hist", "macd_histogram")
            _price = _get_float_from_features(features, "price", "Price", "PRICE")
            
            if _sma5 is not None:
                available_indicators["sma5"] = _sma5
                partial_count += 1
            if _sma20 is not None:
                available_indicators["sma20"] = _sma20
                partial_count += 1
            if _rsi14 is not None:
                available_indicators["rsi14"] = _rsi14
                partial_count += 1
                # RSI signal for partial mode
                if _rsi14 < 30:
                    partial_buy += 1.0
                elif _rsi14 > 70:
                    partial_sell += 1.0
            if _macd_hist is not None:
                available_indicators["macd_hist"] = _macd_hist
                partial_count += 1
                # MACD signal for partial mode
                if _macd_hist > 0:
                    partial_buy += 0.5
                elif _macd_hist < 0:
                    partial_sell += 0.5
            if _price is not None:
                available_indicators["price"] = _price
            
            # SMA crossover for partial mode (if both available)
            if _sma5 is not None and _sma20 is not None:
                if _sma5 > _sma20:
                    partial_buy += 1.0
                elif _sma5 < _sma20:
                    partial_sell += 1.0
        except Exception:
            pass
    
    explanation["available_indicators"] = list(available_indicators.keys())
    explanation["partial_indicator_count"] = partial_count
    
    # ============================================================
    # PARTIAL MODE (some indicators but not fully warmed)
    # Use partial indicators + sentiment for better decisions
    # ============================================================
    if not is_warmed_up or not has_basic_features:
        # Check if we have enough partial indicators for partial mode
        has_partial_indicators = partial_count >= MIN_PARTIAL_INDICATORS
        
        if has_partial_indicators:
            # PARTIAL MODE: Combine sentiment with available indicators
            explanation["mode"] = "partial"
            explanation["reason"] = f"Partial mode: {partial_count} indicators available"
            
            # Calculate weighted combined score
            # Sentiment contributes 60% in partial mode, available indicators 40%
            sentiment_contribution = score * 0.6
            
            # Normalize partial indicator score to -1 to 1 range
            if partial_count > 0:
                partial_tech_score = (partial_buy - partial_sell) / max(partial_buy + partial_sell, 1)
            else:
                partial_tech_score = 0.0
            
            technical_contribution = partial_tech_score * 0.4
            combined_score = sentiment_contribution + technical_contribution
            
            explanation["signals"].append({
                "name": "sentiment",
                "value": score,
                "weight": 0.6,
                "signal": "BUY" if score > 0.3 else ("SELL" if score < -0.3 else "HOLD")
            })
            
            explanation["signals"].append({
                "name": "partial_technical",
                "value": {"buy": partial_buy, "sell": partial_sell, "score": partial_tech_score},
                "weight": 0.4,
                "signal": "BUY" if partial_buy > partial_sell else ("SELL" if partial_sell > partial_buy else "HOLD"),
                "indicators_used": list(available_indicators.keys())
            })
            
            explanation["combined_score"] = combined_score
            explanation["sentiment_contribution"] = sentiment_contribution
            explanation["technical_contribution"] = technical_contribution
            
            # Decision based on combined score
            if combined_score >= PARTIAL_BUY_THRESHOLD:
                explanation["buy_score"] = combined_score
                result = {"decision": "BUY", "explanation": explanation}
                return result if return_explanation else "BUY"
            elif combined_score <= PARTIAL_SELL_THRESHOLD:
                explanation["sell_score"] = abs(combined_score)
                result = {"decision": "SELL", "explanation": explanation}
                return result if return_explanation else "SELL"
            
            result = {"decision": "HOLD", "explanation": explanation}
            return result if return_explanation else "HOLD"
        
        # NEWS-ONLY MODE (no partial indicators available)
        explanation["mode"] = "news_only"
        explanation["reason"] = f"News-only: indicators not ready (warmed_up={is_warmed_up}, basic={has_basic_features}, partial={partial_count})"
        
        explanation["signals"].append({
            "name": "sentiment_only",
            "value": score,
            "weight": 1.0,
            "signal": "BUY" if score >= SENTIMENT_STRONG_BUY else ("SELL" if score <= SENTIMENT_STRONG_SELL else "HOLD"),
            "note": f"News-only mode: score={score:.3f}, thresholds: buy>={SENTIMENT_STRONG_BUY}, sell<={SENTIMENT_STRONG_SELL}"
        })
        
        if score >= SENTIMENT_STRONG_BUY:
            explanation["buy_score"] = 1.0
            result = {"decision": "BUY", "explanation": explanation}
            return result if return_explanation else "BUY"
        if score <= SENTIMENT_STRONG_SELL:
            explanation["sell_score"] = 1.0
            result = {"decision": "SELL", "explanation": explanation}
            return result if return_explanation else "SELL"
        
        result = {"decision": "HOLD", "explanation": explanation}
        return result if return_explanation else "HOLD"
    
    # ============================================================
    # FULL MODE (with technical indicators)
    # ============================================================
    explanation["mode"] = "full" if has_advanced_features else "basic"

    # Calculate signal scores based on available indicators
    buy_signals = 0.0
    sell_signals = 0.0
    signal_count = 0.0
    
    # 1. Sentiment Score (weight: SENTIMENT_WEIGHT as total sentiment contribution)
    sent_signal = "HOLD"
    if score > SENTIMENT_BUY:
        buy_signals += SENTIMENT_WEIGHT * 2
        sent_signal = "BUY"
    elif score < SENTIMENT_SELL:
        sell_signals += SENTIMENT_WEIGHT * 2
        sent_signal = "SELL"
    signal_count += SENTIMENT_WEIGHT * 2
    
    explanation["signals"].append({
        "name": "sentiment",
        "value": score,
        "weight": SENTIMENT_WEIGHT * 2,
        "signal": sent_signal,
        "thresholds": {"buy": SENTIMENT_BUY, "sell": SENTIMENT_SELL}
    })
    
    # Technical indicators (only if features ready)
    if has_basic_features:
        # Scale technical weights to sum to TECHNICAL_WEIGHT
        tech_scale = TECHNICAL_WEIGHT / 0.6  # Normalize
        
        # 2. Moving Average Crossover (weight: 2 * tech_scale)
        ma_signal = "HOLD"
        if sma5 > sma20:
            buy_signals += 2 * tech_scale
            ma_signal = "BUY"
        elif sma5 < sma20:
            sell_signals += 2 * tech_scale
            ma_signal = "SELL"
        signal_count += 2 * tech_scale
        
        explanation["signals"].append({
            "name": "ma_crossover",
            "value": {"sma5": sma5, "sma20": sma20},
            "weight": 2 * tech_scale,
            "signal": ma_signal
        })
        
        # 3. RSI (weight: 1.5 * tech_scale)
        if rsi14 is not None:
            rsi_signal = "HOLD"
            if rsi14 < 30:  # Oversold - potential buy
                buy_signals += 1.5 * tech_scale
                rsi_signal = "BUY"
            elif rsi14 > 70:  # Overbought - potential sell
                sell_signals += 1.5 * tech_scale
                rsi_signal = "SELL"
            signal_count += 1.5 * tech_scale
            
            explanation["signals"].append({
                "name": "rsi",
                "value": rsi14,
                "weight": 1.5 * tech_scale,
                "signal": rsi_signal,
                "thresholds": {"oversold": 30, "overbought": 70}
            })
        
        # 4. MACD Histogram (weight: 1.5 * tech_scale)
        if macd_hist is not None:
            macd_signal_val = "HOLD"
            if macd_hist > 0:
                buy_signals += 1.5 * tech_scale
                macd_signal_val = "BUY"
            elif macd_hist < 0:
                sell_signals += 1.5 * tech_scale
                macd_signal_val = "SELL"
            signal_count += 1.5 * tech_scale
            
            explanation["signals"].append({
                "name": "macd_hist",
                "value": macd_hist,
                "weight": 1.5 * tech_scale,
                "signal": macd_signal_val
            })
        
        # 5. Bollinger Bands (weight: 1 * tech_scale)
        if price is not None and boll_upper is not None and boll_lower is not None:
            bb_signal = "HOLD"
            if price < boll_lower:  # Below lower band - potential buy
                buy_signals += 1 * tech_scale
                bb_signal = "BUY"
            elif price > boll_upper:  # Above upper band - potential sell
                sell_signals += 1 * tech_scale
                bb_signal = "SELL"
            signal_count += 1 * tech_scale
            
            explanation["signals"].append({
                "name": "bollinger",
                "value": {"price": price, "upper": boll_upper, "lower": boll_lower},
                "weight": 1 * tech_scale,
                "signal": bb_signal
            })
    
    if has_advanced_features:
        tech_scale = TECHNICAL_WEIGHT / 0.6
        
        # 6. Stochastic Oscillator (weight: 1 * tech_scale)
        if stoch_k is not None and stoch_d is not None:
            stoch_signal = "HOLD"
            if stoch_k < 20 and stoch_d < 20:  # Oversold
                buy_signals += 1 * tech_scale
                stoch_signal = "BUY"
            elif stoch_k > 80 and stoch_d > 80:  # Overbought
                sell_signals += 1 * tech_scale
                stoch_signal = "SELL"
            signal_count += 1 * tech_scale
            
            explanation["signals"].append({
                "name": "stochastic",
                "value": {"k": stoch_k, "d": stoch_d},
                "weight": 1 * tech_scale,
                "signal": stoch_signal
            })
        
        # 7. Williams %R (weight: 1 * tech_scale)
        if williams is not None:
            will_signal = "HOLD"
            if williams < -80:  # Oversold
                buy_signals += 1 * tech_scale
                will_signal = "BUY"
            elif williams > -20:  # Overbought
                sell_signals += 1 * tech_scale
                will_signal = "SELL"
            signal_count += 1 * tech_scale
            
            explanation["signals"].append({
                "name": "williams_r",
                "value": williams,
                "weight": 1 * tech_scale,
                "signal": will_signal
            })
        
        # 8. CCI (weight: 1 * tech_scale)
        if cci_val is not None:
            cci_signal = "HOLD"
            if cci_val < -100:  # Oversold
                buy_signals += 1 * tech_scale
                cci_signal = "BUY"
            elif cci_val > 100:  # Overbought
                sell_signals += 1 * tech_scale
                cci_signal = "SELL"
            signal_count += 1 * tech_scale
            
            explanation["signals"].append({
                "name": "cci",
                "value": cci_val,
                "weight": 1 * tech_scale,
                "signal": cci_signal
            })
        
        # 9. ADX with DI (weight: 1.5 * tech_scale - trend confirmation)
        if adx_val is not None and plus_di is not None and minus_di is not None:
            adx_signal = "HOLD"
            if adx_val > 25:  # Strong trend
                if plus_di > minus_di:  # Uptrend
                    buy_signals += 1.5 * tech_scale
                    adx_signal = "BUY"
                else:  # Downtrend
                    sell_signals += 1.5 * tech_scale
                    adx_signal = "SELL"
            signal_count += 1.5 * tech_scale
            
            explanation["signals"].append({
                "name": "adx",
                "value": {"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di},
                "weight": 1.5 * tech_scale,
                "signal": adx_signal
            })
        
        # 10. Momentum/ROC (weight: 1 * tech_scale)
        mom_signal = "HOLD"
        if momentum_val is not None:
            if momentum_val > 0:
                buy_signals += 0.5 * tech_scale
                mom_signal = "BUY"
            elif momentum_val < 0:
                sell_signals += 0.5 * tech_scale
                mom_signal = "SELL"
        if roc_val is not None:
            if roc_val > 0:
                buy_signals += 0.5 * tech_scale
            elif roc_val < 0:
                sell_signals += 0.5 * tech_scale
        signal_count += 1 * tech_scale
        
        explanation["signals"].append({
            "name": "momentum",
            "value": {"momentum": momentum_val, "roc": roc_val},
            "weight": 1 * tech_scale,
            "signal": mom_signal
        })
    
    # Store scores in explanation
    explanation["buy_score"] = buy_signals
    explanation["sell_score"] = sell_signals
    explanation["signal_count"] = signal_count
    
    # Decision based on signal scores
    decision = "HOLD"
    if signal_count > 0:
        buy_ratio = buy_signals / signal_count
        sell_ratio = sell_signals / signal_count
        
        explanation["buy_ratio"] = round(buy_ratio, 3)
        explanation["sell_ratio"] = round(sell_ratio, 3)
        
        # Use configurable threshold for action (default 45%, lowered from 50%)
        if buy_ratio >= FULL_MODE_SIGNAL_RATIO and buy_signals > sell_signals:
            decision = "BUY"
        elif sell_ratio >= FULL_MODE_SIGNAL_RATIO and sell_signals > buy_signals:
            decision = "SELL"
    
    result = {"decision": decision, "explanation": explanation}
    return result if return_explanation else decision

def extract_indicator_summary(features: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Extract a summary of all indicators from features for inclusion in trade decisions.
    """
    if not features:
        return {}
    
    summary = {}
    indicator_keys = [
        "price", "sma_5", "sma_20", "sma_50", "ema_12", "ema_26",
        "macd", "macd_signal", "macd_hist", "rsi_14",
        "boll_upper", "boll_lower", "boll_mid", "vwap", "obv",
        "atr_14", "stoch_k", "stoch_d", "williams_r", "cci_20",
        "adx", "plus_di", "minus_di", "momentum_10", "roc_10",
        "price_change", "price_change_pct"
    ]
    
    for key in indicator_keys:
        # Try multiple case variations
        for k in [key, key.upper(), key.replace("_", "")]:
            if k in features:
                try:
                    val = features[k]
                    if val is not None:
                        summary[key] = float(val) if not isinstance(val, str) else val
                        break
                except Exception:
                    continue
    
    return summary


def build_human_readable_explanation(decision: str, explanation: Dict[str, Any], 
                                      effective_sentiment: float, 
                                      ticker_confidence: float) -> Dict[str, Any]:
    """
    Build a clean, human-readable decision explanation object.
    
    Returns:
    {
      "decision": "BUY"/"SELL"/"HOLD",
      "sentiment_score": float,
      "technical_score": float,
      "confidence": float,
      "reasons": [list of human-readable strings]
    }
    """
    reasons = []
    mode = explanation.get("mode", "unknown")
    buy_score = explanation.get("buy_score", 0.0)
    sell_score = explanation.get("sell_score", 0.0)
    signal_count = explanation.get("signal_count", 0.0)
    
    # Calculate technical score (-1 to 1 scale)
    technical_score = 0.0
    if signal_count > 0:
        # Normalize: (buy - sell) / total gives -1 to 1 range
        technical_score = (buy_score - sell_score) / signal_count
    
    # For partial mode, use combined_score as technical
    if mode == "partial":
        technical_score = explanation.get("technical_contribution", 0.0) / 0.4 if explanation.get("technical_contribution") else 0.0
    
    # Build human-readable reasons
    if mode == "news_only":
        reasons.append(f"Mode: NEWS-ONLY (indicators not ready)")
        if decision == "BUY":
            reasons.append(f"Strong positive sentiment ({effective_sentiment:.2f} >= {SENTIMENT_STRONG_BUY} threshold)")
        elif decision == "SELL":
            reasons.append(f"Strong negative sentiment ({effective_sentiment:.2f} <= {SENTIMENT_STRONG_SELL} threshold)")
        else:
            reasons.append(f"Sentiment ({effective_sentiment:.2f}) not strong enough for news-only trade")
            reasons.append("Waiting for technical indicators to warm up")
    elif mode == "partial":
        # Partial mode explanation
        partial_count = explanation.get("partial_indicator_count", 0)
        available = explanation.get("available_indicators", [])
        combined = explanation.get("combined_score", 0.0)
        
        reasons.append(f"Mode: PARTIAL ({partial_count} indicators available)")
        reasons.append(f"Indicators: {', '.join(available)}")
        reasons.append(f"Combined score: {combined:.3f} (sentiment 60% + tech 40%)")
        
        if decision == "BUY":
            reasons.append(f"Combined score {combined:.3f} >= {PARTIAL_BUY_THRESHOLD} buy threshold")
        elif decision == "SELL":
            reasons.append(f"Combined score {combined:.3f} <= {PARTIAL_SELL_THRESHOLD} sell threshold")
        else:
            reasons.append(f"Combined score {combined:.3f} in HOLD range")
    elif mode == "no_sentiment":
        reasons.append("No sentiment score available - defaulting to HOLD")
    else:
        # Full or basic mode with technical indicators
        reasons.append(f"Mode: {mode.upper()} (sentiment + technical indicators)")
        
        # Summarize signals
        signals = explanation.get("signals", [])
        buy_signals = [s for s in signals if s.get("signal") == "BUY"]
        sell_signals = [s for s in signals if s.get("signal") == "SELL"]
        hold_signals = [s for s in signals if s.get("signal") == "HOLD"]
        
        if buy_signals:
            buy_names = [s["name"] for s in buy_signals]
            reasons.append(f"BUY signals ({len(buy_signals)}): {', '.join(buy_names)}")
        
        if sell_signals:
            sell_names = [s["name"] for s in sell_signals]
            reasons.append(f"SELL signals ({len(sell_signals)}): {', '.join(sell_names)}")
        
        if hold_signals:
            hold_names = [s["name"] for s in hold_signals]
            reasons.append(f"NEUTRAL signals ({len(hold_signals)}): {', '.join(hold_names)}")
        
        # Explain final decision
        buy_ratio = explanation.get("buy_ratio", 0.0)
        sell_ratio = explanation.get("sell_ratio", 0.0)
        
        if decision == "BUY":
            reasons.append(f"Decision: BUY (buy_ratio={buy_ratio:.1%} >= 50%, buy > sell)")
        elif decision == "SELL":
            reasons.append(f"Decision: SELL (sell_ratio={sell_ratio:.1%} >= 50%, sell > buy)")
        else:
            reasons.append(f"Decision: HOLD (no clear signal majority: buy={buy_ratio:.1%}, sell={sell_ratio:.1%})")
    
    # Add confidence context
    if ticker_confidence < 0.8:
        reasons.append(f"Note: Ticker confidence is moderate ({ticker_confidence:.2f})")
    
    return {
        "decision": decision,
        "sentiment_score": round(effective_sentiment, 4),
        "technical_score": round(technical_score, 4),
        "confidence": round(ticker_confidence, 4),
        "reasons": reasons,
        "mode": mode,
        "signal_summary": {
            "buy_score": round(buy_score, 3),
            "sell_score": round(sell_score, 3),
            "total_signals": round(signal_count, 1)
        }
    }


def _get_confidence_band(confidence: float) -> str:
    """
    Get confidence band for tracking and analysis.
    
    Bands:
    - very_high: 0.9-1.0
    - high: 0.8-0.9
    - medium_high: 0.7-0.8
    - medium: 0.6-0.7
    - low: 0.5-0.6
    """
    if confidence >= 0.9:
        return "very_high_0.9-1.0"
    elif confidence >= 0.8:
        return "high_0.8-0.9"
    elif confidence >= 0.7:
        return "medium_high_0.7-0.8"
    elif confidence >= 0.6:
        return "medium_0.6-0.7"
    else:
        return "low_0.5-0.6"

# -------------------------------------------------------------------
# Main worker
# -------------------------------------------------------------------
def main():
    bootstrap_list = _bootstrap_list(KAFKA_BOOTSTRAP)
    logger.info("Kafka bootstrap servers: %s", bootstrap_list)

    # establish consumer/producer with retry
    while True:
        try:
            consumer = KafkaConsumer(
                INPUT_TOPIC,
                bootstrap_servers=bootstrap_list,
                value_deserializer=_deserialize_message,
                group_id="strategy-worker-group",
                auto_offset_reset="earliest",
                enable_auto_commit=True,
            )
            producer = KafkaProducer(
                bootstrap_servers=bootstrap_list,
                value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
            )
            break
        except KafkaError as e:
            logger.warning("Kafka not ready (%s), retrying in 2s...", str(e))
            time.sleep(2)
        except Exception:
            logger.exception("Failed to create Kafka consumer/producer; retrying in 2s")
            time.sleep(2)

    logger.info("Strategy worker running (topic=%s)", INPUT_TOPIC)
    
    # Initialize outcome tracker for learning
    outcome_tracker = None
    if OUTCOME_TRACKING_ENABLED and get_tracker:
        try:
            outcome_tracker = get_tracker()
            logger.info("Outcome tracking enabled for system learning")
        except Exception as e:
            logger.warning("Failed to initialize outcome tracker: %s", e)

    # Initialize outcome labeler for automatic outcome tracking
    outcome_labeler = None
    if OUTCOME_LABELING_ENABLED and get_labeler:
        try:
            outcome_labeler = get_labeler()
            # Start background worker to label pending outcomes
            outcome_labeler.start_background_worker(interval_seconds=300)
            logger.info("Outcome labeling enabled for strategy evaluation")
        except Exception as e:
            logger.warning("Failed to initialize outcome labeler: %s", e)

    try:
        for msg in consumer:
            try:
                article = msg.value or {}
                if not isinstance(article, dict):
                    logger.warning("Received non-dict message: %r", article)
                    continue

                sentiment_block = article.get("sentiment", {})
                # Determine default score and per-ticker map
                per_ticker_scores = {}
                default_score = None

                if isinstance(sentiment_block, dict):
                    # prefer per-ticker mapping if provided
                    per_ticker_scores = sentiment_block.get("sentiment_scores") or {}
                    default_score = sentiment_block.get("sentiment_score") or sentiment_block.get("score")
                    # if default is nested string/number, coerce
                    try:
                        default_score = float(default_score) if default_score is not None else None
                    except Exception:
                        default_score = None
                else:
                    # sentiment_block might be a number directly
                    try:
                        default_score = float(sentiment_block)
                    except Exception:
                        default_score = None

                # ============================================================
                # NEW: Symbol Confidence-Based Processing
                # ============================================================
                
                # Check for new format: ticker + ticker_confidence from extraction
                ticker = article.get("ticker")
                ticker_confidence = article.get("ticker_confidence", 1.0)
                ticker_extraction = article.get("ticker_extraction", {})
                article_id = article.get("article_id", article.get("url", ""))
                
                # If we have the new format with confidence
                if ticker and ticker_confidence is not None:
                    # Filter by confidence threshold
                    if ticker_confidence < IGNORE_CONFIDENCE_BELOW:
                        logger.debug("Ignoring %s - confidence %.2f below threshold %.2f",
                                    ticker, ticker_confidence, IGNORE_CONFIDENCE_BELOW)
                        continue
                    
                    # Process single ticker with confidence
                    tickers_with_confidence = [{
                        "symbol": ticker,
                        "confidence": ticker_confidence,
                        "reasons": ticker_extraction.get("methods_used", [])
                    }]
                else:
                    # Legacy format: tickers array or single symbol
                    tickers = article.get("tickers") or []
                    if not tickers:
                        s = article.get("symbol") or article.get("ticker")
                        if s:
                            tickers = [s]
                    
                    if not tickers:
                        logger.warning("Message missing tickers/symbol - skipping: %s", 
                                      article.get("title") or article)
                        continue
                    
                    # Convert to unified format (assume high confidence for legacy)
                    tickers_with_confidence = [
                        {"symbol": t, "confidence": 1.0, "reasons": ["legacy_format"]}
                        for t in tickers
                    ]

                # Process each symbol
                for sym_data in tickers_with_confidence:
                    try:
                        sym = sym_data["symbol"]
                        confidence = sym_data.get("confidence", 1.0)
                        extraction_reasons = sym_data.get("reasons", [])
                        
                        # HARDENING: Skip index symbols (not tradeable)
                        if is_index_symbol(sym):
                            logger.debug("Skipping index symbol %s (not tradeable)", sym)
                            continue
                        
                        # Skip if confidence too low for trading
                        if confidence < MIN_CONFIDENCE_FOR_TRADING:
                            logger.info("Skipping %s - confidence %.2f below trading threshold %.2f",
                                       sym, confidence, MIN_CONFIDENCE_FOR_TRADING)
                            continue

                        # Get base sentiment score
                        score = None
                        if isinstance(per_ticker_scores, dict) and sym in per_ticker_scores:
                            try:
                                score = float(per_ticker_scores.get(sym))
                            except Exception:
                                score = None
                        if score is None:
                            score = default_score if default_score is not None else 0.0

                        # ============================================================
                        # CRITICAL: Apply confidence weighting to sentiment
                        # effective_sentiment = sentiment_score * symbol_confidence
                        # ============================================================
                        raw_sentiment = score
                        effective_sentiment = score * confidence
                        
                        logger.info("Symbol %s: raw_sentiment=%.3f * confidence=%.2f = effective=%.3f",
                                   sym, raw_sentiment, confidence, effective_sentiment)

                        features = get_features(sym)
                        
                        # Use effective sentiment for decision (with explanation)
                        decision_result = decide_trade_single(effective_sentiment, features, return_explanation=True)
                        decision = decision_result["decision"]
                        decision_explanation = decision_result["explanation"]
                        
                        # Extract indicator summary for the record
                        indicator_summary = extract_indicator_summary(features)
                        
                        # Get confidence band for tracking
                        confidence_band = _get_confidence_band(confidence)

                        # Build human-readable explanation for API consumers
                        human_explanation = build_human_readable_explanation(
                            decision=decision,
                            explanation=decision_explanation,
                            effective_sentiment=effective_sentiment,
                            ticker_confidence=confidence
                        )

                        record = {
                            "ts": datetime.utcnow().isoformat() + "Z",
                            "symbol": sym,
                            "decision": decision,
                            # ACTIONABLE TRADE FLAG
                            # ready_for_trade: True if decision is BUY/SELL with confidence > 0.7
                            "ready_for_trade": decision in ("BUY", "SELL") and confidence >= MIN_CONFIDENCE_FOR_TRADING,
                            # Sentiment details
                            "raw_sentiment": raw_sentiment,
                            "ticker_confidence": confidence,
                            "effective_sentiment": effective_sentiment,
                            "confidence_band": confidence_band,
                            # Legacy field
                            "sentiment_score": effective_sentiment,
                            # Extraction details
                            "extraction_reasons": extraction_reasons,
                            # Features
                            "features": features or {},
                            "indicators": indicator_summary,
                            # HARDENING: Decision explanation (detailed)
                            "decision_explanation": decision_explanation,
                            "decision_mode": decision_explanation.get("mode", "unknown"),
                            "is_warmed_up": decision_explanation.get("is_warmed_up", False),
                            # EXPLAINABLE AI: Human-readable explanation
                            "explanation": human_explanation,
                            # Source
                            "source_title": article.get("title"),
                            "source_url": article.get("url"),
                            "article_id": article_id,
                        }

                        # Log the decision with structured explanation
                        logger.info(
                            "DECISION: %s %s | sentiment=%.3f technical=%.3f confidence=%.2f | reasons: %s",
                            sym, decision,
                            human_explanation["sentiment_score"],
                            human_explanation["technical_score"],
                            human_explanation["confidence"],
                            " | ".join(human_explanation["reasons"][:2])  # First 2 reasons
                        )

                        # Publish and persist
                        producer.send(OUTPUT_TOPIC, record)
                        producer.flush(timeout=10)
                        append_result(record)
                        
                        # Track decision for learning
                        if outcome_tracker and decision in ("BUY", "SELL"):
                            try:
                                outcome_tracker.record_decision(
                                    symbol=sym,
                                    confidence=confidence,
                                    decision=decision,
                                    extraction_methods=extraction_reasons,
                                    reasons=extraction_reasons,
                                    article_id=article_id,
                                    headline=article.get("title", "")
                                )
                            except Exception as e:
                                logger.warning("Failed to record decision for tracking: %s", e)

                        # Register decision for outcome labeling (automatic price tracking)
                        if outcome_labeler and decision in ("BUY", "SELL"):
                            try:
                                # Get price from features
                                price_t0 = None
                                if features:
                                    price_t0 = features.get('price')
                                if price_t0:
                                    decision_id = f"{sym}_{record['ts']}"
                                    outcome_labeler.register_decision(
                                        decision_id=decision_id,
                                        symbol=sym,
                                        decision=decision,
                                        price_t0=float(price_t0),
                                        timestamp=record['ts'],
                                        sentiment_score=effective_sentiment,
                                        ticker_confidence=confidence,
                                        confidence_band=confidence_band,
                                        extraction_methods=extraction_reasons,
                                        source_title=article.get("title", "")
                                    )
                            except Exception as e:
                                logger.warning("Failed to register decision for outcome labeling: %s", e)

                        logger.info("Decision: %s %s (mode=%s, effective_sent=%.3f, conf=%.2f, band=%s, indicators=%d)", 
                                    sym, decision, decision_explanation.get("mode", "unknown"),
                                    effective_sentiment, confidence, 
                                    confidence_band, len(indicator_summary))
                    except Exception:
                        logger.exception("Failed to handle symbol %s in article %s", 
                                        sym_data.get("symbol"), article.get("title"))
            except Exception:
                logger.exception("Failed processing message; continuing")
    except KeyboardInterrupt:
        logger.info("Worker interrupted, shutting down")
    finally:
        # Stop outcome labeler background worker
        if outcome_labeler:
            try:
                outcome_labeler.stop_background_worker()
                logger.info("Outcome labeler stopped")
            except Exception:
                pass
        
        try:
            consumer.close()
        except Exception:
            pass
        try:
            producer.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
