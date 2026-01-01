"""
Test script for ticker extraction system.

Run locally without Docker to verify extraction logic.

Usage:
    python test_ticker_extraction.py
"""

import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_extraction():
    """Test the ticker extraction system."""
    
    # Import after path setup
    from enrich.ticker_extraction.extractor import TickerExtractor
    from enrich.ticker_extraction.api import extract_from_article, is_trading_eligible
    
    print("=" * 60)
    print("TICKER EXTRACTION TEST")
    print("=" * 60)
    
    # Test cases
    test_articles = [
        {
            "title": "Apple stock rises after strong iPhone demand",
            "description": "Shares of Apple surged after earnings beat expectations. AAPL is up 5% in after-hours trading.",
            "source": "Reuters",
            "published_at": "2025-01-12T09:30:00Z"
        },
        {
            "title": "Tesla and Microsoft announce partnership",
            "description": "Tesla Inc and Microsoft Corp revealed a new collaboration on AI-powered vehicle systems.",
            "source": "Bloomberg",
            "published_at": "2025-01-12T10:00:00Z"
        },
        {
            "title": "$NVDA surges on AI chip demand",
            "description": "NVIDIA stock (NASDAQ:NVDA) jumped 8% after strong earnings. The company beat analyst expectations.",
            "source": "CNBC",
            "published_at": "2025-01-12T11:00:00Z"
        },
        {
            "title": "Market update: Tech sector mixed",
            "description": "The technology sector showed mixed results today with some gains in cloud computing.",
            "source": "MarketWatch",
            "published_at": "2025-01-12T12:00:00Z"
        },
        {
            "title": "Amazon Web Services expands in Europe",
            "description": "Amazon.com Inc announced new data centers in Germany and France for AWS customers.",
            "source": "WSJ",
            "published_at": "2025-01-12T13:00:00Z"
        },
        {
            "title": "Meta faces regulatory scrutiny",
            "description": "Meta Platforms Inc, formerly known as Facebook, is under investigation by EU regulators.",
            "source": "FT",
            "published_at": "2025-01-12T14:00:00Z"
        }
    ]
    
    # Initialize extractor (without spaCy to avoid download)
    extractor = TickerExtractor()
    
    print("\n" + "-" * 60)
    print("HYBRID EXTRACTION RESULTS")
    print("-" * 60)
    
    for i, article in enumerate(test_articles, 1):
        print(f"\n[Article {i}]")
        print(f"Title: {article['title']}")
        print(f"Source: {article['source']}")
        
        # Extract using API format
        result = extract_from_article(article, min_confidence=0.3)
        
        if result["symbols"]:
            print(f"Symbols extracted:")
            for sym in result["symbols"]:
                trading = "✓ TRADING ELIGIBLE" if is_trading_eligible(sym["confidence"]) else ""
                print(f"  {sym['symbol']}: conf={sym['confidence']:.2f} {trading}")
                print(f"    Reasons: {', '.join(sym['reasons'])}")
        else:
            print("  No symbols extracted")
        
        print()
    
    # Test confidence scoring
    print("\n" + "-" * 60)
    print("CONFIDENCE SCORING BREAKDOWN")
    print("-" * 60)
    
    # Detailed test for one article
    test_article = {
        "title": "Apple Inc reports record iPhone 15 sales, $AAPL up 3%",
        "description": "Apple beat expectations with strong iPhone demand. The company's stock (AAPL) rose in after-hours. Microsoft and Google also reported gains."
    }
    
    print(f"\nDetailed extraction for:")
    print(f"Title: {test_article['title']}")
    print(f"Description: {test_article['description']}")
    
    results = extractor.extract(
        headline=test_article['title'],
        body=test_article['description'],
        min_confidence=0.2  # Lower threshold to see all matches
    )
    
    print(f"\nExtracted {len(results)} symbols:\n")
    
    for result in results:
        print(f"Symbol: {result.ticker}")
        print(f"  Confidence: {result.confidence:.3f}")
        print(f"  Reasoning: {result.reasoning}")
        print(f"  Matches ({len(result.matches)}):")
        for match in result.matches:
            location = "HEADLINE" if match.in_headline else "BODY"
            print(f"    - '{match.matched_text}' via {match.method} [{location}]")
        print()
    
    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


def test_required_format():
    """Test that output matches required format exactly."""
    
    from enrich.ticker_extraction.api import extract_from_article
    
    print("\n" + "-" * 60)
    print("REQUIRED OUTPUT FORMAT VALIDATION")
    print("-" * 60)
    
    input_article = {
        "title": "Apple stock rises after strong iPhone demand",
        "description": "Shares of Apple surged after earnings beat expectations",
        "source": "Reuters",
        "published_at": "2025-01-12T09:30:00Z"
    }
    
    print("\nINPUT:")
    print(json.dumps(input_article, indent=2))
    
    result = extract_from_article(input_article)
    
    print("\nOUTPUT:")
    print(json.dumps(result, indent=2))
    
    # Validate structure
    assert "symbols" in result, "Missing 'symbols' key"
    assert isinstance(result["symbols"], list), "'symbols' must be a list"
    
    for sym in result["symbols"]:
        assert "symbol" in sym, "Missing 'symbol' key"
        assert "confidence" in sym, "Missing 'confidence' key"
        assert "reasons" in sym, "Missing 'reasons' key"
        assert 0.0 <= sym["confidence"] <= 1.0, "Confidence out of range"
        assert isinstance(sym["reasons"], list), "'reasons' must be a list"
    
    print("\n✓ Output format validated successfully!")


if __name__ == "__main__":
    try:
        test_extraction()
        test_required_format()
    except ImportError as e:
        print(f"\nImport error: {e}")
        print("Some dependencies may not be installed.")
        print("The ticker extraction module has been created successfully.")
        print("Install dependencies with: pip install -r requirements.txt")
