# data/fetch_real_stock.py
import os
import argparse
import logging
import time
import json
from datetime import datetime
import pandas as pd
import yfinance as yf
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetch_real_stock")

# Correct bootstrap — works inside Docker
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "redpanda:9092")

def create_producer():
    """
    Create Kafka producer with retries and backoff.
    Retries until Redpanda is healthy.
    """
    for i in range(12):   # try for ~30 seconds
        try:
            producer = KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                acks="all",
                retries=5,
                linger_ms=50,
                batch_size=32768,
            )
            logger.info("Connected to Kafka at %s", KAFKA_BOOTSTRAP)
            return producer
        except NoBrokersAvailable:
            wait = 2 + i
            logger.warning("Kafka not ready — retrying in %s seconds...", wait)
            time.sleep(wait)

    raise RuntimeError("❌ Could not connect to Kafka after multiple attempts.")

producer = create_producer()


def download_and_save(ticker: str, period="7d", interval="1m", outpath="data/real_ticks.csv"):
    """
    Downloads 7 days of 1-min ticker data using yfinance & saves to CSV.
    Always standardizes schema => (symbol, ts, price)
    """
    logger.info("Downloading %s market data...", ticker)
    
    df = yf.download(ticker, period=period, interval=interval, progress=False)

    if df.empty:
        raise RuntimeError(f"❌ No data returned for ticker {ticker}. Check symbol.")

    df = df.reset_index()

    # Normalize timestamp column
    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "ts"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "ts"})
    elif "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "ts"})
    else:
        raise RuntimeError("❌ Could not find timestamp column in downloaded data")

    # Normalize price column
    if "Close" in df.columns:
        df = df.rename(columns={"Close": "price"})
    elif "close" in df.columns:
        df = df.rename(columns={"close": "price"})
    else:
        raise RuntimeError("❌ No close/Close price column in ticker data")

    df["symbol"] = ticker
    out_df = df[["symbol", "ts", "price"]]

    os.makedirs(os.path.dirname(outpath), exist_ok=True)
    out_df.to_csv(outpath, index=False)

    logger.info("✅ Saved ticks => %s (%d rows)", outpath, len(out_df))
    return outpath


def replay_to_kafka(csv_path: str, speedup: float = 60.0):
    """
    Replays CSV tick data into Kafka topic `market-ticks`.
    Speedup allows fast replay (e.g., 60 => 1 minute data plays in 1 second).
    """
    logger.info("Starting market replay → Kafka (topic: market-ticks)")

    df = pd.read_csv(csv_path, parse_dates=["ts"])
    df = df.sort_values("ts").reset_index(drop=True)

    first_ts = df["ts"].iloc[0]

    for _, row in df.iterrows():
        event = {
            "symbol": row["symbol"],
            "ts": row["ts"].isoformat(),
            "price": float(row["price"]),
        }

        producer.send("market-ticks", event)

        # compute replay delay
        delta = (row["ts"] - first_ts).total_seconds() / speedup
        time.sleep(max(0.0, delta))

    producer.flush()
    logger.info("🎉 Replay finished — all ticks published!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--out", default="data/real_ticks.csv")
    parser.add_argument("--period", default="7d")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--speedup", type=float, default=60.0)

    args = parser.parse_args()

    path = download_and_save(
        args.ticker,
        period=args.period,
        interval=args.interval,
        outpath=args.out
    )

    if args.replay:
        replay_to_kafka(path, speedup=args.speedup)
