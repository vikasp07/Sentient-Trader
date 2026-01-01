# feature_store/feature_store.py
"""
Feature Store:
- Computes technical indicators (SMA, EMA, MACD, RSI, Bollinger, VWAP, OBV, ATR, Stochastic, Williams %R, CCI, ADX, Momentum, ROC)
- Stores online features in Redis as JSON
- Stores offline snapshots in Parquet for model training
"""

import os
import logging
from typing import Dict, Any, List, Tuple
import pandas as pd
import numpy as np
import redis
import pyarrow as pa
import pyarrow.parquet as pq
import json
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feature_store")

# -----------------------------
# Redis configuration
# -----------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Use decode_responses=True so r.get returns strings (JSON), not bytes
r = redis.from_url(REDIS_URL, decode_responses=True)

FEATURE_SNAPSHOT_DIR = os.getenv("FEATURE_SNAPSHOT_DIR", "data/features")
FEATURE_TTL = int(os.getenv("FEATURE_TTL_SEC", 60 * 60 * 24 * 2))  # default 48h

# -----------------------------
# Indicator functions
# -----------------------------
def sma(series: pd.Series, n: int):
    return series.rolling(n, min_periods=1).mean()


def ema(series: pd.Series, n: int):
    return series.ewm(span=n, adjust=False).mean()


def macd(series: pd.Series, n_fast=12, n_slow=26, n_signal=9):
    fast = ema(series, n_fast)
    slow = ema(series, n_slow)
    macd_line = fast - slow
    signal = macd_line.ewm(span=n_signal, adjust=False).mean()
    hist = macd_line - signal
    return macd_line, signal, hist


def rsi(series: pd.Series, n=14):
    delta = series.diff()
    up = delta.clip(lower=0).rolling(n, min_periods=1).mean()
    down = -delta.clip(upper=0).rolling(n, min_periods=1).mean()
    rs = up / (down.replace(0, np.nan))
    # handle division by zero / nan
    rs = rs.fillna(0.0)
    return 100 - 100 / (1 + rs)


def bollinger(series: pd.Series, n=20, k=2):
    ma = series.rolling(n, min_periods=1).mean()
    std = series.rolling(n, min_periods=1).std().fillna(0.0)
    return (ma + k * std, ma - k * std)


def vwap(df: pd.DataFrame):
    # require 'price' and optional 'volume' column
    if "volume" not in df.columns:
        df["volume"] = 1.0
    vol = df["volume"].fillna(0.0)
    cum_price_vol = (df["price"] * vol).cumsum()
    cum_vol = vol.cumsum().replace(0, np.nan)
    return (cum_price_vol / cum_vol).fillna(df["price"])


def obv(df: pd.DataFrame):
    if "volume" not in df.columns:
        df["volume"] = 1.0
    volumes = df["volume"].fillna(0.0).astype(float)
    obv_vals = [0.0]
    for i in range(1, len(df)):
        if df["price"].iat[i] > df["price"].iat[i - 1]:
            obv_vals.append(obv_vals[-1] + volumes.iat[i])
        elif df["price"].iat[i] < df["price"].iat[i - 1]:
            obv_vals.append(obv_vals[-1] - volumes.iat[i])
        else:
            obv_vals.append(obv_vals[-1])
    return pd.Series(obv_vals, index=df.index)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """
    Average True Range (ATR) - measures volatility
    Uses price as proxy for high/low/close when those columns are not available
    """
    if all(col in df.columns for col in ["high", "low", "close"]):
        high = df["high"]
        low = df["low"]
        close = df["close"]
    else:
        # Use price as proxy - estimate high/low from price movement
        high = df["price"]
        low = df["price"]
        close = df["price"]
    
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> Tuple[pd.Series, pd.Series]:
    """
    Stochastic Oscillator - momentum indicator comparing closing price to price range
    Returns: (stoch_k, stoch_d)
    """
    if all(col in df.columns for col in ["high", "low", "close"]):
        high = df["high"]
        low = df["low"]
        close = df["close"]
    else:
        # Estimate high/low from rolling window of price
        high = df["price"].rolling(k_period, min_periods=1).max()
        low = df["price"].rolling(k_period, min_periods=1).min()
        close = df["price"]
    
    lowest_low = low.rolling(k_period, min_periods=1).min()
    highest_high = high.rolling(k_period, min_periods=1).max()
    
    # %K = (Close - Lowest Low) / (Highest High - Lowest Low) * 100
    denominator = highest_high - lowest_low
    denominator = denominator.replace(0, np.nan)
    stoch_k = ((close - lowest_low) / denominator * 100).fillna(50)
    
    # %D = SMA of %K
    stoch_d = stoch_k.rolling(d_period, min_periods=1).mean()
    
    return stoch_k, stoch_d


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """
    Williams %R - momentum indicator, similar to stochastic but inverted
    Range: -100 to 0 (oversold: -80 to -100, overbought: -20 to 0)
    """
    if all(col in df.columns for col in ["high", "low", "close"]):
        high = df["high"]
        low = df["low"]
        close = df["close"]
    else:
        high = df["price"].rolling(n, min_periods=1).max()
        low = df["price"].rolling(n, min_periods=1).min()
        close = df["price"]
    
    highest_high = high.rolling(n, min_periods=1).max()
    lowest_low = low.rolling(n, min_periods=1).min()
    
    denominator = highest_high - lowest_low
    denominator = denominator.replace(0, np.nan)
    wr = ((highest_high - close) / denominator * -100).fillna(-50)
    
    return wr


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """
    Commodity Channel Index (CCI) - identifies cyclical trends
    """
    if all(col in df.columns for col in ["high", "low", "close"]):
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
    else:
        typical_price = df["price"]
    
    sma_tp = typical_price.rolling(n, min_periods=1).mean()
    mean_deviation = typical_price.rolling(n, min_periods=1).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    ).fillna(1.0)
    
    # CCI = (Typical Price - SMA) / (0.015 * Mean Deviation)
    mean_deviation = mean_deviation.replace(0, np.nan)
    result = ((typical_price - sma_tp) / (0.015 * mean_deviation)).fillna(0)
    return result


def adx(df: pd.DataFrame, n: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Average Directional Index (ADX) - measures trend strength
    Returns: (adx, plus_di, minus_di)
    """
    if all(col in df.columns for col in ["high", "low", "close"]):
        high = df["high"]
        low = df["low"]
        close = df["close"]
    else:
        # Estimate from price
        high = df["price"]
        low = df["price"]
        close = df["price"]
    
    # Calculate +DM and -DM
    high_diff = high.diff()
    low_diff = -low.diff()
    
    plus_dm = high_diff.where((high_diff > low_diff) & (high_diff > 0), 0)
    minus_dm = low_diff.where((low_diff > high_diff) & (low_diff > 0), 0)
    
    # Calculate True Range
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Smoothed values
    atr_smooth = tr.rolling(n, min_periods=1).mean()
    plus_dm_smooth = plus_dm.rolling(n, min_periods=1).mean()
    minus_dm_smooth = minus_dm.rolling(n, min_periods=1).mean()
    
    # +DI and -DI
    atr_smooth = atr_smooth.replace(0, np.nan)
    plus_di = (100 * plus_dm_smooth / atr_smooth).fillna(0)
    minus_di = (100 * minus_dm_smooth / atr_smooth).fillna(0)
    
    # DX and ADX
    di_sum = plus_di + minus_di
    di_sum = di_sum.replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0)
    adx_val = dx.rolling(n, min_periods=1).mean()
    
    return adx_val, plus_di, minus_di


def momentum(series: pd.Series, n: int = 10) -> pd.Series:
    """
    Momentum - measures rate of price change
    """
    return series.diff(n).fillna(0)


def roc(series: pd.Series, n: int = 10) -> pd.Series:
    """
    Rate of Change (ROC) - percentage change over n periods
    """
    shifted = series.shift(n)
    shifted = shifted.replace(0, np.nan)
    return ((series - shifted) / shifted * 100).fillna(0)


def price_change(series: pd.Series) -> pd.Series:
    """
    Price change from previous period
    """
    return series.diff().fillna(0)


def price_change_pct(series: pd.Series) -> pd.Series:
    """
    Percentage price change from previous period
    """
    shifted = series.shift(1)
    shifted = shifted.replace(0, np.nan)
    return ((series - shifted) / shifted * 100).fillna(0)


# -----------------------------
# Indicator computation
# -----------------------------
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    # ensure ts column is datetime
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)

    # required numeric fields
    df["price"] = pd.to_numeric(df["price"], errors="coerce").ffill().fillna(0.0)
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    else:
        df["volume"] = 1.0

    # SMAs - strategy worker uses SMA_5 and SMA_20
    df["sma_5"] = sma(df["price"], 5)
    df["sma_20"] = sma(df["price"], 20)
    df["sma_50"] = sma(df["price"], 50)
    df["ema_12"] = ema(df["price"], 12)
    df["ema_26"] = ema(df["price"], 26)

    macd_line, signal, hist = macd(df["price"])
    df["macd"] = macd_line
    df["macd_signal"] = signal
    df["macd_hist"] = hist

    df["rsi_14"] = rsi(df["price"])

    boll_up, boll_low = bollinger(df["price"])
    df["boll_upper"] = boll_up
    df["boll_lower"] = boll_low
    df["boll_mid"] = (boll_up + boll_low) / 2

    df["vwap"] = vwap(df)
    df["obv"] = obv(df)
    
    # New indicators
    df["atr_14"] = atr(df, n=14)
    
    stoch_k, stoch_d = stochastic(df)
    df["stoch_k"] = stoch_k
    df["stoch_d"] = stoch_d
    
    df["williams_r"] = williams_r(df)
    df["cci_20"] = cci(df)
    
    adx_val, plus_di, minus_di = adx(df)
    df["adx"] = adx_val
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di
    
    df["momentum_10"] = momentum(df["price"], n=10)
    df["roc_10"] = roc(df["price"], n=10)
    
    df["price_change"] = price_change(df["price"])
    df["price_change_pct"] = price_change_pct(df["price"])

    # replace inf/nan with None for JSON serialization later
    return df.replace([np.inf, -np.inf], np.nan)


# -----------------------------
# Offline feature snapshot
# -----------------------------
def save_snapshot(df: pd.DataFrame, symbol: str, date_suffix: str):
    os.makedirs(FEATURE_SNAPSHOT_DIR, exist_ok=True)
    out_path = os.path.join(FEATURE_SNAPSHOT_DIR, f"{symbol}_{date_suffix}.parquet")
    table = pa.Table.from_pandas(df)
    pq.write_table(table, out_path)
    logger.info(f"Saved feature snapshot → {out_path}")
    return out_path


# -----------------------------
# Online Feature helpers (Redis)
# -----------------------------
def _feature_key(symbol: str, ts_iso: str) -> str:
    return f"features:{symbol}:{ts_iso}"


def save_online_feature(symbol: str, ts_iso: str, features: Dict[str, Any], ex: int = FEATURE_TTL) -> None:
    """
    Save features as JSON string in Redis. ts_iso must be ISO string.
    """
    key = _feature_key(symbol, ts_iso)
    try:
        r.set(key, json.dumps(features, default=str), ex=ex)
    except Exception:
        logger.exception("Failed to save feature key=%s", key)


def get_online_features(symbol: str, ts_iso: str) -> Dict[str, Any]:
    """
    Return features JSON for exact ts_iso OR the latest <= ts_iso (by scanning keys).
    If nothing found, return {}.
    """
    # try exact
    key = _feature_key(symbol, ts_iso)
    raw = r.get(key)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            logger.exception("Failed parse JSON for key %s", key)
            return {}

    # fallback: find recent keys and pick latest <= ts_iso
    try:
        target_ts = pd.to_datetime(ts_iso, utc=True)
    except Exception:
        target_ts = None

    pattern = f"features:{symbol}:*"
    try:
        keys = r.keys(pattern)
    except Exception:
        logger.exception("Redis keys() failed for pattern %s", pattern)
        return {}

    candidates = []
    for k in keys:
        try:
            # key format features:{symbol}:{ts}
            parts = k.split(":", 2)
            tspart = parts[2]
            ts = pd.to_datetime(tspart, utc=True)
            if target_ts is None or ts <= target_ts:
                candidates.append((ts, k))
        except Exception:
            continue

    if not candidates:
        return {}

    # pick latest ts
    candidates.sort(key=lambda x: x[0])
    best_key = candidates[-1][1]
    raw = r.get(best_key)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            logger.exception("Failed parse JSON for key %s", best_key)
    return {}


def list_recent_feature_keys(symbol: str, limit: int = 50) -> List[str]:
    pattern = f"features:{symbol}:*"
    try:
        keys = r.keys(pattern)
        keys = sorted(keys)
        return keys[-limit:]
    except Exception:
        return []


# -----------------------------
# CLI usage
# -----------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="data/real_ticks.csv")
    parser.add_argument("--symbol", default="AAPL")
    args = parser.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["ts"])
    df_ind = compute_indicators(df)
    save_snapshot(df_ind, args.symbol, "demo")

    print("Feature computation complete.")
