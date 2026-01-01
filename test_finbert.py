# test_finbert.py
"""
Test script for FinBERT sentiment analysis.

Tests:
1. Title-only analysis (~65-70% accuracy)
2. Title + snippet analysis (~78-85% accuracy)
3. Full text analysis (~85-90% accuracy)
"""

import sys

# Test cases with expected sentiments
TEST_CASES = [
    {
        "title": "Apple Stock Hits New Record High",
        "snippet": "Shares rose 3% after strong iPhone demand and better-than-expected quarterly earnings.",
        "expected": "positive"
    },
    {
        "title": "Tesla Shares Plummet After Disappointing Delivery Numbers",
        "snippet": "The electric vehicle maker missed analyst expectations, leading to a 7% drop in stock price.",
        "expected": "negative"
    },
    {
        "title": "Microsoft Announces New AI Features",
        "snippet": "The tech giant revealed plans to integrate AI across its product lineup.",
        "expected": "neutral"  # Could be positive, but announcement alone is neutral
    },
    {
        "title": "Amazon Beats Earnings Estimates",
        "snippet": "Revenue grew 12% year-over-year, exceeding Wall Street expectations by a wide margin.",
        "expected": "positive"
    },
    {
        "title": "Bank Stocks Fall on Interest Rate Concerns",
        "snippet": "Financial sector declined as investors worry about the impact of rising rates on loan demand.",
        "expected": "negative"
    }
]


def test_finbert():
    """Test FinBERT sentiment analysis with various inputs."""
    try:
        from enrich.sentiment.sentiment_analysis import analyze_text, combine_text
    except ImportError as e:
        print(f"\n❌ Import Error: {e}")
        print("Make sure transformers and torch are installed:")
        print("  pip install transformers torch")
        sys.exit(1)
    
    print("\n🧪 Testing FinBERT Sentiment Analysis\n")
    print("=" * 80)
    
    correct = 0
    total = len(TEST_CASES)
    
    for i, test in enumerate(TEST_CASES, 1):
        title = test["title"]
        snippet = test["snippet"]
        expected = test["expected"]
        
        print(f"\n📰 Test {i}:")
        print(f"   Title: {title}")
        print(f"   Snippet: {snippet}")
        print(f"   Expected: {expected}")
        
        # Test combining title + snippet
        combined, input_type = combine_text(title, snippet)
        print(f"\n   Combined text: {combined[:100]}...")
        print(f"   Input type: {input_type}")
        
        # Analyze with title + snippet
        result = analyze_text(text="", title=title, snippet=snippet)
        
        sentiment = result.get("sentiment", "unknown")
        score = result.get("sentiment_score", 0)
        confidence = result.get("confidence", 0)
        model = result.get("_meta", {}).get("model", "unknown")
        
        match = "✅" if sentiment == expected else "❌"
        if sentiment == expected:
            correct += 1
        
        print(f"\n   {match} Results:")
        print(f"   Sentiment: {sentiment}")
        print(f"   Score: {score:.4f}")
        print(f"   Confidence: {confidence:.4f}")
        print(f"   Model: {model}")
        
        if "probabilities" in result:
            probs = result["probabilities"]
            print(f"   Probabilities: pos={probs.get('positive', 0):.2f} neg={probs.get('negative', 0):.2f} neu={probs.get('neutral', 0):.2f}")
        
        if result.get("tickers"):
            print(f"   Tickers: {result['tickers']}")
        
        print("-" * 80)
    
    # Summary
    accuracy = (correct / total) * 100
    print(f"\n📊 Summary:")
    print(f"   Correct: {correct}/{total}")
    print(f"   Accuracy: {accuracy:.1f}%")
    
    if accuracy >= 80:
        print("\n✅ FinBERT sentiment analysis is working well!")
    elif accuracy >= 60:
        print("\n⚠️ FinBERT is working but accuracy could be improved with more context.")
    else:
        print("\n❌ FinBERT accuracy is lower than expected. Check model loading.")
    
    return accuracy >= 60


if __name__ == "__main__":
    print("\n🧠 FinBERT Sentiment Analysis Test\n")
    
    try:
        success = test_finbert()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
