# feature_store/api.py
"""
Feature API: lightweight FastAPI service to read online features from Redis.
Run: python -m feature_store.api
"""

import os
import logging
from fastapi import FastAPI, HTTPException
from typing import Optional, List
from feature_store.feature_store import get_online_features, list_recent_feature_keys, r
import uvicorn
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feature_api")

app = FastAPI(title="Feature API", description="API for technical indicator features")

# List of all available indicators
AVAILABLE_INDICATORS = [
    "price", "sma_5", "sma_20", "sma_50", "ema_12", "ema_26",
    "macd", "macd_signal", "macd_hist", "rsi_14",
    "boll_upper", "boll_lower", "boll_mid", "vwap", "obv",
    "atr_14", "stoch_k", "stoch_d", "williams_r", "cci_20",
    "adx", "plus_di", "minus_di", "momentum_10", "roc_10",
    "price_change", "price_change_pct"
]


@app.get("/")
def root():
    return {
        "service": "Feature API",
        "version": "2.0",
        "endpoints": [
            "/features/{symbol}/{ts_iso}",
            "/features/{symbol}/latest",
            "/indicators",
            "/health"
        ],
        "available_indicators": AVAILABLE_INDICATORS
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/indicators")
def get_indicators_list():
    """Return list of all available technical indicators"""
    return {
        "indicators": AVAILABLE_INDICATORS,
        "count": len(AVAILABLE_INDICATORS),
        "categories": {
            "trend": ["sma_5", "sma_20", "sma_50", "ema_12", "ema_26", "adx", "plus_di", "minus_di"],
            "momentum": ["macd", "macd_signal", "macd_hist", "rsi_14", "stoch_k", "stoch_d", "williams_r", "cci_20", "momentum_10", "roc_10"],
            "volatility": ["boll_upper", "boll_lower", "boll_mid", "atr_14"],
            "volume": ["vwap", "obv"],
            "price": ["price", "price_change", "price_change_pct"]
        }
    }


@app.get("/features/{symbol}/{ts_iso}")
def get_features(symbol: str, ts_iso: str):
    f = get_online_features(symbol, ts_iso)
    if not f:
        raise HTTPException(status_code=404, detail="features not found")
    return f


@app.get("/features/{symbol}/latest")
def get_latest_features(symbol: str):
    # naive dev-only fallback: return latest stored feature for symbol
    keys = list_recent_feature_keys(symbol)
    if not keys:
        raise HTTPException(status_code=404, detail="no features")
    latest = keys[-1]
    raw = r.get(latest)
    if not raw:
        raise HTTPException(status_code=404, detail="no features")
    try:
        return json.loads(raw)
    except Exception:
        raise HTTPException(status_code=500, detail="failed to decode features")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8100))
    uvicorn.run("feature_store.api:app", host="0.0.0.0", port=port, log_level="info")
