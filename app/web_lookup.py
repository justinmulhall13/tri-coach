"""Bounded Anthropic web-search routing and citation rendering.

Search is opt-in per turn.  It is enabled for explicit web/current-information
requests and event-switch discovery, not for stable coaching math or ordinary
conversation.  Anthropic executes the server tool and returns source metadata;
we preserve those source links when converting content blocks to chat text.
"""
from __future__ import annotations

import re
from typing import Any


_EXPLICIT_WEB_RE = re.compile(
    r"\b(?:look\s*up|search(?:\s+the)?\s+web|search\s+online|browse|google|"
    r"find\s+(?:the\s+)?official|latest|current\s+(?:details?|information|rules?|course)|"
    r"online|website|web)\b",
    re.I,
)
_EVENT_DISCOVERY_RE = re.compile(
    r"\b(?:switch\s+to|event|race|marathon|half[ -]?marathon|triathlon|duathlon|"
    r"ultra(?:marathon)?|10k|5k)\b.*\b(?:date|distance|course|guide|official|mode)\b|"
    r"\bswitch\s+to\s+.+",
    re.I,
)


def should_enable(message: str) -> bool:
    """True only when current external information can materially help."""
    text = (message or "").strip()
    return bool(text and (_EXPLICIT_WEB_RE.search(text) or _EVENT_DISCOVERY_RE.search(text)))


def tool_definition() -> dict[str, Any]:
    """Conservative basic-search tool supported by current Anthropic SDKs."""
    return {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
        "user_location": {
            "type": "approximate",
            "city": "Vancouver",
            "region": "British Columbia",
            "country": "CA",
            "timezone": "America/Vancouver",
        },
    }


def text_with_citations(message: Any) -> str:
    """Return assistant text plus unique Markdown source links.

    Web-search citations live on individual text blocks.  Joining ``block.text``
    alone silently discards them, which would leave the athlete unable to audit
    event facts used for a profile switch.
    """
    pieces: list[str] = []
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", None)
        if text:
            pieces.append(str(text))
        for citation in getattr(block, "citations", None) or []:
            url = getattr(citation, "url", None)
            title = getattr(citation, "title", None) or "Source"
            if url and url not in seen:
                seen.add(url)
                sources.append((str(title), str(url)))
    rendered = "".join(pieces).strip()
    if sources:
        links = " · ".join(f"[{title}]({url})" for title, url in sources)
        rendered = f"{rendered}\n\nSources: {links}" if rendered else f"Sources: {links}"
    return rendered
