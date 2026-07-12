#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

text = "Labor unions, it seems, have taken on the role of modern-day Luddites, standing firm against the tide of automation that might otherwise lift all boats. With Democrats caught between tech-loving abundance advocates and union interests, there's a classic conflict brewing. But perhaps it's fitting—after all, who needs progress when you can have protectionism?"
print("len", len(text))
try:
    from twitter_poster import post_tweet
    post_tweet(text)
except Exception as e:
    print("ERROR:", e)
