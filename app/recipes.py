"""Real recipe discovery for the nutrition tab.

Anthropic web search finds current source pages. The server then fetches those
pages and reads their schema.org Recipe JSON-LD, so titles, photos, ingredients,
times, and nutrition come from the publisher rather than model invention.
"""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from typing import Any

from . import config, web_lookup


MAX_PAGE_BYTES = 2_000_000
MAX_SOURCE_PAGES = 12
RECIPE_DOMAINS = [
    "skinnytaste.com", "recipetineats.com", "budgetbytes.com",
    "therealfooddietitians.com", "ambitiouskitchen.com",
]
RECIPE_CACHE_TTL_SECONDS = 30 * 60
_CACHE_LIMIT = 24
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", unescape(str(value or ""))).strip()
    return text[:limit]


def _public_url(url: Any, *, resolve: bool = False) -> str:
    text = str(url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public http(s) recipe URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("recipe URLs cannot contain credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("local recipe URLs are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal and not literal.is_global:
        raise ValueError("private recipe URLs are not allowed")
    if resolve:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ValueError("recipe host could not be resolved") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global
                                for address in addresses):
            raise ValueError("recipe host did not resolve to a public address")
    return text


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _public_url(newurl, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_html(url: str) -> str:
    safe_url = _public_url(url, resolve=True)
    request = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "TrainingAppRecipeReader/1.0 (+recipe metadata)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirect())
    with opener.open(request, timeout=8) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            raise ValueError("recipe source did not return HTML")
        raw = response.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            raise ValueError("recipe page is too large")
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


class _RecipePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.og_image = ""
        self.og_title = ""
        self._script = False
        self._title = False
        self._parts: list[str] = []
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "script" and "ld+json" in values.get("type", "").lower():
            self._script = True
            self._parts = []
        elif tag.lower() == "meta":
            prop = (values.get("property") or values.get("name") or "").lower()
            if prop in {"og:image", "twitter:image", "twitter:image:src"} and not self.og_image:
                self.og_image = values.get("content", "")
            elif prop in {"og:title", "twitter:title"} and not self.og_title:
                self.og_title = values.get("content", "")
        elif tag.lower() == "title":
            self._title = True
            self._title_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script:
            self.json_ld.append("".join(self._parts).strip())
            self._script = False
            self._parts = []
        elif tag.lower() == "title" and self._title:
            if not self.og_title:
                self.og_title = "".join(self._title_parts).strip()
            self._title = False

    def handle_data(self, data: str) -> None:
        if self._script:
            self._parts.append(data)
        elif self._title:
            self._title_parts.append(data)


def _is_recipe(node: dict[str, Any]) -> bool:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return any(str(value).lower() == "recipe" for value in values)


def _recipe_nodes(value: Any):
    if isinstance(value, dict):
        if _is_recipe(value):
            yield value
        for child in value.values():
            yield from _recipe_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _recipe_nodes(child)


def _image_url(value: Any, fallback: str = "") -> str:
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("url") or candidate.get("contentUrl")
        try:
            return _public_url(candidate)
        except ValueError:
            continue
    try:
        return _public_url(fallback)
    except ValueError:
        return ""


def _duration_minutes(value: Any) -> int | None:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?)?",
        str(value or "").upper(),
    )
    if not match:
        return None
    total = int(match.group("days") or 0) * 1440
    total += int(match.group("hours") or 0) * 60
    total += int(match.group("minutes") or 0)
    return total or None


def _nutrition_number(value: Any) -> float | int | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    number = float(match.group())
    return int(number) if number.is_integer() else number


def _servings(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return _clean_text(value, 80)


def parse_recipe_page(html: str, source_url: str, *, source_title: str = "") -> dict[str, Any] | None:
    """Extract the first real schema.org Recipe from one publisher page."""
    parser = _RecipePageParser()
    parser.feed(html or "")
    nodes: list[dict[str, Any]] = []
    for raw in parser.json_ld:
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        nodes.extend(node for node in _recipe_nodes(decoded) if isinstance(node, dict))
    if not nodes:
        return None
    node = nodes[0]
    title = _clean_text(node.get("name") or parser.og_title or source_title, 180)
    ingredients = [_clean_text(item, 300) for item in node.get("recipeIngredient") or []]
    ingredients = [item for item in ingredients if item][:80]
    image = _image_url(node.get("image"), parser.og_image)
    if not title or not ingredients or not image:
        return None
    nutrition = node.get("nutrition") if isinstance(node.get("nutrition"), dict) else {}
    parsed_url = urllib.parse.urlparse(_public_url(source_url))
    source_name = parsed_url.hostname.removeprefix("www.") if parsed_url.hostname else "Recipe source"
    return {
        "external_id": hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:20],
        "title": title,
        "description": _clean_text(node.get("description"), 280),
        "source_name": source_name,
        "source_url": source_url,
        "image_url": image,
        "servings": _servings(node.get("recipeYield")),
        "total_minutes": (_duration_minutes(node.get("totalTime"))
                          or ((_duration_minutes(node.get("prepTime")) or 0)
                              + (_duration_minutes(node.get("cookTime")) or 0)) or None),
        "ingredients": ingredients,
        "instruction_count": len(node.get("recipeInstructions") or []),
        "nutrition": {
            "kcal": _nutrition_number(nutrition.get("calories")),
            "protein_g": _nutrition_number(nutrition.get("proteinContent")),
            "carb_g": _nutrition_number(nutrition.get("carbohydrateContent")),
            "fat_g": _nutrition_number(nutrition.get("fatContent")),
        },
        "provenance": "publisher_recipe_json_ld",
    }


def _fit(recipe: dict[str, Any], day: dict[str, Any] | None) -> dict[str, Any]:
    remaining = (day or {}).get("remaining") or {}
    values = recipe.get("nutrition") or {}
    known = {key: value for key, value in values.items()
             if isinstance(value, (int, float)) and not isinstance(value, bool)}
    if not known:
        return {"label": "Macros not published", "score": None}
    desired_protein = min(float(remaining.get("protein_g") or 35), 45)
    desired_carbs = min(float(remaining.get("carb_g") or 70), 100)
    protein = float(known.get("protein_g") or 0)
    carbs = float(known.get("carb_g") or 0)
    score = round(100 * (0.55 * min(1, protein / max(desired_protein, 1))
                         + 0.45 * min(1, carbs / max(desired_carbs, 1))))
    label = "Strong fit today" if score >= 75 else "Useful option" if score >= 45 else "Light meal"
    return {"label": label, "score": score}


def _search_sources(query: str, day: dict[str, Any] | None, limit: int) -> list[dict[str, str]]:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("web recipe search is not configured")
    import anthropic

    session = (day or {}).get("session") or {}
    remaining = (day or {}).get("remaining") or {}
    context = {
        "request": query,
        "today_session": {
            "discipline": session.get("discipline"),
            "duration_min": session.get("duration_min"),
            "intensity": session.get("intensity"),
        },
        "remaining_targets": remaining,
        "preferences": config.NUTRITION_PREFS,
    }
    prompt = (
        f"Find {max(limit * 2, 8)} individual recipe pages that fit this athlete context. "
        "Search the live web using queries for specific dishes that match the request, not broad "
        "recipe categories. Cite every recipe page you recommend. Use the original recipe "
        "publisher page, not search pages, videos, restaurant menus, roundups, or meal plans. "
        "Prefer pages with a real finished-dish photo and complete ingredient list. Do not "
        "invent nutrition values. Keep the answer to linked recipe titles only.\n\n"
        + json.dumps(context, ensure_ascii=False, default=str)
    )
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=2)
    message = client.messages.create(
        model=config.FAST_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
        tools=[web_lookup.tool_definition(max_uses=3, allowed_domains=RECIPE_DOMAINS)],
    )
    return web_lookup.citation_sources(message)


def discover(query: str = "", *, day: dict[str, Any] | None = None,
             limit: int = 6) -> dict[str, Any]:
    """Find and verify current recipes. No source page means no result card."""
    request = _clean_text(query, 240) or "high-protein meal that supports today's training"
    if isinstance(limit, bool):
        raise ValueError("recipe limit must be an integer")
    try:
        limit = max(1, min(int(limit), 8))
    except (TypeError, ValueError) as exc:
        raise ValueError("recipe limit must be an integer") from exc
    cache_key = hashlib.sha256(json.dumps({
        "query": request.casefold(), "day": day or {}, "limit": limit,
    }, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] <= RECIPE_CACHE_TTL_SECONDS:
            payload = copy.deepcopy(cached[1])
            payload["cached"] = True
            return payload
        if cached:
            _cache.pop(cache_key, None)
    try:
        sources = _search_sources(request, day, limit)[:MAX_SOURCE_PAGES]
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Recipe search failed: {type(exc).__name__}: {exc}",
                "recipes": [], "query": request, "web_connected": False}
    if not sources:
        return {"error": "Recipe search returned no source pages", "recipes": [],
                "query": request, "web_connected": True}

    def load(source: dict[str, str]) -> dict[str, Any] | None:
        try:
            html = _fetch_html(source["url"])
            recipe = parse_recipe_page(html, source["url"], source_title=source.get("title", ""))
            if recipe:
                recipe["fit"] = _fit(recipe, day)
            return recipe
        except Exception:  # one blocked publisher must not break the whole search
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(sources))) as pool:
        found = list(pool.map(load, sources))
    recipes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for recipe in found:
        if not recipe or recipe["source_url"] in seen:
            continue
        seen.add(recipe["source_url"])
        recipes.append(recipe)
        if len(recipes) >= limit:
            break
    payload = {"recipes": recipes, "query": request, "web_connected": True,
               "source_count": len(sources)}
    if not recipes:
        payload["error"] = "Sources were found, but none exposed a verifiable recipe and photo"
    else:
        with _cache_lock:
            if len(_cache) >= _CACHE_LIMIT:
                oldest = min(_cache, key=lambda key: _cache[key][0])
                _cache.pop(oldest, None)
            _cache[cache_key] = (time.monotonic(), copy.deepcopy(payload))
    return payload
