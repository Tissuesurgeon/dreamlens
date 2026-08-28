"""Live market headline parsing and API — no live RSS in tests."""

from __future__ import annotations

import pytest

from services.market_news import list_headlines, parse_rss

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample</title>
    <item>
      <title>Bitcoin holds above $80,000 as ETF inflows return</title>
      <link>https://www.coindesk.com/markets/bitcoin-holds</link>
      <pubDate>Fri, 28 Aug 2026 01:00:00 GMT</pubDate>
      <description>BTC spot demand picked up in US hours.</description>
    </item>
    <item>
      <title>Ethereum staking queue shortens after upgrade</title>
      <link>https://cointelegraph.com/news/eth-staking</link>
      <pubDate>Fri, 28 Aug 2026 00:30:00 GMT</pubDate>
      <description>ETH validators are exiting faster than they enter.</description>
    </item>
    <item>
      <title>javascript:alert(1)</title>
      <link>javascript:alert(1)</link>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_tags_assets_and_skips_unsafe_links():
    rows = parse_rss(RSS, "CoinDesk")
    titles = [row["title"] for row in rows]
    assert "Bitcoin holds above $80,000 as ETF inflows return" in titles
    assert "Ethereum staking queue shortens after upgrade" in titles
    assert all(row["url"].startswith("http") for row in rows)
    btc = next(row for row in rows if "Bitcoin" in row["title"])
    eth = next(row for row in rows if "Ethereum" in row["title"])
    assert "BTC" in btc["assets"]
    assert "ETH" in eth["assets"]
    assert btc["source"] == "CoinDesk"


def test_parse_rss_empty_or_broken():
    assert parse_rss("", "CoinDesk") == []
    assert parse_rss("<not-xml", "CoinDesk") == []


@pytest.mark.django_db
def test_list_headlines_filters_asset(monkeypatch):
    from django.core.cache import cache

    cache.clear()
    rows = parse_rss(RSS, "CoinDesk")
    monkeypatch.setattr("services.market_news._refresh", lambda: rows)
    btc = list_headlines(asset="BTC", limit=8)
    assert btc
    assert all("BTC" in (row.get("assets") or []) for row in btc)


@pytest.mark.django_db
def test_news_api_returns_headlines(client, monkeypatch):
    monkeypatch.setattr(
        "apps.core.api.ai.list_headlines",
        lambda asset=None, limit=8: [
            {
                "title": "Bitcoin holds above $80,000",
                "url": "https://www.coindesk.com/markets/bitcoin-holds",
                "source": "CoinDesk",
                "summary": "Spot demand picked up.",
                "published": "2026-08-28T01:00:00+00:00",
                "ago": "12m ago",
                "assets": ["BTC"],
            }
        ],
    )
    res = client.get("/api/news/?asset=BTC")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    assert body["headlines"][0]["title"].startswith("Bitcoin")
    assert body["headlines"][0]["url"].startswith("https://")
