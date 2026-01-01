# tools/send_test_articles.py
"""
Send test news articles to demonstrate FinBERT sentiment analysis pipeline.
"""

import json
from kafka import KafkaProducer
import os

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

# Test articles with title + description for FinBERT to analyze
articles = [
    {
        "title": "Apple Stock Surges 5% After Record iPhone Sales",
        "summary": "Apple shares jumped 5% in early trading after the company reported record-breaking iPhone sales for the holiday quarter, exceeding analyst expectations by a wide margin.",
        "url": "https://test-news.com/apple-surge",
        "source": "Test Financial News",
        "tickers": ["AAPL"],
        "published_at": "2025-12-26T10:00:00Z"
    },
    {
        "title": "Tesla Stock Drops 3% on Delivery Miss",
        "summary": "Tesla shares fell sharply after the electric vehicle maker reported quarterly deliveries below Wall Street estimates, citing ongoing supply chain disruptions.",
        "url": "https://test-news.com/tesla-drop",
        "source": "Test Financial News",
        "tickers": ["TSLA"],
        "published_at": "2025-12-26T10:05:00Z"
    },
    {
        "title": "NVIDIA Reports Strong AI Chip Demand, Stock Jumps",
        "summary": "NVIDIA stock rose 4% as the chipmaker announced unprecedented demand for its AI processors from major tech companies and cloud data centers worldwide.",
        "url": "https://test-news.com/nvidia-ai",
        "source": "Test Financial News",
        "tickers": ["NVDA"],
        "published_at": "2025-12-26T10:10:00Z"
    },
    {
        "title": "Microsoft Cloud Revenue Beats Estimates",
        "summary": "Microsoft reported better-than-expected cloud revenue growth, with Azure gaining market share against competitors. The stock gained 2% in after-hours trading.",
        "url": "https://test-news.com/microsoft-cloud",
        "source": "Test Financial News",
        "tickers": ["MSFT"],
        "published_at": "2025-12-26T10:15:00Z"
    },
    {
        "title": "Amazon Faces Regulatory Challenges in EU",
        "summary": "Amazon stock declined 1.5% amid concerns about new European Union regulations that could significantly impact the company's marketplace business model.",
        "url": "https://test-news.com/amazon-eu",
        "source": "Test Financial News",
        "tickers": ["AMZN"],
        "published_at": "2025-12-26T10:20:00Z"
    }
]

print("\n📰 Sending test articles to news-stream topic...\n")

for article in articles:
    producer.send("news-stream", article)
    print(f"✅ Sent: {article['title']}")
    print(f"   Tickers: {article['tickers']}")
    print(f"   Summary: {article['summary'][:80]}...")
    print()

producer.flush()
print("🎉 All articles sent! Check sentiment-worker logs for FinBERT analysis.")
