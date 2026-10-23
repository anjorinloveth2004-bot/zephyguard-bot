"""v2.2b pure helpers: duration parsing, rose-style fillings and inline buttons, command and
service-message categories. No Telegram or database calls here, so everything is unit-testable.
"""
from __future__ import annotations

import html
import re

DURATION_RE = re.compile(r"^(\d{1,4})([mhdw])$")
UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_DURATION_S = 366 * 86400

BUTTON_RE = re.compile(r"\[([^\]\n]{1,64})\]\(buttonurl:([^)\s]+)\)")

ADMIN_CMDS = {
    "ban", "kick", "mute", "tban", "tmute", "dban", "dkick", "sban", "smute", "skick", "unmute",
    "warn", "swarn", "rwarn", "resetwarns", "resetallwarns", "warnlimit", "warntime", "setwarnaction",
    "purge", "purgey", "purgefrom", "purgeto", "purgeconfirm", "pin", "unpin", "unpinall", "pinloud",
    "pinmute", "antiflood", "antiraid", "approval", "approve", "unapprove", "blocklist",
    "blocklistaction", "filter", "stop", "stopall", "save", "clear", "clearall", "antipin",
    "reports", "logchannel", "log", "nolog", "disable", "enable", "disableall", "disableadmin",
    "lang", "export", "import", "reset", "anonadmin", "adminerror", "goodbye", "setgoodbye",
    "resetgoodbye", "cleangoodbye", "captchamode", "captchakicktime", "setcaptchatext",
    "resetcaptchatext", "captcharules", "privaterules", "setrules", "resetrules", "setrulesbutton",
    "resetrulesbutton", "privatenotes", "settopic", "newtopic", "renametopic", "closetopic",
    "reopentopic", "deletetopic", "actiontopic", "antichannelpin", "channelsettings", "dbanned",
    "cleancommand", "keepcommand", "cleanservice", "keepservice", "approved", "antiflood",
}
USER_CMDS = {
    "ask", "summary", "points", "point", "lb", "leaderboard", "rules", "help", "privacy", "filters",
    "notes", "get", "report", "pinned", "kickme", "id", "info", "limits", "runs", "gettopic",
    "logcategories", "cleancommandtypes", "cleanservicetypes", "disabled", "markdownhelp", "start",
    "settings", "captchamode",
}

SERVICE_CATS = {"join", "leave", "other", "photo", "pin", "title", "videochat"}
COMMAND_CATS = {"admin", "user", "other"}

_RUNS = [
    "running in place 🏃", "ran a lap around the group 🏃‍♂️", "sprinting to the lb 💨",
    "out of breath, but still here", "ran away from the spam 🏃‍♀️",
]


def parse_duration(raw: str) -> int | None:
    """'4m' '3h' '6d' '5w' -> seconds. None when it isn't one of those shapes or is too long."""
    m = DURATION_RE.match((raw or "").strip().lower())
    if not m:
        return None
    secs = int(m.group(1)) * UNIT_S[m.group(2)]
    if secs <= 0 or secs > MAX_DURATION_S:
        return None
    return secs


def parse_buttons(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Strip rose-style [label](buttonurl:https://...) from text. Returns (clean_text, buttons)."""
    buttons: list[tuple[str, str]] = []

    def _take(m: re.Match) -> str:
        url = m.group(2).strip()
        if url.startswith(("https://", "http://", "tg://")):
            buttons.append((m.group(1).strip(), url))
            return ""
        return m.group(0)

    clean = BUTTON_RE.sub(_take, text or "")
    return clean.strip(), buttons


def expand_fillings(
    text: str, *, first: str = "", last: str = "", user_id: int = 0, username: str = "",
    chat_title: str = "", count: int | str = "", ) -> str:
    """Replace {first} {last} {fullname} {username} {id} {chatname} {count}.

    Values are html-escaped here. Pass the template through html.escape() before calling if the
    template itself came from a user.
    """
    first_s = html.escape(first or "friend")
    last_s = html.escape(last or "")
    values = {
        "{first}": first_s,
        "{last}": last_s,
        "{fullname}": (first_s + (" " + last_s if last_s else "")).strip(),
        "{username}": ("@" + html.escape(username)) if username else first_s,
        "{id}": str(user_id or ""),
        "{chatname}": html.escape(chat_title or "this group"),
        "{count}": str(count),
    }
    out = text or ""
    for key, val in values.items():
        out = out.replace(key, val)
    return out


def render_text_with_buttons(template: str, **fill) -> tuple[str, list[tuple[str, str]]]:
    """Full pipeline for user-authored text: parse buttons, escape, then fill placeholders."""
    clean, buttons = parse_buttons(template or "")
    body = expand_fillings(html.escape(clean), **fill)
    return body, buttons


def command_category(cmd: str) -> str:
    if cmd in ADMIN_CMDS:
        return "admin"
    if cmd in USER_CMDS:
        return "user"
    return "other"


def service_category(msg) -> str | None:
    """Category of a service message, or None for a normal message."""
    if getattr(msg, "new_chat_members", None):
        return "join"
    if getattr(msg, "left_chat_member", None):
        return "leave"
    if getattr(msg, "new_chat_photo", None) or getattr(msg, "delete_chat_photo", None):
        return "photo"
    if getattr(msg, "pinned_message", None):
        return "pin"
    if getattr(msg, "new_chat_title", None):
        return "title"
    if any(getattr(msg, a, None) for a in (
        "video_chat_started", "video_chat_ended", "video_chat_scheduled", "video_chat_participants_invited",
    )):
        return "videochat"
    if any(getattr(msg, a, None) for a in (
        "group_chat_created", "supergroup_chat_created", "migrate_to_chat_id", "migrate_from_chat_id",
        "message_auto_delete_timer_changed",
    )):
        return "other"
    return None


def runs_line(seed: int = 0) -> str:
    return _RUNS[seed % len(_RUNS)]
