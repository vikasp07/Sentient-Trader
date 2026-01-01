#!/usr/bin/env python
"""
Test script to send news messages through the pipeline.
This triggers the full flow: news -> sentiment -> strategy -> trade decision
"""

import json
import os
import time
from kafka import KafkaProducer

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")

# Test news articles with sentiment
TEST_NEWS = [
    {
        "title": "Apple announces record-breaking iPhone 16 sales worldwide",
        "tickers": ["AAPL"],
        "sentiment": {"sentiment_score": 0.85},
        "url": "https://example.com/aapl-record-sales"
    },
    {
        "title": "NVIDIA reports massive AI chip demand, stock surges",
        "tickers": ["NVDA"],
        "sentiment": {"sentiment_score": 0.92},
        "url": "https://example.com/nvda-ai-demand"
    },
    {
        "title": "Tesla faces production challenges, delays new model",
        "tickers": ["TSLA"],
        "sentiment": {"sentiment_score": -0.65},
        "url": "https://example.com/tsla-delays"
    },
    {
        "title": "Microsoft Azure cloud revenue exceeds expectations",
        "tickers": ["MSFT"],
        "sentiment": {"sentiment_score": 0.78},
        "url": "https://example.com/msft-azure"
    },
    {
        "title": "Amazon faces antitrust investigation, stock drops",
        "tickers": ["AMZN"],
        "sentiment": {"sentiment_score": -0.55},
        "url": "https://example.com/amzn-antitrust"
    },
    {
        "title": "Meta's new VR headset receives positive reviews",
        "tickers": ["META"],
        "sentiment": {"sentiment_score": 0.70},
        "url": "https://example.com/meta-vr"
    },
    {
        "title": "Google search market share continues to grow",
        "tickers": ["GOOGL"],
        "sentiment": {"sentiment_score": 0.60},
        "url": "https://example.com/googl-search"
    },
    {
        "title": "JPMorgan reports strong quarterly earnings",
        "tickers": ["JPM"],
        "sentiment": {"sentiment_score": 0.75},
        "url": "https://example.com/jpm-earnings"
    },
    {
        "title": "Intel struggles with chip manufacturing issues",
        "tickers": ["INTC"],
        "sentiment": {"sentiment_score": -0.70},
        "url": "https://example.com/intc-issues"
    },
    {
        "title": "AMD gains market share in data center processors",
        "tickers": ["AMD"],
        "sentiment": {"sentiment_score": 0.82},
        "url": "https://example.com/amd-datacenter"
    },
]

def main():
    print(f"Connecting to Kafka at {KAFKA_BOOTSTRAP}...")
    
    producer = KafkaProducer(
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_serializer=lambda v: json.dumps(v).encode("utf-8")
    )
    
    print(f"Connected! Sending {len(TEST_NEWS)} test news articles...")
    
    for i, news in enumerate(TEST_NEWS, 1):
        try:
            producer.send("news-sentiment", news)
            print(f"[{i}/{len(TEST_NEWS)}] Sent: {news['title'][:50]}... (sentiment: {news['sentiment']['sentiment_score']})")
            time.sleep(0.5)  # Small delay between messages
        except Exception as e:
            print(f"Failed to send message: {e}")
    
    producer.flush()
    producer.close()
    
    print("\nAll test news sent! Check strategy-worker logs for trade decisions.")
    print("Trade decisions will be saved to results/trade_decisions.jsonl")

if __name__ == "__main__":
    main()
