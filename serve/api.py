# serve/api.py
"""
FastAPI model server for Sentient Trader.

Exposes READ-ONLY endpoints for decision insights:
- /predict: Model predictions (ONNX or mock)
- /health: Health check
- /metrics: Prometheus metrics
- /decisions/latest: Recent trade decisions with explanations
- /decisions/{symbol}: Decisions for a specific symbol
- /decisions/insights: Decision insights with full explanation
- /metrics/summary: Strategy performance metrics
- /outcomes/recent: Recent labeled outcomes

SAFETY:
- All endpoints are READ-ONLY
- No authentication required
- API never fails due to missing data (graceful fallbacks)
- Validation and error handling on all inputs
"""

import os
import sys
import time
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, make_asgi_app
from feature_store.feature_store import get_online_features
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Optional ONNXRuntime import
try:
    import onnxruntime as ort
except Exception:
    ort = None

# Optional evaluation imports
try:
    from evaluation.outcome_labeler import get_labeler
    from evaluation.metrics import get_metrics
    EVALUATION_AVAILABLE = True
except ImportError:
    EVALUATION_AVAILABLE = False
    get_labeler = None
    get_metrics = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("serve_api")

# Configuration
DECISIONS_PATH = os.getenv("DECISIONS_PATH", "results/trade_decisions.jsonl")

MODEL_PATH = os.getenv("MODEL_PATH", "model/model.onnx")
MODEL_VERSION = os.getenv("MODEL_VERSION", "dev-v0")

# Prometheus metrics
REQUESTS = Counter("pred_requests_total", "Total prediction requests")
REQUEST_LATENCY = Histogram("pred_request_latency_seconds", "Prediction request latency_seconds")

app = FastAPI(title="Sentient Trader Model Serve")
# Mount metrics at /metrics
app.mount("/metrics", make_asgi_app())

# Load ONNX model if available
if ort and os.path.exists(MODEL_PATH):
    try:
        ort_session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        logger.info("Loaded ONNX model from %s", MODEL_PATH)
    except Exception:
        ort_session = None
        logger.exception("Failed to load ONNX model — will use mock scoring")
else:
    ort_session = None
    logger.warning("ONNX model not found at %s — server will return mock predictions", MODEL_PATH)


class PredictRequest(BaseModel):
    symbol: str
    ts: str


class PredictResponse(BaseModel):
    score: float
    model_version: str


# inside serve/api.py — replace current get_features_for_request with this


def get_features_for_request(symbol: str, ts: str) -> Dict[str, float]:
    """
    Pull online features from Redis feature store.
    Returns a dict of numeric features for the model input.
    If features not present, return deterministic defaults for testing.
    """
    f = get_online_features(symbol, ts)
    if not f:
        # deterministic fallback
        return {"price": 100.0, "sma_20": 100.0, "rsi_14": 50.0}

    # pick numeric keys expected by the model; keep order stable
    keys = ["price", "sma_20", "rsi_14", "ema_12", "ema_26", "macd", "macd_signal", "macd_hist", "vwap", "obv"]
    out = {}
    for k in keys:
        v = f.get(k)
        try:
            out[k] = float(v) if v is not None else 0.0
        except Exception:
            out[k] = 0.0
    return out


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    REQUESTS.inc()
    start = time.time()

    features = get_features_for_request(req.symbol, req.ts)
    X = np.array([list(features.values())], dtype=np.float32)

    if ort_session:
        try:
            input_name = ort_session.get_inputs()[0].name
            out = ort_session.run(None, {input_name: X})[0]
            score = float(out[0][0])
        except Exception:
            logger.exception("ONNX inference failed, falling back to mock score")
            score = 0.0
    else:
        # deterministic mock scoring
        ratio = features.get("price", 100.0) / (features.get("sma_20", 100.0) + 1e-9)
        score = float(np.tanh((ratio - 1.0) * 3.0))  # -1..1

    REQUEST_LATENCY.observe(time.time() - start)
    return {"score": score, "model_version": MODEL_VERSION}


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": ort_session is not None}


# =============================================================================
# DECISIONS ENDPOINTS
# =============================================================================

def _read_decisions(limit: int = 50, symbol: str = None) -> List[Dict]:
    """
    Read decisions from JSONL file.
    
    SAFETY: Never fails - returns empty list on any error.
    
    Args:
        limit: Maximum number of decisions to return
        symbol: Filter by symbol if provided
        
    Returns:
        List of decision dicts (most recent first)
    """
    decisions = []
    path = Path(DECISIONS_PATH)
    
    # SAFETY: Return empty list if file doesn't exist
    if not path.exists():
        logger.debug(f"Decisions file not found: {DECISIONS_PATH}")
        return []
    
    try:
        # Read all lines first, then reverse for most recent
        all_decisions = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        decision = json.loads(line.strip())
                        if symbol is None or decision.get('symbol') == symbol:
                            all_decisions.append(decision)
                    except json.JSONDecodeError:
                        # SAFETY: Skip malformed lines, don't crash
                        continue
        
        # Return most recent first
        decisions = list(reversed(all_decisions))[:limit]
        
    except Exception as e:
        # SAFETY: Log error but return empty list, never crash
        logger.error(f"Failed to read decisions: {e}")
    
    return decisions


class DecisionResponse(BaseModel):
    """Response model for a single decision."""
    ts: str
    symbol: str
    decision: str
    sentiment_score: float
    ticker_confidence: Optional[float] = None
    effective_sentiment: Optional[float] = None
    confidence_band: Optional[str] = None
    source_title: Optional[str] = None
    source_url: Optional[str] = None


class DecisionsListResponse(BaseModel):
    """Response model for list of decisions."""
    count: int
    decisions: List[Dict[str, Any]]


@app.get("/decisions/latest", response_model=DecisionsListResponse)
def get_latest_decisions(limit: int = Query(default=50, ge=1, le=500)):
    """
    Get the most recent trade decisions with explanations.
    
    READ-ONLY endpoint that never fails - returns empty list if no data.
    
    Args:
        limit: Maximum number of decisions to return (default: 50, max: 500)
        
    Returns:
        List of recent decisions with:
        - decision: BUY/SELL/HOLD
        - confidence: ticker extraction confidence
        - explanation: human-readable decision explanation
    """
    decisions = _read_decisions(limit=limit)
    
    # Build simplified response with explanation
    simplified = []
    for d in decisions:
        # Extract explanation (new field) or build from legacy fields
        explanation = d.get('explanation')
        if not explanation:
            # Fallback for legacy decisions without explanation field
            explanation = {
                "decision": d.get('decision', 'UNKNOWN'),
                "sentiment_score": d.get('effective_sentiment', 0),
                "technical_score": 0.0,
                "confidence": d.get('ticker_confidence', 1.0),
                "reasons": [f"Mode: {d.get('decision_mode', 'unknown').upper()}"],
                "mode": d.get('decision_mode', 'unknown')
            }
        
        simplified.append({
            'ts': d.get('ts'),
            'symbol': d.get('symbol'),
            'decision': d.get('decision'),
            'sentiment_score': d.get('sentiment_score') or d.get('effective_sentiment', 0),
            'ticker_confidence': d.get('ticker_confidence'),
            'effective_sentiment': d.get('effective_sentiment'),
            'confidence_band': d.get('confidence_band'),
            'source_title': d.get('source_title'),
            'source_url': d.get('source_url'),
            # EXPLAINABLE AI: Include full explanation
            'explanation': explanation
        })
    
    return {"count": len(simplified), "decisions": simplified}


# =============================================================================
# DECISION INSIGHTS ENDPOINT (Explainable AI)
# =============================================================================

class DecisionInsight(BaseModel):
    """Response model for a single decision insight."""
    ts: str
    symbol: str
    decision: str
    confidence: float
    explanation: Dict[str, Any]


class DecisionInsightsResponse(BaseModel):
    """Response model for decision insights."""
    count: int
    insights: List[DecisionInsight]


@app.get("/decisions/insights", response_model=DecisionInsightsResponse)
def get_decision_insights(
    limit: int = Query(default=20, ge=1, le=100),
    decision_type: Optional[str] = Query(default=None, description="Filter by BUY/SELL/HOLD")
):
    """
    Get decision insights with full explanations.
    
    READ-ONLY endpoint focused on explainability and debuggability.
    Returns ONLY: decision, confidence, and explanation.
    
    SAFETY: Never fails - returns empty list if no data.
    
    Args:
        limit: Maximum number of insights to return (default: 20, max: 100)
        decision_type: Optional filter for BUY, SELL, or HOLD decisions
        
    Returns:
        List of decision insights with:
        - decision: BUY/SELL/HOLD
        - confidence: overall confidence score
        - explanation: {
            sentiment_score: float,
            technical_score: float,
            reasons: [human-readable explanations]
          }
    """
    decisions = _read_decisions(limit=limit * 2)  # Read extra in case we filter
    
    insights = []
    for d in decisions:
        # Filter by decision type if specified
        if decision_type and d.get('decision', '').upper() != decision_type.upper():
            continue
        
        # Extract or build explanation
        explanation = d.get('explanation')
        if not explanation:
            # Build explanation from available fields
            mode = d.get('decision_mode', 'unknown')
            reasons = []
            
            if mode == 'news_only':
                reasons.append("Mode: NEWS-ONLY (technical indicators not ready)")
                eff_sent = d.get('effective_sentiment', 0)
                if d.get('decision') == 'BUY':
                    reasons.append(f"Strong positive sentiment ({eff_sent:.2f})")
                elif d.get('decision') == 'SELL':
                    reasons.append(f"Strong negative sentiment ({eff_sent:.2f})")
                else:
                    reasons.append(f"Sentiment ({eff_sent:.2f}) not strong enough for news-only trade")
            else:
                reasons.append(f"Mode: {mode.upper()}")
                if d.get('decision_explanation'):
                    exp = d['decision_explanation']
                    buy_ratio = exp.get('buy_ratio', 0)
                    sell_ratio = exp.get('sell_ratio', 0)
                    reasons.append(f"Signal ratios: buy={buy_ratio:.1%}, sell={sell_ratio:.1%}")
            
            explanation = {
                "decision": d.get('decision', 'UNKNOWN'),
                "sentiment_score": d.get('effective_sentiment', 0),
                "technical_score": 0.0,
                "confidence": d.get('ticker_confidence', 1.0),
                "reasons": reasons,
                "mode": mode
            }
        
        insights.append({
            "ts": d.get('ts', ''),
            "symbol": d.get('symbol', 'UNKNOWN'),
            "decision": d.get('decision', 'UNKNOWN'),
            "confidence": d.get('ticker_confidence', 1.0) or 1.0,
            "explanation": explanation
        })
        
        if len(insights) >= limit:
            break
    
    return {"count": len(insights), "insights": insights}


@app.get("/decisions/{symbol}", response_model=DecisionsListResponse)
def get_decisions_for_symbol(symbol: str, limit: int = Query(default=50, ge=1, le=500)):
    """
    Get trade decisions for a specific symbol.
    
    READ-ONLY endpoint that returns 404 if no decisions found (not an error).
    
    Args:
        symbol: Stock ticker symbol (e.g., AAPL)
        limit: Maximum number of decisions to return
        
    Returns:
        List of decisions for the symbol
    """
    symbol = symbol.upper()
    decisions = _read_decisions(limit=limit, symbol=symbol)
    
    if not decisions:
        raise HTTPException(status_code=404, detail=f"No decisions found for symbol: {symbol}")
    
    # Simplify response
    simplified = []
    for d in decisions:
        simplified.append({
            'ts': d.get('ts'),
            'symbol': d.get('symbol'),
            'decision': d.get('decision'),
            'sentiment_score': d.get('sentiment_score') or d.get('effective_sentiment', 0),
            'ticker_confidence': d.get('ticker_confidence'),
            'effective_sentiment': d.get('effective_sentiment'),
            'confidence_band': d.get('confidence_band'),
            'source_title': d.get('source_title'),
            'indicators': d.get('indicators', {})
        })
    
    return {"count": len(simplified), "decisions": simplified}


# =============================================================================
# METRICS ENDPOINTS
# =============================================================================

class MetricsSummaryResponse(BaseModel):
    """Response model for metrics summary."""
    computed_at: str
    overall: Dict[str, Any]
    by_sentiment: Dict[str, Any]
    by_confidence: Dict[str, Any]
    by_signal: Dict[str, Any]
    false_positive_analysis: Dict[str, Any]


@app.get("/metrics/summary")
def get_metrics_summary():
    """
    Get strategy performance metrics summary.
    
    Computes metrics from labeled outcomes including:
    - Overall win rate and average return
    - Win rate by sentiment bucket
    - Win rate by confidence bucket
    - False positive rates for BUY/SELL signals
    
    Returns:
        Comprehensive metrics summary
    """
    if not EVALUATION_AVAILABLE:
        raise HTTPException(
            status_code=503, 
            detail="Evaluation module not available. Install evaluation package."
        )
    
    try:
        metrics = get_metrics()
        summary = metrics.compute_from_files()
        return summary
    except Exception as e:
        logger.error(f"Failed to compute metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics/quick")
def get_quick_metrics():
    """
    Get quick metrics computed from decision logs (no outcomes needed).
    
    Returns basic statistics about decisions made:
    - Total decisions
    - Decisions by type (BUY/SELL/HOLD)
    - Decisions by symbol
    - Average sentiment and confidence
    """
    decisions = _read_decisions(limit=1000)
    
    if not decisions:
        return {
            "total": 0,
            "by_decision": {},
            "by_symbol": {},
            "avg_sentiment": 0,
            "avg_confidence": 0
        }
    
    # Compute quick stats
    by_decision = {}
    by_symbol = {}
    total_sentiment = 0
    total_confidence = 0
    confidence_count = 0
    
    for d in decisions:
        # By decision type
        decision = d.get('decision', 'UNKNOWN')
        by_decision[decision] = by_decision.get(decision, 0) + 1
        
        # By symbol
        symbol = d.get('symbol', 'UNKNOWN')
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        
        # Sentiment
        sentiment = d.get('effective_sentiment') or d.get('sentiment_score', 0)
        if sentiment:
            total_sentiment += sentiment
        
        # Confidence
        confidence = d.get('ticker_confidence')
        if confidence:
            total_confidence += confidence
            confidence_count += 1
    
    return {
        "total": len(decisions),
        "by_decision": by_decision,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda x: x[1], reverse=True)[:20]),
        "avg_sentiment": round(total_sentiment / len(decisions), 4) if decisions else 0,
        "avg_confidence": round(total_confidence / confidence_count, 4) if confidence_count > 0 else 0
    }


# =============================================================================
# OUTCOMES ENDPOINTS
# =============================================================================

class OutcomeResponse(BaseModel):
    """Response model for a single outcome."""
    decision_id: str
    symbol: str
    decision: str
    price_t0: float
    return_1h: Optional[float] = None
    return_1d: Optional[float] = None
    success_1h: Optional[bool] = None
    success_1d: Optional[bool] = None
    labeled_at: Optional[str] = None


class OutcomesListResponse(BaseModel):
    """Response model for list of outcomes."""
    count: int
    pending_count: int
    outcomes: List[Dict[str, Any]]


@app.get("/outcomes/recent", response_model=OutcomesListResponse)
def get_recent_outcomes(limit: int = Query(default=50, ge=1, le=500)):
    """
    Get recently labeled trade outcomes.
    
    Returns outcomes with:
    - Original decision details
    - Price at decision time and after horizons
    - Return percentages
    - Success/failure labels
    
    Args:
        limit: Maximum number of outcomes to return
        
    Returns:
        List of labeled outcomes
    """
    if not EVALUATION_AVAILABLE:
        raise HTTPException(
            status_code=503, 
            detail="Evaluation module not available. Install evaluation package."
        )
    
    try:
        labeler = get_labeler()
        outcomes = labeler.get_recent_outcomes(limit=limit)
        pending_count = labeler.get_pending_count()
        
        # Simplify for response
        simplified = []
        for o in outcomes:
            simplified.append({
                'decision_id': o.get('decision_id'),
                'symbol': o.get('symbol'),
                'decision': o.get('decision'),
                'price_t0': o.get('price_t0'),
                'sentiment_score': o.get('sentiment_score'),
                'ticker_confidence': o.get('ticker_confidence'),
                'return_30m': o.get('return_30m'),
                'return_1h': o.get('return_1h'),
                'return_1d': o.get('return_1d'),
                'success_30m': o.get('success_30m'),
                'success_1h': o.get('success_1h'),
                'success_1d': o.get('success_1d'),
                'labeled_at': o.get('labeled_at')
            })
        
        return {
            "count": len(simplified),
            "pending_count": pending_count,
            "outcomes": simplified
        }
        
    except Exception as e:
        logger.error(f"Failed to get outcomes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/outcomes/{symbol}")
def get_outcomes_for_symbol(symbol: str, limit: int = Query(default=50, ge=1, le=500)):
    """
    Get labeled outcomes for a specific symbol.
    
    Args:
        symbol: Stock ticker symbol
        limit: Maximum number of outcomes to return
        
    Returns:
        List of outcomes for the symbol
    """
    if not EVALUATION_AVAILABLE:
        raise HTTPException(
            status_code=503, 
            detail="Evaluation module not available. Install evaluation package."
        )
    
    symbol = symbol.upper()
    
    try:
        labeler = get_labeler()
        outcomes = labeler.get_outcomes_for_symbol(symbol, limit=limit)
        
        if not outcomes:
            raise HTTPException(status_code=404, detail=f"No outcomes found for symbol: {symbol}")
        
        return {"count": len(outcomes), "symbol": symbol, "outcomes": outcomes}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get outcomes for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/outcomes/stats")
def get_outcome_stats():
    """
    Get outcome labeling statistics.
    
    Returns:
        Labeler stats including pending count and total labeled
    """
    if not EVALUATION_AVAILABLE:
        return {
            "available": False,
            "message": "Evaluation module not available"
        }
    
    try:
        labeler = get_labeler()
        stats = labeler.get_stats()
        return {"available": True, **stats}
    except Exception as e:
        logger.error(f"Failed to get outcome stats: {e}")
        return {"available": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
