"""Message-formatting helpers shared across receiver services."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from stoat_discord_bridge.models import StandardMessage

# Discord/Stoat dynamic-timestamp markup: <t:UNIX_SECONDS> or <t:UNIX_SECONDS:STYLE>
# where STYLE is one of t T d D f F R (absent == f). IRC has no equivalent, so
# IrcReceiverService renders these to plain text before relaying.
_DISCORD_TIMESTAMP = re.compile(r"<t:(-?\d+)(?::([tTdDfFR]))?>")


def _format_relative(delta_seconds: float) -> str:
    """Discord-style coarse relative phrasing for the `R` style, e.g.
    "in 2 minutes" / "5 days ago". Snapshotted at render time - it does not
    keep updating the way Discord's does."""
    future = delta_seconds > 0
    remaining = abs(delta_seconds)
    for unit, size in (
        ("year", 31_536_000),
        ("month", 2_592_000),
        ("day", 86_400),
        ("hour", 3_600),
        ("minute", 60),
        ("second", 1),
    ):
        count = round(remaining / size)
        if count >= 1:
            phrase = f"{count} {unit}" + ("s" if count != 1 else "")
            return f"in {phrase}" if future else f"{phrase} ago"
    return "now"


def _render_timestamp(epoch_seconds: int, style: str, now: datetime) -> str:
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    if style == "R":
        return _format_relative(epoch_seconds - now.timestamp())

    hour12 = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    short_time = f"{hour12}:{dt.minute:02d} {meridiem} UTC"
    long_time = f"{hour12}:{dt.minute:02d}:{dt.second:02d} {meridiem} UTC"
    short_date = f"{dt.month}/{dt.day}/{dt.year}"
    long_date = f"{dt.strftime('%B')} {dt.day}, {dt.year}"

    if style == "t":
        return short_time
    if style == "T":
        return long_time
    if style == "d":
        return short_date
    if style == "D":
        return long_date
    if style == "F":
        return f"{dt.strftime('%A')}, {long_date} {long_time}"
    # "f" and the no-style default
    return f"{long_date} {short_time}"


def render_discord_timestamps(content: str, *, now: datetime | None = None) -> str:
    """Replace Discord/Stoat `<t:...>` dynamic-timestamp markup with rendered
    plain text (UTC, with a `UTC` label on time-bearing styles). The `R`
    (relative) style is computed against `now` - defaulting to the current
    time, i.e. when the receiver handles the message. A token whose epoch is
    unparseable/out of range is left exactly as it appeared."""
    now = now or datetime.now(timezone.utc)

    def _replace(match: re.Match[str]) -> str:
        try:
            return _render_timestamp(int(match.group(1)), match.group(2) or "f", now)
        except (OverflowError, OSError, ValueError):
            return match.group(0)

    return _DISCORD_TIMESTAMP.sub(_replace, content)


# Discord/Stoat Markdown constructs, stripped to plain text before relaying
# onto IRC (which has no markup). Emphasis markers are unwrapped to their
# inner text; code keeps its contents but loses the backticks; a masked
# `[label](url)` link becomes `label (url)`; leading heading `#`s and
# blockquote `>`s are dropped.
_MD_CODE_BLOCK = re.compile(r"```(?:[A-Za-z0-9_+-]*\n)?(.*?)```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*<?([^)\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_MD_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^ {0,3}> ?", re.MULTILINE)
_MD_UNESCAPE = re.compile(r"\\([*_~`|>#\\-])")
# Applied in order: bold/underline (doubled markers) before italic (single),
# so `**x**` and `__x__` don't leave a stray marker behind.
_MD_EMPHASIS = (
    re.compile(r"\*\*(.+?)\*\*", re.DOTALL),
    re.compile(r"__(.+?)__", re.DOTALL),
    re.compile(r"~~(.+?)~~", re.DOTALL),
    re.compile(r"\|\|(.+?)\|\|", re.DOTALL),
    re.compile(r"\*(.+?)\*", re.DOTALL),
    # Underscore italics only at word boundaries - Discord doesn't italicise
    # intra-word underscores, so snake_case identifiers survive.
    re.compile(r"(?<![A-Za-z0-9_])_(.+?)_(?![A-Za-z0-9_])", re.DOTALL),
)


def strip_markdown(content: str) -> str:
    """Reduce Discord/Stoat Markdown to plain text for relaying onto IRC.

    Emphasis (`**bold**`, `*italic*`, `__underline__`, `~~strike~~`,
    `||spoiler||`) is unwrapped to its inner text; inline and fenced code
    keep their contents but lose the backticks; `[label](url)` becomes
    `label (url)` (just `label` when the label already contains the URL);
    leading heading `#`s and blockquote `>`s are dropped; a backslash
    escaping a Markdown character is removed. List bullets/numbers are left
    alone - they read fine as plain text.
    """
    # Pull escaped Markdown characters and code contents out first, as
    # placeholders, so the link/heading/emphasis passes below never see
    # markup that's meant to be literal. Restored verbatim at the end.
    stashed: list[str] = []

    def _stash(text: str) -> str:
        stashed.append(text)
        return f"\x00{len(stashed) - 1}\x00"

    content = _MD_UNESCAPE.sub(lambda m: _stash(m.group(1)), content)
    content = _MD_CODE_BLOCK.sub(lambda m: _stash(m.group(1).strip("\n")), content)
    content = _MD_INLINE_CODE.sub(lambda m: _stash(m.group(1)), content)

    content = _MD_LINK.sub(
        lambda m: m.group(1) if m.group(2) in m.group(1) else f"{m.group(1)} ({m.group(2)})",
        content,
    )
    content = _MD_HEADING.sub("", content)
    content = _MD_BLOCKQUOTE.sub("", content)
    for pattern in _MD_EMPHASIS:
        content = pattern.sub(r"\1", content)

    # re.sub doesn't re-scan inserted text, so a placeholder restored from
    # inside a code span (an escape nested in code) needs another pass -
    # bounded by the stash count so a literal NUL in the input can't loop.
    holder = re.compile(r"\x00(\d+)\x00")
    for _ in range(len(stashed) + 1):
        if not holder.search(content):
            break
        content = holder.sub(lambda m: stashed[int(m.group(1))], content)
    return content


def content_with_attachments(message: StandardMessage) -> str:
    lines = [message.content_markdown] if message.content_markdown else []
    lines.extend(a.url for a in message.attachments)
    return "\n".join(lines) or "\u200b"


def chunk_content(content: str, limit: int) -> list[str]:
    """Split `content` into <=`limit`-char chunks, preferring line boundaries,
    so a long relayed message doesn't blow past a platform's per-message cap."""
    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [content]
