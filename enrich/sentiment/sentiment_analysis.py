# enrich/sentiment/sentiment_analysis.py
"""
Sentiment service (FinBERT-backed) helpers.

Responsibilities:
- Use FinBERT model for financial sentiment analysis
- Combine title + snippet/description for improved accuracy
- Cache results in Redis
- Return stable JSON schema with metadata

Accuracy expectations:
- Title only: ~65–70%
- Title + snippet: ~78–85%
- Full article: ~85–90%
"""

import os
import json
import hashlib
import logging
import re
from typing import Optional, Dict, Any, List

import redis
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sentiment_service")

# ---------------- config ----------------
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "512"))  # FinBERT max tokens ~512
CACHE_TTL = int(os.getenv("CACHE_TTL", "86400"))  # seconds
MODEL_NAME = os.getenv("FINBERT_MODEL", "ProsusAI/finbert")

# ---------------- model loading ----------------
logger.info("Loading FinBERT model: %s", MODEL_NAME)
try:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
    model.eval()  # Set to evaluation mode
    
    # Move to GPU if available
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    logger.info("FinBERT loaded successfully on device: %s", device)
except Exception as e:
    logger.error("Failed to load FinBERT model: %s", e)
    tokenizer = None
    model = None
    device = None

# Label mapping for FinBERT (ProsusAI/finbert)
LABEL_MAP = {0: "positive", 1: "negative", 2: "neutral"}

# ---------------- redis ----------------
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    logger.info("Connected to Redis")
except Exception:
    r = None
    logger.warning("Redis unavailable — caching disabled")


# ---------------- helpers ----------------
def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def combine_text(title: str, snippet: Optional[str] = None) -> tuple:
    """
    Combine title and snippet for better sentiment analysis.
    Returns (combined_text, input_type)
    
    Example:
    "Apple Stock Hits New Record High" + "Shares rose 3% after strong iPhone demand..."
    -> "Apple Stock Hits New Record High. Shares rose 3% after strong iPhone demand..."
    """
    title = (title or "").strip()
    snippet = (snippet or "").strip()
    
    if title and snippet:
        # Combine title and snippet with proper punctuation
        if title and not title.endswith(('.', '!', '?')):
            combined = f"{title}. {snippet}"
        else:
            combined = f"{title} {snippet}"
        input_type = "title+snippet"
    elif title:
        combined = title
        input_type = "title_only"
    elif snippet:
        combined = snippet
        input_type = "snippet_only"
    else:
        combined = ""
        input_type = "empty"
    
    return combined, input_type


def _extract_tickers(text: str) -> List[str]:
    """Extract potential stock tickers from text using pattern matching."""
    tickers = []
    
    # Match $TICKER format
    dollar_tickers = re.findall(r'\$([A-Z]{1,5})\b', text)
    tickers.extend(dollar_tickers)
    
    # Common valid tickers we want to extract
    KNOWN_TICKERS = {
        "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", 
        "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "PYPL",
        "JNJ", "PFE", "MRK", "ABBV", "BMY", "LLY", "UNH",
        "XOM", "CVX", "COP", "OXY", "SLB",
        "DIS", "NFLX", "CMCSA", "WBD",
        "BA", "LMT", "RTX", "GD", "NOC",
        "INTC", "AMD", "QCOM", "AVGO", "TXN",
        "GME", "AMC", "BB", "NOK", "PLTR",
    }
    
    words = text.upper().split()
    for word in words:
        clean = re.sub(r'[^A-Z]', '', word)
        if clean in KNOWN_TICKERS:
            tickers.append(clean)
    
    return list(set(tickers))


def _finbert_analyze(text: str) -> Dict[str, Any]:
    """
    Run FinBERT sentiment analysis on text.
    Returns sentiment, score, and confidence.
    """
    if not model or not tokenizer:
        raise RuntimeError("FinBERT model not loaded")
    
    # Tokenize and truncate
    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=MAX_TEXT_CHARS,
        padding=True
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}
    
    # Get predictions
    with torch.no_grad():
        outputs = model(**inputs)
        logits = outputs.logits
        probs = torch.softmax(logits, dim=-1)
    
    # Get predicted class and confidence
    predicted_class = torch.argmax(probs, dim=-1).item()
    confidence = probs[0][predicted_class].item()
    
    # Get all probabilities
    prob_positive = probs[0][0].item()
    prob_negative = probs[0][1].item()
    prob_neutral = probs[0][2].item()
    
    # Calculate sentiment score: range from -1 (negative) to +1 (positive)
    sentiment_score = (prob_positive - prob_negative)
    
    sentiment = LABEL_MAP[predicted_class]
    
    return {
        "sentiment": sentiment,
        "sentiment_score": round(sentiment_score, 4),
        "confidence": round(confidence, 4),
        "probabilities": {
            "positive": round(prob_positive, 4),
            "negative": round(prob_negative, 4),
            "neutral": round(prob_neutral, 4)
        }
    }


def _heuristic_fallback(text: str) -> Dict[str, Any]:
    """Fallback heuristic when FinBERT is unavailable."""
    t = text.lower()
    if any(w in t for w in ["rise", "jump", "surge", "beat", "record", "gain", "profit", "growth", "bullish"]):
        sentiment = "positive"
        score = 0.5
        confidence = 0.3
    elif any(w in t for w in ["fall", "drop", "loss", "miss", "decline", "crash", "bearish", "down"]):
        sentiment = "negative"
        score = -0.5
        confidence = 0.3
    else:
        sentiment = "neutral"
        score = 0.0
        confidence = 0.3

    return {
        "sentiment": sentiment,
        "sentiment_score": score,
        "confidence": confidence,
        "probabilities": {
            "positive": 0.33 if sentiment != "positive" else 0.5,
            "negative": 0.33 if sentiment != "negative" else 0.5,
            "neutral": 0.33 if sentiment != "neutral" else 0.5
        }
    }


# ---------------- main logic ----------------
def analyze_text(
    text: str,
    article_id: Optional[str] = None,
    title: Optional[str] = None,
    snippet: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyze text and return canonical JSON with FinBERT.
    
    Args:
        text: The main text to analyze (can be pre-combined)
        article_id: Optional ID for caching
        title: Optional title to combine with snippet
        snippet: Optional snippet/description to combine with title
    
    If title and snippet are provided, they will be combined for better accuracy.
    Otherwise, 'text' is used directly.
    
    Returns:
        {
            "summary": str,
            "sentiment": "positive" | "negative" | "neutral",
            "sentiment_score": float (-1 to 1),
            "tickers": list,
            "events": list,
            "confidence": float (0 to 1),
            "_meta": {
                "model": "finbert",
                "input_type": "title+snippet" | "title_only" | "snippet_only" | "text",
                "cached": bool,
                "device": str
            }
        }
    """
    # Combine title + snippet if provided
    if title or snippet:
        combined_text, input_type = combine_text(title, snippet)
    else:
        combined_text = text
        input_type = "text"
    
    if not combined_text or not combined_text.strip():
        return {
            "summary": "",
            "sentiment": "neutral",
            "sentiment_score": 0.0,
            "tickers": [],
            "events": [],
            "confidence": 0.0,
            "_meta": {"model": "finbert", "input_type": "empty", "cached": False}
        }

    cache_key = f"finbert:{article_id or _hash(combined_text)}"

    # ---------- cache ----------
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                data = json.loads(cached)
                data["_meta"] = data.get("_meta", {})
                data["_meta"]["cached"] = True
                return data
        except Exception:
            logger.exception("Redis read failed; continuing without cache")

    # ---------- FinBERT Analysis ----------
    try:
        result = _finbert_analyze(combined_text)
        
        # Extract tickers and build response
        tickers = _extract_tickers(combined_text)
        
        parsed = {
            "summary": combined_text[:200],
            "sentiment": result["sentiment"],
            "sentiment_score": result["sentiment_score"],
            "tickers": tickers,
            "events": [],
            "confidence": result["confidence"],
            "probabilities": result["probabilities"],
            "_meta": {
                "model": "finbert",
                "input_type": input_type,
                "cached": False,
                "device": str(device) if device else "cpu"
            }
        }

        # Cache the result
        if r:
            try:
                r.setex(cache_key, CACHE_TTL, json.dumps(parsed))
            except Exception:
                logger.exception("Redis set failed; continuing")

        return parsed

    except Exception as e:
        logger.exception("FinBERT analysis failed, using heuristic fallback")
        fallback_result = _heuristic_fallback(combined_text)
        tickers = _extract_tickers(combined_text)
        
        return {
            "summary": combined_text[:200],
            "sentiment": fallback_result["sentiment"],
            "sentiment_score": fallback_result["sentiment_score"],
            "tickers": tickers,
            "events": [],
            "confidence": fallback_result["confidence"],
            "probabilities": fallback_result["probabilities"],
            "_meta": {
                "model": "heuristic_fallback",
                "input_type": input_type,
                "cached": False,
                "error": str(e)
            }
        }


def analyze_sentiment(text: str) -> Dict[str, Any]:
    """
    Simplified interface for sentiment analysis.
    Alias for analyze_text for backward compatibility.
    """
    return analyze_text(text=text)


# ---------------- CLI for testing ----------------
if __name__ == "__main__":
    import sys
    
    test_cases = [
        {
            "title": "Apple Stock Hits New Record High",
            "snippet": "Shares rose 3% after strong iPhone demand and better-than-expected quarterly earnings."
        },
        {
            "title": "Tesla Shares Plummet After Disappointing Delivery Numbers",
            "snippet": "The electric vehicle maker missed analyst expectations, leading to a 7% drop in stock price."
        },
        {
            "title": "Microsoft Announces New AI Features",
            "snippet": "The tech giant revealed plans to integrate AI across its product lineup."
        }
    ]
    
    print("\n🧪 Testing FinBERT Sentiment Analysis\n")
    print("=" * 80)
    
    for i, test in enumerate(test_cases, 1):
        title = test["title"]
        snippet = test["snippet"]
        
        print(f"\n📰 Test {i}:")
        print(f"   Title: {title}")
        print(f"   Snippet: {snippet}")
        
        # Analyze with title + snippet
        result = analyze_text(text="", title=title, snippet=snippet)
        
        print(f"\n   ✅ Results:")
        print(f"   Sentiment: {result['sentiment']}")
        print(f"   Score: {result['sentiment_score']}")
        print(f"   Confidence: {result['confidence']}")
        print(f"   Input Type: {result['_meta']['input_type']}")
        print(f"   Model: {result['_meta']['model']}")
        if 'probabilities' in result:
            print(f"   Probabilities: {result['probabilities']}")
        
        print("-" * 80)
    
    print("\n✅ FinBERT sentiment analysis test complete!")
