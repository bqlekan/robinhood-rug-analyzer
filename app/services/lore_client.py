from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import Counter
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.config import settings
from app.models.token import LoreSource, TokenLore

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
# DuckDuckGo serves a 202 anti-bot challenge to UAs containing "bot"/vendor strings,
# so use a plain browser UA. This is a public HTML search endpoint, not scraping private data.
USER_AGENT = "Mozilla/5.0"

# DuckDuckGo HTML result anchors: <a class="result__a" href="...">Title</a>
_RESULT_RE = re.compile(r'result__a"\s+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

_POSITIVE = {
    "bullish", "moon", "gem", "legit", "safe", "based", "community", "strong",
    "pump", "hodl", "buy", "growth", "trending", "backed", "verified", "locked",
}
_NEGATIVE = {
    "rug", "scam", "honeypot", "dump", "rugged", "warning", "avoid", "fake",
    "ponzi", "exit", "dead", "crash", "fraud", "beware", "sketchy", "risky",
}
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "token", "coin", "crypto",
    "robinhood", "chain", "price", "today", "buy", "swap", "how", "what", "you",
    "your", "from", "are", "was", "will", "com", "www", "https", "http",
}


def _strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<encoded>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return unquote(target)
    return href


async def _duckduckgo(query: str, limit: int = 8) -> list[LoreSource]:
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout, follow_redirects=True) as client:
            response = await client.post(
                DDG_HTML_URL,
                data={"q": query},
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            )
            response.raise_for_status()
            body = response.text
    except httpx.HTTPError as exc:
        logger.warning("DuckDuckGo lore search failed for %r: %s", query, exc)
        return []

    results: list[LoreSource] = []
    for href, raw_title in _RESULT_RE.findall(body):
        title = _strip_tags(raw_title)
        if not title:
            continue
        results.append(
            LoreSource(title=title, url=_decode_ddg_url(href), snippet=None, source="duckduckgo")
        )
        if len(results) >= limit:
            break
    return results


def _is_twitter(url: str) -> bool:
    u = (url or "").lower()
    return "twitter.com" in u or "x.com" in u


def _sources_from_socials(market_socials: list[dict[str, str]], websites: list[str]) -> list[LoreSource]:
    sources: list[LoreSource] = []
    for site in websites:
        if site:
            sources.append(LoreSource(title="Official website", url=site, snippet=None, source="dexscreener"))
    for social in market_socials:
        url = social.get("url")
        if not url:
            continue
        raw_label = (social.get("type") or social.get("platform") or "Social").lower()
        if _is_twitter(url) or raw_label in {"twitter", "x"}:
            sources.append(LoreSource(title="Official X (Twitter)", url=url, snippet=None, source="twitter"))
        else:
            label = social.get("type") or social.get("platform") or "Social"
            sources.append(LoreSource(title=f"Official {label}", url=url, snippet=None, source="dexscreener"))
    return sources


def _heuristic_sentiment(text: str) -> tuple[str, list[str]]:
    lowered = text.lower()
    words = re.findall(r"[a-z]{3,}", lowered)
    pos = sum(1 for w in words if w in _POSITIVE)
    neg = sum(1 for w in words if w in _NEGATIVE)

    if neg > pos and neg > 0:
        sentiment = "negative"
    elif pos > neg and pos > 0:
        sentiment = "positive"
    elif pos or neg:
        sentiment = "neutral"
    else:
        sentiment = "unknown"

    themes = [
        word
        for word, _ in Counter(
            w for w in words if w not in _STOPWORDS and len(w) > 3
        ).most_common(6)
    ]
    return sentiment, themes


async def _llm_summary(name: str, symbol: str, sources: list[LoreSource]) -> str | None:
    """Optional richer summary when an LLM key is configured. Best-effort, never blocks."""
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model):
        return None
    titles = "\n".join(f"- {s.title} ({s.url})" for s in sources[:10])
    prompt = (
        f"Summarize the community narrative/lore behind the crypto token {name} ({symbol}) "
        f"on Robinhood Chain in 2-3 sentences, based only on these search results:\n{titles}"
    )
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("LLM lore summary failed: %s", exc)
        return None


async def build_lore(
    name: str | None,
    symbol: str | None,
    market_socials: list[dict[str, str]] | None = None,
    websites: list[str] | None = None,
) -> TokenLore:
    """Interpret a token's lore from free, public web sources.

    Uses DuckDuckGo web search + DexScreener socials. Produces extractive themes and a
    heuristic sentiment. If an LLM key is configured, adds a narrative summary.
    """
    display = name or symbol or ""
    if not display:
        return TokenLore(
            summary=None,
            themes=[],
            sentiment="unknown",
            sources=[],
            generated_by="none",
        )

    query = f'"{display}" robinhood chain token' if symbol else f"{display} robinhood chain token"
    # Also probe X/Twitter specifically for community chatter.
    x_query = f'"{display}" (site:x.com OR site:twitter.com) crypto'
    web_sources, x_raw = await asyncio.gather(_duckduckgo(query), _duckduckgo(x_query, limit=5))

    # Tag any X/Twitter hits (from either query) with the "twitter" source.
    def _retag(items: list[LoreSource]) -> list[LoreSource]:
        for s in items:
            if _is_twitter(s.url):
                s.source = "twitter"
        return items

    web_sources = _retag(web_sources)
    x_sources = [s for s in _retag(x_raw) if s.source == "twitter"]

    social_sources = _sources_from_socials(market_socials or [], websites or [])

    # De-dup by URL while preserving order (socials first, then X, then general web).
    seen_urls: set[str] = set()
    sources: list[LoreSource] = []
    for s in social_sources + x_sources + web_sources:
        if s.url in seen_urls:
            continue
        seen_urls.add(s.url)
        sources.append(s)

    corpus = " ".join(s.title for s in (web_sources + x_sources))
    sentiment, themes = _heuristic_sentiment(corpus)

    llm_text = await _llm_summary(name or "", symbol or "", sources)
    if llm_text:
        summary = llm_text
        generated_by = "llm"
    elif web_sources:
        top_titles = "; ".join(s.title for s in web_sources[:3])
        summary = (
            f"Public discussion around {display} references: {top_titles}. "
            f"Heuristic sentiment reads as {sentiment}."
        )
        generated_by = "extractive"
    else:
        summary = f"No notable public discussion found for {display}."
        generated_by = "extractive"

    return TokenLore(
        summary=summary,
        themes=themes,
        sentiment=sentiment,
        sources=sources[:12],
        generated_by=generated_by,
    )
