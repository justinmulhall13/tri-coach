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
_CURRENT_HELP_RE = re.compile(
    r"\b(?:recipe|meal\s+idea|restaurant|travel|flight|hotel|product|gear|shoe|"
    r"weather|news|price|buy|research|study|paper|guideline|schedule|opening\s+hours)\b|"
    r"https?://",
    re.I,
)
_EVENT_DISCOVERY_RE = re.compile(
    r"\b(?:switch\s+to|change\s+(?:my\s+)?(?:mode|goal|event|race)\s+to|"
    r"(?:start|begin)\s+training\s+for|make\s+.+\s+my\s+(?:goal|event|race)|"
    r"event|race|marathon|half[ -]?marathon|triathlon|duathlon|"
    r"ultra(?:marathon)?|10k|5k)\b.*\b(?:date|distance|course|guide|official|mode)\b|"
    r"\b(?:switch\s+to\s+.+|change\s+(?:my\s+)?(?:mode|goal|event|race)\s+to\s+.+|"
    r"(?:start|begin)\s+training\s+for\s+.+|make\s+.+\s+my\s+(?:goal|event|race))",
    re.I,
)


def should_enable(message: str) -> bool:
    """True only when current external information can materially help."""
    text = (message or "").strip()
    return bool(text and (_EXPLICIT_WEB_RE.search(text) or _EVENT_DISCOVERY_RE.search(text)
                          or _CURRENT_HELP_RE.search(text)))


def tool_definition(*, max_uses: int = 3,
                    allowed_domains: list[str] | None = None) -> dict[str, Any]:
    """Conservative basic-search tool supported by current Anthropic SDKs."""
    tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max(1, min(int(max_uses), 8)),
        "user_location": {
            "type": "approximate",
            "city": "Vancouver",
            "region": "British Columbia",
            "country": "CA",
            "timezone": "America/Vancouver",
        },
    }
    if allowed_domains:
        tool["allowed_domains"] = list(dict.fromkeys(allowed_domains))
    return tool


def citation_sources(message: Any) -> list[dict[str, str]]:
    """Unique source URLs cited by an Anthropic web-search response."""
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "web_search_tool_result":
            # Preserve raw search-result URLs too. A long search can exhaust
            # the response budget before Claude writes citation-bearing prose;
            # those source blocks are still real results and recipe discovery
            # verifies their publisher JSON-LD independently.
            content = getattr(block, "content", None)
            if isinstance(content, list):
                for item in content:
                    url = str(getattr(item, "url", None) or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append({
                        "title": str(getattr(item, "title", None) or "Source"),
                        "url": url,
                    })
            continue
        if getattr(block, "type", None) != "text":
            continue
        for citation in getattr(block, "citations", None) or []:
            url = str(getattr(citation, "url", None) or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({
                "title": str(getattr(citation, "title", None) or "Source"),
                "url": url,
            })
    return sources


def text_with_citations(message: Any) -> str:
    """Return assistant text plus unique Markdown source links.

    Web-search citations live on individual text blocks.  Joining ``block.text``
    alone silently discards them, which would leave the athlete unable to audit
    event facts used for a profile switch.
    """
    pieces: list[str] = []
    sources = citation_sources(message)
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", None)
        if text:
            pieces.append(str(text))
    rendered = "".join(pieces).strip()
    if sources:
        links = " · ".join(f"[{source['title']}]({source['url']})" for source in sources)
        rendered = f"{rendered}\n\nSources: {links}" if rendered else f"Sources: {links}"
    return rendered
