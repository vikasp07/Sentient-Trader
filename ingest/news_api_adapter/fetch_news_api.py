# ingest/news_api_adapter/fetch_news_api.py
from ingest.google_news_adapter.fetch_google_news import fetch_and_publish

# Thin wrapper to use the same canonical pipeline (keeps older filename working)
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="technology OR finance OR market")
    parser.add_argument("--pages", type=int, default=1)
    args = parser.parse_args()
    fetch_and_publish(args.query, pages=args.pages)
