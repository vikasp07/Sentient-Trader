"""
Ticker Extraction API

Provides the exact input/output format required by the pipeline:

INPUT:
{
  "title": "Apple stock rises after strong iPhone demand",
  "description": "Shares of Apple surged after earnings beat expectations",
  "source": "Reuters",
  "published_at": "2025-01-12T09:30:00Z"
}

OUTPUT:
{
  "symbols": [
    {
      "symbol": "AAPL",
      "confidence": 0.88,
      "reasons": [
        "company_name_match",
        "headline_match",
        "positive_context"
      ]
    }
  ]
}

Usage rules:
- Symbols with confidence < 0.5 → ignored
- Symbols with confidence ≥ 0.7 → eligible for trading
- Multiple symbols allowed per article
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass

from .extractor import TickerExtractor, ExtractionResult

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SymbolExtraction:
    """A single extracted symbol with confidence and reasoning."""
    symbol: str
    confidence: float
    reasons: List[str]
    
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons
        }


def extract_symbols(
    title: str,
    description: str = "",
    source: str = "",
    published_at: str = "",
    min_confidence: float = 0.5
) -> Dict:
    """
    Extract stock symbols from news article with confidence scores.
    
    This is the main API function that produces the exact required output format.
    
    Args:
        title: Article headline
        description: Article description/body
        source: News source (for logging)
        published_at: Publication timestamp (for logging)
        min_confidence: Minimum confidence threshold (default 0.5)
    
    Returns:
        Dict with "symbols" list containing extracted symbols
    """
    extractor = TickerExtractor()
    
    # Extract using hybrid approach
    results = extractor.extract(
        headline=title,
        body=description,
        min_confidence=min_confidence
    )
    
    # Convert to required output format
    symbols = []
    
    for result in results:
        # Build reasons list
        reasons = _build_reasons(result)
        
        symbol = SymbolExtraction(
            symbol=result.ticker,
            confidence=result.confidence,
            reasons=reasons
        )
        symbols.append(symbol.to_dict())
    
    return {"symbols": symbols}


def _build_reasons(result: ExtractionResult) -> List[str]:
    """Build human-readable reasons list from extraction result."""
    reasons = []
    
    # Add method-based reasons
    methods_used = set(m.method for m in result.matches)
    
    if 'explicit' in methods_used:
        reasons.append("explicit_ticker_match")
    
    if 'company_name' in methods_used:
        reasons.append("company_name_match")
    
    if 'nlp_entity' in methods_used:
        reasons.append("nlp_entity_match")
    
    # Add location-based reasons
    if any(m.in_headline for m in result.matches):
        reasons.append("headline_match")
    
    # Add frequency-based reasons
    if len(result.matches) >= 2:
        reasons.append("multiple_mentions")
    
    # Add multi-method reason
    if len(methods_used) >= 2:
        reasons.append("multi_method_confirmation")
    
    return reasons


def extract_from_article(article: Dict, min_confidence: float = 0.5) -> Dict:
    """
    Extract symbols from article dict.
    
    Args:
        article: Dict with title, description, source, published_at
        min_confidence: Minimum confidence threshold
    
    Returns:
        Dict with "symbols" list
    """
    return extract_symbols(
        title=article.get("title", ""),
        description=article.get("description", article.get("snippet", "")),
        source=article.get("source", ""),
        published_at=article.get("published_at", ""),
        min_confidence=min_confidence
    )


def is_trading_eligible(confidence: float) -> bool:
    """
    Check if a symbol is eligible for trading based on confidence.
    
    Symbols with confidence ≥ 0.7 are eligible for trading.
    """
    return confidence >= 0.7


def should_ignore(confidence: float) -> bool:
    """
    Check if a symbol should be ignored based on confidence.
    
    Symbols with confidence < 0.5 should be ignored.
    """
    return confidence < 0.5


# Quick test
if __name__ == "__main__":
    # Test with sample input
    test_input = {
        "title": "Apple stock rises after strong iPhone demand",
        "description": "Shares of Apple surged after earnings beat expectations. Tesla also saw gains.",
        "source": "Reuters",
        "published_at": "2025-01-12T09:30:00Z"
    }
    
    result = extract_from_article(test_input)
    
    print("\n=== Input ===")
    import json
    print(json.dumps(test_input, indent=2))
    
    print("\n=== Output ===")
    print(json.dumps(result, indent=2))
    
    print("\n=== Trading Eligibility ===")
    for symbol_data in result["symbols"]:
        symbol = symbol_data["symbol"]
        conf = symbol_data["confidence"]
        eligible = is_trading_eligible(conf)
        print(f"{symbol}: confidence={conf:.2f}, trading_eligible={eligible}")
