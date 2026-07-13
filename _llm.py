"""Centralized LLM config + resilient call wrapper.

Two model tiers, both overridable via env:
  MODEL_WRITE  — post writing / rewriting (quality-critical). Default: opus.
  MODEL_UTIL   — mechanical tasks: focus scoring, story dedup, clustering,
                 translation, Twitter-fit compression. Default: a cheaper model.

anthropic_call() wraps client.messages.create with exponential backoff so a
transient overload/rate-limit (429/500/503/529) no longer silently drops a
whole batch of articles. Raises the last error only after all retries fail.
"""
import os
import random
import time

# Quality tier — used for the actual post writing and refinement.
MODEL_WRITE = os.getenv("MODEL_WRITE", "claude-opus-4-5")
# Cheap tier — used for mechanical/structured tasks. Sonnet keeps JSON reliable
# while costing a fraction of opus; override to haiku for max savings.
MODEL_UTIL = os.getenv("MODEL_UTIL", "claude-sonnet-4-5")

# Transient error substrings worth retrying (status codes + names).
_RETRYABLE = (
    "overloaded", "rate_limit", "rate limit", "timeout", "timed out",
    "429", "500", "502", "503", "529",
    "connection", "temporarily",
)


def _is_retryable(err: Exception) -> bool:
    s = f"{type(err).__name__} {err}".lower()
    return any(tok in s for tok in _RETRYABLE)


def anthropic_call(client, *, max_retries: int = 3, base_delay: float = 1.5, **kwargs):
    """Call client.messages.create(**kwargs) with exponential backoff + jitter.

    Retries only on transient errors. Non-transient errors (e.g. bad request,
    auth) raise immediately. On exhaustion, re-raises the last transient error.
    """
    last = None
    for attempt in range(max_retries + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — we classify below
            last = e
            if attempt >= max_retries or not _is_retryable(e):
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.75)
            try:
                model = kwargs.get("model", "?")
                print(f"[llm-retry] {model} attempt {attempt + 1}/{max_retries} "
                      f"after {type(e).__name__}; sleeping {delay:.1f}s", flush=True)
            except Exception:
                pass
            time.sleep(delay)
    if last:
        raise last
