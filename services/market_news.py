"""Live crypto headlines for DreamDEX Event Contract windows (BTC / ETH)."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger("dreamlens.services.news")

_CACHE_KEY = "dreamlens:market-news:v1"
_CACHE_TTL = 180

FEEDS = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    (
        "Google News",
        "https://news.google.com/rss/search?q=bitcoin+OR+ethereum+crypto&hl=en-US&gl=US&ceid=US:en",
    ),
)

_ASSET_RE = {
    "BTC": re.compile(
        r"\b(bitcoin|btc|satoshi|microstrategy|hashrate)\b",
        re.I,
    ),
    "ETH": re.compile(
        r"\b(ethereum|ether|eth|vitalik|staking|layer-?2)\b",
        re.I,
    ),
}
_refresh_lock = threading.Lock()

_TAG = re.compile(r"<[^>]+>")
_NS = re.compile(r"\{[^}]+\}")


def _local(tag: str) -> str:
    return _NS.sub("", tag or "")


def _text(el) -> str:
    if el is None or el.text is None:
        return ""
    return unescape(_TAG.sub("", el.text)).strip()


def _child(parent, *names: str):
    want = {n.lower() for n in names}
    for child in list(parent):
        if _local(child.tag).lower() in want:
            return child
    return None


def _link(item) -> str:
    link_el = _child(item, "link")
    if link_el is not None:
        href = (link_el.get("href") or "").strip()
        if href:
            return href
        text = _text(link_el)
        if text.startswith("http"):
            return text
    for child in list(item):
        if _local(child.tag).lower() == "id" and (_text(child).startswith("http")):
            return _text(child)
    return ""


def _parse_when(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None and timezone.is_naive(dt):
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except Exception:
        pass
    parsed = parse_datetime(text.replace("Z", "+00:00"))
    if parsed is not None and timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _ago(published: datetime | None) -> str:
    if published is None:
        return ""
    now = timezone.now()
    if timezone.is_naive(published):
        published = timezone.make_aware(published, timezone.utc)
    seconds = int((now - published).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _safe_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url.strip()
    return ""


def _assets_for(title: str, summary: str) -> list[str]:
    blob = f"{title} {summary}"
    return [asset for asset, pattern in _ASSET_RE.items() if pattern.search(blob)]


def parse_rss(xml_text: str, source: str) -> list[dict]:
    """Parse RSS 2.0 or Atom into headline dicts."""
    if not (xml_text or "").strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("market news XML parse failed source=%s", source)
        return []
    items = []
    for el in root.iter():
        if _local(el.tag).lower() in {"item", "entry"}:
            items.append(el)
    headlines: list[dict] = []
    for item in items:
        title = _text(_child(item, "title"))
        url = _safe_url(_link(item))
        if not title or not url:
            continue
        summary = _text(_child(item, "description", "summary", "content"))
        published = _parse_when(
            _text(_child(item, "pubDate", "published", "updated", "date"))
        )
        assets = _assets_for(title, summary)
        headlines.append(
            {
                "title": title[:220],
                "url": url,
                "source": source,
                "summary": summary[:280],
                "published": published.isoformat() if published else "",
                "ago": _ago(published),
                "assets": assets,
            }
        )
    return headlines


def _fetch_feed(source: str, url: str) -> list[dict]:
    import httpx

    try:
        with httpx.Client(timeout=6.0, follow_redirects=True) as client:
            res = client.get(
                url,
                headers={"User-Agent": "DreamLens/1.0 (market feed)"},
            )
            res.raise_for_status()
            return parse_rss(res.text, source)
    except Exception:
        logger.warning("market news fetch failed source=%s", source, exc_info=True)
        return []


def _refresh() -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from django.conf import settings

    if not getattr(settings, "MARKET_NEWS_FETCH", True):
        return []

    collected: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_fetch_feed, name, url): name for name, url in FEEDS}
        for fut in as_completed(futs):
            collected.extend(fut.result() or [])
    seen: set[str] = set()
    unique: list[dict] = []
    for row in collected:
        key = (row.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)

    def _sort_key(row: dict) -> str:
        return row.get("published") or ""

    unique.sort(key=_sort_key, reverse=True)
    return unique[:40]


def list_headlines(*, asset: str | None = None, limit: int = 8) -> list[dict]:
    """Cached live headlines, optionally filtered to BTC or ETH."""
    rows = cache.get(_CACHE_KEY)
    if rows is None:
        with _refresh_lock:
            rows = cache.get(_CACHE_KEY)
            if rows is None:
                rows = _refresh()
                try:
                    cache.set(_CACHE_KEY, rows, timeout=_CACHE_TTL)
                except Exception:
                    logger.warning("market news cache write failed")
    token = (asset or "").strip().upper()
    if token in _ASSET_RE:
        filtered = [row for row in rows if token in (row.get("assets") or [])]
        if filtered:
            rows = filtered
    return list(rows[: max(1, min(int(limit or 8), 20))])


def format_headlines_for_prompt(headlines: list[dict]) -> str:
    if not headlines:
        return "No live headlines available."
    lines = []
    for row in headlines[:8]:
        ago = row.get("ago") or ""
        src = row.get("source") or ""
        lines.append(f"- {row.get('title')} ({src}{', ' + ago if ago else ''})")
    return "\n".join(lines)
