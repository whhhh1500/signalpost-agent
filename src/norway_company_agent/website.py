from __future__ import annotations

import json
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
)


import os as _os

_FAKE_IP_OK = _os.environ.get("SIGNALPOST_ALLOW_FAKE_IP_DNS") == "1"
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if _FAKE_IP_OK and ip in _FAKE_IP_NET:
            continue  # local dev only: sandbox DNS maps every host into 198.18.0.0/15
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            parser.parse(response.read().decode("utf-8", errors="replace").splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        # An unavailable robots file is not permission to ignore explicit site terms; callers retain
        # the URL and can route uncertain domains to review. For this bounded homepage POC, allow one
        # ordinary GET when robots.txt is absent rather than crawl deeper.
        return True


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = parse_social_account(url)
        if not normalized:
            continue
        found.setdefault((normalized["platform"], normalized["handle"]), {"platform": normalized["platform"], "url": normalized["url"]})
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = parse_social_account(raw.strip())
                if normalized:
                    found.setdefault((normalized["platform"], normalized["handle"]), {"platform": normalized["platform"], "url": normalized["url"]})
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


# Per-platform account-path rules (audit: a social URL is an ACCOUNT only when its path is the
# platform's account path; content paths such as posts/videos/reels are never accounts).
_RESERVED_FIRST = {
    "facebook": {"sharer", "sharer.php", "share.php", "share", "dialog", "policy.php", "privacy", "events", "groups",
                 "plugins", "people", "pages", "watch", "hashtag", "story.php", "photo.php", "photo", "photos",
                 "permalink.php", "login", "login.php", "home.php", "marketplace", "gaming", "profile.php", "reel",
                 "reels", "videos", "video", "posts", "help", "legal", "settings", "notifications", "messages",
                 "business", "ads", "l.php", "tr", "media", "stories", "search", "fundraisers", "accounts"},
    "instagram": {"p", "reel", "reels", "stories", "explore", "accounts", "tv", "direct", "about", "developer",
                  "legal", "web", "challenge", "s", "tags"},
    "x": {"intent", "share", "home", "search", "i", "hashtag", "explore", "settings", "login", "signup", "messages",
          "notifications", "compose", "tos", "privacy"},
    "tiktok": set(),
    "linkedin": set(),
    "youtube": {"watch", "shorts", "playlist", "results", "feed", "embed", "live", "hashtag"},
}
# Trailing account "tabs" that still denote the account itself; anything else after the handle is content.
_ACCOUNT_TABS = {
    "facebook": {"about", "info", "community", "reviews"},
    "instagram": set(),
    "x": set(),
    "tiktok": set(),
    "linkedin": {"about", "jobs", "life", "people", "posts", "mycompany"},
    "youtube": {"videos", "featured", "about", "streams", "shorts", "playlists", "community", "channels"},
}
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,100}$")


def parse_social_account(url: str) -> dict[str, str] | None:
    """Return {platform, url, handle} for an account URL, or None for anything else.

    `handle` is the case-folded account identifier used for identity checks and
    de-duplication; it is ALWAYS the platform's account segment, never the last path
    segment (so facebook.com/someone/posts/brandmaker is rejected, not read as 'brandmaker').
    """
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https", ""}:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform or host == "youtu.be":
        return None
    parts = [part.strip() for part in urllib.parse.unquote(parsed.path).split("/") if part.strip()]
    if not parts:
        return None
    lowered = [part.casefold() for part in parts]
    if lowered[0] in _RESERVED_FIRST[platform]:
        return None
    tabs = _ACCOUNT_TABS[platform]
    if platform == "linkedin":
        if lowered[0] != "company" or len(parts) < 2:
            return None
        account, rest = parts[:2], lowered[2:]
        handle = parts[1]
    elif platform == "youtube":
        if parts[0].startswith("@"):
            account, rest, handle = parts[:1], lowered[1:], parts[0][1:]
        elif lowered[0] in {"channel", "user", "c"} and len(parts) >= 2:
            account, rest, handle = parts[:2], lowered[2:], parts[1]
        else:
            return None
    elif platform == "tiktok":
        if not parts[0].startswith("@"):
            return None
        account, rest, handle = parts[:1], lowered[1:], parts[0][1:]
    else:  # facebook, instagram, x: the account is the single first segment
        account, rest, handle = parts[:1], lowered[1:], parts[0].lstrip("@")
    if rest and (len(rest) > 1 or rest[0] not in tabs):
        return None  # content path (post, video, reel, photo, status ...) -- not an account
    if not handle or not _HANDLE_RE.match(handle) or handle.endswith(".php"):
        return None
    canonical_host = {
        "linkedin": "linkedin.com", "facebook": "facebook.com", "instagram": "instagram.com",
        "x": "x.com", "youtube": "youtube.com", "tiktok": "tiktok.com",
    }[platform]
    case_sensitive = platform == "youtube" and lowered[0] == "channel"
    path = "/".join(account)
    return {"platform": platform, "url": f"https://{canonical_host}/{path}", "handle": handle if case_sensitive else handle.casefold()}


def normalize_social_url(url: str) -> dict[str, str] | None:
    """Public form {platform, url} (original handle casing kept for display)."""
    parsed = parse_social_account(url)
    return {"platform": parsed["platform"], "url": parsed["url"]} if parsed else None


def dedupe_social_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """De-duplicate by (platform, normalized account handle), not raw URL text."""
    found: dict[tuple[str, str], dict[str, str]] = {}
    for item in links:
        normalized = parse_social_account(item.get("url") or "")
        if not normalized:
            continue
        found.setdefault((normalized["platform"], normalized["handle"]), normalized)
    found = {key: {"platform": v["platform"], "url": v["url"]} for key, v in found.items()}
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


IDENTITY_SELECTORS = (
    'footer, address, [itemprop="legalName"], [itemprop="address"], [itemprop="taxID"], [itemprop="vatID"], '
    '[itemprop="telephone"], [itemprop="email"], [class*="footer"], [id*="footer"], [class*="copyright"]'
)
_LEGAL_LINE = re.compile(r"(?:org(?:anisasjons)?\.?\s*-?\s*(?:nr|nummer)|foretaksregisteret|mva|©|copyright)", re.I)


def identity_text_excerpt(soup: BeautifulSoup, limit: int = 3000) -> str:
    """Footer / legal-imprint text (who operates the site); feeds the website identity gate."""
    chunks: list[str] = []
    seen: set[str] = set()
    for node in soup.select(IDENTITY_SELECTORS):
        text = " ".join(node.get_text(" ", strip=True).split())
        if text and text not in seen:
            seen.add(text)
            chunks.append(text)
    for string in soup.find_all(string=_LEGAL_LINE):
        parent = string.parent
        if parent is None or parent.name in {"script", "style"}:
            continue
        text = " ".join(parent.get_text(" ", strip=True).split())[:400]
        if text and text not in seen:
            seen.add(text)
            chunks.append(text)
    return " ".join(chunks)[:limit]


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    return [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        try:
            page_structured = _jsonld_organisations(extruct.extract(page_html, base_url=final_url, syntaxes=["json-ld", "microdata"]))
        except Exception:
            page_structured = []
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "identity_text_excerpt": identity_text_excerpt(page_soup),
            "structured_organisations": page_structured,
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        }
        page_social = _social_links(final_url, page_soup) + structured_social_links(page_structured)
        return page, page_social, 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not _robots_allowed(normalized, timeout):
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "identity_text_excerpt": identity_text_excerpt(soup),
            "social_links": _social_links(final_url, soup),
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
        }
        pages = [{"url": final_url, "title": title[:500], "main_text_excerpt": text[:5000], "identity_text_excerpt": value["identity_text_excerpt"], "content_sha256": value["content_sha256"]}]
        social = value["social_links"] + structured_social_links(value["structured_organisations"])
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        for page_url in _priority_links(final_url, soup):
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = dedupe_social_links(social)
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
