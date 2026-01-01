"""
Ticker Extraction Kafka Worker

Consumes from: news-stream
Produces to: news-with-tickers

Pipeline flow:
news-stream → ticker-extraction-worker → news-with-tickers → sentiment-worker → news-sentiment

For each article:
1. Extract multiple tickers with confidence scores
2. Filter tickers below confidence threshold
3. Publish enriched articles (one per ticker) to news-with-tickers
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError
import redis

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from enrich.ticker_extraction.extractor import TickerExtractor, ExtractionResult
from enrich.ticker_extraction.confidence import ConfidenceScorer, AdaptiveConfidenceScorer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TickerExtractionWorker:
    """
    Kafka worker that extracts tickers from news articles.
    
    Consumes raw news articles and produces enriched articles
    with extracted tickers and confidence scores.
    """
    
    def __init__(
        self,
        kafka_bootstrap: str = "localhost:9092",
        redis_host: str = "localhost",
        redis_port: int = 6379,
        input_topic: str = "news-stream",
        output_topic: str = "news-with-tickers",
        consumer_group: str = "ticker-extraction-group",
        min_confidence: float = 0.5,
        company_map_path: Optional[str] = None
    ):
        """
        Initialize the ticker extraction worker.
        
        Args:
            kafka_bootstrap: Kafka broker address
            redis_host: Redis server host
            redis_port: Redis server port
            input_topic: Topic to consume articles from
            output_topic: Topic to produce enriched articles to
            consumer_group: Kafka consumer group ID
            min_confidence: Minimum confidence threshold for tickers
            company_map_path: Path to company-to-ticker mapping JSON
        """
        self.kafka_bootstrap = kafka_bootstrap
        self.input_topic = input_topic
        self.output_topic = output_topic
        self.consumer_group = consumer_group
        self.min_confidence = min_confidence
        
        # Initialize Redis
        try:
            self.redis = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True
            )
            self.redis.ping()
            logger.info(f"Connected to Redis at {redis_host}:{redis_port}")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Caching disabled.")
            self.redis = None
        
        # Initialize ticker extractor
        self.extractor = TickerExtractor(company_map_path=company_map_path)
        logger.info("Ticker extractor initialized")
        
        # Initialize confidence scorer with learning
        self.scorer = AdaptiveConfidenceScorer(redis_client=self.redis)
        logger.info("Confidence scorer initialized")
        
        # Initialize Kafka consumer
        self.consumer = None
        self.producer = None
        
        # Statistics
        self.stats = {
            'articles_processed': 0,
            'tickers_extracted': 0,
            'tickers_filtered': 0,
            'errors': 0,
            'start_time': None
        }
    
    def _init_kafka(self):
        """Initialize Kafka consumer and producer."""
        # Consumer
        self.consumer = KafkaConsumer(
            self.input_topic,
            bootstrap_servers=self.kafka_bootstrap,
            group_id=self.consumer_group,
            auto_offset_reset='earliest',
            enable_auto_commit=True,
            auto_commit_interval_ms=5000,
            value_deserializer=lambda m: json.loads(m.decode('utf-8')),
            max_poll_records=20,
            session_timeout_ms=30000,
            heartbeat_interval_ms=10000
        )
        logger.info(f"Kafka consumer initialized for topic: {self.input_topic}")
        
        # Producer
        self.producer = KafkaProducer(
            bootstrap_servers=self.kafka_bootstrap,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            acks='all',
            retries=3
        )
        logger.info(f"Kafka producer initialized for topic: {self.output_topic}")
    
    def _cache_key(self, article_id: str) -> str:
        """Generate Redis cache key for processed article."""
        return f"ticker_extraction:{article_id}"
    
    def _is_processed(self, article_id: str) -> bool:
        """Check if article was already processed."""
        if not self.redis:
            return False
        
        try:
            return self.redis.exists(self._cache_key(article_id)) > 0
        except Exception:
            return False
    
    def _mark_processed(self, article_id: str, tickers: List[str]):
        """Mark article as processed in cache."""
        if not self.redis:
            return
        
        try:
            cache_data = {
                'tickers': tickers,
                'processed_at': datetime.utcnow().isoformat()
            }
            # Cache for 24 hours
            self.redis.setex(
                self._cache_key(article_id),
                86400,
                json.dumps(cache_data)
            )
        except Exception as e:
            logger.warning(f"Failed to cache processed article: {e}")
    
    def process_article(self, article: Dict) -> List[Dict]:
        """
        Process a single article and extract tickers.
        
        Args:
            article: Raw article dict with title, snippet, etc.
        
        Returns:
            List of enriched article dicts (one per extracted ticker)
        """
        # Extract fields
        title = article.get('title', '')
        snippet = article.get('snippet', article.get('description', ''))
        article_id = article.get('article_id', article.get('id', f"art_{hash(title)}")[:12])
        
        # Check cache
        if self._is_processed(article_id):
            logger.debug(f"Article {article_id} already processed, skipping")
            return []
        
        # Extract tickers
        results = self.extractor.extract(
            headline=title,
            body=snippet,
            min_confidence=self.min_confidence
        )
        
        if not results:
            logger.debug(f"No tickers found in article: {title[:50]}...")
            self._mark_processed(article_id, [])
            return []
        
        # Build enriched articles (one per ticker)
        enriched_articles = []
        extracted_tickers = []
        
        for result in results:
            enriched = article.copy()
            enriched['ticker'] = result.ticker
            enriched['ticker_confidence'] = round(result.confidence, 3)
            enriched['ticker_extraction'] = result.to_dict()
            enriched['article_id'] = article_id
            enriched['extraction_timestamp'] = datetime.utcnow().isoformat()
            
            enriched_articles.append(enriched)
            extracted_tickers.append(result.ticker)
            
            logger.info(
                f"Extracted {result.ticker} (conf={result.confidence:.3f}) from: {title[:40]}..."
            )
        
        # Mark as processed
        self._mark_processed(article_id, extracted_tickers)
        
        # Update stats
        self.stats['tickers_extracted'] += len(results)
        
        return enriched_articles
    
    def publish_enriched(self, enriched_article: Dict):
        """Publish enriched article to output topic."""
        try:
            future = self.producer.send(self.output_topic, value=enriched_article)
            future.get(timeout=10)  # Wait for confirmation
        except KafkaError as e:
            logger.error(f"Failed to publish enriched article: {e}")
            self.stats['errors'] += 1
    
    def run(self):
        """Main processing loop."""
        logger.info("Starting Ticker Extraction Worker...")
        self._init_kafka()
        
        self.stats['start_time'] = datetime.utcnow().isoformat()
        
        try:
            for message in self.consumer:
                try:
                    article = message.value
                    
                    # Process article
                    enriched_articles = self.process_article(article)
                    
                    # Publish each enriched article
                    for enriched in enriched_articles:
                        self.publish_enriched(enriched)
                    
                    self.stats['articles_processed'] += 1
                    
                    # Log progress periodically
                    if self.stats['articles_processed'] % 10 == 0:
                        logger.info(
                            f"Progress: {self.stats['articles_processed']} articles, "
                            f"{self.stats['tickers_extracted']} tickers extracted"
                        )
                
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    self.stats['errors'] += 1
        
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self._cleanup()
    
    def _cleanup(self):
        """Cleanup resources."""
        if self.consumer:
            self.consumer.close()
        if self.producer:
            self.producer.flush()
            self.producer.close()
        
        # Log final stats
        logger.info("=" * 50)
        logger.info("Final Statistics:")
        logger.info(f"  Articles processed: {self.stats['articles_processed']}")
        logger.info(f"  Tickers extracted: {self.stats['tickers_extracted']}")
        logger.info(f"  Errors: {self.stats['errors']}")
        logger.info("=" * 50)


def main():
    """Entry point for the ticker extraction worker."""
    # Configuration from environment
    kafka_bootstrap = os.getenv('KAFKA_BOOTSTRAP', 'redpanda:9092')
    redis_host = os.getenv('REDIS_HOST', 'redis')
    redis_port = int(os.getenv('REDIS_PORT', '6379'))
    input_topic = os.getenv('INPUT_TOPIC', 'news-stream')
    output_topic = os.getenv('OUTPUT_TOPIC', 'news-with-tickers')
    consumer_group = os.getenv('CONSUMER_GROUP', 'ticker-extraction-group')
    min_confidence = float(os.getenv('MIN_CONFIDENCE', '0.5'))
    company_map_path = os.getenv('COMPANY_MAP_PATH', '/app/data/company_ticker_map.json')
    
    logger.info("Configuration:")
    logger.info(f"  Kafka: {kafka_bootstrap}")
    logger.info(f"  Redis: {redis_host}:{redis_port}")
    logger.info(f"  Input Topic: {input_topic}")
    logger.info(f"  Output Topic: {output_topic}")
    logger.info(f"  Min Confidence: {min_confidence}")
    
    # Wait for Kafka to be ready
    logger.info("Waiting for Kafka to be ready...")
    time.sleep(10)
    
    # Create and run worker
    worker = TickerExtractionWorker(
        kafka_bootstrap=kafka_bootstrap,
        redis_host=redis_host,
        redis_port=redis_port,
        input_topic=input_topic,
        output_topic=output_topic,
        consumer_group=consumer_group,
        min_confidence=min_confidence,
        company_map_path=company_map_path
    )
    
    worker.run()


if __name__ == "__main__":
    main()
