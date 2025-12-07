# Sentient Trader

AI-driven paper-trading platform that combines OSINT signals (Twitter / Reddit / YouTube trends) with market ticks to run reproducible backtests and low-latency real-time predictions.

## Architecture

```mermaid
flowchart LR
  T(Twitter Adapter) --> K[Kafka]
  R(Reddit Adapter) --> K
  Y(YouTube Trends) --> K
  M(Market Replay) --> KT[Kafka - Market]

  K --> E(Preprocess & Embedding)
  E --> FS[Feature Store - Redis + Parquet]
  FS --> MS(Model Serving - ONNX)
  MS --> SIM(Simulator Engine)

  KT --> BT(Backtester) --> SIM

  SIM --> DB(Postgres Ledger)
  PROM(Prometheus) --> GRAF(Grafana)
