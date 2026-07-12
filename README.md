# Factiva Exporter (FACT)

Command-center for exporting Factiva/TechCrunch articles, clustering topics, generating social posts with Claude, and publishing to Twitter/X and Telegram.

## Stack

- Python 3.12, Flask, Gunicorn
- Playwright (Factiva export)
- Anthropic Claude / optional OpenAI
- Tweepy (Twitter), Telethon (Telegram)

## Setup

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# fill .env with credentials
python app.py
```

Production deploy on Ubuntu: `bash deploy.sh`

## Environment

See `.env.example` for required keys.

## License

Private project.
