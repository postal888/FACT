"""
Factiva Exporter — web UI
Run: python app.py
Then open http://localhost:5000 in browser.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, Response, jsonify, render_template, request, send_file
from functools import wraps

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
BASE_DIR = Path(__file__).parent

# ── Tweet scheduler ───────────────────────────────────────────────────────────

_tweet_queue_file = BASE_DIR / "tweet_queue.json"
_tweet_queue_lock = threading.Lock()

def _load_tweet_queue():
    if _tweet_queue_file.exists():
        try:
            return json.loads(_tweet_queue_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def _save_tweet_queue(q):
    _tweet_queue_file.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")

def _tweet_scheduler_loop():
    """Background thread: checks queue every 30s and posts due tweets."""
    while True:
        try:
            with _tweet_queue_lock:
                q = _load_tweet_queue()
                now = datetime.utcnow()
                changed = False
                for item in q:
                    if item["status"] != "pending":
                        continue
                    scheduled = datetime.fromisoformat(item["scheduled_at"])
                    if now >= scheduled:
                        try:
                            from twitter_poster import post_tweet
                            result = post_tweet(item["text"])
                            item["status"] = "posted"
                            item["url"] = result.get("url", "")
                        except Exception as e:
                            item["status"] = "error"
                            item["error"] = str(e)
                        changed = True
                if changed:
                    _save_tweet_queue(q)
        except Exception:
            pass
        threading.Event().wait(30)

threading.Thread(target=_tweet_scheduler_loop, daemon=True).start()


# ── Publisher stack (Twitter + Telegram, server-side scheduler) ───────────────

_publisher_stack_file = BASE_DIR / "publisher_stack.json"
_publisher_stack_lock = threading.Lock()

_pipelines_file = BASE_DIR / "pipelines.json"
_pipelines_lock = threading.Lock()
DEFAULT_PIPELINE_ID = "default"


def _default_pipelines() -> dict:
    return {
        "pipelines": [
            {
                "id": DEFAULT_PIPELINE_ID,
                "name": "Основной",
                "default_platform": "telegram",
                "times": ["10:00"],
                "source_mode": "factiva",
                "auto_enabled": False,
                "auto_export_times": ["08:00"],
                "auto_max_drafts": 10,
                "auto_run_article_topics": True,
                "auto_publish_mode": "slots",
                "auto_publish_interval_minutes": 120,
                "auto_publish_start_time": "10:00",
                "last_auto_runs": {},
                "auto_last_status": {},
                "workflow": {},
            }
        ]
    }


def _load_pipelines() -> dict:
    if _pipelines_file.exists():
        try:
            data = json.loads(_pipelines_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("pipelines"), list):
                return data
        except Exception:
            pass
    return _default_pipelines()


def _save_pipelines(data: dict) -> None:
    _pipelines_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _find_pipeline(data: dict, pid: str) -> dict | None:
    for p in data.get("pipelines", []):
        if p.get("id") == pid:
            return p
    return None


def _normalize_stack_item(item: dict) -> dict:
    if not item.get("pipeline"):
        item["pipeline"] = DEFAULT_PIPELINE_ID
    if not item.get("platform"):
        item["platform"] = "telegram"
    return item


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso_utc(value: str) -> datetime:
    s = (value or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _text_dedupe_key(text: str) -> str:
    import re
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    return t[:160]


def _title_dedupe_key(title: str) -> str:
    from twitter_poster import _norm_title
    return _norm_title(title or "")[:160]


def _url_dedupe_key(url: str) -> str:
    from twitter_poster import _norm_url
    return _norm_url(url or "")


def _token_jaccard(a: str, b: str) -> float:
    from twitter_poster import _jaccard, _title_tokens
    return _jaccard(_title_tokens(a or ""), _title_tokens(b or ""))


def _load_publisher_stack() -> list:
    if _publisher_stack_file.exists():
        try:
            data = json.loads(_publisher_stack_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [_normalize_stack_item(x) for x in data]
        except Exception:
            pass
    return []


def _save_publisher_stack(stack: list) -> None:
    _publisher_stack_file.write_text(
        json.dumps(stack, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _twitter_length_error(text: str, platform: str) -> str | None:
    if _normalize_platform(platform) == "twitter" and len(text or "") > 280:
        return f"Слишком длинный твит ({len(text)} / 280). Сократите текст или выберите Telegram."
    return None


def _publish_stack_item(item: dict) -> None:
    platform = (item.get("platform") or "telegram").strip().lower()
    text = (item.get("text") or "").strip()
    err = _twitter_length_error(text, platform)
    if err:
        raise ValueError(err)
    if platform == "telegram":
        from telegram_poster import post_telegram
        result = post_telegram(item["text"])
    else:
        from twitter_poster import post_tweet
        result = post_tweet(item["text"])
    item["status"] = "published"
    item["messageId"] = result.get("id")
    item["url"] = result.get("url", "")
    item["channel"] = result.get("channel", "")
    item["error"] = ""


def _recover_stuck_publisher_items(stack: list) -> bool:
    """Reset items left in 'publishing' after a crashed worker tick."""
    changed = False
    for item in stack:
        if (item.get("status") or "").lower() == "publishing":
            item["status"] = "pending"
            item["error"] = ""
            changed = True
    return changed


def _item_is_due(item: dict, now: datetime | None = None) -> bool:
    if item.get("status") != "pending":
        return False
    scheduled_at = item.get("scheduledAt")
    if not scheduled_at:
        return False
    try:
        return (now or _utc_now()) >= _parse_iso_utc(scheduled_at)
    except Exception:
        return False


def _process_due_publisher_items(stack: list) -> tuple[bool, set[str]]:
    """Post all due pending items. Returns (changed, stale_pipeline_ids).

    Must NOT acquire _pipelines_lock here — caller may already hold
    _publisher_stack_lock (avoid deadlock with automation).
    """
    now = _utc_now()
    changed = _recover_stuck_publisher_items(stack)
    stale_pids: set[str] = set()
    indexes: dict[str, dict] = {}

    for item in stack:
        if item.get("status") != "pending":
            continue
        scheduled_at = item.get("scheduledAt")
        if not scheduled_at:
            continue
        try:
            if now < _parse_iso_utc(scheduled_at):
                continue
        except Exception as e:
            item["status"] = "failed"
            item["error"] = f"Некорректное время публикации: {scheduled_at} ({e})"
            changed = True
            continue

        pid = item.get("pipeline") or DEFAULT_PIPELINE_ID
        item_plat = _normalize_platform(item.get("platform"))
        idx_key = f"{pid}:{item_plat}"
        if idx_key not in indexes:
            texts: set[str] = set()
            titles: set[str] = set()
            urls: set[str] = set()
            title_list: list[str] = []
            text_list: list[str] = []
            for it in stack:
                if it.get("pipeline") != pid:
                    continue
                if _normalize_platform(it.get("platform")) != item_plat:
                    continue
                st = (it.get("status") or "").lower()
                if st not in ("published", "sending", "publishing"):
                    continue
                if it is item:
                    continue
                tk = _text_dedupe_key(it.get("text", ""))
                if tk:
                    texts.add(tk)
                    text_list.append(tk)
                title_k = _title_dedupe_key(it.get("articleTitle") or "")
                if title_k:
                    titles.add(title_k)
                    title_list.append(title_k)
                url_k = _url_dedupe_key(it.get("articleUrl") or "")
                if url_k:
                    urls.add(url_k)
            # Merge long-lived story keys without taking pipelines_lock here —
            # read a snapshot unlocked; slight staleness is OK for dedupe.
            try:
                pdata = _load_pipelines()
                p = _find_pipeline(pdata, pid)
                for s in (p or {}).get("published_stories") or []:
                    if not _story_matches_platform(s, item_plat):
                        continue
                    title_k = _title_dedupe_key(s.get("title") or "")
                    if title_k and title_k not in titles:
                        titles.add(title_k)
                        title_list.append(title_k)
                    url_k = _url_dedupe_key(s.get("url") or "")
                    if url_k:
                        urls.add(url_k)
            except Exception:
                pass
            indexes[idx_key] = {
                "texts": texts,
                "titles": titles,
                "urls": urls,
                "title_list": title_list,
                "text_list": text_list,
            }

        index = indexes[idx_key]
        if not item.get("forcePublish") and _is_stale_against_index(
            text=item.get("text") or "",
            title=item.get("articleTitle") or "",
            url=item.get("articleUrl") or "",
            index=index,
        ):
            item["status"] = "cancelled"
            item["error"] = "Пропуск: уже публиковалось / похожий пост"
            changed = True
            stale_pids.add(pid)
            continue

        item["status"] = "publishing"
        changed = True
        try:
            _publish_stack_item(item)
            _remember_published_stories(pid, [{
                "articleTitle": item.get("articleTitle") or "",
                "articleUrl": item.get("articleUrl") or "",
                "title": item.get("articleTitle") or "",
                "url": item.get("articleUrl") or "",
                "platform": item_plat,
            }], platform=item_plat)
            item.pop("forcePublish", None)
            tk = _text_dedupe_key(item.get("text", ""))
            if tk:
                index["texts"].add(tk)
                index["text_list"].append(tk)
            title_k = _title_dedupe_key(item.get("articleTitle") or "")
            if title_k:
                index["titles"].add(title_k)
                index["title_list"].append(title_k)
            url_k = _url_dedupe_key(item.get("articleUrl") or "")
            if url_k:
                index["urls"].add(url_k)
        except Exception as e:
            item["status"] = "failed"
            item["error"] = str(e)
        changed = True

    return changed, stale_pids


_publisher_scheduler_last_tick: str | None = None
_publisher_scheduler_last_error: str | None = None


def _publisher_scheduler_loop():
    """Background: publish due stack items every 30s (works when browser is closed)."""
    global _publisher_scheduler_last_tick, _publisher_scheduler_last_error
    # Delay first tick so module helpers finish loading
    threading.Event().wait(5)
    while True:
        stale_pids: set[str] = set()
        try:
            with _publisher_stack_lock:
                stack = _load_publisher_stack()
                changed, stale_pids = _process_due_publisher_items(stack)
                if changed:
                    _save_publisher_stack(stack)
            _publisher_scheduler_last_tick = _utc_now().isoformat() + "Z"
            _publisher_scheduler_last_error = None
        except Exception as e:
            _publisher_scheduler_last_error = str(e)
            stale_pids = set()
            try:
                import traceback
                traceback.print_exc()
            except Exception:
                pass
        # Refresh OUTSIDE publisher lock — avoids deadlock with automation
        for pid in stale_pids:
            try:
                _request_pipeline_refresh(pid, "паблишер нашёл старые посты в очереди")
            except Exception:
                pass
        threading.Event().wait(30)


threading.Thread(target=_publisher_scheduler_loop, daemon=True).start()


# ── Pipeline automation (scheduled export → topics → drafts → publisher) ───

MSK_OFFSET = timedelta(hours=3)
_automation_lock = threading.Lock()
_automation_running = False
_automation_running_pid: str | None = None
_automation_cancel = False
_automation_subproc: subprocess.Popen | None = None
_automation_subproc_lock = threading.Lock()
_automation_log: deque[str] = deque(maxlen=120)
_automation_log_lock = threading.Lock()
EXPORT_SUBPROC_GRACE_SEC = 60


class AutomationCancelled(Exception):
    pass


def _automation_log_clear() -> None:
    with _automation_log_lock:
        _automation_log.clear()


def _automation_log_push(line: str) -> None:
    s = (line or "").rstrip()
    if not s:
        return
    with _automation_log_lock:
        _automation_log.append(s)


def _automation_log_tail(n: int = 12) -> list[str]:
    with _automation_log_lock:
        return list(_automation_log)[-max(1, n):]


def _drain_automation_subproc_stdout(proc: subprocess.Popen) -> None:
    try:
        if proc.stdout:
            for line in proc.stdout:
                s = (line or "").rstrip()
                if not s:
                    continue
                _automation_log_push(s)
                try:
                    _job_queue.put_nowait(s)
                except Exception:
                    pass
    except Exception:
        pass


def _set_automation_subproc(proc: subprocess.Popen | None) -> None:
    global _automation_subproc
    with _automation_subproc_lock:
        _automation_subproc = proc


def _kill_automation_subproc() -> None:
    """Terminate the child process started by automation (e.g. factiva_agent)."""
    with _automation_subproc_lock:
        proc = _automation_subproc
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    finally:
        _set_automation_subproc(None)


class NoNewArticles(Exception):
    """Feed has only already-published stories — soft-skip, not a hard failure."""
    pass


def _automation_subproc_alive() -> bool:
    with _automation_subproc_lock:
        proc = _automation_subproc
    return proc is not None and proc.poll() is None


def _maybe_recover_stuck_automation(max_stale_sec: int = 90) -> bool:
    """Clear zombie automation flag when subprocess is gone but running=True."""
    global _automation_running, _automation_running_pid
    if not _automation_running:
        return False
    if _automation_subproc_alive():
        return False
    pid = _automation_running_pid
    stale = False
    if pid:
        with _pipelines_lock:
            p = _find_pipeline(_load_pipelines(), pid)
        st = (p or {}).get("auto_last_status") or {}
        step = st.get("step")
        at = st.get("at")
        age_sec = None
        if at:
            try:
                ts = datetime.fromisoformat(at.replace("Z", "+00:00")).replace(tzinfo=None)
                age_sec = (_utc_now() - ts).total_seconds()
            except ValueError:
                age_sec = None
        if step == "export":
            # Grace period: subprocess starts a moment after status is set.
            stale = age_sec is not None and age_sec >= EXPORT_SUBPROC_GRACE_SEC
        elif at:
            stale = age_sec is not None and age_sec >= max_stale_sec
        else:
            stale = True
    else:
        stale = True
    if not stale:
        return False
    if pid:
        _set_pipeline_auto_status(
            pid, "cancelled", ok=False,
            error="Зависание — сброшено автоматически",
        )
    _end_automation_run()
    return True


def _check_automation_cancel():
    global _automation_cancel
    if _automation_cancel:
        raise AutomationCancelled("Остановлено пользователем")


def _begin_automation_run(pid: str) -> None:
    global _automation_running, _automation_running_pid, _automation_cancel
    _automation_cancel = False
    _automation_log_clear()
    _automation_running = True
    _automation_running_pid = pid


def _end_automation_run() -> None:
    global _automation_running, _automation_running_pid, _automation_cancel
    _kill_automation_subproc()
    _automation_running = False
    _automation_running_pid = None
    _automation_cancel = False


def _msk_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + MSK_OFFSET


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = (s or "").strip().split(":")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


AUTO_MIN_RERUN_MINUTES = int(os.getenv("AUTO_MIN_RERUN_MINUTES", "120"))


def _last_successful_automation_at(pipeline: dict) -> datetime | None:
    status = pipeline.get("auto_last_status") or {}
    if status.get("step") != "done" or not status.get("ok"):
        return None
    at = status.get("at")
    if not at:
        return None
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _automation_on_cooldown(pipeline: dict, min_minutes: int | None = None) -> bool:
    """Block back-to-back full pipeline runs (multiple export slots / manual clicks)."""
    min_m = min_minutes
    if min_m is None:
        min_m = int(pipeline.get("auto_min_rerun_minutes") or AUTO_MIN_RERUN_MINUTES)
    last = _last_successful_automation_at(pipeline)
    if not last:
        return False
    now = datetime.now(timezone.utc)
    return (now - last).total_seconds() < max(1, min_m) * 60


def _normalize_platform(platform: str | None) -> str:
    p = (platform or "telegram").strip().lower()
    return p if p in ("telegram", "twitter") else "telegram"


def _story_matches_platform(story: dict, platform: str) -> bool:
    """Legacy stories without platform only block Telegram, not Twitter."""
    sp = (story.get("platform") or "").strip().lower()
    if not sp:
        return platform == "telegram"
    return sp == platform


def _published_content_index(pid: str, platform: str | None = None) -> dict:
    """Fingerprints of already queued/published posts for this pipeline + platform."""
    plat = _normalize_platform(platform) if platform else None
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
    texts: set[str] = set()
    titles: set[str] = set()
    urls: set[str] = set()
    title_list: list[str] = []
    text_list: list[str] = []
    for it in stack:
        if it.get("pipeline") != pid:
            continue
        if plat and _normalize_platform(it.get("platform")) != plat:
            continue
        if (it.get("status") or "").lower() not in ("pending", "published", "sending", "publishing"):
            continue
        tk = _text_dedupe_key(it.get("text", ""))
        if tk:
            texts.add(tk)
            text_list.append(tk)
        title_k = _title_dedupe_key(it.get("articleTitle") or "")
        if title_k:
            titles.add(title_k)
            title_list.append(title_k)
        url_k = _url_dedupe_key(it.get("articleUrl") or "")
        if url_k:
            urls.add(url_k)

    # Long-lived story memory (survives even if stack items lack articleTitle)
    with _pipelines_lock:
        p = _find_pipeline(_load_pipelines(), pid)
        stories = (p or {}).get("published_stories") or []
    for s in stories:
        if plat and not _story_matches_platform(s, plat):
            continue
        title_k = _title_dedupe_key(s.get("title") or "")
        if title_k and title_k not in titles:
            titles.add(title_k)
            title_list.append(title_k)
        url_k = _url_dedupe_key(s.get("url") or "")
        if url_k:
            urls.add(url_k)

    return {
        "texts": texts,
        "titles": titles,
        "urls": urls,
        "title_list": title_list,
        "text_list": text_list,
    }


def _remember_published_stories(
    pid: str,
    rows: list[dict],
    *,
    platform: str | None = None,
) -> None:
    """Persist article title/url after a successful publish on a specific platform."""
    if not rows:
        return
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return
        stories = list(p.get("published_stories") or [])
        seen_t = {_title_dedupe_key(s.get("title") or "") for s in stories}
        seen_u = {_url_dedupe_key(s.get("url") or "") for s in stories}
        changed = False
        for row in rows:
            title = (row.get("articleTitle") or row.get("title") or "").strip()
            url = (row.get("articleUrl") or row.get("url") or "").strip()
            row_plat = _normalize_platform(row.get("platform") or platform)
            tk = _title_dedupe_key(title)
            uk = _url_dedupe_key(url)
            if (tk and tk in seen_t) or (uk and uk in seen_u):
                continue
            if not tk and not uk:
                continue
            stories.append({
                "title": title,
                "url": url,
                "platform": row_plat,
                "at": _utc_now().isoformat() + "Z",
            })
            if tk:
                seen_t.add(tk)
            if uk:
                seen_u.add(uk)
            changed = True
        if changed:
            p["published_stories"] = stories[-500:]
            _save_pipelines(pdata)


def _is_stale_against_index(
    *,
    text: str = "",
    title: str = "",
    url: str = "",
    index: dict,
    title_thresh: float = 0.65,
    text_thresh: float = 0.32,
) -> bool:
    """True if this post/article was already published or is queued."""
    url_k = _url_dedupe_key(url)
    if url_k and url_k in index["urls"]:
        return True
    title_k = _title_dedupe_key(title)
    if title_k:
        if title_k in index["titles"]:
            return True
        for prev in index["title_list"]:
            if _token_jaccard(title_k, prev) >= title_thresh:
                return True
        # English title vs already-published Russian post text
        for prev in index["text_list"]:
            if _token_jaccard(title_k, prev) >= 0.28:
                return True
    text_k = _text_dedupe_key(text)
    if text_k:
        if text_k in index["texts"]:
            return True
        for prev in index["text_list"]:
            if _token_jaccard(text_k, prev) >= text_thresh:
                return True
    return False


def _filter_duplicate_publisher_items(pid: str, items: list[dict]) -> tuple[list[dict], int]:
    """Skip posts whose text/article already exists in this pipeline's queue."""
    indexes: dict[str, dict] = {}
    out: list[dict] = []
    skipped = 0
    for raw in items:
        plat = _normalize_platform(raw.get("platform"))
        index = indexes.get(plat)
        if index is None:
            index = _published_content_index(pid, plat)
            indexes[plat] = index
        text = raw.get("text", "")
        title = raw.get("articleTitle") or raw.get("title") or ""
        url = raw.get("articleUrl") or raw.get("url") or ""
        if _is_stale_against_index(text=text, title=title, url=url, index=index):
            skipped += 1
            continue
        # Reserve fingerprints so duplicates within the same batch are dropped
        tk = _text_dedupe_key(text)
        if tk:
            index["texts"].add(tk)
            index["text_list"].append(tk)
        title_k = _title_dedupe_key(title)
        if title_k:
            index["titles"].add(title_k)
            index["title_list"].append(title_k)
        url_k = _url_dedupe_key(url)
        if url_k:
            index["urls"].add(url_k)
        out.append(raw)
    return out, skipped


def _filter_novel_drafts(
    pid: str,
    drafts: list[dict],
    platform: str | None = None,
) -> tuple[list[dict], int]:
    """Keep only drafts whose source article / text is not already published."""
    index = _published_content_index(pid, platform)
    novel: list[dict] = []
    skipped = 0
    for d in drafts:
        text = (d.get("draft") or "").strip()
        title = d.get("title") or ""
        url = d.get("url") or ""
        if not text or _is_stale_against_index(text=text, title=title, url=url, index=index):
            skipped += 1
            continue
        tk = _text_dedupe_key(text)
        if tk:
            index["texts"].add(tk)
            index["text_list"].append(tk)
        title_k = _title_dedupe_key(title)
        if title_k:
            index["titles"].add(title_k)
            index["title_list"].append(title_k)
        url_k = _url_dedupe_key(url)
        if url_k:
            index["urls"].add(url_k)
        novel.append(d)
    return novel, skipped


def _filter_articles_excluding_published(
    articles: list[dict],
    pid: str,
    platform: str | None = None,
) -> tuple[list[dict], int]:
    index = _published_content_index(pid, platform)
    kept: list[dict] = []
    skipped = 0
    for art in articles:
        if _is_stale_against_index(
            title=art.get("title") or "",
            url=art.get("url") or "",
            index=index,
        ):
            skipped += 1
            continue
        kept.append(art)
    return kept, skipped


def _request_pipeline_refresh(pid: str, reason: str = "") -> None:
    """Ask automation loop to re-import because queued posts were stale."""
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return
        p["auto_refresh_requested"] = True
        p["auto_refresh_reason"] = reason or "stale posts"
        p["auto_refresh_at"] = _utc_now().isoformat() + "Z"
        _save_pipelines(pdata)


def _auto_export_slot_due(pipeline: dict) -> tuple[str | None, str | None]:
    """Return (slot_key, time_str) if an export slot is due now (2-min window)."""
    times = pipeline.get("auto_export_times") or []
    if not times:
        return None, None
    now = _msk_now()
    today = now.strftime("%Y-%m-%d")
    last_runs = pipeline.get("last_auto_runs") or {}
    for t in times:
        ts = str(t).strip()
        if not ts:
            continue
        try:
            h, m = _parse_hhmm(ts)
        except (ValueError, IndexError):
            continue
        slot_key = f"{today}|{ts}"
        if slot_key in last_runs:
            continue
        slot_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = (now - slot_dt).total_seconds()
        if 0 <= delta < 120:
            return slot_key, ts
    return None, None


def _build_slots_msk(times: list[str], count: int) -> list[str]:
    if not times or count <= 0:
        return []
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_msk = now_utc + MSK_OFFSET
    slots: list[str] = []
    day_offset = 0
    while len(slots) < count and day_offset <= 60:
        for t in times:
            if len(slots) >= count:
                break
            try:
                h, m = _parse_hhmm(str(t))
            except (ValueError, IndexError):
                continue
            d = now_msk.date() + timedelta(days=day_offset)
            slot_utc = datetime(d.year, d.month, d.day, h, m) - MSK_OFFSET
            if slot_utc > now_utc:
                slots.append(slot_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        day_offset += 1
    return slots[:count]


def _compute_publish_slots(pipeline: dict, count: int) -> list[str]:
    """Build publication schedule for automation / pipeline queue (MSK wall clock)."""
    if count <= 0:
        return []
    mode = (pipeline.get("auto_publish_mode") or "slots").lower()
    if mode == "interval":
        interval_min = max(1, int(pipeline.get("auto_publish_interval_minutes") or 120))
        start_str = (pipeline.get("auto_publish_start_time") or "10:00").strip()
        try:
            h, m = _parse_hhmm(start_str)
        except (ValueError, IndexError):
            h, m = 10, 0
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        now_msk = now_utc + MSK_OFFSET
        start_utc = datetime(now_msk.year, now_msk.month, now_msk.day, h, m) - MSK_OFFSET
        if start_utc <= now_utc:
            start_utc = now_utc
        return [
            (start_utc + timedelta(minutes=interval_min * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            for i in range(count)
        ]
    times = pipeline.get("times") or ["10:00"]
    return _build_slots_msk(times, count)


def _set_pipeline_auto_status(pid: str, step: str, ok: bool = True, error: str = "", extra: dict | None = None):
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return
        status = {
            "at": _utc_now().isoformat() + "Z",
            "step": step,
            "ok": ok,
            "error": error,
        }
        if extra:
            status.update(extra)
        p["auto_last_status"] = status
        _save_pipelines(pdata)


def _mark_auto_slot(pid: str, slot_key: str):
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return
        p.setdefault("last_auto_runs", {})[slot_key] = _utc_now().isoformat() + "Z"
        _save_pipelines(pdata)


def _excluded_sources_from_workflow(workflow: dict, filename: str) -> list | None:
    st = (workflow.get("fileSourceState") or {}).get(filename)
    if not st:
        return None
    excluded = []
    checked = st.get("checked") or {}
    for src in st.get("sources") or []:
        name = src.get("name", "")
        if name and not checked.get(name, True):
            excluded.append(name)
    return excluded or None


def _list_sources_for_file(filename: str) -> dict:
    from twitter_poster import load_articles_with_stats

    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        raise FileNotFoundError(f"File not found: {filename}")
    articles, stats = load_articles_with_stats(str(rtf_path))
    counts: dict[str, int] = {}
    for a in articles:
        src = (a.get("source") or "").strip() or "(без источника)"
        counts[src] = counts.get(src, 0) + 1
    sources = sorted(
        [{"name": k, "count": v} for k, v in counts.items()],
        key=lambda x: -x["count"],
    )
    return {
        "sources": sources,
        "total": stats["total"],
        "raw_total": stats["raw_total"],
        "removed_duplicates": stats["removed_duplicates"],
    }


def _excluded_sources_for_automation(workflow: dict, filename: str) -> list | None:
    """Source filter for a new export: per-file state or global unchecked names."""
    direct = _excluded_sources_from_workflow(workflow, filename)
    if direct is not None:
        return direct
    unchecked = set()
    for st in (workflow.get("fileSourceState") or {}).values():
        checked = st.get("checked") or {}
        for src in st.get("sources") or []:
            name = (src.get("name") or "").strip()
            if name and not checked.get(name, True):
                unchecked.add(name.lower())
    if not unchecked:
        return None
    src_data = _list_sources_for_file(filename)
    excluded = [s["name"] for s in src_data["sources"] if s["name"].lower() in unchecked]
    return excluded or None


def _build_file_source_state_entry(filename: str, workflow: dict) -> dict:
    src_data = _list_sources_for_file(filename)
    unchecked = set()
    for st in (workflow.get("fileSourceState") or {}).values():
        checked = st.get("checked") or {}
        for src in st.get("sources") or []:
            name = (src.get("name") or "").strip()
            if name and not checked.get(name, True):
                unchecked.add(name.lower())
    checked_map = {
        s["name"]: s["name"].lower() not in unchecked
        for s in src_data["sources"]
    }
    return {
        "sources": src_data["sources"],
        "checked": checked_map,
        "total": src_data["total"],
        "raw_total": src_data["raw_total"],
        "removed_duplicates": src_data["removed_duplicates"],
    }


def _topic_is_checked(topics_checked: dict, index: int) -> bool:
    if not topics_checked:
        return True
    return bool(topics_checked.get(str(index), topics_checked.get(index, False)))


def _collect_article_indices_from_workflow(topics: list, max_drafts: int, workflow: dict) -> list[int]:
    topics_checked = workflow.get("topicsChecked") or {}
    indices = _collect_diverse_article_indices(topics, max_drafts, topics_checked)
    if indices:
        return indices
    return _collect_article_indices(topics, max_drafts)


def _workflow_lang(workflow: dict, pipeline: dict | None = None) -> str:
    """Resolve output language for a pipeline window."""
    lang = (workflow.get("lang") or "").strip().lower()
    if lang in ("en", "ru", "ua"):
        return lang
    if pipeline:
        dl = (pipeline.get("default_lang") or "").strip().lower()
        if dl in ("en", "ru", "ua"):
            return dl
        if (pipeline.get("default_platform") or "").strip().lower() == "twitter":
            return "en"
    return "ru"


def _save_pipeline_workflow(pid: str, workflow: dict) -> None:
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return
        old = p.get("workflow") or {}
        for key in ("tcArticles", "tcChecked", "tcFeed", "tcDom", "tcCategoryEnabled", "tcExportMeta"):
            if key not in workflow and key in old:
                workflow[key] = old[key]
        p["workflow"] = workflow
        _save_pipelines(pdata)


def _slugify_export_name(text: str) -> str:
    import re
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\s\-]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", "_", t).strip("_")
    return (t[:60] or "factiva_export")


def _wf_flag_checked(bag: dict, index: int, default: bool = True) -> bool:
    if not bag:
        return default
    if str(index) in bag:
        return bool(bag[str(index)])
    if index in bag:
        return bool(bag[index])
    return default


def _build_factiva_run_payload(factiva_dom: dict) -> dict:
    """Mirror runJob() payload from the UI."""
    saved = (factiva_dom.get("savedSearch") or "").strip()
    query = (factiva_dom.get("queryText") or "").strip()
    run_mode = (factiva_dom.get("mode") or ("search" if saved else "query")).strip().lower()
    date_val = (factiva_dom.get("datePreset") or "week").strip()
    if date_val == "custom":
        df = (factiva_dom.get("dateFrom") or "").strip()
        dt = (factiva_dom.get("dateTo") or "").strip()
        if df and dt:
            date_val = f"{df}:{dt}"
        elif df:
            date_val = df
    raw_name = (factiva_dom.get("outName") or "factiva_export").strip()
    active = saved if run_mode == "search" else query
    out_name = raw_name if raw_name and raw_name != "factiva_export" else _slugify_export_name(active)
    return {
        "mode": run_mode,
        "search": saved,
        "query": query,
        "date": date_val,
        "source": (factiva_dom.get("source") or "").strip(),
        "maxPages": int(factiva_dom.get("maxPages") or 0),
        "outName": out_name,
        "headless": True,
        "dedup": factiva_dom.get("dedup", True),
    }


def _validate_factiva_dom(factiva_dom: dict) -> None:
    saved = (factiva_dom.get("savedSearch") or "").strip()
    query = (factiva_dom.get("queryText") or "").strip()
    source = (factiva_dom.get("source") or "").strip()
    run_mode = (factiva_dom.get("mode") or ("search" if saved else "query")).strip().lower()
    if run_mode == "search":
        if not saved:
            raise RuntimeError(
                "Настройки Factiva не сохранены: укажите сохранённый поиск в «Экспорт» "
                "и снова включите автоматизацию"
            )
    elif not query and not source:
        raise RuntimeError(
            "Настройки Factiva не сохранены: укажите запрос или источник в «Экспорт» "
            "и снова включите автоматизацию"
        )


def _run_factiva_export_blocking(factiva_dom: dict) -> str:
    """RPA: runJob() — Factiva export; streams logs to /stream like manual export."""
    global _job_proc, _job_done, _job_out
    with _job_lock:
        if _job_proc and _job_proc.poll() is None:
            raise RuntimeError("Ручной экспорт Factiva уже выполняется")

    _validate_factiva_dom(factiva_dom)
    data = _build_factiva_run_payload(factiva_dom)
    args = _build_args(data)
    if isinstance(args, str):
        raise RuntimeError(args)

    _job_done = False
    _job_out = ""
    while not _job_queue.empty():
        try:
            _job_queue.get_nowait()
        except queue.Empty:
            break

    start_msg = f"[auto] Запуск Factiva: {data.get('search') or data.get('query') or data.get('outName')}"
    _automation_log_push(start_msg)
    _job_queue.put(start_msg)

    proc = subprocess.Popen(
        [sys.executable, "-u", str(BASE_DIR / "factiva_agent.py")] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    _job_proc = proc
    _set_automation_subproc(proc)

    def _reader() -> None:
        global _job_done, _job_out
        try:
            _drain_automation_subproc_stdout(proc)
        finally:
            rc = proc.wait()
            _job_done = True
            try:
                out_idx = args.index("--out") + 1
                candidate = args[out_idx]
                if rc == 0 and Path(candidate).exists():
                    _job_out = candidate
                else:
                    _job_out = ""
                    if rc != 0:
                        _job_queue.put(f"[!] Job exited with code {rc}")
            except (ValueError, IndexError):
                _job_out = ""

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    deadline = time.time() + 3600
    try:
        while proc.poll() is None:
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("factiva_agent timeout (3600s)")
            if _automation_cancel:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise AutomationCancelled("Остановлено пользователем")
            time.sleep(0.3)

        if reader.is_alive():
            reader.join(timeout=5)
        if _automation_cancel:
            raise AutomationCancelled("Остановлено пользователем")
        if proc.returncode != 0:
            tail = "\n".join(_automation_log_tail(20))[-800:]
            raise RuntimeError(f"factiva_agent exit {proc.returncode}: {tail}")
        out_idx = args.index("--out") + 1
        path = Path(args[out_idx])
        if not path.exists():
            raise RuntimeError("Файл экспорта не создан")
        return path.name
    finally:
        _set_automation_subproc(None)


def _tc_date_range_from_dom(tc_dom: dict) -> tuple[str | None, str | None]:
    date_from = (tc_dom.get("dateFrom") or "").strip() or None
    date_to = (tc_dom.get("dateTo") or "").strip() or None
    preset = (tc_dom.get("datePreset") or "all").strip().lower()
    if preset == "today":
        today = _msk_now().strftime("%Y-%m-%d")
        return today, today
    if preset == "week":
        return (_msk_now() - timedelta(days=7)).strftime("%Y-%m-%d"), None
    if preset == "month":
        return (_msk_now() - timedelta(days=30)).strftime("%Y-%m-%d"), None
    return date_from, date_to


def _tc_date_range_from_dom(tc_dom: dict) -> tuple[str | None, str | None]:
    date_from = (tc_dom.get("dateFrom") or "").strip() or None
    date_to = (tc_dom.get("dateTo") or "").strip() or None
    preset = (tc_dom.get("datePreset") or "all").strip().lower()
    if preset == "today":
        today = _msk_now().strftime("%Y-%m-%d")
        return today, today
    if preset == "week":
        return (_msk_now() - timedelta(days=7)).strftime("%Y-%m-%d"), None
    if preset == "month":
        return (_msk_now() - timedelta(days=30)).strftime("%Y-%m-%d"), None
    return date_from, date_to


def _tc_date_range_for_preset(preset: str, tc_dom: dict) -> tuple[str | None, str | None]:
    dom = dict(tc_dom or {})
    dom["datePreset"] = preset
    return _tc_date_range_from_dom(dom)


def _fetch_tc_articles_for_automation(
    tc_dom: dict,
    feed_key: str,
    max_articles: int,
    fetch_full_body: bool = False,
) -> tuple[list[dict], dict, str]:
    """Fetch up to max_articles; widen date preset if «today» yields too few."""
    from techcrunch_feed import fetch_techcrunch_articles

    requested = (tc_dom.get("datePreset") or "all").strip().lower()
    min_want = max(1, int(max_articles))
    widen_order = [requested]
    for p in ("week", "month", "all"):
        if p not in widen_order:
            widen_order.append(p)

    best: list[dict] = []
    best_stats: dict = {}
    used_preset = requested

    for preset in widen_order:
        date_from, date_to = _tc_date_range_for_preset(preset, tc_dom)
        articles, stats = fetch_techcrunch_articles(
            feed_key=feed_key,
            max_articles=max_articles,
            fetch_full_body=fetch_full_body,
            date_from=date_from,
            date_to=date_to,
        )
        if len(articles) > len(best):
            best = articles
            best_stats = dict(stats or {})
            used_preset = preset
        if len(articles) >= min_want:
            break

    best_stats["date_preset_requested"] = requested
    best_stats["date_preset_used"] = used_preset
    return best, best_stats, used_preset


def _filter_tc_articles_by_category(
    articles: list[dict],
    tc_cat_enabled: dict,
) -> list[dict]:
    if not articles:
        return []
    selected: list[dict] = []
    for article in articles:
        cat = (article.get("category") or "other").strip() or "other"
        if tc_cat_enabled.get(cat, tc_cat_enabled.get(str(cat), True)) is False:
            continue
        selected.append(article)
    return selected if selected else list(articles)


def _run_techcrunch_export_blocking(
    wf: dict,
    *,
    force_fresh: bool = False,
    exclude_published_pid: str | None = None,
    exclude_platform: str | None = None,
) -> str:
    """RPA: mirror applyTechCrunchToTopics — full batch up to max, not checkbox subset."""
    from techcrunch_feed import fetch_page_body_for_url, save_articles_json

    tc_dom = wf.get("tcDom") or {}
    tc_cat_enabled = wf.get("tcCategoryEnabled") or (tc_dom.get("categoryEnabled") or {})

    feed_key = (tc_dom.get("feed") or wf.get("tcFeed") or "main").strip().lower()
    max_articles = min(50, max(1, int(tc_dom.get("max") or 15)))
    fetch_full_body = bool(tc_dom.get("fullBody", False))
    # Pull a wider pool so we can drop already-published stories and still fill max
    fetch_pool = min(50, max(max_articles * 2, max_articles + 10))

    saved = wf.get("tcArticles") or []
    saved_feed = (wf.get("tcFeed") or feed_key).strip().lower()
    min_saved = min(max_articles, max(3, max_articles // 2))
    use_saved = (
        not force_fresh
        and len(saved) >= min_saved
        and saved_feed == feed_key
    )

    used_preset = (tc_dom.get("datePreset") or "all").strip().lower()
    fetch_stats: dict = {}
    excluded_n = 0

    if use_saved:
        articles = list(saved)
        fetch_stats = {"from_saved": True, "date_preset_used": "saved"}
    else:
        articles, fetch_stats, used_preset = _fetch_tc_articles_for_automation(
            tc_dom, feed_key, fetch_pool, fetch_full_body=False,
        )
        wf["tcArticles"] = articles
        wf["tcFeed"] = feed_key
        wf["tcChecked"] = {str(i): True for i in range(len(articles))}

    if not articles:
        raise RuntimeError("TechCrunch: нет статей за период")

    if exclude_published_pid:
        articles, excluded_n = _filter_articles_excluding_published(
            articles, exclude_published_pid, exclude_platform,
        )
        if not articles:
            # Fall back to full fresh fetch with wider period if everything was stale
            if not force_fresh or use_saved:
                articles, fetch_stats, used_preset = _fetch_tc_articles_for_automation(
                    {**tc_dom, "datePreset": "month"}, feed_key, fetch_pool, fetch_full_body=False,
                )
                articles, excluded_n = _filter_articles_excluding_published(
                    articles, exclude_published_pid, exclude_platform,
                )
            if not articles:
                raise NoNewArticles(
                    "Все статьи из ленты уже публиковались — "
                    "новых материалов пока нет. Завтрашний слот попробует снова."
                )

    selected = _filter_tc_articles_by_category(articles, tc_cat_enabled)
    if len(selected) < min(max_articles, len(articles)) and len(selected) < max_articles:
        selected = list(articles)
    selected = selected[:max_articles]

    if fetch_full_body:
        for article in selected:
            url = (article.get("url") or "").strip()
            current = (article.get("body") or "").strip()
            if not url:
                continue
            try:
                page_body = fetch_page_body_for_url(url)
                if len(page_body) > len(current):
                    article["body"] = page_body[:8000]
            except Exception:
                pass

    path = save_articles_json(
        selected,
        BASE_DIR / "exports",
        feed_key=feed_key,
        out_name=(tc_dom.get("outName") or "").strip() or None,
    )

    wf["tcExportMeta"] = {
        "rss_fetched": len(articles) + excluded_n,
        "exported": len(selected),
        "excluded_published": excluded_n,
        "max_requested": max_articles,
        "date_preset_requested": fetch_stats.get("date_preset_requested", used_preset),
        "date_preset_used": fetch_stats.get("date_preset_used", used_preset),
        "from_saved": bool(fetch_stats.get("from_saved")),
        "force_fresh": force_fresh,
    }
    return path.name


def _indices_from_checked_topics(
    topics: list,
    topics_checked: dict,
    max_drafts: int = 500,
) -> list[int]:
    return _collect_diverse_article_indices(topics, max_drafts, topics_checked)


def _export_search_context(wf: dict) -> str:
    """Factiva saved search / query — what the user actually wanted from export."""
    dom = wf.get("factivaDom") or {}
    parts: list[str] = []
    ss = (dom.get("savedSearch") or "").strip()
    qt = (dom.get("queryText") or "").strip()
    src = (dom.get("source") or "").strip()
    if ss:
        parts.append(f"сохранённый поиск: «{ss}»")
    if qt:
        parts.append(f"запрос: «{qt}»")
    if src:
        parts.append(f"источник: {src}")
    return "; ".join(parts)


def _search_relevance_score(article: dict, search_context: str) -> float:
    if not search_context:
        return 0.5
    from twitter_poster import _jaccard, _title_tokens
    ctx_tokens = _title_tokens(search_context)
    if not ctx_tokens:
        return 0.5
    blob = " ".join([
        article.get("title") or "",
        article.get("source") or "",
        (article.get("body") or "")[:1200],
    ])
    art_tokens = _title_tokens(blob)
    if not art_tokens:
        return 0.0
    return _jaccard(ctx_tokens, art_tokens)


def _rank_indices_by_jaccard(
    indices: list[int],
    articles: list[dict],
    search_context: str,
    *,
    min_keep: int = 1,
) -> list[int]:
    """Legacy fallback: prefer articles whose words overlap the export query."""
    if not search_context or not indices:
        return indices
    scored = [
        (_search_relevance_score(articles[i] if 0 <= i < len(articles) else {}, search_context), i)
        for i in indices
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))
    strong = [i for s, i in scored if s >= 0.14]
    if len(strong) >= min_keep:
        return strong
    return [i for _, i in scored]


def _rank_indices_by_relevance(
    indices: list[int],
    articles: list[dict],
    search_context: str,
    *,
    min_keep: int = 1,
) -> list[int]:
    """Prioritize articles by importance + fit to the user's focus profile.

    Primary path: LLM scorer (score_articles_by_focus) ranks strictly by combined
    importance/interest score, so the most significant on-focus stories come first
    regardless of which topic cluster they belong to. Boring/niche items sink.
    Falls back to the legacy Jaccard ranker if scoring is unavailable (no API key,
    no focus profile configured, or an API error).
    """
    if not indices:
        return indices
    try:
        from twitter_poster import score_articles_by_focus
        scores = score_articles_by_focus(articles, indices, search_context)
    except Exception:
        scores = {}

    if scores:
        # Strict top ordering by combined rank; unscored indices keep original order at the tail.
        scored_idx = [i for i in indices if i in scores]
        unscored = [i for i in indices if i not in scores]
        scored_idx.sort(key=lambda i: (-scores[i]["rank"], i))
        # Drop clearly boring items entirely when we still have enough strong ones.
        strong = [i for i in scored_idx if not scores[i]["boring"]]
        ordered = (strong if len(strong) >= min_keep else scored_idx) + unscored
        try:
            preview = ", ".join(
                f"#{i}(imp{scores[i]['importance']}/fit{scores[i]['interest_fit']}"
                + (",boring" if scores[i]["boring"] else "") + ")"
                for i in ordered[:8] if i in scores
            )
            print(f"[focus-rank] top: {preview}", flush=True)
        except Exception:
            pass
        return ordered

    # Fallback: legacy word-overlap ranking.
    return _rank_indices_by_jaccard(
        indices, articles, search_context, min_keep=min_keep,
    )


def _rpa_analyze_topics(wf: dict, filename: str) -> dict:
    """RPA: runTopicRadar() → POST /analyze-topics."""
    cluster = wf.get("clusterParams") or {}
    max_topics = int(wf.get("maxTopics") or 15)
    excluded = _excluded_sources_for_automation(wf, filename)
    search_context = _export_search_context(wf)
    themes = cluster.get("themes") or wf.get("topicThemes")
    theme = cluster.get("theme") or wf.get("topicTheme") or "all"
    return _analyze_topics_file(
        filename,
        max_topics=max_topics,
        excluded_sources=excluded,
        depth=int(cluster.get("depth") or 5),
        theme=theme,
        news_weight=cluster.get("news_weight") or wf.get("topicWeight") or "balanced",
        search_context=search_context,
        themes=themes,
    )


def _rpa_generate_article_topics(
    wf: dict,
    pipeline: dict,
    filename: str,
    topics: list,
    max_drafts: int = 10,
) -> list[dict]:
    """RPA: generateArticleTopics() → POST /generate-article-topics."""
    topics_checked = wf.get("topicsChecked") or {}
    cap = min(40, max(1, int(max_drafts)))
    # Rank on a WIDER candidate pool than the final cap so the importance ranker
    # can pick the best stories across all clusters — not just a round-robin subset
    # that was already truncated to `cap` before scoring.
    pool = min(60, max(cap * 3, 30))
    selected_indices = _indices_from_checked_topics(topics, topics_checked, pool)
    if not selected_indices:
        selected_indices = _collect_article_indices(topics, pool)
    if not selected_indices:
        raise RuntimeError("Нет статей для генерации тем постов")

    from twitter_poster import generate_article_topics, load_articles
    search_context = _export_search_context(wf)
    arts = load_articles(str(BASE_DIR / "exports" / filename))
    selected_indices = _rank_indices_by_relevance(
        selected_indices, arts, search_context, min_keep=min(3, cap),
    )
    if not selected_indices:
        raise RuntimeError("Нет релевантных статей для тем постов (вне фокуса экспорта)")
    # Strict top-N: keep only the most important/on-focus articles after ranking.
    selected_indices = selected_indices[:cap]
    raw = generate_article_topics(
        str(BASE_DIR / "exports" / filename),
        selected_indices=selected_indices,
        max_articles=min(cap, len(selected_indices)),
        lang=_workflow_lang(wf, pipeline),
        search_context=search_context,
    )
    return [
        {
            "article_index": t.get("article_index"),
            "title": t.get("title") or "",
            "source": t.get("source") or "",
            "post_topic": t.get("post_topic") or "",
            "angle": t.get("angle") or "",
            "hook": t.get("hook") or "",
        }
        for t in (raw or [])
    ]


def _rpa_generate_drafts(
    wf: dict,
    pipeline: dict,
    filename: str,
    topics: list,
    article_topics_list: list[dict] | None,
    excluded: list | None,
) -> dict:
    """RPA: generateDrafts() → POST /generate-drafts."""
    from twitter_poster import generate_drafts_for_file

    max_drafts = int(pipeline.get("auto_max_drafts") or wf.get("writerMax") or 10)
    writer = wf.get("writerParams") or {}
    post_format = (wf.get("postFormat") or writer.get("post_format") or "short").strip().lower()
    if post_format not in ("short", "brief"):
        post_format = "short"
    params = {
        "cynicism": int(writer.get("cynicism", 5)),
        "harsh": int(writer.get("harsh", 5)),
        "length": int(writer.get("length", 2)),
        "depth": int(writer.get("depth", 5)),
        "numbers": max(0, min(2, int(writer.get("numbers", wf.get("numbersMode", 1))))),
        "ai_model": wf.get("aiModel") or "claude",
        "lang": _workflow_lang(wf, pipeline),
        "persona": wf.get("persona") or "neutral",
        "post_format": post_format,
        "include_link": bool(writer.get("includeLink", False)),
    }

    search_context = _export_search_context(wf)

    selected_topics: list[dict] = []
    if article_topics_list:
        at_checked = wf.get("articleTopicsChecked") or {}
        if at_checked:
            selected_topics = [
                t for i, t in enumerate(article_topics_list)
                if _topic_is_checked(at_checked, i)
            ]
        else:
            selected_topics = list(article_topics_list)

    selected_indices = None
    article_topics_payload = None
    if selected_topics:
        selected_topics = selected_topics[:max_drafts]
        selected_indices = [
            t["article_index"] for t in selected_topics
            if t.get("article_index") is not None
        ]
        article_topics_payload = selected_topics
        topic_map = {}
        for t in selected_topics:
            idx = t.get("article_index")
            if idx is None:
                continue
            topic_map[int(idx)] = {
                "post_topic": t.get("post_topic") or "",
                "angle": t.get("angle") or "",
                "hook": t.get("hook") or "",
                "title": t.get("title") or "",
            }
        params["article_topics"] = topic_map
    else:
        topics_checked = wf.get("topicsChecked") or {}
        selected_indices = _indices_from_checked_topics(topics, topics_checked, max_drafts)
        if not selected_indices:
            selected_indices = _collect_article_indices_from_workflow(topics, max_drafts, wf)

    if search_context and selected_indices:
        from twitter_poster import load_articles
        arts = load_articles(str(BASE_DIR / "exports" / filename))
        selected_indices = _rank_indices_by_relevance(
            selected_indices, arts, search_context, min_keep=min(3, max_drafts),
        )

    return generate_drafts_for_file(
        str(BASE_DIR / "exports" / filename),
        max_drafts,
        params=params,
        selected_indices=selected_indices,
        excluded_sources=excluded,
        article_topics_list=article_topics_payload,
        strict_selection=bool(article_topics_payload or selected_indices),
    )


def _rpa_init_sources(wf: dict, filename: str) -> None:
    """RPA: fetchSourcesForFile() → POST /sources."""
    entry = _build_file_source_state_entry(filename, wf)
    wf.setdefault("fileSourceState", {})[filename] = entry
    wf["workflowFile"] = filename


def _analyze_topics_file(
    filename: str,
    max_topics: int = 15,
    excluded_sources: list | None = None,
    depth: int = 5,
    theme: str = "all",
    news_weight: str = "balanced",
    search_context: str = "",
    themes: list | None = None,
) -> dict:
    import re as _re
    import anthropic
    from twitter_poster import load_articles_with_stats

    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        raise FileNotFoundError(f"File not found: {filename}")

    all_articles, dedupe_stats = load_articles_with_stats(str(rtf_path))
    if not all_articles:
        raise RuntimeError("No articles in export")

    articles = list(enumerate(all_articles))
    if excluded_sources:
        excl_lower = {s.strip().lower() for s in excluded_sources if s.strip()}
        articles = [
            (i, a) for i, a in articles
            if (a.get("source") or "").strip().lower() not in excl_lower
        ]
    if not articles:
        raise RuntimeError("No articles after source filter")

    sample = articles[:500]
    if depth >= 7:
        titles = [f"{i}. [{a.get('source', '')[:40]}] {a['title'][:100]}" for i, a in sample]
    else:
        titles = [f"{i}. {a['title'][:100]}" for i, a in sample]
    titles_text = "\n".join(titles)
    param_block = _topics_cluster_instructions(
        depth, theme, news_weight, max_topics, themes=themes,
    )
    try:
        from twitter_poster import _focus_prompt_block
        profile_block = _focus_prompt_block(search_context)
    except Exception:
        profile_block = f"Export search: {search_context}\n" if search_context else ""
    focus_block = ""
    if profile_block:
        focus_block = f"""
{profile_block}
Clustering MUST follow this focus:
- Top clusters must align with the user's INTERESTS above.
- Niche/local/regional/military/humanitarian items with no clear business/tech/finance link → cluster «Вне фокуса» or «Прочие», never the largest group.
- Prefer industry, tech, business, markets, finance stories that match the focus.
"""

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = f"""You have {len(sample)} news article titles from a Factiva export (indices refer to positions in the full export).
Cluster them into up to {max_topics} most significant topics/themes.
{focus_block}
Clustering parameters:
{param_block}

Article titles:
{titles_text}

Rules:
- Every article index must appear in exactly one topic.
- Sort topics by count descending.
- Topic name: 3-6 words, concise, in Russian.
- Summary: one short sentence in Russian explaining why this cluster matters.

Return a JSON array ONLY — no commentary, no markdown fences:
[{{"topic":"...","count":N,"article_indices":[...],"summary":"..."}}]"""

    r = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        system="You output only raw JSON arrays. No markdown, no explanation, no code fences.",
        temperature=0.1,
    )
    raw = r.content[0].text.strip()
    match = _re.search(r"\[[\s\S]*\]", raw)
    if not match:
        raise RuntimeError(f"Claude returned non-JSON: {raw[:300]}")
    topics = json.loads(match.group(0))

    covered = set(i for t in topics for i in t.get("article_indices", []))
    remaining = [i for i, _ in articles if i not in covered]
    if remaining:
        topics.append({
            "topic": "Прочие статьи",
            "count": len(remaining),
            "article_indices": remaining,
            "summary": f"Статьи, не вошедшие в основные кластеры ({len(remaining)} шт.).",
        })
    topics.sort(key=lambda t: t.get("count", 0), reverse=True)

    return {
        "topics": topics,
        "total_articles": len(articles),
        "filtered_from": len(all_articles),
        "raw_total": dedupe_stats.get("raw_total", len(all_articles)),
        "removed_duplicates": dedupe_stats.get("removed_duplicates", 0),
    }


def _collect_diverse_article_indices(
    topics: list,
    max_drafts: int,
    topics_checked: dict | None = None,
) -> list[int]:
    """Pick up to max_drafts articles, round-robin across topic clusters."""
    use_checks = bool(
        topics_checked
        and any(_topic_is_checked(topics_checked, i) for i in range(len(topics)))
    )
    topic_lists: list[list[int]] = []
    for i, t in enumerate(topics):
        if use_checks and not _topic_is_checked(topics_checked, i):
            continue
        arts = [int(x) for x in (t.get("article_indices") or [])]
        if arts:
            topic_lists.append(arts)
    if not topic_lists:
        topic_lists = [
            [int(x) for x in (t.get("article_indices") or [])]
            for t in topics
            if t.get("article_indices")
        ]
    if not topic_lists:
        return []

    indices: list[int] = []
    seen: set[int] = set()
    max_depth = max(len(a) for a in topic_lists)
    for depth in range(max_depth):
        for arts in topic_lists:
            if depth >= len(arts):
                continue
            idx = arts[depth]
            if idx in seen:
                continue
            seen.add(idx)
            indices.append(idx)
            if len(indices) >= max_drafts:
                return indices
    return indices


def _collect_article_indices(topics: list, max_drafts: int) -> list[int]:
    return _collect_diverse_article_indices(topics, max_drafts)


def _append_publisher_items(pid: str, items: list[dict]) -> int:
    with _pipelines_lock:
        pdata = _load_pipelines()
        pipeline = _find_pipeline(pdata, pid)
        if not pipeline:
            raise RuntimeError("pipeline not found")
        default_platform = pipeline.get("default_platform", "telegram")

    new_items = []
    for raw in items:
        text = (raw.get("text") or "").strip()
        scheduled_at = (raw.get("scheduledAt") or "").strip()
        if not text or not scheduled_at:
            continue
        platform = (raw.get("platform") or default_platform).strip().lower()
        if platform not in ("telegram", "twitter"):
            platform = default_platform
        new_items.append(_normalize_stack_item({
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "scheduledAt": scheduled_at,
            "platform": platform,
            "status": "pending",
            "pipeline": pid,
            "messageId": None,
            "url": "",
            "channel": "",
            "error": "",
            "articleTitle": (raw.get("articleTitle") or raw.get("title") or "").strip(),
            "articleUrl": (raw.get("articleUrl") or "").strip(),
            "articleSource": (raw.get("articleSource") or raw.get("source") or "").strip(),
            "articleIndex": raw.get("articleIndex"),
        }))
    if not new_items:
        return 0
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        stack.extend(new_items)
        _save_publisher_stack(stack)
    return len(new_items)


def _merge_workflow_factiva_dom(pid: str, factiva_dom: dict) -> None:
    if not isinstance(factiva_dom, dict) or not factiva_dom:
        return
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return
        wf = dict(p.get("workflow") or {})
        wf["factivaDom"] = {**(wf.get("factivaDom") or {}), **factiva_dom}
        p["workflow"] = wf
        _save_pipelines(pdata)


def _reload_pipeline_state(pid: str) -> tuple[dict, dict]:
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            raise RuntimeError(f"Pipeline {pid} not found")
        pipeline = dict(p)
    return pipeline, dict(pipeline.get("workflow") or {})


def _execute_pipeline_automation(pipeline: dict, slot_key: str | None = None):
    """Full RPA conveyor: each step mirrors manual module execution.

    Before scheduling, drafts are checked for novelty against already published
    posts. If the batch is stale, re-imports from TechCrunch/Factiva and retries.
    """
    pid = pipeline["id"]
    with _pipelines_lock:
        pdata = _load_pipelines()
        fresh = _find_pipeline(pdata, pid)
        if fresh:
            if fresh.get("auto_refresh_requested"):
                fresh["auto_refresh_requested"] = False
                fresh["auto_refresh_reason"] = ""
                _save_pipelines(pdata)
            pipeline = dict(fresh)

    wf = dict(pipeline.get("workflow") or {})
    if slot_key:
        _mark_auto_slot(pid, slot_key)

    source_mode = (pipeline.get("source_mode") or wf.get("sourceMode") or "factiva").lower()
    default_platform = pipeline.get("default_platform", "telegram")
    if default_platform not in ("telegram", "twitter"):
        default_platform = "telegram"

    max_drafts = int(pipeline.get("auto_max_drafts") or wf.get("writerMax") or 10)
    min_novel = max(1, min(max_drafts, max(3, max_drafts // 2)))
    max_attempts = 3
    total_stale_skipped = 0
    refresh_attempts = 0

    try:
        from twitter_poster import load_articles

        drafts: list[dict] = []
        filename = ""
        total_articles = 0
        topics_result: dict = {}

        for attempt in range(max_attempts):
            force_fresh = True  # never reuse stale TC cache in automation
            refresh_attempts = attempt

            # ── 01 Export ──
            _check_automation_cancel()
            pipeline, wf = _reload_pipeline_state(pid)
            _set_pipeline_auto_status(
                pid, "export", ok=True,
                extra={"phase": "running", "attempt": attempt + 1, "force_fresh": force_fresh},
            )
            if source_mode == "techcrunch":
                filename = _run_techcrunch_export_blocking(
                    wf,
                    force_fresh=force_fresh,
                    exclude_published_pid=pid,
                    exclude_platform=default_platform,
                )
            else:
                factiva_dom = wf.get("factivaDom") or {}
                _validate_factiva_dom(factiva_dom)
                filename = _run_factiva_export_blocking(factiva_dom)
                try:
                    arts = load_articles(str(BASE_DIR / "exports" / filename))
                    kept, excl_n = _filter_articles_excluding_published(arts, pid, default_platform)
                    if excl_n and kept:
                        from techcrunch_feed import save_articles_json
                        path = save_articles_json(
                            kept[: max(max_drafts * 2, len(kept))],
                            BASE_DIR / "exports",
                            feed_key="factiva",
                            out_name=(wf.get("factivaDom") or {}).get("outName") or "factiva_export",
                        )
                        filename = path.name
                        total_stale_skipped += excl_n
                    elif excl_n and not kept:
                        if attempt < max_attempts - 1:
                            _set_pipeline_auto_status(
                                pid, "export", ok=True,
                                extra={
                                    "phase": "retry",
                                    "warning": "Все статьи Factiva уже публиковались — повторный импорт",
                                    "attempt": attempt + 1,
                                },
                            )
                            continue
                        raise NoNewArticles(
                            "Все статьи Factiva уже публиковались — "
                            "новых материалов пока нет. Следующий слот попробует снова."
                        )
                except Exception:
                    pass

            export_count = len(load_articles(str(BASE_DIR / "exports" / filename)))
            wf["lastExportArticleCount"] = export_count
            wf["workflowFile"] = filename
            export_extra = {
                "file": filename,
                "articles_in_export": export_count,
                "phase": "done",
                "attempt": attempt + 1,
            }
            if source_mode == "techcrunch":
                meta = wf.get("tcExportMeta") or {}
                export_extra["tc_exported"] = meta.get("exported")
                export_extra["tc_rss_fetched"] = meta.get("rss_fetched")
                export_extra["excluded_published"] = meta.get("excluded_published", 0)
                req_p = meta.get("date_preset_requested")
                used_p = meta.get("date_preset_used")
                if req_p and used_p and req_p != used_p:
                    export_extra["warning"] = (
                        f"Период «{req_p}» дал мало статей — взята лента за «{used_p}» "
                        f"({export_count} из {meta.get('max_requested', '?')})"
                    )
            _set_pipeline_auto_status(pid, "export", ok=True, extra=export_extra)

            # ── 02 Sources ──
            _check_automation_cancel()
            _set_pipeline_auto_status(
                pid, "sources", ok=True,
                extra={"phase": "running", "articles_in_export": export_count},
            )
            _rpa_init_sources(wf, filename)
            _save_pipeline_workflow(pid, wf)
            excluded = _excluded_sources_for_automation(wf, filename)

            # ── 03 Topics ──
            _check_automation_cancel()
            _set_pipeline_auto_status(
                pid, "topics", ok=True,
                extra={"phase": "running", "articles_in_export": export_count},
            )
            topics_result = _rpa_analyze_topics(wf, filename)
            topics = topics_result.get("topics") or []
            if not topics:
                raise RuntimeError("Темы не сгенерированы")
            wf["topics"] = topics
            wf["topicsChecked"] = {str(i): True for i in range(len(topics))}
            wf["topicsFilteredTotal"] = topics_result.get("total_articles", 0)
            wf["topicsFilteredFrom"] = topics_result.get("filtered_from", 0)
            wf["topicsRawTotal"] = topics_result.get("raw_total", 0)
            wf["topicsRemovedDupes"] = topics_result.get("removed_duplicates", 0)
            _save_pipeline_workflow(pid, wf)

            # ── 04 Article topics ──
            article_topics_list = None
            if pipeline.get("auto_run_article_topics", True):
                _check_automation_cancel()
                _set_pipeline_auto_status(pid, "article_topics", ok=True, extra={"phase": "running"})
                article_topics_list = _rpa_generate_article_topics(wf, pipeline, filename, topics, max_drafts)
                wf["articleTopics"] = article_topics_list
                wf["articleTopicsChecked"] = {str(i): True for i in range(len(article_topics_list))}
                _save_pipeline_workflow(pid, wf)

            # ── 05 Writer ──
            _check_automation_cancel()
            _set_pipeline_auto_status(pid, "drafts", ok=True, extra={"phase": "running"})
            draft_result = _rpa_generate_drafts(
                wf, pipeline, filename, topics, article_topics_list, excluded,
            )
            total_articles = int(
                wf.get("topicsFilteredTotal") or topics_result.get("total_articles") or 0
            )
            drafts = draft_result.get("drafts") or []
            if not drafts:
                if attempt < max_attempts - 1:
                    continue
                raise RuntimeError("Черновики не сгенерированы")

            # ── Novelty gate ──
            novel, stale_n = _filter_novel_drafts(pid, drafts, default_platform)
            total_stale_skipped += stale_n
            if len(novel) >= min_novel or (novel and attempt == max_attempts - 1):
                drafts = novel
                break

            if not novel and attempt == max_attempts - 1:
                drafts = []
                break

            _set_pipeline_auto_status(
                pid, "export", ok=True,
                extra={
                    "phase": "retry",
                    "attempt": attempt + 1,
                    "stale_drafts": stale_n,
                    "novel_drafts": len(novel),
                    "warning": (
                        f"Пачка устарела ({stale_n} уже публиковались) — "
                        f"повторный импорт из {'TechCrunch' if source_mode == 'techcrunch' else 'Factiva'}"
                    ),
                },
            )
            wf["tcArticles"] = []

        if not drafts:
            _set_pipeline_auto_status(
                pid, "done", ok=True,
                extra={
                    "file": filename,
                    "drafts": 0,
                    "scheduled": 0,
                    "skipped_duplicates": total_stale_skipped,
                    "platform": default_platform,
                    "refresh_attempts": refresh_attempts + 1,
                    "warning": (
                        "Нет новых постов для публикации — все статьи уже были в ленте. "
                        "Попробуйте позже или смените источник/период."
                    ),
                },
            )
            return

        wf["drafts"] = drafts
        wf["platform"] = default_platform
        _save_pipeline_workflow(pid, wf)

        # ── 06 Publisher ──
        _check_automation_cancel()
        slots = _compute_publish_slots(pipeline, len(drafts))
        if not slots:
            raise RuntimeError("Нет слотов публикации — задайте расписание в MSK")

        items = []
        for i, d in enumerate(drafts):
            text = (d.get("draft") or "").strip()
            if not text:
                continue
            items.append({
                "text": text,
                "scheduledAt": slots[min(i, len(slots) - 1)],
                "platform": default_platform,
                "articleTitle": d.get("title") or "",
                "articleUrl": d.get("url") or "",
                "articleSource": d.get("source") or "",
                "articleIndex": d.get("article_index"),
                **(
                    {"status": "failed", "error": tw_err}
                    if (tw_err := _twitter_length_error(text, default_platform))
                    else {}
                ),
            })

        items, skipped_dupes = _filter_duplicate_publisher_items(pid, items)
        total_stale_skipped += skipped_dupes
        if not items:
            _set_pipeline_auto_status(
                pid, "done", ok=True,
                extra={
                    "file": filename,
                    "drafts": len(drafts),
                    "scheduled": 0,
                    "skipped_duplicates": total_stale_skipped,
                    "platform": default_platform,
                    "warning": "Все черновики уже в очереди / публиковались — повтор не добавлен",
                },
            )
            return

        _set_pipeline_auto_status(
            pid, "schedule", ok=True,
            extra={"phase": "running", "drafts": len(items)},
        )
        added = _append_publisher_items(pid, items)
        extra = {
            "file": filename,
            "drafts": len(drafts),
            "scheduled": added,
            "platform": default_platform,
            "articles_in_export": total_articles,
            "refresh_attempts": refresh_attempts + 1,
        }
        if total_stale_skipped:
            extra["skipped_duplicates"] = total_stale_skipped
        if refresh_attempts > 0:
            extra["warning"] = (
                f"После повторного импорта запланировано {added} новых постов "
                f"(отфильтровано старых: {total_stale_skipped})"
            )
        elif total_articles < max_drafts:
            extra["warning"] = (
                f"В экспорте только {total_articles} статей — "
                f"для пачки из {max_drafts} увеличьте период/страницы экспорта"
            )
        _set_pipeline_auto_status(pid, "done", ok=True, extra=extra)

    except AutomationCancelled:
        _set_pipeline_auto_status(pid, "cancelled", ok=False, error="Остановлено пользователем")
    except NoNewArticles as e:
        _set_pipeline_auto_status(
            pid, "done", ok=True,
            extra={
                "drafts": 0,
                "scheduled": 0,
                "skipped_duplicates": True,
                "platform": default_platform,
                "warning": str(e),
            },
        )
    except Exception as e:
        _set_pipeline_auto_status(pid, "error", ok=False, error=str(e))
        raise


def _tick_pipeline_automation():
    with _pipelines_lock:
        pipelines = list(_load_pipelines().get("pipelines", []))

    for p in pipelines:
        if not p.get("auto_enabled"):
            continue

        refresh = bool(p.get("auto_refresh_requested"))
        slot_key, _ = _auto_export_slot_due(p)

        if not refresh and not slot_key:
            continue
        if not refresh and slot_key and _automation_on_cooldown(p):
            _mark_auto_slot(p["id"], slot_key)
            continue
        # Refresh requested by publisher: allow even on cooldown, but not more than once / 30 min
        if refresh and _automation_on_cooldown(p, min_minutes=30):
            with _pipelines_lock:
                pdata = _load_pipelines()
                cur = _find_pipeline(pdata, p["id"])
                if cur:
                    cur["auto_refresh_requested"] = False
                    _save_pipelines(pdata)
            continue

        with _automation_lock:
            if _automation_running:
                return
            _begin_automation_run(p["id"])
        try:
            _execute_pipeline_automation(p, slot_key if not refresh else None)
        except Exception:
            pass
        finally:
            with _automation_lock:
                _end_automation_run()
        return


def _pipeline_automation_loop():
    while True:
        try:
            _tick_pipeline_automation()
        except Exception:
            pass
        threading.Event().wait(60)


threading.Thread(target=_pipeline_automation_loop, daemon=True).start()


# ── Basic Auth ────────────────────────────────────────────────────────────────

def check_auth(username, password):
    u = os.getenv("APP_USER", "admin")
    p = os.getenv("APP_PASS", "factiva")
    return username == u and password == p

def require_auth():
    return Response(
        "Access denied", 401,
        {"WWW-Authenticate": 'Basic realm="Factiva Exporter"'}
    )

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return require_auth()
        return f(*args, **kwargs)
    return decorated

# Apply auth to all routes
@app.before_request
def before_request():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return require_auth()

# Active job state (one job at a time)
_job_lock  = threading.Lock()
_job_queue = queue.Queue()   # log lines from subprocess
_job_proc  = None            # current subprocess
_job_done  = False
_job_out   = ""              # output file path when done


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Read saved searches from .env / pre-populate output dir
    out_dir = str(BASE_DIR / "exports")
    return render_template("index.html", out_dir=out_dir)


@app.route("/saved-searches")
def saved_searches():
    """Return list of saved search names from Factiva (quick async call)."""
    # We can't call Factiva here without a browser — return cached or empty.
    # The user can type the name manually; this just provides autocomplete.
    cache_file = BASE_DIR / ".saved_searches_cache.json"
    if cache_file.exists():
        return jsonify(json.loads(cache_file.read_text(encoding="utf-8")))
    return jsonify([])


@app.route("/run", methods=["POST"])
def run_job():
    global _job_proc, _job_done, _job_out

    with _job_lock:
        if _job_proc and _job_proc.poll() is None:
            return jsonify({"error": "A job is already running"}), 409

        data = request.json
        args = _build_args(data)
        if isinstance(args, str):           # error message
            return jsonify({"error": args}), 400

        _job_done = False
        _job_out  = ""
        # drain old queue
        while not _job_queue.empty():
            try:
                _job_queue.get_nowait()
            except queue.Empty:
                break

        _job_proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE_DIR / "factiva_agent.py")] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(BASE_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        def _reader():
            global _job_done, _job_out
            for line in _job_proc.stdout:
                _job_queue.put(line.rstrip())
            rc = _job_proc.wait()
            _job_done = True
            # Only set output path if job succeeded AND file exists
            try:
                out_idx = args.index("--out") + 1
                candidate = args[out_idx]
                from pathlib import Path as _P
                if rc == 0 and _P(candidate).exists():
                    _job_out = candidate
                else:
                    _job_out = ""
                    if rc != 0:
                        _job_queue.put(f"[!] Job exited with code {rc}")
            except (ValueError, IndexError):
                _job_out = ""

        threading.Thread(target=_reader, daemon=True).start()

    return jsonify({"status": "started"})


@app.route("/stop", methods=["POST"])
def stop_job():
    global _job_proc
    if _job_proc and _job_proc.poll() is None:
        _job_proc.terminate()
        return jsonify({"status": "stopped"})
    return jsonify({"status": "not running"})


@app.route("/stream")
def stream():
    """SSE endpoint — pushes log lines to the browser."""
    def _generate():
        while True:
            try:
                line = _job_queue.get(timeout=0.4)
                yield f"data: {json.dumps(line)}\n\n"
            except queue.Empty:
                if _job_done and _job_queue.empty():
                    if _job_out:
                        yield f"data: {json.dumps('__DONE__' + _job_out)}\n\n"
                    else:
                        yield f"data: {json.dumps('__ERROR__')}\n\n"
                    return
                yield ": ping\n\n"   # keep-alive

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _normalize_topic_themes(theme: str | None = None, themes: list | None = None) -> list[str]:
    """Return selected theme slugs; empty list means «all»."""
    valid = {"economics", "markets", "corporate", "tech", "policy", "society"}
    if themes and isinstance(themes, list):
        picked = []
        seen: set[str] = set()
        for raw in themes:
            t = str(raw or "").strip().lower()
            if t in valid and t not in seen:
                picked.append(t)
                seen.add(t)
        if picked:
            return picked
    t = (theme or "").strip().lower()
    if t and t != "all" and t in valid:
        return [t]
    return []


def _topics_cluster_instructions(depth, theme, news_weight, max_topics, themes=None):
    """Build Claude prompt block for topic clustering parameters."""
    depth = max(1, min(10, int(depth or 5)))
    news_weight = (news_weight or "balanced").strip().lower()
    theme_list = _normalize_topic_themes(theme, themes)

    if depth <= 3:
        depth_line = (
            "Глубина низкая: объединяй близкие темы в широкие кластеры, "
            "не дроби на мелкие подтемы — лучше меньше крупных групп."
        )
    elif depth <= 6:
        depth_line = (
            "Глубина средняя: баланс между широкими темами и заметными подтемами."
        )
    elif depth <= 8:
        depth_line = (
            "Глубина высокая: выделяй отдельные кластеры для смежных, но различимых сюжетов; "
            "не сливай разные углы одной истории."
        )
    else:
        depth_line = (
            "Глубина максимальная: максимально детальная кластеризация — "
            f"стремись к {max_topics} осмысленным кластерам, различай нюансы и вторичные сюжеты."
        )

    theme_guides = {
        "all": "Тематика: все направления — не отдавай предпочтение одной отрасли.",
        "economics": (
            "Тематика: макроэкономика — ВВП, инфляция, занятость, торговля, "
            "фискальная и монетарная политика, экономические прогнозы."
        ),
        "markets": (
            "Тематика: рынки и финансы — акции, облигации, валюты, commodities, "
            "индексы, ликвидность, инвестиционные потоки."
        ),
        "corporate": (
            "Тематика: корпоративный бизнес — M&A, отчётность, стратегии, "
            "руководство, скандалы, конкуренция компаний."
        ),
        "tech": (
            "Тематика: технологии — ИИ, стартапы, кибербезопасность, "
            "цифровизация, платформы, инновации."
        ),
        "policy": (
            "Тематика: политика и регулирование — законы, санкции, геополитика, "
            "решения центробанков и регуляторов."
        ),
        "society": (
            "Тематика: общество и публицистика — труд, демография, потребители, "
            "ESG, культура бизнеса, социальные тренды."
        ),
    }

    weight_guides = {
        "industry": (
            "Приоритет новостей — отраслевой вес: выделяй события, значимые для отрасли "
            "(крупные сделки, регуляторные решения, отчёты лидеров, сдвиги рынка). "
            "Мелкие глянцевые заметки — в «Прочие» или не выделяй отдельными топ-кластерами."
        ),
        "balanced": (
            "Приоритет новостей — баланс: сочетай отраслево значимые события "
            "и второстепенные, но интересные для публицистики сюжеты."
        ),
        "commentary": (
            "Приоритет новостей — для размышлений: ищи нишевые отрасли, неочевидные углы, "
            "human-interest в бизнес-контексте; истории с пространством для мнения. "
            "Крупные рыночные ходы отметь, но не доминируй ими — избегай «шумных» однотипных кластеров."
        ),
    }

    theme_line = theme_guides.get("all")
    if theme_list:
        if len(theme_list) == 1:
            theme_line = theme_guides.get(theme_list[0], theme_guides["all"])
        else:
            theme_line = (
                "Тематика — выбрано несколько направлений, учитывай все:\n"
                + "\n".join(f"  • {theme_guides.get(t, t)}" for t in theme_list)
            )
    weight_line = weight_guides.get(news_weight, weight_guides["balanced"])

    lines = [depth_line, theme_line, weight_line]
    if theme_list:
        lines.append(
            "Статьи вне выбранных тематик не включай в основные кластеры — "
            "собери их в один кластер «Вне тематики» или «Прочие статьи»."
        )
    return "\n".join(f"- {line}" for line in lines)


@app.route("/analyze-topics", methods=["POST"])
def analyze_topics():
    """Parse RTF, cluster article titles into themes via Claude, return topic list."""
    data = request.json or {}
    filename = Path(data.get("file", "")).name
    if not filename:
        return jsonify({"error": "No file specified"}), 400

    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        return jsonify({"error": "File not found"}), 404

    max_topics = int(data.get("max_topics", 15))
    excluded_sources = data.get("excluded_sources", None)
    depth = int(data.get("depth", 5))
    theme = data.get("theme", "all")
    themes = data.get("themes")
    news_weight = data.get("news_weight", "balanced")
    search_context = (data.get("search_context") or "").strip()

    try:
        result = _analyze_topics_file(
            filename,
            max_topics=max_topics,
            excluded_sources=excluded_sources,
            depth=depth,
            theme=theme,
            news_weight=news_weight,
            search_context=search_context,
            themes=themes,
        )
        norm_themes = _normalize_topic_themes(theme, themes)
        result["params"] = {
            "depth": depth,
            "theme": theme if not norm_themes else norm_themes[0],
            "themes": norm_themes or ["all"],
            "news_weight": news_weight,
            "search_context": search_context,
        }
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-500:]}), 500


@app.route("/generate-article-topics", methods=["POST"])
def generate_article_topics_route():
    """Read full articles and propose conscious post topics for selected cluster articles."""
    data = request.json or {}
    filename = Path(data.get("file", "")).name
    if not filename:
        return jsonify({"error": "No file specified"}), 400

    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        return jsonify({"error": "File not found"}), 404

    selected_indices = data.get("selected_indices", None)
    max_articles = int(data.get("max", 40))
    lang = data.get("lang", "en")
    search_context = (data.get("search_context") or "").strip()

    try:
        from twitter_poster import generate_article_topics
        topics = generate_article_topics(
            str(rtf_path),
            selected_indices=selected_indices,
            max_articles=max_articles,
            lang=lang,
            search_context=search_context,
        )
        return jsonify({"article_topics": topics, "total": len(topics)})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-400:]}), 500


@app.route("/generate-drafts", methods=["POST"])
def generate_drafts():
    """Parse RTF and generate tweet drafts via Claude API."""
    data = request.json or {}
    filename = Path(data.get("file", "")).name
    if not filename:
        return jsonify({"error": "No file specified"}), 400

    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        return jsonify({"error": "File not found"}), 404

    max_articles = int(data.get("max", 30))
    # Optional filter: only generate from specific article indices
    selected_indices = data.get("selected_indices", None)  # list[int] or null
    # Optional source exclusion list
    excluded_sources = data.get("excluded_sources", None)  # list[str] or null
    # Optional map: article_index -> {post_topic, angle, hook, title?}
    article_topics = data.get("article_topics", None)
    # Preferred: ordered list of selected post topics with titles for resilient resolve
    article_topics_list = data.get("article_topics_list", None)
    strict_selection = data.get("strict_selection")
    if strict_selection is None:
        strict_selection = bool(article_topics_list) or (
            selected_indices is not None and len(selected_indices) > 0
        )

    params = {
        "cynicism": int(data.get("cynicism", 5)),
        "harsh":    int(data.get("harsh", 5)),
        "length":   int(data.get("length", 1)),
        "depth":    int(data.get("depth", 5)),
        "numbers":  int(data["numbers"]) if data.get("numbers") is not None else 1,
        "ai_model": data.get("ai_model", "claude"),
        "lang":     data.get("lang", "en"),
        "persona":  data.get("persona", "neutral"),
        "post_format": data.get("post_format", "short"),
        "include_link": bool(data.get("include_link", False)),
    }
    # Clamp numbers mode 0..2 (0 = no figures)
    params["numbers"] = max(0, min(2, params["numbers"]))
    if article_topics:
        params["article_topics"] = article_topics

    try:
        from twitter_poster import generate_drafts_for_file
        result = generate_drafts_for_file(
            str(rtf_path), max_articles, params=params,
            selected_indices=selected_indices,
            excluded_sources=excluded_sources,
            article_topics_list=article_topics_list,
            strict_selection=strict_selection,
        )
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-400:]}), 500


@app.route("/refine-draft", methods=["POST"])
def refine_draft():
    """Rewrite or enhance a single generated post."""
    data = request.json or {}
    action = data.get("action", "rewrite")
    if action not in ("rewrite", "numbers", "lengthen", "enrich"):
        return jsonify({"error": "Unknown action"}), 400

    filename = Path(data.get("file", "")).name
    if not filename:
        return jsonify({"error": "No file specified"}), 400
    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        return jsonify({"error": "File not found"}), 404

    text = data.get("text", "").strip()
    title = data.get("title", "").strip()
    if not text or not title:
        return jsonify({"error": "Missing text or title"}), 400

    params = {
        "cynicism": int(data.get("cynicism", 5)),
        "harsh": int(data.get("harsh", 5)),
        "length": int(data.get("length", 2)),
        "depth": int(data.get("depth", 5)),
        "numbers": int(data["numbers"]) if data.get("numbers") is not None else 1,
        "ai_model": data.get("ai_model", "claude"),
        "lang": data.get("lang", "en"),
        "persona": data.get("persona", "neutral"),
        "post_format": data.get("post_format", "short"),
        "include_link": bool(data.get("include_link", False)),
    }
    params["numbers"] = max(0, min(2, params["numbers"]))

    try:
        from twitter_poster import refine_draft_post
        result = refine_draft_post(
            action,
            str(rtf_path),
            title,
            data.get("source", ""),
            text,
            params=params,
        )
        return jsonify({"text": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/post-tweet", methods=["POST"])
def post_tweet_route():
    """Post a single tweet."""
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Tweet text is empty"}), 400
    if len(text) > 280:
        return jsonify({"error": f"Tweet too long ({len(text)} chars, max 280)"}), 400

    try:
        from twitter_poster import post_tweet
        result = post_tweet(text)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/edit-post", methods=["POST"])
def edit_post_route():
    """Edit a published post on Twitter or Telegram."""
    data = request.json or {}
    platform = (data.get("platform") or "").strip().lower()
    message_id = data.get("id") or data.get("message_id")
    text = (data.get("text") or "").strip()
    if not message_id:
        return jsonify({"error": "No message id"}), 400
    if not text:
        return jsonify({"error": "Text is empty"}), 400
    try:
        if platform == "telegram":
            from telegram_poster import edit_telegram
            result = edit_telegram(int(message_id), text, data.get("channel"))
            return jsonify(result)
        if platform == "twitter":
            if len(text) > 280:
                return jsonify({"error": f"Tweet too long ({len(text)} chars, max 280)"}), 400
            from twitter_poster import edit_tweet
            result = edit_tweet(str(message_id), text)
            return jsonify(result)
        return jsonify({"error": "Unknown platform"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/delete-post", methods=["POST"])
def delete_post_route():
    """Delete a published post on Twitter or Telegram."""
    data = request.json or {}
    platform = (data.get("platform") or "").strip().lower()
    message_id = data.get("id") or data.get("message_id")
    if not message_id:
        return jsonify({"error": "No message id"}), 400
    try:
        if platform == "telegram":
            from telegram_poster import delete_telegram
            result = delete_telegram(int(message_id), data.get("channel"))
            return jsonify(result)
        if platform == "twitter":
            from twitter_poster import delete_tweet
            result = delete_tweet(str(message_id))
            return jsonify(result)
        return jsonify({"error": "Unknown platform"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/schedule-tweets", methods=["POST"])
def schedule_tweets():
    """Add tweets to the scheduled queue."""
    data = request.json or {}
    texts    = data.get("texts", [])
    schedule = data.get("schedule", {})
    mode     = schedule.get("mode", "now")

    if not texts:
        return jsonify({"error": "No texts provided"}), 400

    now = datetime.utcnow()
    scheduled_times = []

    if mode == "interval":
        interval_min = int(schedule.get("interval_minutes", 120))
        start_str    = schedule.get("start_time", "09:00")
        h, m = map(int, start_str.split(":"))
        # First slot: today at start_time, or next occurrence if past
        base = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=1)
        for i in range(len(texts)):
            scheduled_times.append(base + timedelta(minutes=interval_min * i))

    elif mode == "schedule":
        times = schedule.get("times", ["09:00", "13:00", "18:00"])
        slots = []
        for t in times:
            h, m = map(int, t.split(":"))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            slots.append(dt)
        slots.sort()
        # Cycle through slots if more texts than slots
        for i in range(len(texts)):
            slot = slots[i % len(slots)]
            if i >= len(slots):
                slot += timedelta(days=i // len(slots))
            scheduled_times.append(slot)
    else:
        return jsonify({"error": "Use mode=interval or mode=schedule"}), 400

    with _tweet_queue_lock:
        q = _load_tweet_queue()
        labels = []
        for text, sched_dt in zip(texts, scheduled_times):
            item = {
                "id": str(uuid.uuid4())[:8],
                "text": text,
                "scheduled_at": sched_dt.isoformat(),
                "status": "pending",
            }
            q.append(item)
            labels.append(sched_dt.strftime("%d.%m %H:%M UTC"))
        _save_tweet_queue(q)

    return jsonify({"scheduled": labels, "count": len(labels)})


@app.route("/tweet-queue")
def tweet_queue():
    with _tweet_queue_lock:
        q = _load_tweet_queue()
    # Format for display
    result = []
    for item in sorted(q, key=lambda x: x["scheduled_at"], reverse=False)[-50:]:
        dt = datetime.fromisoformat(item["scheduled_at"])
        result.append({
            "id": item["id"],
            "text": item["text"],
            "scheduled_at": dt.strftime("%d.%m %H:%M UTC"),
            "status": item["status"],
            "url": item.get("url", ""),
        })
    return jsonify({"queue": result})


@app.route("/cancel-tweet/<tweet_id>", methods=["POST"])
def cancel_tweet(tweet_id):
    with _tweet_queue_lock:
        q = _load_tweet_queue()
        for item in q:
            if item["id"] == tweet_id and item["status"] == "pending":
                item["status"] = "cancelled"
        _save_tweet_queue(q)
    return jsonify({"status": "cancelled"})


@app.route("/translate", methods=["POST"])
def translate_text():
    """Translate post text to Russian via Claude, keeping Cinico voice."""
    data = request.json or {}
    text = data.get("text", "").strip()
    target_lang = data.get("lang", "ru")
    if not text:
        return jsonify({"error": "No text"}), 400

    lang_names = {"ru": "Russian", "ua": "Ukrainian", "en": "English"}
    lang_name = lang_names.get(target_lang, "Russian")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        r = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=400,
            system=(
                "You are a translator for sharp, cynical social media posts written in the Cinico style "
                "(anger investor voice — blunt, sardonic, no emojis, no hashtags, no LinkedIn tone). "
                f"Translate the given post to {lang_name}. "
                "Preserve the structure: factual insight on line 1, biting personal comment separated by blank line. "
                "Keep the same punch, rhythm, and cynicism. Do not soften. Return ONLY the translated text."
            ),
            messages=[{"role": "user", "content": text}],
            temperature=0.3,
        )
        return jsonify({"translated": r.content[0].text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/sources", methods=["POST"])
def list_sources():
    """Return unique sources found in RTF, with article counts."""
    data = request.json or {}
    filename = Path(data.get("file", "")).name
    if not filename:
        return jsonify({"error": "No file specified"}), 400
    rtf_path = BASE_DIR / "exports" / filename
    if not rtf_path.exists():
        return jsonify({"error": "File not found"}), 404
    try:
        from twitter_poster import load_articles_with_stats
        articles, stats = load_articles_with_stats(str(rtf_path))
        counts: dict[str, int] = {}
        for a in articles:
            src = (a.get("source") or "").strip()
            if not src:
                src = "(без источника)"
            counts[src] = counts.get(src, 0) + 1
        sources = sorted(
            [{"name": k, "count": v} for k, v in counts.items()],
            key=lambda x: -x["count"]
        )
        return jsonify({
            "sources": sources,
            "total": stats["total"],
            "raw_total": stats["raw_total"],
            "removed_duplicates": stats["removed_duplicates"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/post-telegram", methods=["POST"])
def post_telegram_route():
    """Post a single message to Telegram channel."""
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Text is empty"}), 400
    try:
        from telegram_poster import post_telegram
        result = post_telegram(text)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/publisher-stack", methods=["GET"])
def get_publisher_stack():
    with _publisher_stack_lock:
        return jsonify({"stack": _load_publisher_stack()})


@app.route("/publisher-stack", methods=["PUT"])
def put_publisher_stack():
    data = request.json or {}
    stack = data.get("stack")
    if not isinstance(stack, list):
        return jsonify({"error": "stack required"}), 400
    with _publisher_stack_lock:
        _save_publisher_stack(stack)
    return jsonify({"ok": True, "count": len(stack)})


@app.route("/publisher-stack/process-now", methods=["POST"])
def process_publisher_stack_now():
    """Process all due pending items immediately."""
    stale_pids: set[str] = set()
    stats = {"due": 0, "published": 0, "failed": 0, "cancelled": 0}
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        now = _utc_now()
        stats["due"] = sum(1 for x in stack if _item_is_due(x, now))
        before_status = {x.get("id"): x.get("status") for x in stack}
        changed, stale_pids = _process_due_publisher_items(stack)
        if changed:
            _save_publisher_stack(stack)
        for x in stack:
            prev = before_status.get(x.get("id"))
            cur = x.get("status")
            if prev != cur:
                if cur == "published":
                    stats["published"] += 1
                elif cur == "failed":
                    stats["failed"] += 1
                elif cur == "cancelled":
                    stats["cancelled"] += 1
    for pid in stale_pids:
        try:
            _request_pipeline_refresh(pid, "паблишер нашёл старые посты в очереди")
        except Exception:
            pass
    pending = sum(1 for x in stack if x.get("status") == "pending")
    overdue = sum(1 for x in stack if _item_is_due(x))
    return jsonify({
        "ok": True,
        "pending": pending,
        "overdue": overdue,
        "stats": stats,
        "scheduler_last_tick": _publisher_scheduler_last_tick,
        "scheduler_last_error": _publisher_scheduler_last_error,
        "stack": stack,
    })


@app.route("/publisher-stack/bulk-delete", methods=["POST"])
def bulk_delete_publisher_stack_items():
    body = request.json or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids required"}), 400
    id_set = {str(x) for x in ids}
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        new_stack = [x for x in stack if x.get("id") not in id_set]
        removed = len(stack) - len(new_stack)
        _save_publisher_stack(new_stack)
    return jsonify({"ok": True, "removed": removed})


@app.route("/publisher-stack/bulk-platform", methods=["POST"])
def bulk_set_publisher_stack_platform():
    """Set platform on pending items (by ids or whole pipeline)."""
    body = request.json or {}
    platform = (body.get("platform") or "").strip().lower()
    if platform not in ("telegram", "twitter"):
        return jsonify({"error": "platform must be telegram or twitter"}), 400
    ids = body.get("ids")
    pid = (body.get("pipeline") or "").strip()
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        updated = 0
        for item in stack:
            if item.get("status") != "pending":
                continue
            if ids is not None:
                if item.get("id") not in ids:
                    continue
            elif pid:
                if item.get("pipeline") != pid:
                    continue
            else:
                continue
            item["platform"] = platform
            updated += 1
        if updated:
            _save_publisher_stack(stack)
    return jsonify({"ok": True, "updated": updated, "platform": platform})


@app.route("/publisher-stack/add", methods=["POST"])
def add_publisher_stack_items():
    body = request.json or {}
    pid = (body.get("pipeline") or "").strip()
    items_in = body.get("items")
    if not pid:
        return jsonify({"error": "pipeline required"}), 400
    if not isinstance(items_in, list) or not items_in:
        return jsonify({"error": "items required"}), 400

    with _pipelines_lock:
        pdata = _load_pipelines()
        pipeline = _find_pipeline(pdata, pid)
        if not pipeline:
            return jsonify({"error": "pipeline not found"}), 404
        default_platform = pipeline.get("default_platform", "telegram")

    new_items = []
    for raw in items_in:
        text = (raw.get("text") or "").strip()
        scheduled_at = (raw.get("scheduledAt") or "").strip()
        if not text or not scheduled_at:
            continue
        platform = (raw.get("platform") or default_platform).strip().lower()
        if platform not in ("telegram", "twitter"):
            platform = default_platform
        tw_err = _twitter_length_error(text, platform)
        new_items.append(_normalize_stack_item({
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "scheduledAt": scheduled_at,
            "platform": platform,
            "status": "failed" if tw_err else "pending",
            "pipeline": pid,
            "messageId": None,
            "url": "",
            "channel": "",
            "error": tw_err or "",
        }))
    if not new_items:
        return jsonify({"error": "no valid items"}), 400

    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        stack.extend(new_items)
        _save_publisher_stack(stack)
    return jsonify({"ok": True, "added": len(new_items), "items": new_items})


@app.route("/publisher-stack/cancel/<item_id>", methods=["POST"])
def cancel_publisher_stack_item(item_id):
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        found = False
        for item in stack:
            if item.get("id") == item_id:
                if item.get("status") != "pending":
                    return jsonify({"error": "not pending"}), 400
                item["status"] = "cancelled"
                found = True
                break
        if not found:
            return jsonify({"error": "not found"}), 404
        _save_publisher_stack(stack)
    return jsonify({"ok": True})


@app.route("/publisher-stack/schedule/<item_id>", methods=["POST"])
def reschedule_publisher_stack_item(item_id):
    """Update publication time for a single queued post."""
    body = request.json or {}
    scheduled_at = (body.get("scheduledAt") or "").strip()
    if not scheduled_at:
        return jsonify({"error": "scheduledAt required"}), 400
    try:
        dt = _parse_iso_utc(scheduled_at)
        scheduled_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception as e:
        return jsonify({"error": f"invalid scheduledAt: {e}"}), 400

    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        for item in stack:
            if item.get("id") != item_id:
                continue
            if item.get("status") not in ("pending", "failed", "cancelled"):
                return jsonify({"error": "only pending/failed/cancelled can be rescheduled"}), 400
            item["scheduledAt"] = scheduled_iso
            if item.get("status") in ("failed", "cancelled"):
                item["status"] = "pending"
            item["error"] = ""
            item.pop("forcePublish", None)
            _save_publisher_stack(stack)
            return jsonify({"ok": True, "item": item})
        return jsonify({"error": "not found"}), 404


@app.route("/publisher-stack/retry/<item_id>", methods=["POST"])
def retry_publisher_stack_item(item_id):
    body = request.json or {}
    force = bool(body.get("force"))
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        for item in stack:
            if item.get("id") == item_id:
                if item.get("status") not in ("failed", "cancelled"):
                    return jsonify({"error": "not retriable"}), 400
                item["status"] = "pending"
                item["error"] = ""
                if force:
                    item["forcePublish"] = True
                else:
                    item.pop("forcePublish", None)
                if not item.get("scheduledAt"):
                    item["scheduledAt"] = _utc_now().isoformat() + "Z"
                _save_publisher_stack(stack)
                return jsonify({"ok": True, "item": item})
        return jsonify({"error": "not found"}), 404


@app.route("/publisher-stack/delete/<item_id>", methods=["POST"])
def delete_publisher_stack_item(item_id):
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        new_stack = [x for x in stack if x.get("id") != item_id]
        if len(new_stack) == len(stack):
            return jsonify({"error": "not found"}), 404
        _save_publisher_stack(new_stack)
    return jsonify({"ok": True})


@app.route("/publisher-stack/clear/<pid>", methods=["POST"])
def clear_publisher_stack_pipeline(pid):
    with _pipelines_lock:
        pdata = _load_pipelines()
        if not _find_pipeline(pdata, pid):
            return jsonify({"error": "pipeline not found"}), 404
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        new_stack = [x for x in stack if x.get("pipeline") != pid]
        removed = len(stack) - len(new_stack)
        _save_publisher_stack(new_stack)
    return jsonify({"ok": True, "removed": removed})


@app.route("/publisher-stack/<pid>", methods=["GET"])
def get_publisher_stack_by_pipeline(pid):
    with _pipelines_lock:
        pdata = _load_pipelines()
        if not _find_pipeline(pdata, pid):
            return jsonify({"error": "pipeline not found"}), 404
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        items = [x for x in stack if x.get("pipeline") == pid]
    return jsonify({"pipeline": pid, "stack": items})


@app.route("/pipelines", methods=["GET"])
def get_pipelines():
    with _pipelines_lock:
        pdata = _load_pipelines()
        pipelines = list(pdata.get("pipelines", []))
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
    result = []
    for p in pipelines:
        pid = p.get("id", "")
        pending = sum(
            1 for x in stack
            if x.get("pipeline") == pid and x.get("status") == "pending"
        )
        total = sum(1 for x in stack if x.get("pipeline") == pid)
        result.append({**p, "pending": pending, "total": total})
    return jsonify({"pipelines": result})


@app.route("/pipelines", methods=["POST"])
def create_pipeline():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    default_platform = (body.get("default_platform") or "telegram").strip().lower()
    if default_platform not in ("telegram", "twitter"):
        default_platform = "telegram"
    times = body.get("times")
    if not isinstance(times, list) or not times:
        times = ["10:00"]
    times = [str(t).strip() for t in times if str(t).strip()]
    default_lang = (body.get("default_lang") or "").strip().lower()
    if default_lang not in ("en", "ru", "ua"):
        default_lang = "en" if default_platform == "twitter" else "ru"
    seed_wf = body.get("workflow") if isinstance(body.get("workflow"), dict) else {}
    if not seed_wf.get("lang"):
        seed_wf = {**seed_wf, "lang": default_lang, "v": seed_wf.get("v", 1)}
    new_p = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "default_platform": default_platform,
        "default_lang": default_lang,
        "times": times,
        "source_mode": (body.get("source_mode") or "factiva").strip().lower(),
        "auto_enabled": bool(body.get("auto_enabled", False)),
        "auto_export_times": ["08:00"],
        "auto_max_drafts": 10,
        "auto_run_article_topics": True,
        "auto_publish_mode": "slots",
        "auto_publish_interval_minutes": 120,
        "auto_publish_start_time": "10:00",
        "last_auto_runs": {},
        "auto_last_status": {},
        "workflow": seed_wf,
    }
    if new_p["source_mode"] not in ("factiva", "techcrunch"):
        new_p["source_mode"] = "factiva"
    with _pipelines_lock:
        pdata = _load_pipelines()
        pdata.setdefault("pipelines", []).append(new_p)
        _save_pipelines(pdata)
    return jsonify(new_p), 201


@app.route("/pipelines/<pid>", methods=["PUT"])
def update_pipeline(pid):
    body = request.json or {}
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return jsonify({"error": "not found"}), 404
        if "name" in body:
            name = (body.get("name") or "").strip()
            if name:
                p["name"] = name
        if "default_platform" in body:
            dp = (body.get("default_platform") or "").strip().lower()
            if dp in ("telegram", "twitter"):
                p["default_platform"] = dp
                if "default_lang" not in body:
                    p["default_lang"] = "en" if dp == "twitter" else "ru"
        if "default_lang" in body:
            dl = (body.get("default_lang") or "").strip().lower()
            if dl in ("en", "ru", "ua"):
                p["default_lang"] = dl
        if "times" in body:
            times = body.get("times")
            if isinstance(times, list):
                p["times"] = [str(t).strip() for t in times if str(t).strip()]
            elif isinstance(times, str):
                p["times"] = [t.strip() for t in times.split(",") if t.strip()]
        if "source_mode" in body:
            sm = (body.get("source_mode") or "").strip().lower()
            if sm in ("factiva", "techcrunch"):
                p["source_mode"] = sm
        if "auto_enabled" in body:
            p["auto_enabled"] = bool(body.get("auto_enabled"))
        if "auto_export_times" in body:
            times = body.get("auto_export_times")
            if isinstance(times, list):
                p["auto_export_times"] = [str(t).strip() for t in times if str(t).strip()]
            elif isinstance(times, str):
                p["auto_export_times"] = [t.strip() for t in times.split(",") if t.strip()]
        if "auto_max_drafts" in body:
            p["auto_max_drafts"] = max(1, min(50, int(body.get("auto_max_drafts") or 10)))
        if "auto_run_article_topics" in body:
            p["auto_run_article_topics"] = bool(body.get("auto_run_article_topics"))
        if "auto_publish_mode" in body:
            mode = (body.get("auto_publish_mode") or "slots").strip().lower()
            if mode in ("slots", "interval"):
                p["auto_publish_mode"] = mode
        if "auto_publish_interval_minutes" in body:
            p["auto_publish_interval_minutes"] = max(1, int(body.get("auto_publish_interval_minutes") or 120))
        if "auto_publish_start_time" in body:
            st = (body.get("auto_publish_start_time") or "").strip()
            if st:
                p["auto_publish_start_time"] = st
        _save_pipelines(pdata)
        return jsonify(p)


@app.route("/pipelines/<pid>/run-auto", methods=["POST"])
def run_pipeline_automation_now(pid):
    """Manually trigger full automation chain for a pipeline (background thread)."""
    body = request.json or {}
    force = bool(body.get("force"))
    factiva_dom = body.get("factivaDom") or body.get("factiva_dom")
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return jsonify({"error": "not found"}), 404
        pipeline = dict(p)

    if isinstance(factiva_dom, dict) and factiva_dom:
        _merge_workflow_factiva_dom(pid, factiva_dom)
        with _pipelines_lock:
            fresh = _find_pipeline(_load_pipelines(), pid)
            if fresh:
                pipeline = dict(fresh)

    if not force and _automation_on_cooldown(pipeline):
        last = _last_successful_automation_at(pipeline)
        mins = int(pipeline.get("auto_min_rerun_minutes") or AUTO_MIN_RERUN_MINUTES)
        return jsonify({
            "error": f"Пайплайн уже отработал недавно — повтор через {mins} мин "
                      f"(посл. {last.strftime('%H:%M') if last else '?'})",
            "cooldown": True,
        }), 429

    _maybe_recover_stuck_automation()

    with _automation_lock:
        if _automation_running:
            return jsonify({"error": "Автоматизация уже выполняется"}), 409
        _begin_automation_run(pid)

    def _bg():
        try:
            _execute_pipeline_automation(pipeline, slot_key=None)
        except Exception as e:
            import traceback
            _automation_log_push(f"[auto] FATAL: {e}")
            traceback.print_exc()
        finally:
            with _automation_lock:
                _end_automation_run()

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/pipelines/<pid>/stop-auto", methods=["POST"])
def stop_pipeline_automation(pid):
    """Request stop for running automation; optionally disable auto schedule."""
    global _automation_cancel
    body = request.json or {}
    disable = bool(body.get("disable_auto", False))
    _automation_cancel = True
    _kill_automation_subproc()
    if _automation_running and _automation_running_pid == pid:
        _set_pipeline_auto_status(pid, "cancelled", ok=False, error="Остановлено пользователем")
    if disable:
        with _pipelines_lock:
            pdata = _load_pipelines()
            p = _find_pipeline(pdata, pid)
            if p:
                p["auto_enabled"] = False
                _save_pipelines(pdata)
    return jsonify({
        "ok": True,
        "stopping": _automation_running,
        "running_pid": _automation_running_pid,
    })


@app.route("/automation/status", methods=["GET"])
def automation_status():
    _maybe_recover_stuck_automation()
    step = None
    status_at = None
    extra: dict = {}
    active_steps = {"export", "sources", "topics", "article_topics", "drafts", "schedule"}
    if _automation_running and _automation_running_pid:
        with _pipelines_lock:
            p = _find_pipeline(_load_pipelines(), _automation_running_pid)
        if p:
            st = p.get("auto_last_status") or {}
            step = st.get("step")
            status_at = st.get("at")
            for key in (
                "file", "drafts", "scheduled", "articles_in_export",
                "platform", "warning", "skipped_duplicates", "error", "ok",
            ):
                if key in st and st[key] is not None:
                    extra[key] = st[key]
            if step in active_steps:
                extra["in_progress"] = True
    return jsonify({
        "running": _automation_running,
        "pipeline_id": _automation_running_pid,
        "cancel_requested": _automation_cancel,
        "step": step,
        "status_at": status_at,
        "log_tail": _automation_log_tail(12),
        **extra,
    })


@app.route("/pipelines/<pid>/workflow", methods=["GET"])
def get_pipeline_workflow(pid):
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "pipeline": pid,
            "source_mode": p.get("source_mode", "factiva"),
            "auto_enabled": bool(p.get("auto_enabled", False)),
            "workflow": p.get("workflow") or {},
        })


@app.route("/pipelines/<pid>/workflow", methods=["PUT"])
def put_pipeline_workflow(pid):
    body = request.json or {}
    workflow = body.get("workflow")
    if not isinstance(workflow, dict):
        return jsonify({"error": "workflow required"}), 400
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return jsonify({"error": "not found"}), 404
        p["workflow"] = workflow
        if "source_mode" in body:
            sm = (body.get("source_mode") or "").strip().lower()
            if sm in ("factiva", "techcrunch"):
                p["source_mode"] = sm
        if "auto_enabled" in body:
            p["auto_enabled"] = bool(body.get("auto_enabled"))
        _save_pipelines(pdata)
        return jsonify({
            "ok": True,
            "pipeline": pid,
            "source_mode": p.get("source_mode", "factiva"),
            "auto_enabled": bool(p.get("auto_enabled", False)),
            "workflow": p.get("workflow") or {},
        })


@app.route("/pipelines/<pid>/published-memory", methods=["DELETE"])
def clear_pipeline_published_memory(pid):
    with _pipelines_lock:
        pdata = _load_pipelines()
        p = _find_pipeline(pdata, pid)
        if not p:
            return jsonify({"error": "pipeline not found"}), 404
        n = len(p.get("published_stories") or [])
        p["published_stories"] = []
        _save_pipelines(pdata)
    return jsonify({"ok": True, "cleared": n})


@app.route("/pipelines/<pid>", methods=["DELETE"])
def delete_pipeline(pid):
    with _pipelines_lock:
        pdata = _load_pipelines()
        pipelines = pdata.get("pipelines", [])
        if len(pipelines) <= 1:
            return jsonify({"error": "cannot delete last pipeline"}), 400
        if not _find_pipeline(pdata, pid):
            return jsonify({"error": "not found"}), 404
        pdata["pipelines"] = [x for x in pipelines if x.get("id") != pid]
        _save_pipelines(pdata)
    with _publisher_stack_lock:
        stack = _load_publisher_stack()
        changed = False
        for item in stack:
            if item.get("pipeline") == pid and item.get("status") == "pending":
                item["status"] = "cancelled"
                changed = True
        if changed:
            _save_publisher_stack(stack)
    return jsonify({"ok": True})


@app.route("/check-twitter-keys")
def check_twitter_keys():
    """Check if Twitter API keys are configured."""
    import os
    from twitter_poster import _twitter_env_status, verify_twitter_access

    status = _twitter_env_status()
    missing = list(status["missing"])
    if "TWITTER_ACCESS_SECRET" in missing:
        missing = [k for k in missing if k != "TWITTER_ACCESS_SECRET"]
        if not os.getenv("TWITTER_ACCESS_TOKEN_SECRET"):
            missing.append("TWITTER_ACCESS_SECRET")
    anthropic_ok = bool(os.getenv("ANTHROPIC_API_KEY"))
    payload = {"missing_twitter": missing, "anthropic_ok": anthropic_ok}
    if not missing:
        try:
            info = verify_twitter_access(test_post=False)
            payload["twitter_user"] = info.get("username")
            payload["twitter_ok"] = True
        except Exception as e:
            payload["twitter_ok"] = False
            payload["twitter_error"] = str(e)
    return jsonify(payload)


@app.route("/test-twitter", methods=["POST"])
def test_twitter_route():
    """Verify Twitter credentials and optional test post."""
    data = request.json or {}
    test_post = bool(data.get("post"))
    try:
        from twitter_poster import verify_twitter_access
        return jsonify(verify_twitter_access(test_post=test_post))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/exports")
def exports():
    exports_dir = BASE_DIR / "exports"
    exports_dir.mkdir(exist_ok=True)
    files = []
    paths = list(exports_dir.glob("*.rtf")) + list(exports_dir.glob("*.json"))
    for p in sorted(paths, key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        size_mb = st.st_size / 1_048_576
        size_label = f"{size_mb:.1f} MB" if size_mb >= 0.1 else f"{st.st_size // 1024} KB"
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")
        kind = "techcrunch" if p.name.startswith("techcrunch_") else ("json" if p.suffix == ".json" else "rtf")
        files.append({
            "name": p.name,
            "path": p.name,   # just filename, resolved server-side
            "size": size_label,
            "mtime": mtime,
            "kind": kind,
        })
    return jsonify(files)


@app.route("/techcrunch/feeds")
def techcrunch_feeds():
    from techcrunch_feed import FEED_LABELS, TECHCRUNCH_FEEDS
    return jsonify({
        "feeds": [
            {"id": k, "label": FEED_LABELS.get(k, k), "url": TECHCRUNCH_FEEDS[k]}
            for k in TECHCRUNCH_FEEDS
        ]
    })


@app.route("/techcrunch/fetch", methods=["POST"])
def techcrunch_fetch():
    """Pull TechCrunch RSS, save as JSON export, return articles + file name."""
    data = request.json or {}
    feed_key = (data.get("feed") or "main").strip().lower()
    max_articles = int(data.get("max", 20))
    fetch_full_body = bool(data.get("fetch_full_body", True))
    date_from = (data.get("date_from") or "").strip() or None
    date_to = (data.get("date_to") or "").strip() or None
    out_name = (data.get("outName") or data.get("out_name") or "").strip() or None

    try:
        from techcrunch_feed import FEED_LABELS, fetch_techcrunch_articles, save_articles_json

        articles, tc_stats = fetch_techcrunch_articles(
            feed_key=feed_key,
            max_articles=max_articles,
            fetch_full_body=fetch_full_body,
            date_from=date_from,
            date_to=date_to,
        )

        if not articles:
            if date_from or date_to:
                return jsonify({
                    "error": (
                        "В ленте нет статей за выбранный период. "
                        f"RSS содержит ~{tc_stats.get('feed_entries', 0)} последних записей."
                    ),
                    "stats": tc_stats,
                }), 502
            return jsonify({"error": "Лента пуста или недоступна"}), 502

        path = save_articles_json(
            articles, BASE_DIR / "exports", feed_key=feed_key, out_name=out_name,
        )
        sources = [{"name": "TechCrunch", "count": len(articles)}]
        return jsonify({
            "file": path.name,
            "feed": feed_key,
            "feed_label": FEED_LABELS.get(feed_key, feed_key),
            "articles": articles,
            "sources": sources,
            "total": len(articles),
            "stats": tc_stats,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-400:]}), 500


@app.route("/techcrunch/enrich-one", methods=["POST"])
def techcrunch_enrich_one():
    """Fetch full page text for a single TechCrunch article (used for progressive UI load)."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    current = (data.get("body") or "").strip()
    if not url:
        return jsonify({"error": "Нет URL"}), 400
    try:
        from techcrunch_feed import fetch_page_body_for_url
        page_body = fetch_page_body_for_url(url)
        body = page_body if len(page_body) > len(current) else current
        if not body:
            body = current or url
        return jsonify({"body": body[:8000]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/techcrunch/save", methods=["POST"])
def techcrunch_save():
    """Save a selected subset of TechCrunch articles as a JSON export."""
    data = request.json or {}
    articles = data.get("articles") or []
    feed_key = (data.get("feed") or "main").strip().lower()
    out_name = (data.get("outName") or data.get("out_name") or "").strip() or None
    if not articles:
        return jsonify({"error": "Нет выбранных статей"}), 400
    try:
        from techcrunch_feed import FEED_LABELS, save_articles_json
        clean = []
        for a in articles:
            if not isinstance(a, dict):
                continue
            title = (a.get("title") or "").strip()
            if not title:
                continue
            body = (a.get("body") or title).strip()
            item = {
                "title": title,
                "source": (a.get("source") or "TechCrunch").strip() or "TechCrunch",
                "date": (a.get("date") or "").strip(),
                "body": body[:8000],
            }
            url = (a.get("url") or "").strip()
            if url:
                item["url"] = url
            cat = (a.get("category") or "").strip()
            if cat:
                item["category"] = cat
                item["category_label"] = (a.get("category_label") or "").strip() or cat
            tags = a.get("tags")
            if isinstance(tags, list) and tags:
                item["tags"] = [str(t).strip() for t in tags if str(t).strip()][:8]
            clean.append(item)
        if not clean:
            return jsonify({"error": "Нет валидных статей"}), 400
        path = save_articles_json(
            clean, BASE_DIR / "exports", feed_key=feed_key, out_name=out_name,
        )
        return jsonify({
            "file": path.name,
            "feed": feed_key,
            "feed_label": FEED_LABELS.get(feed_key, feed_key),
            "total": len(clean),
            "sources": [{"name": "TechCrunch", "count": len(clean)}],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/file-info")
def file_info():
    name = Path(request.args.get("path", "")).name
    path = BASE_DIR / "exports" / name
    if not path.exists():
        return jsonify({"size": ""})
    size_mb = path.stat().st_size / 1_048_576
    return jsonify({"size": f"{size_mb:.1f} MB"})


@app.route("/download")
def download():
    name = Path(request.args.get("path", "")).name   # strip any directory traversal
    path = BASE_DIR / "exports" / name
    if not path.exists() or path.suffix.lower() != ".rtf":
        return "File not found", 404
    return send_file(str(path), as_attachment=True, download_name=path.name)


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_args(data: dict) -> list | str:
    mode       = data.get("mode", "search")         # "search" | "query"
    search     = data.get("search", "").strip()
    query      = data.get("query", "").strip()
    date_val   = data.get("date", "week").strip()
    source     = data.get("source", "").strip()
    max_pages  = str(data.get("maxPages", 0))
    out_name   = data.get("outName", "factiva_export").strip()
    # Force headless on server — no X11 display available
    headless   = True
    dedup      = data.get("dedup", True)

    import datetime, re
    # Append today's date to filename so exports don't overwrite each other
    date_suffix = datetime.date.today().strftime("%Y%m%d")
    base = re.sub(r'\.rtf$', '', out_name, flags=re.IGNORECASE)
    out_name = f"{base}_{date_suffix}.rtf"

    out_path = str(BASE_DIR / "exports" / out_name)

    if mode == "search":
        if not search:
            return "Saved search name is required"
        args = ["--search", search]
    else:
        if not query and not source:
            return "Укажите текст запроса или источник"

        # Map source name → Factiva source code (sc= field, no quotes needed)
        SOURCE_CODES = {
            "wall street journal": "wsj",
            "the wall street journal": "wsj",
            "wsj": "wsj",
            "financial times": "ft",
            "the financial times": "ft",
            "ft": "ft",
            "new york times": "nyt",
            "the new york times": "nyt",
            "nyt": "nyt",
            "reuters": "reuwam",
            "bloomberg": "bfrt",
            "the economist": "econ",
            "economist": "econ",
            "washington post": "wpcom",
            "the washington post": "wpcom",
            "guardian": "grdn",
            "the guardian": "grdn",
            "bbc": "bbcnew",
            "ap": "aprs",
            "associated press": "aprs",
            "forbes": "forbs",
            "fortune": "frtun",
            "business insider": "bsins",
        }
        sc_code = SOURCE_CODES.get(source.lower().strip(), "") if source else ""

        # Always pass query and source separately — source applied as filter on results page
        args = ["--query", query or "a", "--date", date_val]
        if source:
            args += ["--source", source]

    args += ["--out", out_path]

    if int(max_pages) > 0:
        args += ["--max-pages", max_pages]

    if headless:
        args.append("--headless")

    if dedup:
        args.append("--dedup")

    return args


if __name__ == "__main__":
    (BASE_DIR / "exports").mkdir(exist_ok=True)
    print("Opening http://localhost:5000 ...")
    import webbrowser, time
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, threaded=True, host="0.0.0.0", port=5000)
