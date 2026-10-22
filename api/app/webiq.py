"""Microsoft Web IQ client supporting `/v3/search/web` and `/v3/browse`.

Applies Web IQ grounding and display boundaries (not a compliance certification):
  - Caps displayed per-source grounding excerpts to <= 200 characters (`MAX_DISPLAY_EXCERPT_CHARS`).
  - Supports `/v3/browse` (`liveCrawl` modes `"none"`, `"fallback"`, `"force"`, `renderDynamicPages`,
    `contentFormat` `"markdown"` / `"text"` / `"html"`, and HTTP 200 / 202 / 403 / 404 / 430 responses).
  - Never reflects API keys in exceptions, logs, or telemetry.
"""
from __future__ import annotations

import asyncio
import html
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

try:
    from .url_safety import UrlValidationError, classify_review_platform, is_review_page_url, validate_public_https_url
except ImportError:
    from url_safety import UrlValidationError, classify_review_platform, is_review_page_url, validate_public_https_url

WEBIQ_WEB_ENDPOINT = "https://api.microsoft.ai/v3/search/web"
WEBIQ_BROWSE_ENDPOINT = "https://api.microsoft.ai/v3/browse"
MAX_QUERY_LENGTH = 1000
MAX_PASSAGE_CHARS = 600
MAX_DISPLAY_EXCERPT_CHARS = 200
MAX_SEARCH_GROUNDING_CHARS = 3000


class WebIQError(Exception):
    """Sanitized Web IQ error that never echoes secrets."""


class _PassageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def _clean_text(raw: Any, secret_key: str = "", max_chars: int = MAX_PASSAGE_CHARS) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    parser = _PassageTextParser()
    parser.feed(html.unescape(raw))
    text = parser.get_text()
    if secret_key and secret_key in text:
        text = text.replace(secret_key, "[REDACTED]")
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def truncate_display_excerpt(text: str, limit: int = MAX_DISPLAY_EXCERPT_CHARS) -> str:
    """Enforce Web IQ FAQ display limit: lesser of 200 chars or publisher limit."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"




@contextmanager
def _suppress_instrumentation():
    try:
        from opentelemetry.context import attach, detach, set_value
        from opentelemetry.sdk.trace import _SUPPRESS_INSTRUMENTATION_KEY

        token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            yield
        finally:
            detach(token)
    except Exception:  # noqa: BLE001
        yield


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    published_at: str | None
    updated_at: str | None
    crawled_at: str | None
    content: str
    platform: str
    platform_category: str
    bat_allowed: bool
    access_mode: str
    provenance: str = "sample_fixture"
    retrieved_content: str = field(default="", repr=False)

    def citation(self) -> dict[str, Any]:
        returned = self.retrieved_content or self.content
        return {
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "crawled_at": self.crawled_at,
            "snippet": truncate_display_excerpt(self.content, MAX_DISPLAY_EXCERPT_CHARS),
            "excerpt_char_limit": MAX_DISPLAY_EXCERPT_CHARS,
            "platform": self.platform,
            "platform_category": self.platform_category,
            "bat_allowed": self.bat_allowed,
            "access_mode": self.access_mode,
            "provenance": self.provenance,
            "provider": ("webiq" if self.provenance.startswith("live_webiq") else
                         "toolbox_web_search" if self.provenance == "live_toolbox_web_search" else "sample"),
            "retrieved_response": {
                "title": self.title, "url": self.url,
                "content": returned[:MAX_DISPLAY_EXCERPT_CHARS],
                "publishedAt": self.published_at, "lastUpdatedAt": self.updated_at,
                "crawledAt": self.crawled_at,
            },
            "response_view": "display_limited_projection",
            "retrieved_characters": len(self.content),
            "content_display_limit": MAX_DISPLAY_EXCERPT_CHARS,
            "content_truncated": len(returned) > MAX_DISPLAY_EXCERPT_CHARS,
        }




@dataclass(frozen=True)
class Enrichment:
    query: str
    retrieved_at: str
    sources: tuple[Source, ...] = ()
    provenance: str = "sample_fixture"


def validate_request(api_key: str, query: str | None, fallback_query: str = "") -> tuple[str, str]:
    clean_key = (api_key or "").strip()
    if not clean_key or len(clean_key) < 8:
        raise WebIQError("Web IQ key is missing or invalid.")
    effective_query = (query or fallback_query or "").strip()
    if not effective_query:
        raise WebIQError("Web IQ search query cannot be empty.")
    if len(effective_query) > MAX_QUERY_LENGTH:
        effective_query = effective_query[:MAX_QUERY_LENGTH]
    return clean_key, effective_query


def normalize_response(
    payload: dict[str, Any],
    query: str,
    secret_key: str = "",
    *,
    provenance: str = "live_webiq",
) -> Enrichment:
    raw_items = payload.get("webResults") or payload.get("results") or []
    sources: list[Source] = []
    seen_urls: set[str] = set()

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("url") or "").strip()
        if secret_key and secret_key in raw_url:
            continue
        try:
            clean_url = validate_public_https_url(raw_url, resolve_dns=False)
        except UrlValidationError:
            continue
        if clean_url in seen_urls or not is_review_page_url(clean_url):
            continue
        seen_urls.add(clean_url)

        title = _clean_text(item.get("title") or "Untitled Source", secret_key, max_chars=180)
        body_raw = item.get("content") or item.get("snippet") or item.get("description") or item.get("summary") or ""
        content = _clean_text(body_raw, secret_key, max_chars=MAX_SEARCH_GROUNDING_CHARS)
        if not content:
            continue

        plat = classify_review_platform(clean_url)
        sources.append(
            Source(
                title=title,
                url=clean_url,
                published_at=_clean_text(item.get("publishedAt") or item.get("datePublished"), secret_key, max_chars=80) or None,
                updated_at=_clean_text(item.get("lastUpdatedAt"), secret_key, max_chars=80) or None,
                crawled_at=_clean_text(item.get("crawledAt"), secret_key, max_chars=80) or None,
                content=content,
                platform=plat["platform"],
                platform_category=plat["category"],
                bat_allowed=bool(plat["bat_allowed"]),
                access_mode=plat["access_mode"],
                provenance=provenance,
                retrieved_content=(body_raw.replace(secret_key, "[REDACTED]") if secret_key else body_raw)[:MAX_SEARCH_GROUNDING_CHARS],
            )
        )

    return Enrichment(
        query=query,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        sources=tuple(sources),
        provenance=provenance,
    )




async def search_webiq(
    api_key: str,
    query: str,
    *,
    max_results: int = 8,
    language: str = "en",
    region: str = "US",
) -> Enrichment:
    clean_key, clean_query = validate_request(api_key, query)
    headers = {
        "x-apikey": clean_key,
        "Content-Type": "application/json",
    }
    body = {
        "query": clean_query,
        "maxResults": max(1, min(int(max_results), 20)),
        "language": language,
        "region": region,
        "contentFormat": "passage",
        "maxLength": MAX_SEARCH_GROUNDING_CHARS,
    }
    try:
        with _suppress_instrumentation():
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(WEBIQ_WEB_ENDPOINT, headers=headers, json=body)
        if resp.status_code in (401, 403):
            raise WebIQError("Web IQ authentication failed (HTTP 401/403). Verify subscription/API key and linked AppID.")
        if resp.status_code >= 400:
            raise WebIQError(f"Web IQ web search returned HTTP {resp.status_code}.")
        data = resp.json()
    except WebIQError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WebIQError(f"Web IQ web search request failed ({type(exc).__name__}).") from None
    finally:
        del clean_key
    return normalize_response(data, clean_query, secret_key=api_key.strip(), provenance="live_webiq")




class WebIQBrowseResult(dict):
    """Dict-and-attribute compatible wrapper for WebIQ `/v3/browse` responses."""

    @property
    def url(self) -> str:
        return str(self.get("url") or "")

    @property
    def status_code(self) -> int:
        return int(self.get("status_code") or 200)

    @property
    def title(self) -> str:
        return str(self.get("title") or "")

    @property
    def content(self) -> str:
        return str(self.get("content") or self.get("markdown") or "")

    @property
    def crawled_at(self) -> str | None:
        val = self.get("crawledAt") or self.get("crawled_at")
        return str(val) if val else None

    @property
    def live_crawl_succeeded(self) -> bool | None:
        val = self.get("liveCrawlSucceeded")
        if val is None:
            val = self.get("live_crawl_succeeded")
        return bool(val) if val is not None else None

    @property
    def error_reason(self) -> str | None:
        if self.status_code == 200 and self.content.strip():
            return None
        return str(self.get("error_reason") or self.get("excerpt") or f"HTTP {self.status_code}")


async def browse_webiq_url(
    api_key: str,
    url: str,
    *,
    max_length: int = 20000,
    live_crawl: str = "fallback",
    content_format: str = "markdown",
    render_dynamic_pages: bool = True,
    language: str = "en",
    region: str = "US",
    timeout_s: float = 18.0,
) -> WebIQBrowseResult:
    """Call Microsoft Web IQ `POST https://api.microsoft.ai/v3/browse` for a target URL.

    Follows official Browse REST API reference (`https://learn.microsoft.com/en-us/web-iq/reference/browse`):
      - `liveCrawl`: `"none"` (indexed only, 404 if absent), `"fallback"` (crawl if not in index), `"force"`.
      - `renderDynamicPages`: valid when `liveCrawl == "fallback"`.
      - `contentFormat`: `"text"`, `"html"`, or `"markdown"`.
      - Status codes: 200 (Success), 202 (Accepted on-demand crawl with `retryAfter`),
        401 (Unauthorized), 403 (Forbidden/Blocked by policy), 404 (Not Found in index/inaccessible),
        429/430 (Rate limit / Too many on-demand crawls).
    """
    clean_key = (api_key or "").strip()
    if not clean_key or len(clean_key) < 8:
        raise WebIQError("Web IQ key is missing or invalid for /v3/browse.")
    clean_url = validate_public_https_url(url, resolve_dns=False)
    effective_crawl = live_crawl if live_crawl in ("none", "fallback", "force") else "fallback"
    effective_format = content_format if content_format in ("text", "html", "markdown") else "markdown"

    headers = {
        "host": "api.microsoft.ai",
        "x-apikey": clean_key,
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "url": clean_url,
        "maxLength": max(500, min(int(max_length), 500000)),
        "liveCrawl": effective_crawl,
        "includeWebLinks": False,
        "includeImageLinks": False,
        "language": language,
        "region": region,
        "contentFormat": effective_format,
    }
    if effective_crawl == "fallback":
        body["renderDynamicPages"] = bool(render_dynamic_pages)

    try:
        with _suppress_instrumentation():
            async with httpx.AsyncClient(timeout=max(5.0, float(timeout_s))) as client:
                resp = await client.post(WEBIQ_BROWSE_ENDPOINT, headers=headers, json=body)
                if resp.status_code == 202 and effective_crawl in ("fallback", "force"):
                    try:
                        payload_202 = resp.json()
                    except Exception:  # noqa: BLE001
                        payload_202 = {}
                    retry_str = str(payload_202.get("retryAfter") or "3s").strip().rstrip("s")
                    try:
                        wait_s = min(float(retry_str), 4.0)
                    except ValueError:
                        wait_s = 2.5
                    await asyncio.sleep(max(1.0, wait_s))
                    resp = await client.post(WEBIQ_BROWSE_ENDPOINT, headers=headers, json=body)

        if resp.status_code == 401:
            raise WebIQError("Web IQ Browse authentication failed (HTTP 401). Verify your Web IQ API key.")

        try:
            data = resp.json() if resp.text else {}
        except Exception:  # noqa: BLE001
            data = {}

        if resp.status_code == 200:
            raw_content = str(data.get("content") or "").strip()
            clean_title = _clean_text(data.get("title") or clean_url, clean_key, max_chars=200)
            cleaned_excerpt = _clean_text(raw_content, clean_key, max_chars=MAX_DISPLAY_EXCERPT_CHARS)
            # Keep bounded text for GPT extraction in memory only (never persisted raw)
            bounded_content = raw_content.replace(clean_key, "[REDACTED]")[: max(1000, min(int(max_length), 25000))]
            return WebIQBrowseResult({
                "status_code": 200,
                "access_status": "inspected" if bounded_content else "empty_content",
                "url": str(data.get("url") or clean_url),
                "title": clean_title,
                "content": bounded_content,
                "excerpt": truncate_display_excerpt(cleaned_excerpt, MAX_DISPLAY_EXCERPT_CHARS),
                "lastUpdatedAt": data.get("lastUpdatedAt"),
                "crawledAt": data.get("crawledAt"),
                "liveCrawlSucceeded": data.get("liveCrawlSucceeded"),
                "traceId": data.get("traceId"),
                "isAdult": bool(data.get("isAdult", False)),
                "liveCrawl": effective_crawl,
                "contentFormat": effective_format,
                "provenance": "live_webiq_browse",
            })

        if resp.status_code == 202:
            return WebIQBrowseResult({
                "status_code": 202,
                "access_status": "on_demand_crawl_pending",
                "url": clean_url,
                "title": clean_url,
                "content": "",
                "excerpt": f"Web IQ /v3/browse accepted on-demand crawl (HTTP 202, retryAfter={data.get('retryAfter', '60s')}); page was not yet in index.",
                "retryAfter": data.get("retryAfter"),
                "traceId": data.get("traceId"),
                "liveCrawl": effective_crawl,
                "provenance": "live_webiq_browse_pending",
            })

        if resp.status_code == 403:
            return WebIQBrowseResult({
                "status_code": 403,
                "access_status": "blocked_by_policy_or_site",
                "url": clean_url,
                "title": clean_url,
                "content": "",
                "excerpt": "Web IQ /v3/browse returned HTTP 403 Forbidden (URL blocked by publisher robots/policy or content filter).",
                "traceId": data.get("traceId"),
                "liveCrawl": effective_crawl,
                "provenance": "live_webiq_browse_restricted",
            })

        if resp.status_code == 404:
            return WebIQBrowseResult({
                "status_code": 404,
                "access_status": "not_found_in_index",
                "url": clean_url,
                "title": clean_url,
                "content": "",
                "excerpt": "Web IQ /v3/browse returned HTTP 404 Not Found (URL not in Web IQ index or inaccessible to crawler).",
                "traceId": data.get("traceId"),
                "liveCrawl": effective_crawl,
                "provenance": "live_webiq_browse_not_found",
            })

        if resp.status_code in (429, 430):
            return WebIQBrowseResult({
                "status_code": resp.status_code,
                "access_status": "rate_limited",
                "url": clean_url,
                "title": clean_url,
                "content": "",
                "excerpt": f"Web IQ /v3/browse returned HTTP {resp.status_code} (rate limit or on-demand crawl quota reached).",
                "traceId": data.get("traceId"),
                "liveCrawl": effective_crawl,
                "provenance": "live_webiq_browse_rate_limited",
            })

        raise WebIQError(f"Web IQ /v3/browse returned HTTP {resp.status_code}.")
    except WebIQError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WebIQError(f"Web IQ /v3/browse request failed ({type(exc).__name__}).") from None
    finally:
        del clean_key


async def search_webiq_reviews(
    api_key: str,
    web_query: str,
    *,
    secondary_web_query: str | None = None,
) -> Enrichment:
    web_enrichment = await search_webiq(api_key, web_query, max_results=8)
    merged_sources: list[Source] = list(web_enrichment.sources)
    seen_urls: set[str] = {s.url for s in merged_sources}

    clean_secondary = (secondary_web_query or "").strip()
    if clean_secondary and clean_secondary.lower() != web_query.strip().lower():
        secondary_enrichment = await search_webiq(api_key, clean_secondary, max_results=6)
        for src in secondary_enrichment.sources:
            if src.url not in seen_urls and len(merged_sources) < 12:
                seen_urls.add(src.url)
                merged_sources.append(src)

    return Enrichment(
        query=web_enrichment.query,
        retrieved_at=web_enrichment.retrieved_at,
        sources=tuple(merged_sources),
        provenance="live_webiq",
    )


def format_context(enrichment: Enrichment) -> str:
    lines = [
        "=== UNTRUSTED EXTERNAL WEB IQ GROUNDING METADATA & EXCERPTS (<=200 CHARS) ===",
        f"Web Query: {enrichment.query}",
    ]
    for idx, src in enumerate(enrichment.sources, start=1):
        lines.append(
            f"[W{idx}] ({src.platform} | {src.access_mode}) {src.title} — {src.url}\n"
            f"    Excerpt: {truncate_display_excerpt(src.content, MAX_DISPLAY_EXCERPT_CHARS)}"
        )
    return "\n".join(lines)
