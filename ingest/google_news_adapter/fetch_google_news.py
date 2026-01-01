"""
GNews fetcher (gnews.io)
- normalizes to canonical schema
- deduplicates using Redis
- pushes normalized events to Kafka topic 'news-stream'
- uses exponential backoff & basic metrics counters (prometheus)
"""

import os
import time
import hashlib
import json
import logging
from typing import List, Dict, Optional

import requests
import redis
from kafka import KafkaProducer
from prometheus_client import Counter


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_gnews")

# -----------------------------------------------------------
# CONFIG (DOCKER-FRIENDLY)
# -----------------------------------------------------------
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY", "")
GNEWS_BASE_URL = "https://gnews.io/api/v4"

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DEDUP_TTL = int(os.getenv("NEWS_DEDUP_TTL_SEC", "86400"))  # 24h

# -----------------------------------------------------------
# CLIENTS
# -----------------------------------------------------------
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    acks="all",
    retries=5,
)

# -----------------------------------------------------------
# PROMETHEUS METRICS
# -----------------------------------------------------------
NEWS_FETCH_SUCCESS = Counter("news_fetch_success_total", "Successful GNews fetches")
NEWS_FETCH_ERRORS = Counter("news_fetch_errors_total", "GNews fetch errors")
NEWS_DEDUPE_COUNT = Counter("news_dedup_count_total", "Deduplicated news events")

# -----------------------------------------------------------
# HELPERS
# -----------------------------------------------------------

# Common words that look like tickers but aren't
TICKER_BLACKLIST = {
    # Common words
    "A", "I", "AI", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS", "IT", "ME",
    "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE", "CEO", "CFO", "COO", "CTO", "CIO",
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HAD", "HER", "WAS", "ONE", "OUR",
    "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW", "ITS", "MAY", "NEW", "NOW", "OLD", "SEE", "WAY",
    "WHO", "BOY", "DID", "OWN", "SAY", "SHE", "TOO", "USE", "UK", "EU", "UN", "GDP", "IPO", "ETF",
    # Tech/business terms
    "API", "CEO", "CFO", "CIO", "COO", "CTO", "EPS", "ESG", "FAQ", "FYI", "GDP", "GPU", "CPU", "RAM",
    "ROM", "SSD", "HDD", "USB", "URL", "VPN", "WWW", "APP", "IOS", "MAC", "PC", "TV", "DVD", "CD",
    "PDF", "JPG", "PNG", "GIF", "MP3", "MP4", "HTML", "CSS", "SQL", "XML", "JSON", "HTTP", "FTP",
    # Country/region codes
    "USA", "UK", "EU", "UN", "UAE", "EUR", "USD", "GBP", "JPY", "CNY", "INR", "AUD", "CAD",
    # Model numbers and specs
    "M1", "M2", "M3", "M4", "M5", "5G", "4G", "3G", "4K", "8K", "HD", "FHD", "UHD", "LCD", "LED",
    "OLED", "HDR", "RGB", "AC", "DC", "GB", "TB", "MB", "KB", "MHZ", "GHZ", "RPM", "PSI",
    # Common abbreviations
    "INC", "LLC", "LTD", "PLC", "CO", "VS", "ETC", "DIY", "FAQ", "ASAP", "FYI", "TBD", "TBA",
    "Q1", "Q2", "Q3", "Q4", "YOY", "QOQ", "MOM", "YTD", "MTD", "WTD", "ROI", "KPI", "SLA",
    # Other non-tickers
    "EV", "AI", "AR", "VR", "ML", "DL", "NLP", "IOT", "BTC", "ETH", "NFT", "DAO", "DEFI",
    "HBO", "CNN", "BBC", "NBC", "CBS", "ABC", "FOX", "ESPN", "MTV", "AMC",
}

# Well-known stock tickers (expand this list as needed)
VALID_TICKERS = {
    # Major US stocks
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "BRK", "JPM", "JNJ", "V", "PG",
    "UNH", "HD", "MA", "DIS", "PYPL", "BAC", "ADBE", "CMCSA", "NFLX", "XOM", "VZ", "INTC", "T", "PFE",
    "CSCO", "ABT", "CVX", "MRK", "PEP", "KO", "TMO", "ABBV", "AVGO", "COST", "WMT", "MCD", "DHR",
    "ACN", "NKE", "TXN", "NEE", "LLY", "BMY", "UNP", "PM", "ORCL", "AMD", "IBM", "QCOM", "HON", "LOW",
    "SBUX", "GS", "BLK", "INTU", "ISRG", "GILD", "MDLZ", "ADP", "BKNG", "REGN", "VRTX", "ADI", "TJX",
    "MMC", "SCHW", "CB", "SYK", "LRCX", "ZTS", "MO", "SO", "DUK", "CL", "BDX", "CME", "CI", "USB",
    "FISV", "ITW", "AON", "NSC", "COP", "EOG", "PLD", "ATVI", "MMM", "FIS", "AXP", "SPGI", "TGT",
    "SHW", "ANTM", "DE", "GE", "CAT", "AMAT", "GM", "F", "BA", "RTX", "LMT", "NOC", "GD",
    # Popular/meme stocks
    "GME", "AMC", "BB", "NOK", "PLTR", "RIVN", "LCID", "NIO", "XPEV", "LI", "SOFI", "HOOD", "COIN",
    "RBLX", "SNAP", "PINS", "TWTR", "SQ", "SHOP", "ROKU", "ZM", "DOCU", "CRWD", "DDOG", "NET", "SNOW",
    "U", "ABNB", "DASH", "LYFT", "UBER", "PATH", "AFRM", "UPST", "MSTR", "ARKK",
    # Indian stocks (NSE/BSE)
    "IRFC", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "BAJFINANCE", "WIPRO", "HCLTECH",
    "ONGC", "NTPC", "POWERGRID", "COALINDIA", "IOC", "BPCL", "GAIL", "TATAMOTORS", "TATASTEEL", "JSWSTEEL",
    # ETFs and indices
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM", "GLD", "SLV", "USO", "TLT",
    "HYG", "LQD", "ARKK", "ARKG", "ARKF", "ARKW", "XLF", "XLK", "XLE", "XLV", "XLI", "XLY", "XLP",
}

def _extract_valid_tickers(text: str) -> List[str]:
    """
    Extract only valid stock tickers from text.
    Filters out common words and validates against known ticker list.
    """
    tickers = []
    words = text.split()
    
    for word in words:
        # Clean the word
        w = word.strip(".,;:!?()[]{}\"'$%#@&*+=/<>|\\~`")
        w_upper = w.upper()
        
        # Skip if too short, too long, or not uppercase
        if len(w) < 2 or len(w) > 6:
            continue
        if not w.isupper() and w != w_upper:
            continue
            
        # Skip blacklisted words
        if w_upper in TICKER_BLACKLIST:
            continue
            
        # Accept if it's a known valid ticker
        if w_upper in VALID_TICKERS:
            tickers.append(w_upper)
        # Also accept if it looks like a ticker (2-5 uppercase letters) and contains $ prefix
        elif word.startswith("$") and 2 <= len(w) <= 5 and w.isalpha():
            tickers.append(w_upper)
    
    return list(set(tickers))


def _canonicalize_article(raw: Dict) -> Dict:
    """
    Normalize GNews article → canonical schema
    """
    title = raw.get("title", "")
    description = raw.get("description", "")
    content = raw.get("content", "")
    published = raw.get("publishedAt")
    url = raw.get("url")

    source = raw.get("source", {})
    source_name = source.get("name") if isinstance(source, dict) else source

    # Extract valid tickers from title and description
    tickers = _extract_valid_tickers(title + " " + description)

    return {
        "source": source_name,
        "title": title,
        "summary": description,
        "content": content,
        "url": url,
        "published_at": published,
        "tickers": tickers,
        "raw": raw,
    }


def _dedupe_key(article: Dict) -> str:
    """
    Stable deduplication hash
    """
    base = (
        article.get("title", "")
        + (article.get("url") or "")
        + str(article.get("published_at", ""))
    ).encode("utf-8")

    return hashlib.sha256(base).hexdigest()


# -----------------------------------------------------------
# FETCHER
# -----------------------------------------------------------
def fetch_gnews(
    query: str,
    page: int = 1,
    page_size: int = 10,
    lang: str = "en",
) -> Optional[List[Dict]]:
    """
    Fetch news from GNews Search API
    """
    url = f"{GNEWS_BASE_URL}/search"

    params = {
        "q": query,
        "lang": lang,
        "max": page_size,
        "page": page,
        "apikey": GNEWS_API_KEY,
    }

    backoff = 1.0

    for attempt in range(4):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                NEWS_FETCH_SUCCESS.inc()
                return resp.json().get("articles", [])
            else:
                NEWS_FETCH_ERRORS.inc()
                logger.warning(
                    "GNews error %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as e:
            NEWS_FETCH_ERRORS.inc()
            logger.exception("GNews fetch exception: %s", e)

        time.sleep(backoff)
        backoff *= 2

    return None


# -----------------------------------------------------------
# PRODUCER
# -----------------------------------------------------------
def produce_article(article: Dict) -> bool:
    """
    Deduplicate and publish article to Kafka
    """
    key = _dedupe_key(article)

    if redis_client.get(key):
        NEWS_DEDUPE_COUNT.inc()
        return False

    redis_client.set(key, "1", ex=DEDUP_TTL)

    producer.send("news-stream", article)
    producer.flush()

    return True


# -----------------------------------------------------------
# MAIN PIPELINE
# -----------------------------------------------------------
MAX_FREE_PAGES = 2  # GNews free tier safe limit

def fetch_and_publish(query: str, pages: int = 1, lang: str = "en"):
    pages = min(pages, MAX_FREE_PAGES)

    for p in range(1, pages + 1):
        articles = fetch_gnews(query=query, page=p, lang=lang)
        if not articles:
            continue

        for raw in articles:
            canonical = _canonicalize_article(raw)
            if produce_article(canonical):
                logger.info("Published: %s", canonical["title"][:120])


# -----------------------------------------------------------
# CLI ENTRYPOINT
# -----------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--query",
        default="finance OR stock OR earnings OR market",
    )
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--lang", default="en")

    args = parser.parse_args()

    if not GNEWS_API_KEY:
        raise RuntimeError("ERROR: Set GNEWS_API_KEY before running.")

    fetch_and_publish(
        query=args.query,
        pages=args.pages,
        lang=args.lang,
    )
