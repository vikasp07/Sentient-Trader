import praw
import csv
from datetime import datetime

# CREATE these from Reddit app settings
client_id = "YOUR_CLIENT_ID"
client_secret = "YOUR_CLIENT_SECRET"
user_agent = "sentient_trader"

reddit = praw.Reddit(
    client_id=client_id,
    client_secret=client_secret,
    user_agent=user_agent
)

query = "AAPL"
subreddits = ["stocks", "investing", "wallstreetbets"]

rows = []
for sub in subreddits:
    for post in reddit.subreddit(sub).search(query, limit=100):
        rows.append([
            datetime.utcfromtimestamp(post.created_utc).isoformat(),
            post.title + " " + (post.selftext or "")
        ])

with open("data/real_osint.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["ts", "text"])
    writer.writerows(rows)

print("✅ Real Reddit OSINT saved to data/real_osint.csv")
