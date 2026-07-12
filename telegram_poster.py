"""
telegram_poster.py — Post messages to a Telegram channel via Telethon user session.

Session must be pre-authorized. Copy tg_session.session from TGBOT2 or run authorize() once.
"""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

API_ID   = int(os.getenv("TG_API_ID", "31945445"))
API_HASH = os.getenv("TG_API_HASH", "c40430686bd5a5af7642e9391dc3adaa")
CHANNEL  = os.getenv("TG_CHANNEL", "@facts8888")
SESSION  = os.getenv("TG_SESSION", "tg_session")  # path without .session


def _session_path() -> str:
    """Return absolute path to session file (without .session extension)."""
    p = Path(SESSION)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    return str(p)


async def _with_client(coro_fn):
    from telethon import TelegramClient
    session = _session_path()
    client = TelegramClient(session, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telegram session not authorized. "
            "Run: python telegram_poster.py --auth"
        )
    try:
        return await coro_fn(client)
    finally:
        await client.disconnect()


async def _post_async(text: str, channel: str) -> dict:
    async def run(client):
        msg = await client.send_message(channel, text)
        return {
            "id": msg.id,
            "channel": channel,
            "url": f"https://t.me/{channel.lstrip('@')}/{msg.id}",
        }
    return await _with_client(run)


async def _edit_async(message_id: int, text: str, channel: str) -> dict:
    async def run(client):
        msg = await client.edit_message(channel, int(message_id), text)
        return {
            "id": msg.id,
            "channel": channel,
            "url": f"https://t.me/{channel.lstrip('@')}/{msg.id}",
        }
    return await _with_client(run)


async def _delete_async(message_id: int, channel: str) -> dict:
    async def run(client):
        await client.delete_messages(channel, [int(message_id)])
        return {"ok": True, "id": int(message_id), "channel": channel}
    return await _with_client(run)


def post_telegram(text: str, channel: str = None) -> dict:
    """Post a message to Telegram channel. Synchronous wrapper."""
    ch = channel or CHANNEL
    return asyncio.run(_post_async(text, ch))


def edit_telegram(message_id: int, text: str, channel: str = None) -> dict:
    """Edit an existing channel message."""
    ch = channel or CHANNEL
    return asyncio.run(_edit_async(message_id, text, ch))


def delete_telegram(message_id: int, channel: str = None) -> dict:
    """Delete a channel message."""
    ch = channel or CHANNEL
    return asyncio.run(_delete_async(message_id, ch))


async def _auth_async():
    """Interactive authorization — run once to create session."""
    from telethon import TelegramClient
    session = _session_path()
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"Authorized as: {me.first_name} @{me.username or ''}")
    await client.disconnect()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--auth", action="store_true", help="Authorize Telegram session")
    p.add_argument("--test", action="store_true", help="Send test message")
    p.add_argument("--channel", default=CHANNEL)
    args = p.parse_args()

    if args.auth:
        asyncio.run(_auth_async())
    elif args.test:
        r = post_telegram("Test from Factiva Exporter", args.channel)
        print("Posted:", r)
