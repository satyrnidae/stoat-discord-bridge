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
