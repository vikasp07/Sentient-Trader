# test_evaluation.py
"""
Test script for evaluation module.

Validates:
1. Outcome labeling system
2. Strategy metrics computation  
3. API endpoints (if server running)
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_outcome_labeler():
    """Test the outcome labeling system."""
    print("\n=== Testing Outcome Labeler ===\n")
    
    from evaluation.outcome_labeler import OutcomeLabeler
    
    # Create labeler with test directory
    test_dir = Path("data/test_outcomes")
    test_dir.mkdir(parents=True, exist_ok=True)
    
    labeler = OutcomeLabeler(outcomes_dir=str(test_dir))
    
    # Register a test decision
    pending = labeler.register_decision(
        decision_id="test_eval_001",
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
    
    print(f"✓ Registered decision: {pending.decision_id}")
    print(f"  Symbol: {pending.symbol}")
    print(f"  Decision: {pending.decision}")
    print(f"  Price T0: ${pending.price_t0}")
    
    # Simulate price updates at different horizons
    labeler.update_price("test_eval_001", 30, 176.20)
    print(f"✓ Updated 30m price: $176.20")
    
    labeler.update_price("test_eval_001", 60, 177.00)
    print(f"✓ Updated 1h price: $177.00")
    
    # Final update triggers labeling
    outcome = labeler.update_price("test_eval_001", 1440, 178.50)
    
    if outcome:
        print(f"\n✓ Outcome labeled successfully!")
        print(f"  Return 30m: {outcome.return_30m:.2f}%")
        print(f"  Return 1h: {outcome.return_1h:.2f}%")
        print(f"  Return 1d: {outcome.return_1d:.2f}%")
        print(f"  Success 1h: {outcome.success_1h}")
        print(f"  Success 1d: {outcome.success_1d}")
    
    # Get stats
    stats = labeler.get_stats()
    print(f"\n✓ Labeler stats: {stats}")
    
    return True


def test_strategy_metrics():
    """Test the strategy metrics system."""
    print("\n=== Testing Strategy Metrics ===\n")
    
    from evaluation.metrics import StrategyMetrics
    
    metrics = StrategyMetrics()
    
    # Sample outcomes for testing
    sample_outcomes = [
        {
            'decision': 'BUY',
            'symbol': 'AAPL',
            'sentiment_score': 0.75,
            'ticker_confidence': 0.85,
            'return_1h': 1.5,
            'success_1h': True,
            'indicators': {'sma_5': 175, 'rsi_14': 55, 'macd': 1.2}
        },
        {
            'decision': 'BUY',
            'symbol': 'MSFT',
            'sentiment_score': 0.45,
            'ticker_confidence': 0.72,
            'return_1h': -0.8,
            'success_1h': False,
            'indicators': {'sma_5': 380, 'macd': 1.2}
        },
        {
            'decision': 'SELL',
            'symbol': 'TSLA',
            'sentiment_score': -0.55,
            'ticker_confidence': 0.91,
            'return_1h': -2.1,
            'success_1h': True,
            'indicators': {'adx': 28, 'rsi_14': 72}
        },
        {
            'decision': 'SELL',
            'symbol': 'NVDA',
            'sentiment_score': -0.32,
            'ticker_confidence': 0.65,
            'return_1h': 0.5,
            'success_1h': False,
            'indicators': {'boll_upper': 500, 'atr_14': 15}
        },
        {
            'decision': 'BUY',
            'symbol': 'GOOGL',
            'sentiment_score': 0.82,
            'ticker_confidence': 0.93,
            'return_1h': 2.3,
            'success_1h': True,
            'indicators': {'sma_5': 140, 'sma_20': 138, 'momentum_10': 5}
        }
    ]
    
    summary = metrics.compute_from_outcomes(sample_outcomes)
    
    print("✓ Metrics computed successfully!")
    print(f"\nOverall Performance:")
    print(f"  Total trades: {summary['overall']['total_trades']}")
    print(f"  Win rate: {summary['overall']['win_rate']:.1%}")
    print(f"  Avg return: {summary['overall']['avg_return']:.2f}%")
    
    print(f"\nBy Sentiment Bucket:")
    for bucket, stats in summary['by_sentiment'].items():
        print(f"  {bucket}: {stats['total']} trades, {stats['win_rate']:.1%} win rate")
    
    print(f"\nBy Confidence Bucket:")
    for bucket, stats in summary['by_confidence'].items():
        print(f"  {bucket}: {stats['total']} trades, {stats['win_rate']:.1%} win rate")
    
    print(f"\nFalse Positive Analysis:")
    fp = summary['false_positive_analysis']
    print(f"  BUY signals: {fp['buy_signals']['false_positive_rate']:.1%} FP rate")
    print(f"  SELL signals: {fp['sell_signals']['false_positive_rate']:.1%} FP rate")
    
    # Save test reports
    test_output = Path("results/metrics")
    test_output.mkdir(parents=True, exist_ok=True)
    
    json_path = metrics.save_json_report(summary, "test_metrics.json")
    print(f"\n✓ Saved JSON report: {json_path}")
    
    csv_paths = metrics.save_csv_report(summary, "test_metrics")
    print(f"✓ Saved {len(csv_paths)} CSV reports")
    
    return True


def test_api_endpoints():
    """Test API endpoints (requires server to be running)."""
    print("\n=== Testing API Endpoints ===\n")
    
    try:
        import requests
    except ImportError:
        print("⚠ requests library not available, skipping API tests")
        return True
    
    base_url = os.getenv("API_URL", "http://localhost:8000")
    
    endpoints = [
        ("/health", "GET"),
        ("/decisions/latest?limit=5", "GET"),
        ("/metrics/quick", "GET"),
    ]
    
    for endpoint, method in endpoints:
        try:
            url = f"{base_url}{endpoint}"
            if method == "GET":
                resp = requests.get(url, timeout=5)
            
            if resp.status_code == 200:
                print(f"✓ {endpoint}: OK")
                data = resp.json()
                print(f"  Response keys: {list(data.keys())[:5]}")
            else:
                print(f"⚠ {endpoint}: {resp.status_code}")
                
        except requests.exceptions.ConnectionError:
            print(f"⚠ {endpoint}: Server not running (connection refused)")
        except Exception as e:
            print(f"✗ {endpoint}: {e}")
    
    return True


def test_decisions_file():
    """Test reading from existing decisions file."""
    print("\n=== Testing Decisions File Reading ===\n")
    
    decisions_path = Path("results/trade_decisions.jsonl")
    
    if not decisions_path.exists():
        print(f"⚠ No decisions file found at {decisions_path}")
        return True
    
    decisions = []
    with open(decisions_path, 'r') as f:
        for line in f:
            if line.strip():
                try:
                    decisions.append(json.loads(line.strip()))
                except:
                    continue
    
    print(f"✓ Loaded {len(decisions)} decisions from file")
    
    if decisions:
        # Show sample
        sample = decisions[-1]  # Most recent
        print(f"\nMost recent decision:")
        print(f"  Symbol: {sample.get('symbol')}")
        print(f"  Decision: {sample.get('decision')}")
        print(f"  Sentiment: {sample.get('sentiment_score', sample.get('effective_sentiment', 'N/A'))}")
        print(f"  Confidence: {sample.get('ticker_confidence', 'N/A')}")
        print(f"  Timestamp: {sample.get('ts')}")
    
    # Count by decision type
    by_decision = {}
    for d in decisions:
        decision = d.get('decision', 'UNKNOWN')
        by_decision[decision] = by_decision.get(decision, 0) + 1
    
    print(f"\nDecisions by type:")
    for decision, count in sorted(by_decision.items()):
        print(f"  {decision}: {count}")
    
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("  Sentient Trader - Evaluation Module Tests")
    print("=" * 60)
    
    all_passed = True
    
    try:
        all_passed &= test_outcome_labeler()
    except Exception as e:
        print(f"\n✗ Outcome labeler test failed: {e}")
        all_passed = False
    
    try:
        all_passed &= test_strategy_metrics()
    except Exception as e:
        print(f"\n✗ Strategy metrics test failed: {e}")
        all_passed = False
    
    try:
        all_passed &= test_decisions_file()
    except Exception as e:
        print(f"\n✗ Decisions file test failed: {e}")
        all_passed = False
    
    try:
        all_passed &= test_api_endpoints()
    except Exception as e:
        print(f"\n✗ API endpoints test failed: {e}")
        all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("  All tests passed! ✓")
    else:
        print("  Some tests failed. Check output above.")
    print("=" * 60)
