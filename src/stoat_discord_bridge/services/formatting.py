"""Message-formatting helpers shared across receiver services."""

from __future__ import annotations

from stoat_discord_bridge.models import StandardMessage


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
