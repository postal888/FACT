#!/usr/bin/env python3
"""Quick Twitter API connectivity check."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

keys = [
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_SECRET",
    "TWITTER_ACCESS_TOKEN_SECRET",
]
for k in keys:
    v = os.getenv(k)
    print(f"{k}: {'SET len=' + str(len(v)) if v else 'MISSING'}")

try:
    from twitter_poster import get_twitter_client
    client = get_twitter_client()
    me = client.get_me()
    if me and me.data:
        print(f"get_me: OK @{me.data.username} id={me.data.id}")
    else:
        print("get_me: FAIL (no data)")
        sys.exit(1)

    # Dry-run only unless --post passed
    if "--post" in sys.argv:
        r = client.create_tweet(text="FACT connectivity test — please ignore")
        print(f"create_tweet: OK id={r.data['id']}")
    else:
        print("create_tweet: skipped (pass --post to test write)")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(1)
