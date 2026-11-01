"""Recent news for a Norwegian company — blank when identity is unclear.

Two verified channels only:

1. Pages already fetched from a *publishable* company website whose path looks
   like news/press (same identity gate as socials).
2. Google News RSS items whose *title* contains the exact legal name as a
   whole phrase (not nested inside a longer company name). Titles that only
   share a brand token are rejected.

No article body is scraped. Prefer an empty list over a wrong match.
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from .evidence import evidence, utc_now

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOURCE_TYPE = "verified_company_news"
NEWS_PATH = re.compile(r"/(?:news|press|aktuelt|nyheter|artikler|blog)(?:/|$)", re.I)
LEGAL = {"as", "asa", "sa", "ba", "da", "ans", "enk", "nuf", "sti", "ks", "iks", "sf", "fli", "sam"}
ALLOWED_PREDECESSORS = {"av", "for", "fra", "hos", "i", "med", "om", "på", "til", "og", "kjøper", "velger"}


def exact_title_match(company_name: str, title: str) -> bool:
    company_tokens = re.findall(r"[a-z0-9æøå]+", str(company_name or "").casefold())
    title_tokens = re.findall(r"[a-z0-9æøå]+", str(title or "").rsplit(" - ", 1)[0].casefold())
    if not company_tokens or not title_tokens or len(company_tokens) > len(title_tokens):
        return False
    for index in range(len(title_tokens) - len(company_tokens) + 1):
        if title_tokens[index:index + len(company_tokens)] != company_tokens:
            continue
        if index == 0 or title_tokens[index - 1] in ALLOWED_PREDECESSORS:
            return True
    return False


def _company_site_items(profile: dict[str, Any]) -> list[dict[str, Any]]:
    from .identity import website_identity_publishable

    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    if website.get("status") != "available" or not website_identity_publishable(website):
        return []
    pages = [
        page for page in (value.get("pages") or [])
        if NEWS_PATH.search(urlparse(str(page.get("url") or "")).path)
    ]
    if not pages:
        return []
    pages.sort(
        key=lambda page: (
            -len([part for part in urlparse(str(page.get("url") or "")).path.split("/") if part]),
            str(page.get("url") or ""),
        )
    )
    org = str(profile.get("organisation_number") or "")
    items: list[dict[str, Any]] = []
    for page in pages[:5]:
        url = str(page.get("url") or "")
        digest = str(page.get("content_sha256") or "")
        if not url.startswith(("http://", "https://")):
            continue
        title = str(page.get("title") or "Company news page").strip()
        items.append({
            "title": title[:300],
            "url": url,
            "publisher": "company_website",
            "published_at": None,
            "channel": "company_site",
            "exact_entity": True,
            "identity_proof": [
                {"type": "website_identity_gate", "method": (value.get("identity_assessment") or {}).get("method")},
            ],
            "content_sha256": digest or None,
            "id": "company-site-news-" + hashlib.sha256(f"{org}|{url}".encode()).hexdigest()[:24],
        })
    return items


def _google_news_items(profile: dict[str, Any], *, limit: int, years: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not org or not name:
        return [], metrics
    query = urllib.parse.quote(f'"{name}" when:{years}y')
    url = f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"
    started = time.monotonic()
    metrics["requests"] += 1
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(2_000_000)
        metrics["bytes"] += len(raw)
        metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
        root = ET.fromstring(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError, OSError) as exc:
        metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
        metrics["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return [], metrics

    retrieved_at = utc_now()
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in root.findall(".//item"):
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        publisher = str(item.findtext("source") or "").strip()
        if not link or not exact_title_match(name, title):
            continue
        key = (title.casefold(), publisher.casefold())
        if key in seen:
            continue
        seen.add(key)
        published = item.findtext("pubDate")
        try:
            from datetime import timezone as _tz
            published_at = parsedate_to_datetime(published).astimezone(_tz.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            published_at = None
        digest = hashlib.sha256(raw + title.encode("utf-8") + publisher.encode("utf-8")).hexdigest()
        output.append({
            "title": title[:300],
            "url": link,
            "publisher": publisher or None,
            "published_at": published_at,
            "channel": "google_news_rss",
            "exact_entity": True,
            "identity_proof": [
                {"type": "exact_legal_name_in_news_title", "value": name},
                {"type": "publisher_label", "value": publisher},
            ],
            "content_sha256": digest,
            "retrieved_at": retrieved_at,
            "id": "google-news-title-" + hashlib.sha256(f"{org}|{title}|{publisher}".encode()).hexdigest()[:24],
        })
        if len(output) >= limit:
            break
    return output, metrics


def fetch_news(profile: dict[str, Any], *, limit: int = 8, years: int = 2) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (evidence_record, metrics). Fail-soft: never invent matches."""
    site_items = _company_site_items(profile)
    rss_items, metrics = _google_news_items(profile, limit=limit, years=years)
    # Prefer company-site pages first; then independent titles. Cap total.
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in site_items + rss_items:
        url = str(item.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(item)
        if len(merged) >= limit:
            break
    src = "https://news.google.com/rss/search"
    if not merged:
        status = "source_error" if metrics.get("error") else "not_found"
        note = metrics.get("error") or "No exact-entity news title or verified company-site news page"
        return evidence("news", status, SOURCE_TYPE, src, value={"items": []}, note=note), metrics
    return evidence(
        "news",
        "available",
        SOURCE_TYPE,
        src,
        value={
            "items": merged,
            "company_site_count": len(site_items),
            "google_news_count": len(rss_items),
            "policy": "Publish only exact legal-name titles or news pages from a publishable company website. Blank beats a wrong match.",
        },
        note="Verified news mentions only; article bodies not scraped",
    ), metrics
