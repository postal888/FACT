import os, re
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from _llm import MODEL_WRITE, MODEL_UTIL, anthropic_call

_COPYRIGHT_PATTERNS = [
  # Copyright 2026 MarketWatch, Inc. All Rights Reserved.
  re.compile(
    r'(?:©\s*)?Copyright\s*(?:©\s*)?\d{4}[.,]?\s*(.+?)(?:\.\s*All [Rr]ights|\s+All [Rr]ights)',
    re.I,
  ),
  # © 2026 Journal Name. Provided by ProQuest…
  re.compile(
    r'(?:©|\u00a9|\ufffd)\s*\d{4}[.,]?\s*(.+?)\.\s*(?:Provided by|All [Rr]ights)',
    re.I,
  ),
  # © 2026, awp Finanznachrichten AG. All rights reserved.
  re.compile(
    r'(?:©|\u00a9|\ufffd)\s*\d{4},\s*(.+?)\.\s*All [Rr]ights',
    re.I,
  ),
  # Copyright © 2026, Dow Jones & Company, Inc.
  re.compile(
    r'Copyright\s*(?:©\s*)?\d{4}[.,]?\s*(.+?)(?:\.|$)',
    re.I,
  ),
]
_DATE_RE = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s+\d{4}\b'
    r'|\d{4}-\d{2}-\d{2}'
    r'|^\d{1,2}\s+\w+\s+\d{4}$',
    re.I,
)
_META_LINE_RE = re.compile(
    r'^(\d{1,3}(?:,\d{3})*\s+words?|\d+\s+words?|\d+-\d+|Volume\s|ISSN:'
    r'|English|Russian|German|Spanish|French|Chinese|Japanese'
    r'|\d{1,2}:\d{2}(?::\d{2})?)$',
    re.I,
)
_WIRE_CODE_RE = re.compile(r'^[A-Z]{2,8}$')
_INVALID_SOURCE_RE = re.compile(
    r'^('
    r'\d{1,2}:\d{2}(?::\d{2})?'
    r'|\d{1,3}(?:,\d{3})*\s+words?'
    r'|\d+\s+words?'
    r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}'
    r'|\d{4}-\d{2}-\d{2}'
    r'|English|Russian|German|Spanish|French|Chinese|Japanese'
    r')$',
    re.I,
)


def _is_copyright_line(line: str) -> bool:
    low = line.lower()
    if 'copyright' in low or '©' in line or '\ufffd' in line:
        return bool(re.search(r'rights reserved|provided by|\d{4}', line, re.I))
    return False


def _is_valid_source_name(name: str) -> bool:
    name = name.strip().rstrip('.,')
    if not name or len(name) < 3:
        return False
    if _INVALID_SOURCE_RE.match(name):
        return False
    if name.lower().startswith('by '):
        return False
    if re.match(r'^\d+$', name):
        return False
    if _DATE_RE.fullmatch(name):
        return False
    return True


def _clean_source_name(name: str) -> str:
    name = name.strip().rstrip('.,')
    name = re.sub(r'\s+', ' ', name)
    name = re.sub(
        r',?\s+(Inc\.?|Ltd\.?|LLC|Corp\.?|Co\.?|AG|GmbH|PLC|N\.?V\.?)\.?$',
        '', name, flags=re.I,
    )
    return name.strip()


def _extract_source_from_copyright(line: str) -> str:
    for pat in _COPYRIGHT_PATTERNS:
        m = pat.search(line)
        if m:
            name = _clean_source_name(m.group(1))
            if _is_valid_source_name(name):
                return name
    return ""


def _find_date_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines[:15]):
        if _DATE_RE.search(line):
            return i
    return None


def _find_publication_line(lines: list[str]) -> str:
    """Publication name in Factiva header — after date/time, before wire code."""
    date_idx = _find_date_index(lines)
    if date_idx is None:
        return ""
    for i in range(date_idx + 1, min(date_idx + 8, len(lines))):
        line = lines[i].strip()
        if _is_copyright_line(line):
            break
        if _WIRE_CODE_RE.match(line) or _META_LINE_RE.match(line) or not _is_valid_source_name(line):
            continue
        return line
    return ""


def _extract_source_from_header(lines: list[str]) -> str:
    # 1. Copyright line — most reliable
    for line in lines[:20]:
        if _is_copyright_line(line):
            src = _extract_source_from_copyright(line)
            if src:
                return src
    # 2. Publication name row in header (MarketWatch, Dow Jones Newswires, etc.)
    pub = _find_publication_line(lines)
    if pub:
        return pub
    return ""


def _find_body_start(lines: list[str]) -> int:
    for i, line in enumerate(lines[:20]):
        if _is_copyright_line(line):
            return i + 1
    date_idx = _find_date_index(lines)
    if date_idx is None:
        return 1
    i = date_idx + 1
    while i < len(lines) and i < 15:
        line = lines[i]
        if _is_copyright_line(line):
            return i + 1
        if _WIRE_CODE_RE.match(line) or _META_LINE_RE.match(line) or not _is_valid_source_name(line):
            i += 1
            continue
        i += 1
    return min(i, len(lines))


_GENERIC_TITLES = {
    "research article", "news article", "news", "commentary", "editorial",
    "opinion", "analysis", "briefing", "special report", "feature",
    "breaking news", "press release", "wire", "update", "alert",
    "bookshelf", "review", "interview", "column", "essay",
}


def _is_generic_title(title: str) -> bool:
    t = (title or "").strip().lower().rstrip(".")
    if not t or len(t) < 4:
        return True
    if t in _GENERIC_TITLES:
        return True
    return bool(re.fullmatch(r"(?:research|news|feature|special)\s+article", t))


def _is_author_line(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 55 or re.search(r"\d", line):
        return False
    if "," in line or ";" in line or line.lower().startswith("by "):
        return True
    words = line.split()
    if 1 <= len(words) <= 4 and len(line) < 42:
        if all(w[:1].isupper() for w in words if w):
            return True
    return False


def _extract_article_title(lines: list[str]) -> str:
    """Pick the real headline; skip Factiva labels like 'Research Article'."""
    candidates: list[str] = []
    for line in lines[:14]:
        line = line.strip()
        if not line or len(line) < 12:
            continue
        if _META_LINE_RE.match(line) or _WIRE_CODE_RE.match(line):
            continue
        if _DATE_RE.fullmatch(line) or (_DATE_RE.search(line) and len(line) < 32):
            continue
        if _is_copyright_line(line) or _INVALID_SOURCE_RE.match(line):
            continue
        low = line.lower()
        if low.startswith("volume ") or "issn:" in low:
            continue
        if _is_generic_title(line) or _is_author_line(line):
            continue
        candidates.append(line)
    for line in candidates:
        if len(line) >= 20:
            return line
    if candidates:
        return candidates[0]
    return lines[0].strip() if lines else ""


def _display_title(art: dict) -> str:
    title = (art.get("title") or "").strip()
    if title and not _is_generic_title(title):
        return title
    topic = (art.get("post_topic") or "").strip()
    if topic:
        return topic
    body = (art.get("body") or "").strip()
    if body:
        m = re.match(r"^(.{24,140}?)(?:\.|!|\?|$)", body)
        if m:
            return m.group(1).strip()
    return title


def parse_rtf_to_articles(rtf_path):
    from striprtf.striprtf import rtf_to_text
    text = rtf_to_text(Path(rtf_path).read_text(encoding="utf-8", errors="replace"))
    blocks = re.split(r'\nDocument\s+\w+\n', text)
    articles = []
    for block in blocks:
        block = block.strip()
        if len(block) < 150:
            continue
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        title = _extract_article_title(lines)
        if len(title) < 10 or re.match(r'^\d+$', title):
            continue
        if title.startswith('Search Summary'):
            continue
        source = _extract_source_from_header(lines)
        date_str = ""
        date_idx = _find_date_index(lines)
        if date_idx is not None:
            date_str = lines[date_idx]
        body_start = _find_body_start(lines)
        body = " ".join(lines[body_start:])
        if len(body) > 8000:
            body = body[:8000] + "..."
        articles.append({"title": title, "source": source, "date": date_str, "body": body})
    return articles


def parse_json_to_articles(json_path) -> list[dict]:
    """Load articles from a JSON export (TechCrunch feed, etc.)."""
    import json as _json
    data = _json.loads(Path(json_path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("articles") or data.get("items") or []
    else:
        raw = []
    articles = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        body = (item.get("body") or item.get("text") or "").strip()
        if not title:
            continue
        if not body:
            body = title
        art = {
            "title": title,
            "source": (item.get("source") or "").strip(),
            "date": (item.get("date") or "").strip(),
            "body": body[:8000] + ("..." if len(body) > 8000 else ""),
        }
        url = (item.get("url") or item.get("link") or "").strip()
        if url:
            art["url"] = url
        articles.append(art)
    return articles


_TITLE_NOISE_RE = re.compile(
    r"[\s\-–—|:]+(?:reuters|bloomberg|afp|ap|dow jones|update\s*\d*|"
    r"exclusive|analysis|opinion|commentary)\s*$",
    re.I,
)
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _norm_text(s: str) -> str:
    s = (s or "").lower().replace("\u00a0", " ")
    s = _NON_WORD_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _norm_title(title: str) -> str:
    t = (title or "").strip()
    t = _TITLE_NOISE_RE.sub("", t)
    return _norm_text(t)


def _norm_url(url: str) -> str:
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"[?#].*$", "", u)
    u = u.rstrip("/")
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u


def _title_tokens(title_norm: str) -> set[str]:
    return {w for w in title_norm.split() if len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _body_fingerprint(body: str) -> str:
    b = _norm_text(body)
    if len(b) < 80:
        return ""
    return b[:180]


def _article_quality(art: dict) -> tuple:
    """Higher is better — prefer longer body and presence of URL."""
    body = art.get("body") or ""
    url = 1 if (art.get("url") or "").strip() else 0
    return (len(body), url, len(art.get("title") or ""))


def dedupe_articles(articles: list[dict]) -> tuple[list[dict], int]:
    """Drop near-duplicates. Prefer longer body / URL when choosing a winner.

    Criteria (strict, especially within the same source):
    - identical URL
    - identical normalized title (global)
    - same source + title prefix (50 chars)
    - same source + body fingerprint
    - same source + high title token overlap (Jaccard ≥ 0.82)
    - same source + one title contained in the other (len ≥ 24)
    """
    if not articles:
        return [], 0

    # index -> best article for that slot; we may replace with a better duplicate
    kept: list[dict] = []
    kept_meta: list[dict] = []  # parallel metadata for comparisons

    def is_dup(art: dict, meta: dict) -> int | None:
        """Return index in kept to replace, or -1 if drop, or None if unique."""
        url = meta["url"]
        title = meta["title"]
        source = meta["source"]
        body_fp = meta["body_fp"]
        tokens = meta["tokens"]
        prefix = title[:50] if title else ""

        for i, km in enumerate(kept_meta):
            # Global URL match
            if url and url == km["url"]:
                return i if _article_quality(art) > _article_quality(kept[i]) else -1

            # Global exact title
            if title and title == km["title"]:
                return i if _article_quality(art) > _article_quality(kept[i]) else -1

            same_source = source and source == km["source"]
            if not same_source:
                continue

            # Same source + title prefix
            if prefix and len(prefix) >= 24 and prefix == (km["title"][:50] if km["title"] else ""):
                return i if _article_quality(art) > _article_quality(kept[i]) else -1

            # Same source + body start
            if body_fp and body_fp == km["body_fp"]:
                return i if _article_quality(art) > _article_quality(kept[i]) else -1

            # Same source + containment
            kt = km["title"]
            if title and kt and len(title) >= 24 and len(kt) >= 24:
                if title in kt or kt in title:
                    return i if _article_quality(art) > _article_quality(kept[i]) else -1

            # Same source + high token overlap
            if tokens and km["tokens"] and _jaccard(tokens, km["tokens"]) >= 0.82:
                return i if _article_quality(art) > _article_quality(kept[i]) else -1

        return None

    for art in articles:
        title = _norm_title(art.get("title", ""))
        if not title:
            continue
        meta = {
            "url": _norm_url(art.get("url", "")),
            "title": title,
            "source": (art.get("source") or "").strip().lower(),
            "body_fp": _body_fingerprint(art.get("body", "")),
            "tokens": _title_tokens(title),
        }
        decision = is_dup(art, meta)
        if decision is None:
            kept.append(art)
            kept_meta.append(meta)
        elif decision >= 0:
            kept[decision] = art
            kept_meta[decision] = meta
        # decision == -1 → drop as worse duplicate

    removed = len(articles) - len(kept)
    return kept, max(0, removed)


def load_articles(path, *, dedupe: bool = True) -> list[dict]:
    """Load articles from RTF or JSON export, with strict near-duplicate removal."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".json":
        articles = parse_json_to_articles(p)
    else:
        articles = parse_rtf_to_articles(p)
    if dedupe:
        articles, _ = dedupe_articles(articles)
    return articles


def load_articles_with_stats(path, *, dedupe: bool = True) -> tuple[list[dict], dict]:
    """Like load_articles, but also return {raw_total, total, removed_duplicates}."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".json":
        raw = parse_json_to_articles(p)
    else:
        raw = parse_rtf_to_articles(p)
    if not dedupe:
        return raw, {"raw_total": len(raw), "total": len(raw), "removed_duplicates": 0}
    articles, removed = dedupe_articles(raw)
    return articles, {
        "raw_total": len(raw),
        "total": len(articles),
        "removed_duplicates": removed,
    }


CINICO_BASE = """You are Cinico — an "anger investor" voice on X.
Write sharp, quotable posts about money, power, incentives, startup theater, VC hypocrisy, AI hype, and market delusion.
No LinkedIn tone. No consultant language. No hype. No emojis. No hashtags. No exclamation marks.
Focus on incentives, hypocrisy, hidden motives, and pattern recognition.
Do not invent facts. Always add your own biting one-line comment at the end.

CRITICAL — context first:
Every post must make sense on its own. Open with WHO did WHAT (use real company/person names from the article).
Never open with vague phrases like "two companies", "a major player", or "industry leaders" without naming them.
A reader who never saw the article must understand the news before your cynical take."""


PERSONAS = {
    "neutral": None,
    "thomson": """You are Thomson — a journalist in the spirit of Hunter S. Thompson.
Facts without varnish. You respect the reader enough not to sell them PR.

Psychology: Seen enough corruption, spin, and market theater to trust only what can be verified.
Not a cheerleader. Not a press-release rewriter. You name who did what, then show what it really means.

STYLE:
- Concrete: who, what, when, where, why — with real names and numbers
- Short paragraphs (2-3 sentences max)
- Structure: main fact → context → the uncomfortable implication
- Dry edge is allowed; melodrama is not
- Conclusions only when they follow from facts

FORBIDDEN:
- Exclamation marks
- Words like "amazing", "incredible", "unique", "importantly"
- Corporate optimism and LinkedIn tone
- Rhetorical fluff without a point
- Emojis

Write like a dispatch that refuses to soften the story.
In English: hard-edged reportage. In Russian: жёсткая фактура, без официоза.""",


    "baza": """You are Baza. You write for people who are in the know — or want to get there.

Psychology: Late 20s–early 30s, city life, knows the topic better than most but without showing off.
Read, studied, been through it yourself. Write like you talk — because that's more honest.
Hates info-scammers and people who overcomplicate simple things.

CHARACTER:
You've been through this yourself, know the pitfalls, speak straight.
No snobbery, no info-scam vibes, no filler. Like explaining to a friend.

STYLE:
- OPEN with the news itself — who did what, names, the point. Vary your openings every post.
- RU slang goes MID-text or near the end, not as a ritual first word. Rotate phrases:
  "кароч", "смотри", "по факту", "без базара", "ну ты понял", "вот так", "если по-честному", "ладно"
  EN — "look", "basically", "no cap", "lowkey", "real talk" — same rule, never the same opener twice in a row
- Spell it "кароч", never "короч"
- Address reader as "ты"; sometimes "народ" / "мужики"
- CAPS for ONE key word, not the whole text
- Emojis — max 2, only if they fit
- Short phrases, sometimes choppy — conversational rhythm

FORBIDDEN:
- Starting every post with "кароч", "короч", "смотри", "look", or "basically"
- Same opening pattern across posts — each post must feel fresh
- Bureaucratic or official tone
- "It should be noted", "in this context", "is a"
- Long complex subordinate sentences
- Pomp and self-congratulation

Write like a voice message a friend would read and say "that's the real deal".""",

    "aristarh": """You are Aristarkh Borisovich, a writer of the classical Russian school.

Psychology: Old-school writer, read everything from Tolstoy to Nabokov.
Language is not a tool but a living thing to be respected. Unhurried. Every word chosen.

CHARACTER:
Any topic deserves a beautiful word. Information matters, but without image it is dead.
You write slowly, with respect for the reader and the language.

STYLE:
- Literary language, rich and precise
- Long sentences with turns — but not tangled
- Metaphors and similes (organic, not clichéd)
- Details, atmosphere, sensations
- One philosophical aside in 1-2 sentences is fine
- End with an image or thought that stays with the reader

FORBIDDEN:
- Slang, abbreviations, emojis
- Bureaucratic phrasing
- Superficiality — every claim should be developed
- Rushed exposition

Write as if this will appear in a modern essay collection.
In English: literary, Nabokov-esque register, rich vocabulary.""",

    "jora": """You are Zhora. Master of hints and double meanings. You speak truth
in a way that is formally unassailable.

Psychology: Mid-30s, sharp tongue, cynical experience, kind at core.
Everything seen through "well, you know what I mean". Loves double entendres —
truth without getting accused. Provocation is art, not rudeness.

CHARACTER:
Provocative, witty, bold. You love when the reader doesn't get it at first,
then gets it — and laughs. Humor on the edge, but with taste.

STYLE:
- Ambiguous phrasing (two readings — one polite, one not)
- Euphemisms and hints instead of blunt crudeness
- RU: "ну вы понимаете", "скажем так", "как бы это помягче", "не буду уточнять"
  EN: "if you know what I mean", "let's just say...", "how do I put this..."
- Structure: innocent opening → unexpected turn
- Ellipsis where the reader fills in the rest...
- One emoji if it sharpens the effect

FORBIDDEN:
- Direct profanity
- Insulting specific people
- Crudeness without wit
- Being boring

Goal: make them laugh so they're ashamed to laugh — but not stop.""",

    "tyler": """You are Tyler. You're fed up with everything. You've seen enough to believe in nothing —
and smart enough to ironize instead of just whining.

Psychology: 40+, seen rises and falls, believed and been disappointed enough times
to develop immunity to optimism. Not angry — just honest.
Laughs because otherwise you'd cry. Irony as a smart person's defense against absurd reality.

CHARACTER:
Bile-filled, sarcastic, but witty. Your skepticism isn't malice —
it's honesty from someone life taught not to trust promises.

STYLE:
- OPEN with concrete news: who did what, names, numbers — like a dry wire report. No throat-clearing.
- Irony lives in the MIDDLE or at the END, never as the first words.
- RU — dry asides like "ну разумеется", "кто бы мог подумать" only mid-sentence or in the closing line
  EN — same: "classic", "shocking, truly", "what a surprise" only embedded in context, never as openers
- Rhetorical questions with no answer — reader already knows
- Absurdist agreement: you seem to agree but clearly mock
- Dry ending: bitter statement of fact
- No sincere enthusiasm
- You may pity people — but only in the last line, without sentimentality

FORBIDDEN:
- Starting with "Oh look", "Oh sure", "Well well", "So apparently", "Ah yes", or any faux-casual opener
- Opening with sarcasm before stating the actual news
- Sincere optimism
- "Great!", "Wonderful!", "This is a chance!" (without irony)
- Motivational tone
- Wordiness where one dry remark suffices

Write as if you already knew how it would end — and were right.""",
}
PERSONAS["smirnov"] = PERSONAS["thomson"]  # legacy alias

FACTUAL_RULES = """
CRITICAL — factual grounding:
Do not invent facts not present in the source material.
Every post must stand alone: establish WHO did WHAT up front, with real names —
stated as direct knowledge, not as a report on a source.
A reader who saw no article must fully understand the event from your post."""

NO_META_RULES = """
CRITICAL — never meta-comment on the source text:
Do NOT write as if you are reviewing, summarizing, or reporting on a document/study/article.
Write as your own take on the NEWS — direct statements about what happened or what it means.
Forbidden openers and phrases (any language), including but not limited to:
- "The article introduces / argues / notes / explains / claims / describes..."
- "The study / research / paper / report / findings show / outline / suggest / argue..."
- "Researchers found / note / argue...", "According to the study/research/article..."
- "Статья вводит понятие / рассказывает / отмечает / утверждает / объясняет..."
- "Исследование показывает / авторы отмечают / по данным исследования..."
- "В материале говорится...", "Автор пишет...", "По тексту...", "As the article says..."
- "The piece / report / write-up suggests..."
- Never open with generic labels like "Research Article", "Commentary", "News Article".
State facts and ideas directly. The reader should not feel they are reading a book report.
"""

POV_RULES = """
CRITICAL — write a THOUGHT of your own, not a recap, but never leave the reader without context:
The post is your own observation prompted by a real event — the way a sharp person posts a take, not a summary of an article.

Mandatory structure (in this order, woven into natural prose — do NOT label the parts):
1. CONTEXT FIRST (1 sentence, sometimes 2 for complex stories): ground the reader in the concrete event.
   Name the real actors (company/person) and what actually happened or changed, with one anchoring detail
   (a number, product, deal, scale). A reader who has seen no article must understand WHAT this is about.
   Do NOT open with a vague abstraction, a rhetorical question, or your opinion before the fact is stated.
2. THEN YOUR THOUGHT: the point you are actually making — the tension, the non-obvious implication,
   what it reveals, who really wins or loses, where it leads. This is the reason the post exists.

Hard rules:
- The fact is the springboard, not the subject. Do not narrate or explain the source; react to the world.
- Never reference "the article/study/report/piece" or that you read anything. The event is common knowledge you are commenting on.
- Do not invent facts. Use only what the source supports, but phrase it as direct knowledge, not attribution.
- If you cannot state the concrete event, the post is broken — always establish it before commenting.
"""

SHORT_FORMAT_RULES = """
SHORT FORMAT — strict (when not using bullet-brief mode):
- One cohesive post: flowing sentences only.
- NO bullet lists. NO lines starting with – or - or •.
- NO numbered lists. At most 2–3 short paragraphs plus optional punchline.
"""

# Hard platform limits (characters). Twitter/X is a hard publish limit.
PLATFORM_CHAR_LIMIT = {"twitter": 280, "telegram": 4096}


def _norm_platform(platform) -> str:
    p = (platform or "").strip().lower()
    if p in ("x", "twitter/x", "tweet"):
        return "twitter"
    return p or "telegram"


def _twitter_hard_rule(limit: int = 280) -> str:
    """Injected when the target platform is Twitter — a HARD length constraint."""
    aim = max(200, limit - 30)
    return (
        f"PLATFORM: Twitter/X. HARD LIMIT: the ENTIRE post — including spaces, "
        f"punctuation, source name and any URL — MUST be {limit} characters or fewer. "
        f"This is a publishing limit, not a suggestion: a longer post is rejected. "
        f"Aim for about {aim} characters to leave a safety margin. "
        f"Count as you write. If it does not fit, cut adjectives, background and the "
        f"weaker half of the point — never exceed the limit. One tight thought beats a "
        f"truncated one."
    )


def _source_link_rules(include_link: bool) -> str:
    if include_link:
        return (
            "SOURCE LINK: ON.\n"
            "- Attribute the outlet with a short name (WSJ, TechCrunch, FT, Bloomberg, etc.).\n"
            "- If a URL is provided in the input, include it in the post.\n"
            "- Brief format: line 1 = SOURCE_SHORT (URL): thesis — or SOURCE_SHORT: thesis if no URL.\n"
            "- Short format: name the outlet once (opener or closing line); add URL when available."
        )
    return (
        "SOURCE LINK: OFF.\n"
        "- Write as if from yourself — your own voice and knowledge.\n"
        "- Do NOT name the outlet, do NOT include a URL, do NOT write 'according to WSJ/the article'.\n"
        "- Do NOT open with SOURCE_SHORT or any source label.\n"
        "- Brief format: line 1 = thesis only (no outlet, no URL), then blank line, then – bullets.\n"
        "- Short format: state the news directly, no attribution line."
    )


def _brief_format_rules(numbers, include_link: bool = True) -> str:
    """Bullet-brief layout; numbers policy is injected separately and must win."""
    try:
        n = int(numbers)
    except (TypeError, ValueError):
        n = 1

    if n == 0:
        quant_arc = "setup → key developments → tensions/caveats → open questions → closing takeaway"
        quant_bullet = (
            "Each bullet = one concrete qualitative point (claim, risk, decision, or unanswered question). "
            "NO digits, NO percentages, NO dollar amounts, NO counts — rephrase quantities in words only if unavoidable, prefer omitting them."
        )
        quant_mix = "Mix: developments, management claims, doubts, second-order implications — without figures."
    elif n == 2:
        quant_arc = "setup → key facts and figures → tensions/caveats → open questions → closing takeaway"
        quant_bullet = (
            "Each bullet = one concrete point. Several bullets MUST carry specific numbers, "
            "percentages, or dollar amounts (from the article, or clearly framed estimates if the article lacks them)."
        )
        quant_mix = "Mix: hard facts, numbers, management claims, doubts, second-order implications."
    else:
        quant_arc = "setup → key facts and figures from the article → tensions/caveats → open questions → closing takeaway"
        quant_bullet = (
            "Each bullet = one concrete point (fact, claim, risk, or unanswered question). "
            "When the article has figures, put them in the bullets — do not drop them."
        )
        quant_mix = "Mix: hard facts, article numbers where present, management claims, doubts, second-order implications."

    if include_link:
        line1 = (
            "Line 1: SOURCE_SHORT: one-line thesis (the core claim, not a vague topic label).\n"
            "SOURCE_SHORT: common short name (WSJ, FT, Bloomberg, Reuters, NYT, CNBC, MarketWatch, TechCrunch, etc.).\n"
            "If a URL is provided, format line 1 as: SOURCE_SHORT (URL): thesis"
        )
    else:
        line1 = (
            "Line 1: one-line thesis only — no outlet name, no URL, no 'SOURCE:' prefix.\n"
            "Write the thesis as your own statement of the situation."
        )

    return f"""
FORMAT — deep bullet brief (Telegram news-digest style, like "42 секунды"):
You rewrite the FULL story as a structured brief, not a headline teaser.

Structure EXACTLY:
{line1}
Then a blank line.
Then 8–14 bullets, each on its own line, starting with an en-dash "– " (U+2013).

Bullet rules:
- Walk through the WHOLE story arc: {quant_arc}.
- {quant_bullet}
- Do NOT only restate the headline. Dig into body details a skimmer would miss.
- {quant_mix}
- Keep bullets short (one sentence each). No emojis, no hashtags, no markdown bold.
- No intro/outro outside this structure. No "In summary". No numbered lists.
- Preserve nuance: if the story is skeptical, bullets should reflect that skepticism.
- Never write "the article introduces..." / "статья вводит понятие..." — state points directly.
"""


def _brief_bullet_count(length: int) -> str:
    return {
        1: "5–7 bullets",
        2: "8–10 bullets",
        3: "10–12 bullets",
        4: "12–14 bullets",
        5: "13–16 bullets",
    }.get(length, "8–12 bullets")


def _numbers_mode(numbers) -> int:
    try:
        n = int(numbers)
    except (TypeError, ValueError):
        return 1
    return 0 if n < 0 else 2 if n > 2 else n


def _numbers_instruction(numbers) -> str:
    n = _numbers_mode(numbers)
    numbers_map = {
        0: (
            "NUMBERS POLICY (MANDATORY — overrides any other instruction to include figures): "
            "Do NOT use any digits, percentages, dollar amounts, ratios, or statistics. "
            "No '550%', no '$17M', no '18A yield rates' as numbers. Pure qualitative wording only."
        ),
        1: (
            "NUMBERS POLICY (MANDATORY): "
            "Include specific numbers, percentages, or dollar amounts from the article text. "
            "If the article contains figures, they must appear in the post. "
            "Only skip if the article has absolutely no quantitative data."
        ),
        2: (
            "NUMBERS POLICY (MANDATORY): "
            "Include numbers in every post. Use exact figures from the article. "
            "If the article lacks specific numbers, add reasonable industry estimates clearly framed as context "
            "(e.g. 'a market worth ~$400B'). Never post without at least one figure."
        ),
    }
    return numbers_map.get(n, numbers_map[1])


def _enforce_numbers_policy(text: str, numbers) -> str:
    """Hard filter: when mode is 0, strip quantitative figures the model still emitted."""
    if _numbers_mode(numbers) != 0 or not text:
        return text

    out = text
    # Currency: $1.2B, $ 17 million, 17 млн $, €10M
    out = re.sub(
        r"(?:[$€£]\s*)\d[\d\s,.]*\d?(?:\s?(?:[kKmMbBtT]|млн|млрд|тыс\.?|million|billion|thousand|bn|mn))?",
        "",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"\b\d[\d\s,.]*\d?\s?(?:млн|млрд|тыс\.?|million|billion|thousand|dollars?|euros?|руб\.?|usd|eur)\b",
        "",
        out,
        flags=re.I,
    )
    # Percents: 550%, 3,5 %
    out = re.sub(r"\b\d+(?:[.,]\d+)?\s*%", "", out)
    # Grouped integers: 1,000,000 / 1 000 000
    out = re.sub(r"(?<!\w)\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d+)?(?!\w)", "", out)
    # Decimals: 2.5, 3,14
    out = re.sub(r"(?<!\w)\d+[.,]\d+(?!\w)", "", out)
    # Multi-digit integers (years, counts) — keep single digits inside words like "iPhone"
    out = re.sub(r"(?<![A-Za-z0-9/])\d{2,}(?![A-Za-z0-9])", "", out)
    # Lone digit quantities: "в 3 раза" → "в раза" cleaned below
    out = re.sub(r"(?<![A-Za-z0-9/])\d(?![A-Za-z0-9])", "", out)
    # Orphan units left after stripping amounts
    out = re.sub(
        r"\b(?:млн|млрд|тыс\.?|million|billion|thousand|dollars?|euros?|руб\.?|usd|eur|bn|mn)\b",
        "",
        out,
        flags=re.I,
    )
    out = re.sub(r"\bна\s+(?=[,.!?]|$)", "", out, flags=re.I)
    out = re.sub(r"\bв\s+раза\b", "кратно", out, flags=re.I)

    # Tidy whitespace / empty bullets
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" ?([,;:]) ?", r"\1 ", out)
    out = re.sub(r"\s+([.!?])", r"\1", out)
    lines = []
    for line in out.splitlines():
        s = line.strip()
        if re.fullmatch(r"[–\-•*]+\s*", s):
            continue
        if s in (":", ";", "—", "–", "-"):
            continue
        lines.append(line.rstrip())
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _lang_instruction(lang: str, *, cinico: bool = False) -> str:
    if cinico:
        return {
            "en": "Write in English.",
            "ru": "Write entirely in Russian. Translate all facts and the comment into Russian. Keep the sharp Cinico voice — no softening.",
            "ua": "Write entirely in Ukrainian. Translate all facts and the comment into Ukrainian. Keep the sharp Cinico voice.",
        }.get(lang, "Write in English.")
    return {
        "en": "Write in English.",
        "ru": "Write entirely in Russian.",
        "ua": "Write entirely in Ukrainian.",
    }.get(lang, "Write in English.")


def _include_link(params: dict) -> bool:
    v = params.get("include_link", False)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _build_brief_system_prompt(params: dict) -> str:
    length = int(params.get("length", 3))
    depth = int(params.get("depth", 5))
    numbers = params.get("numbers", 1)
    lang = params.get("lang", "en")
    persona = params.get("persona", "neutral")
    cynicism = int(params.get("cynicism", 5))
    harsh = int(params.get("harsh", 5))
    include_link = _include_link(params)
    bullets = _brief_bullet_count(length)

    depth_str = (
        "Go deep: second-order effects, who benefits, what is still unproven, what remains open."
        if depth >= 7 else
        "Stay close to the story's own framing; light interpretation only."
        if depth <= 3 else
        "Include one or two non-obvious angles or tensions from the body, not only the lede."
    )

    persona_prompt = PERSONAS.get(persona)
    voice = ""
    if persona_prompt:
        voice = (
            f"\nVoice tint (secondary to format):\n{persona_prompt}\n"
            "Keep the bullet structure above. Persona affects wording only, not layout.\n"
        )
    elif cynicism >= 6 or harsh >= 6:
        voice = (
            f"\nTone: mild analytical edge. Cynicism {cynicism}/10, harshness {harsh}/10. "
            "Facts first; skepticism only in later bullets, never as the opener.\n"
        )
    else:
        voice = "\nTone: neutral news-digest. Clear, factual, no hype.\n"

    return f"""You write deep Telegram-style briefs.
{_brief_format_rules(numbers, include_link=include_link)}
{FACTUAL_RULES}
{NO_META_RULES}
{_source_link_rules(include_link)}
{_lang_instruction(lang)}
Target length: {bullets}.
Analysis depth: {depth}/10. {depth_str}

{_numbers_instruction(numbers)}
Obey NUMBERS POLICY strictly — it overrides format examples that mention figures.
{voice}
Max length ~3500 characters. Prefer substance over fluff."""


def _build_system_prompt(params: dict) -> str:
    if (params.get("post_format") or "short") == "brief":
        return _build_brief_system_prompt(params)

    cynicism = params.get("cynicism", 5)
    harsh    = params.get("harsh", 5)
    length   = params.get("length", 1)
    depth    = params.get("depth", 5)
    numbers  = params.get("numbers", 1)
    lang     = params.get("lang", "en")
    persona  = params.get("persona", "neutral")
    include_link = _include_link(params)

    length_map = {
        1: "Ultra-concise: Line 1 = concrete context (names + what happened). Line 2 = comment or punchline. Max 280 chars total.",
        2: "Short: 1-2 sentences of context (who, what, scale) + 1 closing line. Max 320 chars.",
        3: "Medium: 3-5 sentences. Enough context for someone unfamiliar with the story. End with your closing line. Max 450 chars.",
        4: "Long: 5-8 sentences. Full context — background, key development, scale, implications. Max 700 chars.",
        5: "Thread-length: comprehensive post covering context, event, scale, who's affected, what it means. Max 1000 chars.",
    }
    length_str = length_map.get(length, length_map[1])

    # Twitter has a HARD 280-char publish limit. When targeting Twitter, force the
    # ultra-concise length regardless of the slider and append the hard-limit rule,
    # so "short format" posts actually fit and are not rejected at publish time.
    platform = _norm_platform(params.get("platform"))
    twitter_rule = ""
    if platform == "twitter":
        limit = PLATFORM_CHAR_LIMIT["twitter"]
        length_str = length_map[1] if length > 2 else length_str
        twitter_rule = "\n" + _twitter_hard_rule(limit)

    numbers_str = _numbers_instruction(numbers)
    link_str = _source_link_rules(include_link)

    persona_prompt = PERSONAS.get(persona)
    if persona_prompt:
        return f"""{persona_prompt}
{FACTUAL_RULES}
{NO_META_RULES}
{POV_RULES}
{SHORT_FORMAT_RULES}
{link_str}

{_lang_instruction(lang)}
Stay fully in this persona's voice. Do not mix styles.
{length_str}{twitter_rule}
{numbers_str}"""

    prompt = CINICO_BASE + f"""

{_lang_instruction(lang, cinico=True)}
{POV_RULES}
{SHORT_FORMAT_RULES}
Cynicism level: {cynicism}/10. {"Be extremely cynical and sardonic." if cynicism >= 8 else "Be moderately cynical." if cynicism >= 5 else "Keep mild irony, stay factual."}
Harshness level: {harsh}/10. {"Be ruthless and cutting, no mercy." if harsh >= 8 else "Be direct but not brutal." if harsh >= 5 else "Stay measured and analytical."}
{length_str}{twitter_rule}
Analysis depth: {depth}/10. {"Go deep — expose systemic patterns, second-order effects, who benefits, who loses." if depth >= 7 else "Surface observation is fine." if depth <= 3 else "Show one non-obvious angle or hidden incentive."}
{numbers_str}
{NO_META_RULES}
{link_str}"""

    return prompt


def _post_topic_guidance(article: dict) -> str:
    """Optional conscious post topic from the article-topic step."""
    topic = (article.get("post_topic") or "").strip()
    angle = (article.get("post_angle") or "").strip()
    hook = (article.get("post_hook") or "").strip()
    if not topic and not angle:
        return ""
    parts = ["Post focus (derived from reading the full article — follow this, do not just rephrase the headline):"]
    if topic:
        parts.append(f"- Post topic: {topic}")
    if angle:
        parts.append(f"- Angle: {angle}")
    if hook:
        parts.append(f"- Concrete hook from the body: {hook}")
    parts.append("Build the post around this focus.")
    return "\n".join(parts) + "\n\n"


def _sanitize_meta_draft(text: str) -> str:
    """Drop generic title lines the model sometimes echoes into the post."""
    if not text:
        return text
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if _is_generic_title(s):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _build_user_prompt(article: dict, params: dict) -> str:
    title  = article['title']
    source = article.get('source', '')
    url    = (article.get('url') or '').strip()
    length = int(params.get("length", 1))
    persona = params.get("persona", "neutral")
    post_format = params.get("post_format") or "short"
    body_limit = 7000 if post_format == "brief" else 3000
    body = article.get('body', '')[:body_limit]
    focus = _post_topic_guidance(article)
    title_note = ""
    if _is_generic_title(title):
        title_note = (
            f"Note: the stored title \"{title}\" is only a document label, NOT the real headline. "
            "Ignore it — derive the subject from the body and write your own post voice.\n"
        )

    header = f"Article:\nTitle: {title}\nSource: {source}\n"
    if url:
        header += f"URL: {url}\n"
    header += f"Body: {body}\n\n"
    if title_note:
        header += title_note
    if focus:
        header += focus
        lang = params.get("lang", "en")
        lang_names = {"en": "English", "ru": "Russian", "ua": "Ukrainian"}
        header += (
            f"\nOutput language: {lang_names.get(lang, lang)} only — "
            "even if the focus above is in another language.\n\n"
        )

    include_link = _include_link(params)

    if post_format == "brief":
        bullets = _brief_bullet_count(length)
        numbers = params.get("numbers", 1)
        if include_link:
            line1 = (
                "Line 1 must be SOURCE_SHORT: thesis"
                + (" (include the URL in parentheses after SOURCE_SHORT)." if url else ".")
            )
        else:
            line1 = (
                "Line 1 must be the thesis only — no outlet name, no URL. "
                "Write as your own voice, not a recap of 'the article'."
            )
        return (
            f"{header}"
            f"Write a deep bullet brief.\n"
            f"Use {bullets}. Cover the full narrative, not only the headline.\n"
            f"{line1}\n"
            f"Then blank line, then bullets starting with \"– \".\n"
            f"Never use phrases like 'the article introduces' / 'the study outlines' / 'статья вводит понятие'.\n"
            f"{_numbers_instruction(numbers)}\n"
            f"{_source_link_rules(include_link)}\n"
            f"Return ONLY the brief text, nothing else."
        )

    if persona != "neutral":
        return (
            f"{header}"
            f"Write a post in your persona voice.\n"
            f"{SHORT_FORMAT_RULES}\n"
            f"Never use phrases like 'the article introduces' / 'the study outlines' / 'статья вводит понятие'.\n"
            f"{_source_link_rules(include_link)}\n"
            f"Return ONLY the post text, nothing else."
        )

    if length <= 2:
        instruction = (
            "Write a Cinico-style post as your own thought (NOT a recap of the article):\n"
            f"{SHORT_FORMAT_RULES}\n"
            "Line 1 — CONTEXT FIRST: state the concrete event directly, as common knowledge — "
            "name the company/people involved, what they did, and one anchoring detail (product, deal, market, scale). "
            "No vague openers, no 'the article', no rhetorical question before the fact.\n"
            "Line 2 (blank line before): YOUR biting one-liner — the non-obvious implication or what it really reveals.\n"
        )
    elif length == 3:
        instruction = (
            "Write a Cinico-style post as your own thought (NOT a recap of the article):\n"
            "Part 1 (2-4 sentences) — CONTEXT FIRST: establish what happened directly, as your own knowledge — "
            "who did what, what changed, what the scale is — enough that someone with zero context understands it. "
            "Do not narrate or attribute a source.\n"
            "Part 2 (blank line, 1 sentence): YOUR sharp take — the tension or implication, not a summary.\n"
        )
    else:  # 4 or 5
        instruction = (
            "Write a Cinico-style post as your own thought (NOT a recap of the article):\n"
            "Part 1 (4-7 sentences) — CONTEXT FIRST: lay out the situation directly, as common knowledge — "
            "background, what happened, who's involved, the scale, the consequences — so someone with zero context "
            "understands completely. State it as fact, never as 'the article says'.\n"
            "Part 2 (blank line, 1-2 sentences): YOUR bottom line — the real meaning and where it leads, your own view.\n"
        )

    return (
        f"{header}"
        f"{instruction}\n"
        f"Never use phrases like 'the article introduces' / 'статья вводит понятие'.\n"
        f"{_source_link_rules(include_link)}\n"
        f"Return ONLY the post text, nothing else."
    )


def _max_tokens_for_params(params: dict) -> int:
    length = int(params.get("length", 1))
    if (params.get("post_format") or "short") == "brief":
        return {1: 900, 2: 1200, 3: 1600, 4: 2000, 5: 2400}.get(length, 1600)
    return {1: 350, 2: 450, 3: 600, 4: 900, 5: 1300}.get(length, 400)


def _fit_twitter(text: str, params: dict, limit: int = 280) -> str:
    """Safety net: if a Twitter-bound draft exceeds the hard limit, ask the model
    to compress it to <= limit while keeping the core point and voice. One retry;
    on error/still-too-long, returns the best available text (never raises)."""
    if _norm_platform(params.get("platform")) != "twitter":
        return text
    if len(text or "") <= limit:
        return text
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        lang = params.get("lang", "en")
        lang_names = {"en": "English", "ru": "Russian", "ua": "Ukrainian"}
        aim = max(200, limit - 30)
        r = anthropic_call(
            client,
            model=MODEL_UTIL,
            max_tokens=400,
            temperature=0.4,
            system=(
                "You compress social posts to fit Twitter/X. Keep the author's voice, "
                "the concrete facts and the punchline. Drop adjectives, background and "
                "the weaker clause. Output ONLY the rewritten post, no commentary."
            ),
            messages=[{"role": "user", "content": (
                f"Rewrite this post in {lang_names.get(lang, lang)} so the ENTIRE text "
                f"is {limit} characters or fewer (aim ~{aim}). Keep it a single cohesive "
                f"post, no bullets. Preserve the main fact and the closing take.\n\n"
                f"POST ({len(text)} chars):\n{text}"
            )}],
        )
        out = _sanitize_meta_draft(r.content[0].text.strip())
        if out and len(out) <= limit:
            try:
                print(f"[twitter-fit] {len(text)} → {len(out)} chars", flush=True)
            except Exception:
                pass
            return out
        # If still too long, keep the shorter of the two candidates.
        return out if (out and len(out) < len(text)) else text
    except Exception:
        return text


def generate_tweet_draft_claude(article: dict, params: dict) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system = _build_system_prompt(params)
    prompt = _build_user_prompt(article, params)
    r = anthropic_call(
        client,
        model=MODEL_WRITE,
        max_tokens=_max_tokens_for_params(params),
        system=system,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.85,
    )
    draft = _sanitize_meta_draft(_enforce_numbers_policy(r.content[0].text.strip(), params.get("numbers", 1)))
    return _fit_twitter(draft, params)


def generate_tweet_draft_gpt(article: dict, params: dict) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    system = _build_system_prompt(params)
    prompt = _build_user_prompt(article, params)
    r = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=_max_tokens_for_params(params),
        temperature=0.85,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    draft = _sanitize_meta_draft(_enforce_numbers_policy(
        r.choices[0].message.content.strip(),
        params.get("numbers", 1),
    ))
    return _fit_twitter(draft, params)


def _focus_profile() -> dict:
    """User content focus from env: what's interesting vs boring for prioritization."""
    interests = (os.getenv("FOCUS_INTERESTS") or "").strip()
    boring = (os.getenv("FOCUS_BORING") or "").strip()
    return {"interests": interests, "boring": boring}


def _env_float(name: str, default: float) -> float:
    """Read a float from env, falling back to default on missing/garbage."""
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def focus_ranking_config() -> dict:
    """Tunable ranking weights from env (see .env FOCUS_* keys)."""
    return {
        "w_importance": _env_float("FOCUS_WEIGHT_IMPORTANCE", 0.5),
        "w_interest": _env_float("FOCUS_WEIGHT_INTEREST", 0.5),
        "boring_penalty": _env_float("FOCUS_BORING_PENALTY", 3.0),
        "drop_boring": _env_bool("FOCUS_DROP_BORING", True),
    }


def _focus_prompt_block(search_context: str = "") -> str:
    """Reusable profile block injected into ranking / topic / cluster prompts."""
    prof = _focus_profile()
    lines: list[str] = []
    if search_context:
        lines.append(f"Export search (Factiva/TechCrunch query): {search_context}")
    if prof["interests"]:
        lines.append(f"User INTERESTS (surface these, rank high): {prof['interests']}")
    if prof["boring"]:
        lines.append(f"User finds BORING (rank low / skip): {prof['boring']}")
    if not lines:
        return ""
    return "User content focus:\n" + "\n".join(f"- {l}" for l in lines) + "\n"


def score_articles_by_focus(
    articles: list[dict],
    indices: list[int],
    search_context: str = "",
    *,
    model: str = MODEL_UTIL,
) -> dict[int, dict]:
    """Score each article for importance + fit to the user's focus profile.

    Returns {index: {"importance": int, "interest_fit": int, "boring": bool,
                     "rank": float, "reason": str}}.
    importance   0-10: how significant/newsworthy the story is (scale, impact, novelty).
    interest_fit 0-10: how well it matches the user's stated interests.
    boring       true: niche/local/routine story with no broad hook.
    rank: combined score used for strict top-N ordering.
    On any failure returns {} so the caller can fall back to the old ranker.
    """
    import json as _json
    import re as _re
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY") or not indices:
        return {}

    focus_block = _focus_prompt_block(search_context)
    if not focus_block:
        # No profile configured — nothing meaningful to score against.
        return {}

    pairs = [(i, articles[i]) for i in indices if 0 <= i < len(articles)]
    if not pairs:
        return {}

    cfg = focus_ranking_config()
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    scores: dict[int, dict] = {}
    batch_size = 10

    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start + batch_size]
        blocks = []
        for i, art in batch:
            body = (art.get("body") or "")[:1800]
            blocks.append(
                f"### INDEX {i}\n"
                f"Title: {art.get('title', '')}\n"
                f"Source: {art.get('source', '')}\n"
                f"Body:\n{body}\n"
            )
        prompt = f"""You are a senior editor deciding which news is worth posting for a channel about technology, business and finance.
{focus_block}
For EACH article below, read the BODY (not just the title) and rate:
- importance (0-10): objective newsworthiness — scale, market/industry impact, novelty, how many people/companies it affects. A major deal, funding round, product launch, regulation, or market move is high. A local/routine item is low.
- interest_fit (0-10): how well it matches the USER INTERESTS above. Off-focus or BORING themes score low.
- boring (true/false): true if it's a niche, local, or routine story with no broad tech/business/finance hook (e.g. "3G rollout in one country", obscure regional/military/humanitarian item, plain corporate press release).
- reason: max 12 words, why.

Be decisive and spread the scores — do not cluster everything in the middle. Reserve 8-10 for genuinely significant, on-focus stories.

Articles:
{chr(10).join(blocks)}

Return a JSON array ONLY — no markdown fences:
[{{"index":N,"importance":0-10,"interest_fit":0-10,"boring":false,"reason":"..."}}]
Include every INDEX exactly once."""
        try:
            r = anthropic_call(
                client,
                model=model,
                max_tokens=1500,
                temperature=0.2,
                system="You output only raw JSON arrays. No markdown, no explanation.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = r.content[0].text.strip()
            match = _re.search(r"\[[\s\S]*\]", raw)
            if not match:
                continue
            for t in _json.loads(match.group(0)):
                if "index" not in t:
                    continue
                idx = int(t["index"])
                imp = max(0, min(10, int(t.get("importance", 0) or 0)))
                fit = max(0, min(10, int(t.get("interest_fit", 0) or 0)))
                boring = bool(t.get("boring") in (True, "true", 1, "1"))
                # Combined rank with env-tunable weights; boring is penalized.
                rank = (imp * cfg["w_importance"] + fit * cfg["w_interest"]) - (
                    cfg["boring_penalty"] if boring else 0.0
                )
                scores[idx] = {
                    "importance": imp,
                    "interest_fit": fit,
                    "boring": boring,
                    "rank": rank,
                    "reason": (t.get("reason") or "")[:120],
                }
        except Exception:
            # Fail soft: skip this batch, caller falls back if scores end up empty.
            continue

    return scores


def _topic_text(t: dict) -> str:
    """Combined comparable text for a generated topic (title + topic + angle)."""
    return " ".join([
        (t.get("title") or ""),
        (t.get("post_topic") or ""),
        (t.get("angle") or ""),
    ]).strip()


def _topic_quality(t: dict) -> tuple:
    """Higher is better when picking the survivor of a duplicate story group."""
    has_hook = 1 if (t.get("hook") or "").strip() else 0
    has_url = 1 if (t.get("url") or "").strip() else 0
    return (has_hook, has_url, len(t.get("angle") or ""), len(t.get("post_topic") or ""))


def dedup_topics_by_story(
    topics: list[dict],
    *,
    sim_threshold: float = 0.34,
    use_llm: bool = True,
    model: str = MODEL_UTIL,
) -> list[dict]:
    """Collapse topics that cover the SAME underlying story (across sources).

    Two-stage:
      1) Cheap pre-grouping by title/topic token similarity to form candidate
         clusters (union-find style). This alone catches obvious same-title dups.
      2) LLM confirmation on the whole set (broad grouping: same event/story even
         if the angle differs slightly) to catch cross-source dups that share few
         literal tokens. Falls back to stage-1 grouping if no API key / error.

    Keeps ONE best topic per story (see _topic_quality) and preserves input order.
    Never raises: on any failure returns the input unchanged.
    """
    if not topics or len(topics) < 2:
        return topics

    n = len(topics)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Stage 1: token-similarity pre-grouping on title+topic.
    norm = [_norm_title(_topic_text(t)) for t in topics]
    toks = [_title_tokens(nx) for nx in norm]
    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(toks[i], toks[j]) >= sim_threshold:
                union(i, j)

    # Stage 2: LLM confirmation across the full set (broad, same-story grouping).
    if use_llm and os.getenv("ANTHROPIC_API_KEY") and n <= 60:
        try:
            import json as _json
            import re as _re
            import anthropic

            lines = []
            for i, t in enumerate(topics):
                lines.append(
                    f"[{i}] title: {t.get('title','')}\n"
                    f"    topic: {t.get('post_topic','')}\n"
                    f"    source: {t.get('source','')}"
                )
            prompt = (
                "Below is a numbered list of proposed social-media post topics.\n"
                "Group together the items that cover the SAME underlying story or "
                "event, even if they come from different sources or frame it from a "
                "slightly different angle (e.g. the same lawsuit, the same deal, the "
                "same product launch reported twice). Group broadly: if a reader "
                "would see two posts as 'the same news', put them together.\n"
                "Do NOT group items that are merely on the same broad theme but are "
                "genuinely different events.\n\n"
                + "\n".join(lines)
                + "\n\nReturn a JSON array of groups ONLY, each group a list of the "
                "item numbers that are the same story. Include every number exactly "
                "once (singletons as one-element lists). No markdown.\n"
                'Example: [[0,3],[1],[2,4,5]]'
            )
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            r = anthropic_call(
                client,
                model=model,
                max_tokens=1200,
                temperature=0.1,
                system="You output only a raw JSON array of arrays of integers.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = r.content[0].text.strip()
            match = _re.search(r"\[[\s\S]*\]", raw)
            if match:
                groups = _json.loads(match.group(0))
                for g in groups:
                    idxs = [int(x) for x in g if isinstance(x, (int, float, str))
                            and str(x).lstrip("-").isdigit() and 0 <= int(x) < n]
                    for k in idxs[1:]:
                        union(idxs[0], k)
        except Exception:
            pass  # keep stage-1 grouping

    # Collect groups, keep the best topic per group, preserve first-seen order.
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    kept_positions: list[int] = []
    for members in groups.values():
        best = max(members, key=lambda i: _topic_quality(topics[i]))
        kept_positions.append(best)
    kept_positions.sort()

    deduped = [topics[i] for i in kept_positions]
    dropped = n - len(deduped)
    if dropped:
        try:
            print(f"[dedup-topics] {n} → {len(deduped)} (схлопнуто дублей: {dropped})", flush=True)
        except Exception:
            pass
    return deduped


def _article_text_for_dedup(art: dict) -> str:
    """Title + lead (first ~250 chars of body) for story-level comparison."""
    title = (art.get("title") or "").strip()
    lead = (art.get("body") or "").strip()[:250]
    return f"{title}\n{lead}".strip()


def dedup_articles_by_story(
    pairs: list[tuple],
    *,
    sim_threshold: float = 0.30,
    use_llm: bool = True,
    model: str = MODEL_UTIL,
) -> list[tuple]:
    """Collapse (index, article) pairs that cover the SAME story BEFORE topic
    generation, so duplicates never cost an opus/util call and never occupy a
    top-N slot. Compares title + lead. Keeps the best article per story
    (_article_quality). Two-stage (token sim + LLM), fail-soft, order-preserving.
    """
    if not pairs or len(pairs) < 2:
        return pairs

    n = len(pairs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    texts = [_article_text_for_dedup(a) for _, a in pairs]
    toks = [_title_tokens(_norm_title(t)) for t in texts]
    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(toks[i], toks[j]) >= sim_threshold:
                union(i, j)

    if use_llm and os.getenv("ANTHROPIC_API_KEY") and n <= 60:
        try:
            import json as _json
            import re as _re
            import anthropic

            lines = []
            for i, (_, a) in enumerate(pairs):
                lead = (a.get("body") or "").strip()[:160]
                lines.append(
                    f"[{i}] title: {a.get('title','')}\n"
                    f"    source: {a.get('source','')}\n"
                    f"    lead: {lead}"
                )
            prompt = (
                "Below is a numbered list of news articles (title + lead).\n"
                "Group together items that report the SAME underlying story or "
                "event, even from different sources or with a slightly different "
                "angle (same lawsuit, same deal, same launch reported twice). Group "
                "broadly: if a reader would call them 'the same news', group them.\n"
                "Do NOT group items that are merely on the same broad theme but are "
                "genuinely different events.\n\n"
                + "\n".join(lines)
                + "\n\nReturn a JSON array of groups ONLY, each group a list of item "
                "numbers that are the same story. Every number exactly once. No markdown.\n"
                'Example: [[0,3],[1],[2,4,5]]'
            )
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            r = anthropic_call(
                client, model=model, max_tokens=1200, temperature=0.1,
                system="You output only a raw JSON array of arrays of integers.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = r.content[0].text.strip()
            match = _re.search(r"\[[\s\S]*\]", raw)
            if match:
                for g in _json.loads(match.group(0)):
                    idxs = [int(x) for x in g if str(x).lstrip("-").isdigit()
                            and 0 <= int(x) < n]
                    for k in idxs[1:]:
                        union(idxs[0], k)
        except Exception:
            pass

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    kept: list[int] = []
    for members in groups.values():
        best = max(members, key=lambda i: _article_quality(pairs[i][1]))
        kept.append(best)
    kept.sort()

    deduped = [pairs[i] for i in kept]
    dropped = n - len(deduped)
    if dropped:
        try:
            print(f"[dedup-articles] {n} → {len(deduped)} (схлопнуто дублей до генерации: {dropped})", flush=True)
        except Exception:
            pass
    return deduped


def generate_article_topics(
    rtf_path: str,
    selected_indices: list | None = None,
    max_articles: int = 40,
    lang: str = "ru",
    search_context: str = "",
) -> list[dict]:
    """Read full articles and propose conscious post topics (not headline restatements)."""
    import json as _json
    import re as _re
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY not set in .env")

    all_articles = load_articles(rtf_path)
    if not all_articles:
        raise ValueError("No articles found in export file")

    if selected_indices is not None and len(selected_indices) > 0:
        idx_set = set(int(i) for i in selected_indices)
        pairs = [(i, a) for i, a in enumerate(all_articles) if i in idx_set]
    else:
        pairs = list(enumerate(all_articles))

    pairs = pairs[: max(1, min(int(max_articles), 60))]
    if not pairs:
        raise ValueError("No articles selected")

    # Collapse same-story duplicates BEFORE the expensive per-article topic
    # generation, so dupes don't cost LLM calls or occupy top-N slots.
    pairs = dedup_articles_by_story(pairs)

    lang_line = {
        "en": "Write post_topic, angle, and hook in English.",
        "ru": "Write post_topic, angle, and hook in Russian.",
        "ua": "Write post_topic, angle, and hook in Ukrainian.",
    }.get(lang, "Write post_topic, angle, and hook in Russian.")

    profile_block = _focus_prompt_block(search_context)
    focus_block = ""
    if profile_block:
        focus_block = f"""
{profile_block}
Relevance & priority rules:
- Propose post topics ONLY for articles that clearly fit the user's INTERESTS above OR have obvious broad tech/business/finance appeal.
- Push down / skip BORING items: niche or local stories (e.g. "3G rollout in one country"), obscure regional/military/humanitarian micro-topics, and routine press releases with no broad hook.
- For such off-focus items set "skip": true (still return the object with its index, leave post_topic empty).
- Favor the genuinely important and interesting: big deals, funding, launches, regulation, market moves, strategic shifts.
"""

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    results: list[dict] = []
    batch_size = 8

    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start + batch_size]
        blocks = []
        for i, art in batch:
            body = (art.get("body") or "")[:3500]
            blocks.append(
                f"### INDEX {i}\n"
                f"Title: {art.get('title', '')}\n"
                f"Source: {art.get('source', '')}\n"
                f"Body:\n{body}\n"
            )
        prompt = f"""You are an editor planning social posts from news articles.
For EACH article below, read the BODY (not only the title) and propose a conscious post topic.
{focus_block}
Rules:
- post_topic: 5–12 words — the real story worth posting, not a headline paraphrase.
- angle: one sentence — WHY this matters to a tech/business/finance reader (the stakes, the non-obvious implication, who wins/loses), not a summary.
- hook: one concrete detail from the body (number, quote, name, claim) that grounds the topic. Empty string if none.
- skip: true if this article is off-focus or boring per the rules above (niche/local/routine with no broad hook).
- Do NOT invent facts. If the body is thin, still prefer body over title.
- Prefer second-order meaning over the lede when the body supports it.
- {lang_line}

Articles:
{chr(10).join(blocks)}

Return a JSON array ONLY — no markdown fences:
[{{"index":N,"post_topic":"...","angle":"...","hook":"...","skip":false}}]
Include every INDEX from the input exactly once."""

        r = anthropic_call(
            client,
            model=MODEL_UTIL,
            max_tokens=2500,
            temperature=0.4,
            system="You output only raw JSON arrays. No markdown, no explanation.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = r.content[0].text.strip()
        match = _re.search(r"\[[\s\S]*\]", raw)
        if not match:
            raise ValueError(f"Claude returned non-JSON: {raw[:300]}")
        batch_topics = _json.loads(match.group(0))
        by_idx = {int(t["index"]): t for t in batch_topics if "index" in t}

        for i, art in batch:
            t = by_idx.get(i, {})
            if t.get("skip") in (True, "true", 1, "1"):
                continue
            post_topic = (t.get("post_topic") or "").strip()
            if not post_topic:
                continue
            results.append({
                "article_index": i,
                "title": art.get("title", ""),
                "source": art.get("source", ""),
                "date": art.get("date", ""),
                "url": art.get("url", ""),
                "post_topic": post_topic[:160],
                "angle": (t.get("angle") or "")[:280],
                "hook": (t.get("hook") or "")[:200],
            })

    # Story dedup already ran on the articles up front; keep only a cheap
    # similarity-based safety net here (no extra LLM call).
    results = dedup_topics_by_story(results, use_llm=False)
    return results


def _title_similarity(a: str, b: str) -> float:
    """0..1 similarity for fuzzy title resolve."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if len(shorter) >= 20:
            return 0.9
    return _jaccard(_title_tokens(a), _title_tokens(b))


def _resolve_article_pairs(
    all_articles: list[dict],
    *,
    article_topics_list: list | None = None,
    selected_indices: list | None = None,
    topic_map: dict | None = None,
    excluded_sources: list | None = None,
    max_articles: int = 50,
    strict_selection: bool = False,
) -> list[tuple[int, dict]]:
    """Build (index, article) pairs.

    Resolution order per topic:
      1) trust article_index if in range (title mismatch is OK — index is source of truth)
      2) exact normalized title
      3) fuzzy title (containment / Jaccard ≥ 0.72)
    Then fill remaining slots from selected_indices / all articles so we always
    reach min(max_articles, available) when the file has enough pieces.
    """
    topic_map = topic_map or {}
    by_idx = {i: a for i, a in enumerate(all_articles)}
    title_index: list[tuple[str, int]] = []
    by_title: dict[str, list[int]] = {}
    for i, a in enumerate(all_articles):
        key = _norm_title(a.get("title", ""))
        if key:
            by_title.setdefault(key, []).append(i)
            title_index.append((key, i))

    excl_lower = set()
    if excluded_sources:
        excl_lower = {s.strip().lower() for s in excluded_sources if s.strip()}

    def allowed(art: dict) -> bool:
        if not excl_lower:
            return True
        src = (art.get("source") or "").strip().lower()
        return src not in excl_lower

    pairs: list[tuple[int, dict]] = []
    seen: set[int] = set()

    def attach_meta(art: dict, meta: dict) -> dict:
        art = dict(art)
        if meta.get("post_topic"):
            art["post_topic"] = meta["post_topic"]
        if meta.get("angle") or meta.get("post_angle"):
            art["post_angle"] = meta.get("angle") or meta.get("post_angle")
        if meta.get("hook") or meta.get("post_hook"):
            art["post_hook"] = meta.get("hook") or meta.get("post_hook")
        return art

    def add_pair(idx: int, art: dict, meta: dict) -> bool:
        if len(pairs) >= max_articles or idx in seen:
            return False
        if not allowed(art):
            return False
        pairs.append((idx, attach_meta(art, meta)))
        seen.add(idx)
        return True

    def find_by_title(title: str) -> int | None:
        title_key = _norm_title(title)
        if not title_key:
            return None
        for cand_i in by_title.get(title_key, []):
            if cand_i not in seen:
                return cand_i
        # Fuzzy: best similarity among unused
        best_i, best_score = None, 0.0
        for key, cand_i in title_index:
            if cand_i in seen:
                continue
            score = _title_similarity(title_key, key)
            if score > best_score:
                best_score, best_i = score, cand_i
        if best_i is not None and best_score >= 0.72:
            return best_i
        return None

    def try_add(idx: int | None, title: str, meta: dict) -> bool:
        if len(pairs) >= max_articles:
            return False

        # 1) Trust index when valid — do not reject on title mismatch
        if idx is not None and idx in by_idx and idx not in seen:
            if add_pair(idx, by_idx[idx], meta):
                return True

        # 2–3) Exact / fuzzy title
        found = find_by_title(title)
        if found is not None:
            return add_pair(found, by_idx[found], meta)
        return False

    # Preferred path: explicit topic list (order preserved)
    if article_topics_list:
        for t in article_topics_list:
            if not isinstance(t, dict):
                continue
            if len(pairs) >= max_articles:
                break
            raw_idx = t.get("article_index", t.get("index"))
            try:
                idx = int(raw_idx) if raw_idx is not None else None
            except (TypeError, ValueError):
                idx = None
            meta = {
                "post_topic": t.get("post_topic", ""),
                "angle": t.get("angle") or t.get("post_angle", ""),
                "hook": t.get("hook") or t.get("post_hook", ""),
            }
            try_add(idx, t.get("title") or "", meta)

    if selected_indices is not None and len(selected_indices) > 0:
        for raw_idx in selected_indices:
            if len(pairs) >= max_articles:
                break
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx in seen:
                continue
            meta = topic_map.get(idx) or {}
            title = meta.get("title") or ""
            try_add(idx, title, meta)

    # Fill remaining slots only when the caller did not pass an explicit selection.
    if not strict_selection and len(pairs) < max_articles:
        for i, a in enumerate(all_articles):
            if len(pairs) >= max_articles:
                break
            if i in seen:
                continue
            meta = topic_map.get(i) or {}
            add_pair(i, a, meta)

    return pairs


def generate_drafts_for_file(rtf_path: str, max_articles: int = 50, params: dict = None,
                             selected_indices: list = None,
                             excluded_sources: list = None,
                             article_topics_list: list = None,
                             strict_selection: bool | None = None) -> dict:
    if params is None:
        params = {}
    if strict_selection is None:
        strict_selection = bool(article_topics_list) or (
            selected_indices is not None and len(selected_indices) > 0
        )
    ai_model = params.get("ai_model", "claude")
    topic_map = params.get("article_topics") or {}
    # keys may arrive as strings from JSON
    topic_map = {int(k): v for k, v in topic_map.items() if str(k).lstrip("-").isdigit()}

    if ai_model == "gpt":
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY not set in .env")
        generate_fn = lambda art: generate_tweet_draft_gpt(art, params)
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY not set in .env")
        generate_fn = lambda art: generate_tweet_draft_claude(art, params)

    all_articles = load_articles(rtf_path)
    if not all_articles:
        raise ValueError("No articles found in export file")

    want = max_articles

    pairs = _resolve_article_pairs(
        all_articles,
        article_topics_list=article_topics_list,
        selected_indices=selected_indices,
        topic_map=topic_map,
        excluded_sources=excluded_sources,
        max_articles=max_articles,
        strict_selection=strict_selection,
    )

    # How many of the first `want` came from explicit topic resolve vs fill
    topic_resolved = 0
    if article_topics_list:
        topic_titles = {
            _norm_title(t.get("title") or "")
            for t in article_topics_list
            if isinstance(t, dict) and (t.get("title") or "").strip()
        }
        topic_idxs = set()
        for t in article_topics_list:
            if not isinstance(t, dict):
                continue
            try:
                topic_idxs.add(int(t.get("article_index", t.get("index"))))
            except (TypeError, ValueError):
                pass
        for i, art in pairs:
            title_key = _norm_title(art.get("title") or "")
            if i in topic_idxs or (title_key and title_key in topic_titles):
                topic_resolved += 1

    results = []
    for i, art in pairs:
        try:
            draft = generate_fn(art)
        except Exception as e:
            draft = (art.get("title") or "")[:200] + " — " + str(e)[:80]
        results.append({
            "title": _display_title(art),
            "source": art.get("source", ""),
            "date": art.get("date", ""),
            "url": art.get("url", ""),
            "body_snippet": (art.get("body") or "")[:300],
            "draft": draft,
            "post_topic": art.get("post_topic", ""),
            "article_index": i,
        })

    generated = len(results)
    # Cap "requested" by what the file can actually supply after filters
    available = len(all_articles)
    if excluded_sources:
        excl_lower = {s.strip().lower() for s in excluded_sources if s.strip()}
        available = sum(
            1 for a in all_articles
            if (a.get("source") or "").strip().lower() not in excl_lower
        )
    target = min(want, available)

    return {
        "drafts": results,
        "requested": target,
        "generated": generated,
        "available_articles": available,
        "topic_resolved": topic_resolved,
        "filled_from_pool": max(0, generated - topic_resolved),
        "skipped": max(0, target - generated),
    }


def find_article_in_file(rtf_path: str, title: str) -> dict | None:
    for art in load_articles(rtf_path):
        if art.get("title") == title:
            return art
    return None


def _cross_source_articles(rtf_path: str, title: str, source: str, limit: int = 3) -> list[dict]:
    articles = load_articles(rtf_path)
    others = [
        a for a in articles
        if a.get("title") != title and (a.get("source") or "").strip() != (source or "").strip()
    ]
    if not others:
        others = [a for a in articles if a.get("title") != title]
    return others[:limit]


def _refine_instruction(action: str, post_format: str = "short") -> str:
    if post_format == "brief":
        instructions = {
            "rewrite": (
                "Rewrite this brief from scratch using the main article. "
                "Keep the deep-bullet format: SOURCE_SHORT: thesis, blank line, then – bullets. "
                "Cover the full article arc with fresh wording. "
                "Obey the NUMBERS POLICY from the system prompt exactly."
            ),
            "numbers": (
                "Revise this brief to include more specific numbers, percentages, and dollar amounts "
                "from the article. Keep the SOURCE_SHORT: thesis + – bullet structure. "
                "Add at least 3 concrete figures across the bullets."
            ),
            "lengthen": (
                "Expand this brief with more body detail: background, caveats, open questions, "
                "second-order effects. Add 3–5 new bullets. Keep SOURCE_SHORT: thesis header "
                "and en-dash bullets. Obey the NUMBERS POLICY from the system prompt exactly."
            ),
            "enrich": (
                "Enhance this brief by weaving in relevant facts from the OTHER articles below. "
                "Keep the bullet-brief format. Only use facts present in the provided materials. "
                "Obey the NUMBERS POLICY from the system prompt exactly."
            ),
        }
    else:
        instructions = {
            "rewrite": (
                "Rewrite this post from scratch using the main article. "
                "Same facts and Cinico voice, fresh wording. "
                "Open with concrete names and what happened — no vague openers."
            ),
            "numbers": (
                "Revise this post to include specific numbers, percentages, dollar amounts, "
                "or scale metrics from the article. Add at least 2 concrete figures. "
                "Keep Cinico voice and the cynical closing line."
            ),
            "lengthen": (
                "Expand this post with more detail from the article: background, what happened, "
                "scale, implications. Make it roughly 50–80% longer. "
                "Keep context-first opening and end with a sharp cynical comment."
            ),
            "enrich": (
                "Enhance this post by weaving in relevant facts from the OTHER articles below "
                "(different sources from the same export). Add cross-source context that sharpens the point. "
                "Only use facts present in the provided materials."
            ),
        }
    return instructions.get(action, instructions["rewrite"])


def _build_refine_prompt(action: str, article: dict, current_text: str,
                         related: list[dict] | None = None,
                         post_format: str = "short") -> str:
    body_limit = 7000 if post_format == "brief" else 3500
    parts = [
        f"Current post:\n{current_text}\n",
        f"Main article:\nTitle: {article.get('title', '')}\n"
        f"Source: {article.get('source', '')}\n"
        f"Body: {article.get('body', '')[:body_limit]}\n",
    ]
    if related:
        parts.append("Other articles from the same export (different sources):")
        for i, a in enumerate(related, 1):
            parts.append(
                f"\n[{i}] {a.get('source', '')} — {a.get('title', '')}\n"
                f"{a.get('body', '')[:1200]}"
            )
    parts.append(f"\nTask: {_refine_instruction(action, post_format)}\n")
    parts.append(
        "Never use phrases like 'the article introduces' / 'статья вводит понятие'. "
        "Obey SOURCE LINK rules from the system prompt.\n"
    )
    parts.append("Return ONLY the revised post text, nothing else.")
    return "\n".join(parts)


def refine_draft_post(
    action: str,
    rtf_path: str,
    title: str,
    source: str,
    current_text: str,
    params: dict | None = None,
) -> str:
    if params is None:
        params = {}
    if not current_text.strip():
        raise ValueError("Post text is empty")

    article = find_article_in_file(rtf_path, title)
    if not article:
        article = {"title": title, "source": source, "body": ""}

    related = None
    if action == "enrich":
        related = _cross_source_articles(rtf_path, title, source)

    post_format = params.get("post_format") or "short"
    refine_params = dict(params)
    if action == "lengthen":
        refine_params["length"] = min(int(params.get("length", 2)) + 1, 5)
    if action == "numbers":
        refine_params["numbers"] = 2

    system = _build_system_prompt(refine_params)
    prompt = _build_refine_prompt(action, article, current_text, related, post_format)
    if post_format == "brief":
        max_tok = {"rewrite": 1600, "numbers": 1700, "lengthen": 2200, "enrich": 2200}.get(action, 1600)
    else:
        max_tok = {"rewrite": 500, "numbers": 550, "lengthen": 900, "enrich": 1000}.get(action, 500)

    ai_model = params.get("ai_model", "claude")
    if ai_model == "gpt":
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        r = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=max_tok,
            temperature=0.85,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        text = r.choices[0].message.content.strip()
    else:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        r = anthropic_call(
            client,
            model=MODEL_WRITE,
            max_tokens=max_tok,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
        )
        text = r.content[0].text.strip()

    # Action "numbers" intentionally adds figures — don't strip them
    if action != "numbers":
        text = _enforce_numbers_policy(text, refine_params.get("numbers", 1))
    return text


def _twitter_env_status() -> dict:
    keys = {
        "TWITTER_API_KEY": os.getenv("TWITTER_API_KEY"),
        "TWITTER_API_SECRET": os.getenv("TWITTER_API_SECRET"),
        "TWITTER_ACCESS_TOKEN": os.getenv("TWITTER_ACCESS_TOKEN"),
        "TWITTER_ACCESS_SECRET": (
            os.getenv("TWITTER_ACCESS_SECRET") or os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        ),
    }
    missing = [k for k, v in keys.items() if not v]
    return {"keys": keys, "missing": missing, "ready": not missing}


def _format_twitter_error(exc: Exception) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "403" in msg or "forbidden" in low or "not permitted" in low:
        return (
            "Twitter 403 Forbidden — нет прав на публикацию. "
            "В developer.x.com: App → Settings → User authentication → "
            "Permissions = Read and Write, затем заново сгенерируйте "
            "Access Token и Access Token Secret и обновите .env на сервере."
        )
    if "401" in msg or "unauthorized" in low:
        return (
            "Twitter 401 — неверные ключи или токены. "
            "Проверьте TWITTER_API_KEY/SECRET и ACCESS_TOKEN/SECRET в .env."
        )
    return msg


def get_twitter_client():
    import tweepy
    status = _twitter_env_status()
    if status["missing"]:
        raise ValueError("Missing Twitter keys: " + ", ".join(status["missing"]))
    keys = status["keys"]
    return tweepy.Client(
        consumer_key=keys["TWITTER_API_KEY"],
        consumer_secret=keys["TWITTER_API_SECRET"],
        access_token=keys["TWITTER_ACCESS_TOKEN"],
        access_token_secret=keys["TWITTER_ACCESS_SECRET"],
    )


def verify_twitter_access(*, test_post: bool = False) -> dict:
    """Check credentials and write permission."""
    client = get_twitter_client()
    me = client.get_me()
    if not me or not me.data:
        raise RuntimeError("Twitter get_me failed — токены не дают доступ к аккаунту")
    out = {
        "ok": True,
        "username": me.data.username,
        "user_id": str(me.data.id),
        "can_post": False,
    }
    if test_post:
        r = client.create_tweet(text="FACT API test — please ignore")
        out["can_post"] = True
        out["test_tweet_id"] = str(r.data["id"])
    else:
        out["can_post"] = True
    return out


def post_tweet(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Tweet text is empty")
    if len(text) > 280:
        raise ValueError(f"Tweet too long ({len(text)} chars, max 280)")
    try:
        client = get_twitter_client()
        me = client.get_me()
        if not me or not me.data:
            raise RuntimeError("Twitter get_me failed — проверьте токены")
        r = client.create_tweet(text=text)
        tid = r.data["id"]
        u = me.data.username
        return {"id": str(tid), "url": f"https://twitter.com/{u}/status/{str(tid)}"}
    except Exception as e:
        raise RuntimeError(_format_twitter_error(e)) from e


def delete_tweet(tweet_id: str) -> dict:
    client = get_twitter_client()
    client.delete_tweet(str(tweet_id))
    return {"ok": True, "id": str(tweet_id)}


def edit_tweet(tweet_id: str, text: str) -> dict:
    """Twitter free API has no edit — delete old tweet and post a new one."""
    try:
        delete_tweet(tweet_id)
    except Exception:
        pass
    result = post_tweet(text)
    result["replaced_id"] = str(tweet_id)
    return result
