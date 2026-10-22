"""Pure V2 logic: no network, no database. Everything here is unit-tested.

Covers trivia scoring, FAQ matching, slow-mode heuristics, memory/verdict parsing,
capsule time parsing, persona presets, spend-cap math, DSN validation and the
super-admin heat map rendering.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BOT_NAME = "Zephy"
DEFAULT_BOT_USERNAME = "zephyguard"

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", (text or "").lower())).strip()


# ------------------------------------------------------------------ trivia
TRIVIA_BANK: list[tuple[str, str]] = [
    ("what is the largest planet in the solar system?", "jupiter"),
    ("which chemical element has the symbol O?", "oxygen"),
    ("how many sides does a hexagon have?", "six|6"),
    ("what is the capital of japan?", "tokyo"),
    ("who wrote the novel 'things fall apart'?", "chinua achebe|achebe"),
    ("what gas do plants absorb from the air for photosynthesis?", "carbon dioxide|co2"),
    ("in which year did the first human land on the moon?", "1969"),
    ("what is the hardest natural substance?", "diamond"),
    ("how many players are on a basketball team on court per side?", "five|5"),
    ("what is the smallest prime number?", "two|2"),
    ("which ocean is the largest?", "pacific"),
    ("what currency does japan use?", "yen"),
]

_ANSWER_STOP = {"a", "an", "the", "is", "are", "of", "to", "in", "on", "and"}


def _answer_variants(answer: str) -> list[str]:
    return [norm(a) for a in (answer or "").split("|") if norm(a)]


def answer_matches(guess: str, answer: str) -> bool:
    """True if the guess matches any accepted answer (articles ignored, whole-word containment for 4+ chars)."""
    g = norm(guess)
    if not g:
        return False
    g_core = " ".join(w for w in g.split() if w not in _ANSWER_STOP)
    for a in _answer_variants(answer):
        if g == a:
            return True
        a_core = " ".join(w for w in a.split() if w not in _ANSWER_STOP)
        if a_core and g_core == a_core:
            return True
        if len(a_core) >= 4 and re.search(rf"(?<!\w){re.escape(a_core)}(?!\w)", g_core):
            return True
    return False


def parse_trivia_output(text: str) -> tuple[str, str] | None:
    """Parse the model's 'Q: ... / A: ...' output. Returns (question, answer) or None."""
    q = re.search(r"^\s*Q\s*:\s*(.+)$", text or "", re.IGNORECASE | re.MULTILINE)
    a = re.search(r"^\s*A\s*:\s*(.+)$", text or "", re.IGNORECASE | re.MULTILINE)
    if not q or not a:
        return None
    question, answer = q.group(1).strip()[:300], a.group(1).strip()[:120]
    if not question or not answer:
        return None
    return question, answer


EVENT_TEMPLATES = {
    "wyr": [
        "would you rather never have to sleep or never have to eat again?",
        "would you rather be able to fly or be invisible?",
        "would you rather know the date you die or how you die?",
        "would you rather have unlimited money with no friends or great friends and no money?",
    ],
    "truth": [
        "what is a skill you picked up in the last year?",
        "what is the best meal you have had this month?",
        "what is a small thing that made your week better?",
    ],
}


# ------------------------------------------------------------------ slow mode
def slow_score(text: str, same_as_last: bool = False, burst: int = 0) -> int:
    """Deterministic low-effort/flood score. Higher means more likely spam or noise."""
    t = (text or "").strip()
    words = t.split()
    letters = [c for c in t if c.isalpha()]
    score = 0
    if len(t) <= 2:
        score += 2
    if t and not letters:
        score += 2  # emoji or punctuation only
    if len(letters) >= 6 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score += 1
    if re.search(r"(.)\1{4,}", t):
        score += 1
    if same_as_last:
        score += 2
    if burst >= 4:
        score += 2
    if len(words) >= 12:
        score -= 3  # real conversation is never flagged by volume alone
    elif len(words) >= 6 and "?" in t:
        score -= 1
    return max(0, score)


def slow_decision(score: int) -> str:
    """'delete' on clear noise, 'ai' for borderline (AI-assist if configured), else 'ok'."""
    if score >= 4:
        return "delete"
    if score in (2, 3):
        return "ai"
    return "ok"


# ------------------------------------------------------------------ FAQ
_FAQ_STOP = {
    "the", "and", "for", "are", "you", "your", "can", "how", "what", "when", "where", "why",
    "who", "which", "does", "this", "that", "with", "from", "have", "will", "there", "here",
    "is", "it", "do", "we", "our", "any", "some", "get", "got", "i", "me", "my", "be", "to",
    "of", "in", "on", "at", "or", "so", "if", "an", "a", "about", "please", "anyone",
}
_QUESTION_START = ("how", "what", "when", "where", "why", "who", "which", "can", "could", "does", "do", "is", "are", "will", "should")


def faq_terms(text: str) -> frozenset[str]:
    return frozenset(w for w in norm(text).split() if len(w) >= 3 and w not in _FAQ_STOP)


def faq_similarity(a: str, b: str) -> float:
    ta, tb = faq_terms(a), faq_terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_question(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.endswith("?"):
        return len(norm(raw).split()) >= 3
    words = norm(raw).split()
    return len(words) >= 4 and words[0] in _QUESTION_START


def faq_best_match(question: str, faqs: list[dict], threshold: float = 0.6) -> dict | None:
    best, best_score = None, 0.0
    for row in faqs:
        if not row.get("answer"):
            continue
        s = faq_similarity(question, row["question"])
        if s > best_score:
            best, best_score = row, s
    return best if best and best_score >= threshold else None


# ------------------------------------------------------------------ memory / twin
_MEM_RE = re.compile(r"^\s*[-*]?\s*(DECISION|FACT|RULE|SLANG)\s*:\s*(.+?)\s*$", re.IGNORECASE)


def parse_memory_output(text: str, limit: int = 5) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        m = _MEM_RE.match(line)
        if not m:
            continue
        kind, body = m.group(1).lower(), m.group(2)[:200]
        key = norm(body)
        if not key or key in seen or key in ("none", "nothing"):
            continue
        seen.add(key)
        out.append((kind, body))
        if len(out) >= limit:
            break
    return out


MEMORY_PROMPT = (
    "You read a group chat excerpt. Output ONLY lines that start with DECISION:, FACT:, RULE: or SLANG:. "
    "DECISION = something the group agreed on. FACT = a durable fact about the group or its topic. "
    "RULE = a stated group rule. SLANG = a recurring inside joke, nickname or slang term with its meaning. "
    "Skip small talk and anything personal about a single person. If nothing qualifies output NONE."
)


# ------------------------------------------------------------------ appeals / verdicts
def parse_verdict(text: str) -> tuple[str, str]:
    """Return ('overturn' | 'uphold', reason). Defaults to uphold when unclear (safe side)."""
    t = (text or "").strip()
    if not t:
        return "uphold", "no draft produced"
    first = t.split()[0].upper().strip(":.-*")
    if first == "OVERTURN":
        return "overturn", t[len(t.split()[0]):].strip(" :-\n") or "reviewed"
    if first == "UPHOLD":
        return "uphold", t[len(t.split()[0]):].strip(" :-\n") or "reviewed"
    return "uphold", "draft was unclear, so it defaults to upholding the ban"


APPEAL_PROMPT = (
    "You are drafting a moderation appeal verdict for a group admin to approve. "
    "Start your reply with exactly OVERTURN or UPHOLD, then one or two sentences of reasoning. "
    "Overturn only if the appeal shows the ban was a mistake or the member has clearly changed. Be fair and brief."
)


# ------------------------------------------------------------------ capsules
def parse_capsule_when(text: str, tz_name: str, now_ts: float) -> int | None:
    """'YYYY-MM-DD HH:MM' in the group's timezone, or '+3h' / '+2d' relative. None if invalid or in the past."""
    t = (text or "").strip()
    rel = re.fullmatch(r"\+?(\d{1,4})\s*([hd])", t, re.IGNORECASE)
    if rel:
        n = int(rel.group(1))
        ts = int(now_ts) + n * (3600 if rel.group(2).lower() == "h" else 86400)
        return ts
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    try:
        local = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except ValueError:
        return None
    ts = int(local.timestamp())
    return ts if ts > now_ts else None


# ------------------------------------------------------------------ personas
PERSONA_PRESETS = {
    "study": "patient study tutor. explain step by step with one short example, then ask one check question. no essays.",
    "trading": "calm markets explainer. no financial advice, flag risk, use plain numbers, always say what is uncertain.",
    "gaming": "hype gamer buddy. short, energetic, knows meta talk, light and never toxic.",
}


# ------------------------------------------------------------------ spend caps
def est_cost_cents(system: str, user: str, max_tokens: int, cents_per_1k: float) -> float:
    """Rough estimate from text length. This is an estimate, not a provider invoice."""
    tokens = (len(system or "") + len(user or "")) // 4 + max_tokens
    return round(tokens / 1000 * cents_per_1k, 4)


def within_cap(spent_cents: float, est_cents: float, cap_cents: float) -> bool:
    """A cap of 0 means unlimited."""
    return cap_cents <= 0 or spent_cents + est_cents <= cap_cents


SPEND_FEATURES = ("ask", "summary", "memory", "faq", "roast", "trivia", "appeal", "capsule", "slow", "shadow", "twin")


# ------------------------------------------------------------------ BYOD DSN handling
def validate_dsn(url: str, main_url: str = "") -> tuple[bool, str]:
    u = (url or "").strip()
    if not u or len(u) > 600:
        return False, "send one postgres connection string (up to 600 chars)"
    if not u.startswith(("postgresql://", "postgres://")):
        return False, "it must start with postgresql:// (standard Postgres connection string)"
    parts = urlsplit(u.replace("postgres://", "postgresql://", 1))
    if not parts.hostname or not parts.path.strip("/"):
        return False, "missing host or database name in the string"
    if main_url:
        main = urlsplit(main_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        if (main.hostname, main.path) == (parts.hostname, parts.path):
            return False, "that is the bot's own database. bring a separate database you own."
    return True, ""


def mask_dsn(url: str) -> str:
    """postgresql://user:***@host:port/db. Password, query and secrets never shown."""
    try:
        parts = urlsplit(url.replace("postgres://", "postgresql://", 1))
    except ValueError:
        return "postgresql://…"
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    user = parts.username or "user"
    return f"postgresql://{user}:***@{host}{port}{parts.path or '/'}"


# ------------------------------------------------------------------ admin dashboard
def render_heat(counts: dict[int, int], width: int = 12) -> str:
    """counts: hour-of-day (0..23) -> AI calls/messages. Plain bar chart, UTC."""
    peak = max(counts.values()) if counts else 0
    if peak <= 0:
        return "no activity recorded yet."
    lines = []
    for h in range(24):
        n = counts.get(h, 0)
        bar = "█" * max(0, round(n / peak * width)) if n else ""
        lines.append(f"{h:02d}:00 {bar} {n}")
    return "\n".join(lines)


def new_code(prefix: str = "") -> str:
    return prefix + secrets.token_hex(4)


def group_health(group: dict, key_status: str) -> str:
    """One-line health label for the super-admin view."""
    if not group.get("key_enc"):
        return "no key (free tier or idle)"
    if key_status == "expired":
        return "EXPIRED key, admin must replace it"
    return "key ok"
