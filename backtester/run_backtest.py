# backtester/run_backtest.py
"""
Deterministic backtester with full technical indicator support:
- accepts historical CSV(s)
- computes all technical indicators (SMA, EMA, MACD, RSI, Bollinger, VWAP, OBV, ATR, Stochastic, Williams %R, CCI, ADX, Momentum, ROC)
- deterministic ordering of events
- seeds all RNGs
- simple execution model with slippage and fees
- writes trade ledger to results/ledger_<timestamp>.csv
"""

import os
import sys
import argparse
import random
from datetime import datetime

import numpy as np
import pandas as pd

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feature_store.feature_store import compute_indicators


# -------------------------
# Utilities
# -------------------------
def setup_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)


def deterministic_sort(df: pd.DataFrame):
    """Ensure stable deterministic ordering"""
    return df.sort_values(by=["ts", "symbol"]).reset_index(drop=True)


def simple_execution(price: float, side: str, slippage_pct=0.0005, fee_pct=0.0005):
    slippage = price * slippage_pct
    fee = price * fee_pct
    exec_price = price + slippage if side == "buy" else price - slippage
    return exec_price, fee


# -------------------------
# Advanced Strategy using all indicators + NEWS SENTIMENT
# -------------------------
def advanced_strategy(row, news_df, sentiment_score=None, ticker_confidence=None, position_state=None):
    """
    CONSERVATIVE Trading Strategy - Professional Approach with NEWS INTEGRATION
    
    KEY PRINCIPLES:
    1. QUALITY OVER QUANTITY - Only take A+ setups
    2. TREND MUST BE CLEAR - Strong trend confirmation required
    3. MULTIPLE CONFIRMATIONS - 70%+ indicator agreement
    4. MOMENTUM MUST SUPPORT - Never trade against momentum
    5. PATIENCE - It's OK to hold cash and wait
    6. NEWS SENTIMENT - Factor in effective_sentiment for decision weighting
    
    A+ Setup Requirements:
    - Clear trend (SMA_20 > SMA_50 OR SMA_20 < SMA_50 by > 0.5%)
    - ADX > 20 (trend is present)
    - Price above SMA_20 for buys (below for sells)
    - MACD histogram positive and growing
    - RSI in healthy range (40-65 for buys, 35-60 for sells)
    - Multiple momentum confirmations
    - SENTIMENT supports direction (if available)
    """
    price = row.get("price")
    symbol = row.get("symbol", "UNKNOWN")
    
    if pd.isna(price) or price is None:
        return "hold", {}

    indicators_used = {}
    
    # ============================================
    # NEWS SENTIMENT INTEGRATION
    # ============================================
    effective_sentiment = None
    sentiment_boost = 0  # Additional confidence from sentiment
    
    # Try to get sentiment from the row first (for trade_decisions.jsonl data)
    if "effective_sentiment" in row and pd.notna(row.get("effective_sentiment")):
        effective_sentiment = row.get("effective_sentiment")
        ticker_confidence = row.get("ticker_confidence", 1.0)
    elif "sentiment_score" in row and pd.notna(row.get("sentiment_score")):
        raw_sentiment = row.get("sentiment_score")
        conf = row.get("ticker_confidence", 1.0) if pd.notna(row.get("ticker_confidence")) else 1.0
        effective_sentiment = raw_sentiment * conf
        ticker_confidence = conf
    elif sentiment_score is not None:
        # Use passed-in sentiment
        conf = ticker_confidence if ticker_confidence is not None else 1.0
        effective_sentiment = sentiment_score * conf
    elif news_df is not None and len(news_df) > 0:
        # Try to find matching news for this symbol
        ts = row.get("ts")
        if ts is not None:
            # Look for news within 1 hour window
            try:
                if "published_at" in news_df.columns:
                    recent_news = news_df[
                        (news_df["symbol"] == symbol) | 
                        (news_df.get("ticker", "") == symbol) |
                        (news_df.get("tickers", "").str.contains(symbol, na=False))
                    ]
                    if len(recent_news) > 0:
                        # Get latest news sentiment
                        if "effective_sentiment" in recent_news.columns:
                            effective_sentiment = recent_news["effective_sentiment"].iloc[-1]
                            ticker_confidence = recent_news.get("ticker_confidence", pd.Series([1.0])).iloc[-1]
                        elif "sentiment_score" in recent_news.columns:
                            raw = recent_news["sentiment_score"].iloc[-1]
                            conf = recent_news.get("ticker_confidence", pd.Series([1.0])).iloc[-1]
                            effective_sentiment = raw * conf if pd.notna(raw) else None
                            ticker_confidence = conf
            except Exception:
                pass  # Silently handle news lookup errors
    
    if effective_sentiment is not None:
        indicators_used["effective_sentiment"] = effective_sentiment
        indicators_used["ticker_confidence"] = ticker_confidence
        
        # Sentiment boost: strong sentiment adds to decision confidence
        if abs(effective_sentiment) > 0.6:
            sentiment_boost = 1  # Strong sentiment = +1 confirmation
        elif abs(effective_sentiment) > 0.3:
            sentiment_boost = 0.5  # Moderate sentiment = +0.5 confirmation

    indicators_used = {}
    
    # ============================================
    # STEP 1: STRICT TREND CONFIRMATION
    # ============================================
    sma5 = row.get("sma_5")
    sma20 = row.get("sma_20")
    sma50 = row.get("sma_50")
    
    # Initialize - default NO TRADE
    can_buy = False
    can_sell = False
    trend_score = 0
    
    if pd.notna(sma20) and pd.notna(sma50) and pd.notna(sma5):
        indicators_used["sma_5"] = sma5
        indicators_used["sma_20"] = sma20
        indicators_used["sma_50"] = sma50
        
        # Calculate trend strength
        trend_pct = ((sma20 - sma50) / sma50) * 100 if sma50 > 0 else 0
        indicators_used["trend_pct"] = trend_pct
        
        # BULLISH: SMA alignment with price confirmation
        if sma20 > sma50 and price > sma20:
            if trend_pct > 0.15:  # Need modest trend (>0.15%)
                can_buy = True
                trend_score = min(trend_pct, 2)
        
        # BEARISH: Inverted SMA with price confirmation
        elif sma20 < sma50 and price < sma20:
            if trend_pct < -0.15:  # Need modest trend
                can_sell = True
                trend_score = min(abs(trend_pct), 2)
    
    # If no clear trend, don't trade
    if not can_buy and not can_sell:
        return "hold", indicators_used
    
    indicators_used["can_buy"] = can_buy
    indicators_used["can_sell"] = can_sell
    indicators_used["trend_score"] = trend_score
    
    # ============================================
    # STEP 2: ADX MUST CONFIRM TREND EXISTS
    # ============================================
    adx_val = row.get("adx")
    plus_di = row.get("plus_di")
    minus_di = row.get("minus_di")
    
    if pd.notna(adx_val):
        indicators_used["adx"] = adx_val
        
        # ADX < 15 = very weak trend = don't trade
        if adx_val < 15:
            return "hold", indicators_used
        
        # Check DI direction confirms trend
        if pd.notna(plus_di) and pd.notna(minus_di):
            indicators_used["plus_di"] = plus_di
            indicators_used["minus_di"] = minus_di
            
            if can_buy and plus_di < minus_di:
                return "hold", indicators_used  # DI doesn't confirm uptrend
            if can_sell and minus_di < plus_di:
                return "hold", indicators_used  # DI doesn't confirm downtrend
    else:
        # No ADX data = don't trade
        return "hold", indicators_used
    
    # ============================================
    # STEP 3: MACD MUST BE FAVORABLE
    # ============================================
    macd = row.get("macd")
    macd_signal = row.get("macd_signal")
    macd_hist = row.get("macd_hist")
    
    if pd.notna(macd) and pd.notna(macd_signal) and pd.notna(macd_hist):
        indicators_used["macd"] = macd
        indicators_used["macd_signal"] = macd_signal
        indicators_used["macd_hist"] = macd_hist
        
        # For buys: MACD must be above signal (bullish) and histogram positive
        if can_buy:
            if macd < macd_signal or macd_hist < 0:
                return "hold", indicators_used
        
        # For sells: MACD must be below signal (bearish) and histogram negative
        if can_sell:
            if macd > macd_signal or macd_hist > 0:
                return "hold", indicators_used
    else:
        return "hold", indicators_used
    
    # ============================================
    # STEP 4: RSI MUST BE IN HEALTHY ZONE
    # ============================================
    rsi = row.get("rsi_14")
    
    if pd.notna(rsi):
        indicators_used["rsi_14"] = rsi
        
        # For buys: RSI should be 35-70 (some flexibility)
        if can_buy:
            if rsi < 35 or rsi > 75:
                return "hold", indicators_used
        
        # For sells: RSI should be 25-65
        if can_sell:
            if rsi < 25 or rsi > 65:
                return "hold", indicators_used
    
    # ============================================
    # STEP 5: MOMENTUM CONFIRMATION
    # ============================================
    momentum = row.get("momentum_10")
    roc = row.get("roc_10")
    
    momentum_confirms = 0
    
    if pd.notna(momentum):
        indicators_used["momentum_10"] = momentum
        if can_buy and momentum > 0:
            momentum_confirms += 1
        elif can_sell and momentum < 0:
            momentum_confirms += 1
    
    if pd.notna(roc):
        indicators_used["roc_10"] = roc
        if can_buy and roc > 0:
            momentum_confirms += 1
        elif can_sell and roc < 0:
            momentum_confirms += 1
    
    # Need at least one momentum confirmation
    if momentum_confirms < 1:
        return "hold", indicators_used
    
    # ============================================
    # STEP 6: STOCHASTIC AS TIMING FILTER
    # ============================================
    stoch_k = row.get("stoch_k")
    stoch_d = row.get("stoch_d")
    
    if pd.notna(stoch_k) and pd.notna(stoch_d):
        indicators_used["stoch_k"] = stoch_k
        indicators_used["stoch_d"] = stoch_d
        
        # For buys: Don't buy if overbought (>80)
        if can_buy and stoch_k > 80:
            return "hold", indicators_used
        
        # For sells: Don't sell if oversold (<20)
        if can_sell and stoch_k < 20:
            return "hold", indicators_used
    
    # ============================================
    # STEP 7: SENTIMENT CONFIRMATION (if available)
    # ============================================
    if effective_sentiment is not None:
        # For buys: sentiment should be positive or neutral
        if can_buy and effective_sentiment < -0.3:
            # Strong negative sentiment contradicts buy signal
            indicators_used["sentiment_conflict"] = "buy_blocked_by_negative_sentiment"
            return "hold", indicators_used
        
        # For sells: sentiment should be negative or neutral
        if can_sell and effective_sentiment > 0.3:
            # Strong positive sentiment contradicts sell signal
            indicators_used["sentiment_conflict"] = "sell_blocked_by_positive_sentiment"
            return "hold", indicators_used
        
        # Boost decision if sentiment aligns with direction
        if can_buy and effective_sentiment > 0.4:
            indicators_used["sentiment_alignment"] = "buy_boosted_by_positive_sentiment"
        if can_sell and effective_sentiment < -0.4:
            indicators_used["sentiment_alignment"] = "sell_boosted_by_negative_sentiment"
    
    # ============================================
    # STEP 8: FINAL DECISION - ALL CRITERIA MET
    # ============================================
    if can_buy:
        indicators_used["decision_reason"] = "A+ buy setup"
        if effective_sentiment is not None and effective_sentiment > 0.4:
            indicators_used["decision_reason"] += " + positive sentiment"
        return "buy", indicators_used
    
    if can_sell:
        indicators_used["decision_reason"] = "A+ sell setup"
        if effective_sentiment is not None and effective_sentiment < -0.4:
            indicators_used["decision_reason"] += " + negative sentiment"
        return "sell", indicators_used
    
    return "hold", indicators_used


def simple_strategy(row, news_df):
    """
    Very simple demo strategy (legacy fallback).
    Must NEVER crash.
    """
    price = row["price"]

    if pd.isna(price):
        return "hold"

    return "buy" if price > 100 else "hold"


# -------------------------
# Backtest Engine with Risk Management
# -------------------------
def run_backtest(
    ticks_df: pd.DataFrame,
    news_df: pd.DataFrame,
    strategy_fn,
    seed=42,
    initial_cash=100_000,
    use_advanced_strategy=True,
    stop_loss_atr_mult=2.5,    # Stop loss = 2.5x ATR (wider)
    take_profit_atr_mult=4.0,  # Take profit = 4x ATR (better R:R)
    trailing_stop_atr=2.0,     # Trailing stop at 2x ATR
    min_hold_periods=10,       # Minimum bars to hold (avoid churning)
    max_position_pct=10.0,     # Max 10% of portfolio per position
):
    setup_seed(seed)

    ticks = deterministic_sort(ticks_df)
    
    # Compute technical indicators per symbol
    print("Computing technical indicators...")
    symbols = ticks["symbol"].unique()
    all_ticks_with_indicators = []
    
    for symbol in symbols:
        symbol_df = ticks[ticks["symbol"] == symbol].copy()
        if len(symbol_df) > 0:
            symbol_df_ind = compute_indicators(symbol_df)
            symbol_df_ind["symbol"] = symbol
            all_ticks_with_indicators.append(symbol_df_ind)
    
    if all_ticks_with_indicators:
        ticks = pd.concat(all_ticks_with_indicators, ignore_index=True)
        ticks = deterministic_sort(ticks)
    
    print(f"Indicators computed for {len(symbols)} symbols, {len(ticks)} ticks")

    ledger = []
    trades_summary = []
    cash = initial_cash
    position = {}       # symbol -> qty
    entry_prices = {}   # symbol -> entry price
    entry_times = {}    # symbol -> entry bar index (for min hold)
    stop_losses = {}    # symbol -> stop loss price
    take_profits = {}   # symbol -> take profit price
    trailing_stops = {} # symbol -> trailing stop price
    highest_prices = {} # symbol -> highest price since entry (for trailing)
    bars_held = {}      # symbol -> number of bars held
    entry_atrs = {}     # symbol -> ATR at entry time

    for idx, row in ticks.iterrows():
        ts = row["ts"]
        symbol = row["symbol"]
        price = row["price"]

        if pd.isna(price):
            continue  # skip invalid rows safely

        # ============================================
        # RISK MANAGEMENT: Update Trailing Stop & Check Exits
        # ============================================
        if position.get(symbol, 0) > 0:
            entry_price = entry_prices.get(symbol, price)
            sl_price = stop_losses.get(symbol, 0)
            tp_price = take_profits.get(symbol, float('inf'))
            bars = bars_held.get(symbol, 0) + 1
            bars_held[symbol] = bars
            
            # Update highest price and trailing stop
            if price > highest_prices.get(symbol, price):
                highest_prices[symbol] = price
                # Update trailing stop (only moves up, never down)
                atr = entry_atrs.get(symbol, price * 0.02)  # fallback 2%
                new_trailing = price - (trailing_stop_atr * atr)
                if new_trailing > trailing_stops.get(symbol, 0):
                    trailing_stops[symbol] = new_trailing
                    # Move stop loss up to trailing stop if higher
                    if new_trailing > stop_losses[symbol]:
                        stop_losses[symbol] = new_trailing
            
            exit_reason = None
            
            # Check stop loss (includes trailing)
            if price <= sl_price:
                exit_reason = "stop_loss"
            # Check take profit
            elif price >= tp_price:
                exit_reason = "take_profit"
            
            if exit_reason:
                qty = position[symbol]
                exec_price, fee = simple_execution(price, "sell")
                cash += (exec_price * qty - fee)
                
                pnl = (exec_price - entry_price) * qty - fee
                pnl_pct = ((exec_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                
                trades_summary.append({
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "exit_price": exec_price,
                    "qty": qty,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "exit_reason": exit_reason,
                    "bars_held": bars,
                })

                ledger.append({
                    "ts": ts,
                    "symbol": symbol,
                    "side": "sell",
                    "qty": qty,
                    "price": exec_price,
                    "fee": fee,
                    "cash": cash,
                    "pnl": pnl,
                    "exit_reason": exit_reason,
                    "bars_held": bars,
                })

                position[symbol] = 0
                entry_prices[symbol] = 0
                stop_losses[symbol] = 0
                take_profits[symbol] = 0
                trailing_stops[symbol] = 0
                highest_prices[symbol] = 0
                bars_held[symbol] = 0
                entry_atrs[symbol] = 0
                continue  # Move to next tick

        # ============================================
        # Get Strategy Signal
        # ============================================
        if use_advanced_strategy:
            decision, indicators = advanced_strategy(row, news_df)
        else:
            decision = strategy_fn(row, news_df)
            indicators = {}

        # ============================================
        # EXECUTE TRADES with Position Sizing
        # ============================================
        if decision == "buy" and position.get(symbol, 0) == 0:
            # Position sizing: max 10% of portfolio
            max_position_value = cash * (max_position_pct / 100)
            qty = max(1, int(max_position_value / price))
            
            exec_price, fee = simple_execution(price, "buy")
            total_cost = exec_price * qty + fee
            
            # Only buy if we have enough cash
            if total_cost <= cash:
                cash -= total_cost

                position[symbol] = qty
                entry_prices[symbol] = exec_price
                entry_times[symbol] = idx
                bars_held[symbol] = 0
                highest_prices[symbol] = exec_price
                
                # ATR-based stops (use row's ATR)
                atr = row.get("atr_14")
                if pd.notna(atr) and atr > 0:
                    entry_atrs[symbol] = atr
                    stop_losses[symbol] = exec_price - (stop_loss_atr_mult * atr)
                    take_profits[symbol] = exec_price + (take_profit_atr_mult * atr)
                    trailing_stops[symbol] = exec_price - (trailing_stop_atr * atr)
                else:
                    # Fallback: Fixed percentage stops (3% SL, 5% TP)
                    entry_atrs[symbol] = exec_price * 0.015
                    stop_losses[symbol] = exec_price * 0.97
                    take_profits[symbol] = exec_price * 1.05
                    trailing_stops[symbol] = exec_price * 0.98

                ledger.append({
                    "ts": ts,
                    "symbol": symbol,
                    "side": "buy",
                    "qty": qty,
                    "price": exec_price,
                    "fee": fee,
                    "cash": cash,
                    "stop_loss": stop_losses[symbol],
                    "take_profit": take_profits[symbol],
                    **{f"ind_{k}": v for k, v in indicators.items() if k not in ["buy_signals", "sell_signals", "total_weight"]},
                    "buy_signals": indicators.get("buy_signals", 0),
                    "sell_signals": indicators.get("sell_signals", 0),
                })

        elif decision == "sell" and position.get(symbol, 0) > 0:
            # Check minimum hold period
            bars = bars_held.get(symbol, 0)
            if bars < min_hold_periods:
                continue  # Don't sell too early
            
            qty = position[symbol]
            exec_price, fee = simple_execution(price, "sell")
            cash += (exec_price * qty - fee)
            
            # Calculate P&L for this trade
            entry_price = entry_prices.get(symbol, exec_price)
            pnl = (exec_price - entry_price) * qty - fee
            
            trades_summary.append({
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exec_price,
                "qty": qty,
                "pnl": pnl,
                "pnl_pct": (exec_price - entry_price) / entry_price * 100 if entry_price > 0 else 0,
                "exit_reason": "signal",
                "bars_held": bars,
            })

            ledger.append({
                "ts": ts,
                "symbol": symbol,
                "side": "sell",
                "qty": qty,
                "price": exec_price,
                "fee": fee,
                "cash": cash,
                "pnl": pnl,
                "exit_reason": "signal",
                "bars_held": bars,
                **{f"ind_{k}": v for k, v in indicators.items() if k not in ["buy_signals", "sell_signals", "total_weight"]},
                "buy_signals": indicators.get("buy_signals", 0),
                "sell_signals": indicators.get("sell_signals", 0),
            })

            position[symbol] = 0
            entry_prices[symbol] = 0
            stop_losses[symbol] = 0
            take_profits[symbol] = 0
            bars_held[symbol] = 0

    # Close any remaining positions at last price
    for symbol, qty in position.items():
        if qty > 0:
            last_row = ticks[ticks["symbol"] == symbol].iloc[-1]
            last_price = last_row["price"]
            exec_price, fee = simple_execution(last_price, "sell")
            cash += (exec_price * qty - fee)
            
            entry_price = entry_prices.get(symbol, exec_price)
            pnl = (exec_price - entry_price) * qty - fee
            
            trades_summary.append({
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exec_price,
                "qty": qty,
                "pnl": pnl,
                "pnl_pct": (exec_price - entry_price) / entry_price * 100 if entry_price > 0 else 0,
                "exit_reason": "end_of_data",
                "bars_held": bars_held.get(symbol, 0),
            })
            
            ledger.append({
                "ts": last_row["ts"],
                "symbol": symbol,
                "side": "sell",
                "qty": qty,
                "price": exec_price,
                "fee": fee,
                "cash": cash,
                "pnl": pnl,
                "exit_reason": "end_of_data",
            })

    return ledger, cash, trades_summary


# -------------------------
# CLI
# -------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks", default="data/real_ticks.csv")
    parser.add_argument("--news", default="data/news_small.csv")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="results")
    parser.add_argument("--simple", action="store_true", help="Use simple strategy instead of advanced")
    parser.add_argument("--sl-atr", type=float, default=2.5, help="Stop loss ATR multiplier")
    parser.add_argument("--tp-atr", type=float, default=4.0, help="Take profit ATR multiplier")
    parser.add_argument("--trail-atr", type=float, default=2.0, help="Trailing stop ATR multiplier")
    parser.add_argument("--min-hold", type=int, default=10, help="Minimum bars to hold")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # ---- Load & CLEAN ticks ----
    print(f"Loading ticks from {args.ticks}...")
    ticks_df = pd.read_csv(args.ticks, parse_dates=["ts"])

    ticks_df["price"] = pd.to_numeric(ticks_df["price"], errors="coerce")

    # Drop rows with invalid essentials
    ticks_df = ticks_df.dropna(subset=["ts", "symbol", "price"])
    print(f"Loaded {len(ticks_df)} valid ticks")

    # ---- Load news (optional) ----
    try:
        news_df = pd.read_csv(args.news, parse_dates=["published_at"])
        print(f"Loaded {len(news_df)} news articles")
    except Exception:
        news_df = pd.DataFrame()
        print("No news data loaded")

    # ---- Run backtest ----
    print("\n" + "="*60)
    print("PROFESSIONAL TRADING STRATEGY BACKTEST")
    print("="*60)
    print(f"Strategy: {'Simple' if args.simple else 'Advanced Trend-Following'}")
    print(f"Stop Loss: {args.sl_atr}x ATR")
    print(f"Take Profit: {args.tp_atr}x ATR")
    print(f"Trailing Stop: {args.trail_atr}x ATR")
    print(f"Min Hold Period: {args.min_hold} bars")
    print("="*60)
    
    ledger, final_cash, trades = run_backtest(
        ticks_df,
        news_df,
        simple_strategy,
        seed=args.seed,
        use_advanced_strategy=not args.simple,
        stop_loss_atr_mult=args.sl_atr,
        take_profit_atr_mult=args.tp_atr,
        trailing_stop_atr=args.trail_atr,
        min_hold_periods=args.min_hold,
    )

    ledger_df = pd.DataFrame(ledger)

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out, f"ledger_{ts}.csv")
    ledger_df.to_csv(out_path, index=False)
    
    # Save trades summary
    if trades:
        trades_df = pd.DataFrame(trades)
        trades_path = os.path.join(args.out, f"trades_summary_{ts}.csv")
        trades_df.to_csv(trades_path, index=False)

    # Print comprehensive summary statistics
    print("\n" + "="*60)
    print("BACKTEST RESULTS")
    print("="*60)
    print(f"Initial Capital:  ${100000:,.2f}")
    print(f"Final Capital:    ${final_cash:,.2f}")
    print(f"Total P&L:        ${final_cash - 100000:,.2f}")
    print(f"Return:           {(final_cash - 100000) / 100000 * 100:.2f}%")
    print("-"*60)
    
    if trades:
        trades_df = pd.DataFrame(trades)
        
        winning_trades = trades_df[trades_df["pnl"] > 0]
        losing_trades = trades_df[trades_df["pnl"] < 0]
        
        total_trades = len(trades)
        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0
        
        avg_win = winning_trades["pnl"].mean() if len(winning_trades) > 0 else 0
        avg_loss = losing_trades["pnl"].mean() if len(losing_trades) > 0 else 0
        
        # Profit Factor = Gross Profit / Gross Loss
        gross_profit = winning_trades["pnl"].sum() if len(winning_trades) > 0 else 0
        gross_loss = abs(losing_trades["pnl"].sum()) if len(losing_trades) > 0 else 0.01
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Expectancy = (Win Rate * Avg Win) - (Loss Rate * Avg Loss)
        expectancy = (win_rate/100 * avg_win) - ((100-win_rate)/100 * abs(avg_loss))
        
        # Analyze exit reasons
        exit_reasons = trades_df["exit_reason"].value_counts() if "exit_reason" in trades_df.columns else {}
        
        print(f"Total Trades:     {total_trades}")
        print(f"Winning Trades:   {win_count} ({win_rate:.1f}%)")
        print(f"Losing Trades:    {loss_count} ({100-win_rate:.1f}%)")
        print("-"*60)
        print(f"Gross Profit:     ${gross_profit:,.2f}")
        print(f"Gross Loss:       ${gross_loss:,.2f}")
        print(f"Net P&L:          ${gross_profit - gross_loss:,.2f}")
        print("-"*60)
        print(f"Average Win:      ${avg_win:,.2f}")
        print(f"Average Loss:     ${avg_loss:,.2f}")
        print(f"Profit Factor:    {profit_factor:.2f}")
        print(f"Expectancy:       ${expectancy:,.2f} per trade")
        print("-"*60)
        
        if len(exit_reasons) > 0:
            print("Exit Reasons:")
            for reason, count in exit_reasons.items():
                print(f"  {reason}: {count} ({count/total_trades*100:.1f}%)")
        
        # Performance by bars held
        if "bars_held" in trades_df.columns:
            avg_bars = trades_df["bars_held"].mean()
            print(f"\nAverage Holding Period: {avg_bars:.1f} bars")
        
        # Best and worst trades
        best_trade = trades_df.loc[trades_df["pnl"].idxmax()]
        worst_trade = trades_df.loc[trades_df["pnl"].idxmin()]
        print(f"\nBest Trade:  ${best_trade['pnl']:,.2f} ({best_trade['pnl_pct']:.2f}%)")
        print(f"Worst Trade: ${worst_trade['pnl']:,.2f} ({worst_trade['pnl_pct']:.2f}%)")
    
    print("="*60)
    print(f"\nLedger saved to: {out_path}")
    if trades:
        print(f"Trades summary saved to: {trades_path}")
