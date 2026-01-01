#!/usr/bin/env python
"""
Full Pipeline Runner - Demonstrates the complete Sentient-Trader workflow:
1. Load market data
2. Compute all technical indicators
3. Run strategy with indicator-based decision making
4. Execute backtest and generate results

This script runs the entire pipeline locally without Docker.
"""

import os
import sys
import json
from datetime import datetime

import pandas as pd
import numpy as np

# Ensure the project root is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feature_store.feature_store import compute_indicators
from backtester.run_backtest import run_backtest, advanced_strategy, simple_strategy


def print_section(title):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def main():
    print_section("SENTIENT-TRADER FULL PIPELINE")
    print(f"Started at: {datetime.now().isoformat()}")
    
    # Configuration
    TICKS_FILE = "data/real_ticks.csv"
    OUTPUT_DIR = "results"
    SEED = 42
    INITIAL_CASH = 100_000
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Step 1: Load Market Data
    print_section("STEP 1: Loading Market Data")
    
    ticks_df = pd.read_csv(TICKS_FILE, parse_dates=["ts"])
    ticks_df["price"] = pd.to_numeric(ticks_df["price"], errors="coerce")
    ticks_df = ticks_df.dropna(subset=["ts", "symbol", "price"])
    
    symbols = ticks_df["symbol"].unique()
    print(f"Loaded {len(ticks_df)} ticks for {len(symbols)} symbols")
    print(f"Symbols: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
    print(f"Date range: {ticks_df['ts'].min()} to {ticks_df['ts'].max()}")
    
    # Step 2: Compute Technical Indicators
    print_section("STEP 2: Computing Technical Indicators")
    
    all_indicators = []
    for symbol in symbols:
        symbol_df = ticks_df[ticks_df["symbol"] == symbol].copy()
        if len(symbol_df) > 0:
            symbol_df_ind = compute_indicators(symbol_df)
            symbol_df_ind["symbol"] = symbol
            all_indicators.append(symbol_df_ind)
            
            # Show sample of computed indicators for first symbol
            if symbol == symbols[0]:
                last_row = symbol_df_ind.iloc[-1]
                print(f"\nSample indicators for {symbol} (latest tick):")
                indicator_cols = [
                    "price", "sma_5", "sma_20", "sma_50", "ema_12", "ema_26",
                    "macd", "macd_signal", "macd_hist", "rsi_14",
                    "boll_upper", "boll_lower", "boll_mid",
                    "atr_14", "stoch_k", "stoch_d", "williams_r", "cci_20",
                    "adx", "plus_di", "minus_di", "momentum_10", "roc_10"
                ]
                for col in indicator_cols:
                    if col in last_row:
                        val = last_row[col]
                        if pd.notna(val):
                            print(f"  {col:20s}: {val:.4f}")
    
    ticks_with_indicators = pd.concat(all_indicators, ignore_index=True)
    print(f"\nTotal ticks with indicators: {len(ticks_with_indicators)}")
    
    # Step 3: Run Backtest with Advanced Strategy
    print_section("STEP 3: Running Backtest with Indicator Strategy")
    
    ledger, final_cash, trades = run_backtest(
        ticks_df,
        pd.DataFrame(),  # No news data for this demo
        simple_strategy,
        seed=SEED,
        initial_cash=INITIAL_CASH,
        use_advanced_strategy=True
    )
    
    # Step 4: Generate Results
    print_section("STEP 4: Results Summary")
    
    print(f"\nInitial Capital: ${INITIAL_CASH:,.2f}")
    print(f"Final Capital:   ${final_cash:,.2f}")
    print(f"Total P&L:       ${final_cash - INITIAL_CASH:,.2f}")
    print(f"Return:          {(final_cash - INITIAL_CASH) / INITIAL_CASH * 100:.2f}%")
    
    print(f"\nTotal Trades: {len(ledger)}")
    if trades:
        winning = [t for t in trades if t["pnl"] > 0]
        losing = [t for t in trades if t["pnl"] < 0]
        print(f"Winning Trades: {len(winning)}")
        print(f"Losing Trades:  {len(losing)}")
        if trades:
            print(f"Win Rate:       {len(winning)/len(trades)*100:.1f}%")
            total_pnl = sum(t["pnl"] for t in trades)
            avg_win = sum(t["pnl"] for t in winning) / len(winning) if winning else 0
            avg_loss = sum(t["pnl"] for t in losing) / len(losing) if losing else 0
            print(f"Total P&L:      ${total_pnl:,.2f}")
            print(f"Avg Win:        ${avg_win:,.2f}")
            print(f"Avg Loss:       ${avg_loss:,.2f}")
    
    # Save results
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    
    # Save ledger
    ledger_df = pd.DataFrame(ledger)
    ledger_path = os.path.join(OUTPUT_DIR, f"pipeline_ledger_{ts}.csv")
    ledger_df.to_csv(ledger_path, index=False)
    print(f"\nLedger saved to: {ledger_path}")
    
    # Save trades summary
    if trades:
        trades_df = pd.DataFrame(trades)
        trades_path = os.path.join(OUTPUT_DIR, f"pipeline_trades_{ts}.csv")
        trades_df.to_csv(trades_path, index=False)
        print(f"Trades saved to: {trades_path}")
    
    # Save run summary
    summary = {
        "timestamp": ts,
        "ticks_file": TICKS_FILE,
        "num_ticks": len(ticks_df),
        "symbols": list(symbols),
        "seed": SEED,
        "initial_cash": INITIAL_CASH,
        "final_cash": final_cash,
        "total_pnl": final_cash - INITIAL_CASH,
        "return_pct": (final_cash - INITIAL_CASH) / INITIAL_CASH * 100,
        "num_trades": len(ledger),
        "num_winning": len([t for t in trades if t["pnl"] > 0]) if trades else 0,
        "num_losing": len([t for t in trades if t["pnl"] < 0]) if trades else 0,
        "indicators_used": [
            "sma_5", "sma_20", "sma_50", "ema_12", "ema_26",
            "macd", "macd_signal", "macd_hist", "rsi_14",
            "boll_upper", "boll_lower", "boll_mid",
            "atr_14", "stoch_k", "stoch_d", "williams_r", "cci_20",
            "adx", "plus_di", "minus_di", "momentum_10", "roc_10"
        ]
    }
    
    summary_path = os.path.join(OUTPUT_DIR, f"pipeline_summary_{ts}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary saved to: {summary_path}")
    
    print_section("PIPELINE COMPLETE")
    print(f"Finished at: {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
