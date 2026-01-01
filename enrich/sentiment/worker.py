# enrich/sentiment/worker.py
"""
Kafka worker:
- consumes news-with-tickers (enriched by ticker-extraction-worker)
- calls analyze_text() with title + snippet for improved accuracy
- uses FinBERT for sentiment analysis (no external API rate limits)
- publishes enriched records to news-sentiment

Pipeline flow:
news-stream → ticker-extraction-worker → news-with-tickers → sentiment-worker → news-sentiment

FinBERT provides:
- Better financial domain accuracy
- No API rate limits (runs locally)
- Faster inference
- Title + snippet combination for ~78-85% accuracy

Each article from news-with-tickers contains:
- ticker: The extracted ticker symbol
- ticker_confidence: Confidence score (0.0-1.0)
- ticker_extraction: Full extraction details
"""

import os
import json
import logging
from time import sleep
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError

from enrich.sentiment.sentiment_analysis import analyze_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sentiment_worker")

# ------- config -------
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")
# Now consumes from news-with-tickers (enriched by ticker extraction worker)
INPUT_TOPIC = os.getenv("INPUT_TOPIC", "news-with-tickers")
OUTPUT_TOPIC = os.getenv("OUTPUT_TOPIC", "news-sentiment")
GROUP_ID = os.getenv("GROUP_ID", "sentiment-worker-group")


def main():
    # Create consumer & producer
    while True:
        try:
            consumer = KafkaConsumer(
                INPUT_TOPIC,
                bootstrap_servers=[KAFKA_BOOTSTRAP],
                group_id=GROUP_ID,
                auto_offset_reset="earliest",
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                max_poll_records=10,  # FinBERT is fast, can process more messages
                enable_auto_commit=True,
            )

            producer = KafkaProducer(
                bootstrap_servers=[KAFKA_BOOTSTRAP],
                value_serializer=lambda v: json.dumps(v).encode("utf-8")
            )
            break
        except KafkaError as e:
            logger.warning("Kafka not ready: %s", e)
            sleep(2)

    logger.info("Sentiment worker started with FinBERT model")

    for msg in consumer:
        try:
            article = msg.value
            title = article.get("title", "")
            # Get snippet from summary, description, or content (in order of preference)
            snippet = article.get("summary") or article.get("description") or article.get("content") or article.get("snippet", "")

            # If both title and snippet are empty, skip
            if not title and not snippet:
                logger.info("Skipping empty article")
                continue

            # Get ticker extraction info (from ticker-extraction-worker)
            ticker = article.get("ticker")
            ticker_confidence = article.get("ticker_confidence", 1.0)
            ticker_extraction = article.get("ticker_extraction", {})
            article_id = article.get("article_id", article.get("url"))

            # Analyze with title + snippet combined for better accuracy
            # This follows the pattern: "Title. Snippet" for ~78-85% accuracy
            sentiment = analyze_text(
                text="",  # Will be ignored if title/snippet provided
                title=title,
                snippet=snippet,
                article_id=article_id
            )

            # Use the ticker from extraction (high confidence) or fall back to old method
            if ticker:
                combined_tickers = [ticker]
            else:
                # Fallback: merge tickers from original article AND FinBERT response
                article_tickers = article.get("tickers", []) or []
                finbert_tickers = sentiment.get("tickers", []) if isinstance(sentiment, dict) else []
                combined_tickers = list(set(article_tickers + finbert_tickers))
            
            enriched = {
                "title": title,
                "url": article.get("url"),
                "source": article.get("source"),
                "published_at": article.get("published_at"),
                "ticker": ticker,  # Primary ticker (from extraction)
                "ticker_confidence": ticker_confidence,
                "ticker_extraction": ticker_extraction,
                "tickers": combined_tickers,  # For backward compatibility
                "sentiment": sentiment,
                "article_id": article_id,
            }
            
            # Log with sentiment details
            meta = sentiment.get("_meta", {})
            logger.info(
                "Analyzed: %s | ticker=%s (conf=%.2f) sentiment=%s score=%.2f conf=%.2f input=%s",
                (title or "")[:50],
                ticker or "N/A",
                ticker_confidence,
                sentiment.get("sentiment", "unknown"),
                sentiment.get("sentiment_score", 0),
                sentiment.get("confidence", 0),
                meta.get("input_type", "unknown")
            )

            producer.send(OUTPUT_TOPIC, enriched)
            producer.flush()

        except Exception as exc:
            logger.exception("Processing failed: %s", exc)


if __name__ == "__main__":
    main()
