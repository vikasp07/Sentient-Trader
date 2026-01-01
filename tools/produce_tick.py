# tools/produce_tick.py
import json
from kafka import KafkaProducer
import time

p = KafkaProducer(bootstrap_servers=["localhost:9092"], value_serializer=lambda v: json.dumps(v).encode("utf-8"))

def send(symbol, ts, price, volume=0):
    msg = {"symbol": symbol, "ts": ts, "price": price, "volume": volume}
    p.send("market-ticks", msg)
    p.flush()
    print("sent", msg)

if __name__ == "__main__":
    send("AAPL", "2025-11-26T14:30:00Z", 277.95, 100)
    time.sleep(0.5)
    send("AAPL", "2025-11-26T14:31:00Z", 278.91, 150)
    time.sleep(0.5)
    send("AAPL", "2025-11-26T14:32:00Z", 278.54, 120)
