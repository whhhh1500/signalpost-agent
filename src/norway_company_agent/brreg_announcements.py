"""Brreg kunngjøringer (official announcements) by organisation number.

There is no public JSON API for kunngjøringer on data.brreg.no — the open
Enhetsregisteret API covers entities/roles/accounts only. The free public HTML
lookup at w2.brreg.no/kunngjoring/hent_nr.jsp?orgnr=… lists dated announcement
types for an organisation. We fetch and parse that page fail-soft, hash the
response, and emit evidence without inventing rows. Paid XML SFTP subscriptions
are out of scope (spend).
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .evidence import evidence, utc_now
from .http import FetchResult

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
KUNNGJORING_URL = "https://w2.brreg.no/kunngjoring/hent_nr.jsp?orgnr={org}"
DETAIL_BASE = "https://w2.brreg.no/kunngjoring/"
SOURCE_TYPE = "brreg_kunngjoringer"
ROW_RE = re.compile(
    r"<p>(\d{2}\.\d{2}\.\d{4})</p>.*?<a href=\"(hent_en\.jsp\?[^\"]+)\">([^<]+)</a>",
    re.S | re.I,
)
MAX_ROWS = 40


def _fetch_html(url: str, *, timeout: float = 20.0, attempts: int = 2) -> FetchResult:
    last_error = "request failed"
    for attempt in range(attempts):
        started = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                elapsed = int((time.monotonic() - started) * 1000)
                # Brreg historically serves windows-1252 / latin-1
                text = raw.decode("latin-1", errors="replace")
                return FetchResult(
                    url,
                    response.status,
                    elapsed,
                    len(raw),
                    text,
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    retrieved_at=utc_now(),
                )
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read() or b""
            if exc.code in {404, 410}:
                return FetchResult(
                    url,
                    exc.code,
                    elapsed,
                    len(raw),
                    error=f"HTTP {exc.code}",
                    content_sha256=hashlib.sha256(raw).hexdigest() if raw else None,
                    retrieved_at=utc_now(),
                )
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = type(exc).__name__
        if attempt + 1 < attempts:
            time.sleep(0.3 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=utc_now())


def parse_kunngjoringer(html: str, org: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for date_no, href, title in ROW_RE.findall(html or ""):
        href_clean = html_lib.unescape(href).replace("&amp;", "&")
        title_clean = html_lib.unescape(title).strip()
        day, month, year = date_no.split(".")
        iso = f"{year}-{month}-{day}"
        rows.append(
            {
                "date": iso,
                "date_no": date_no,
                "type": title_clean,
                "url": DETAIL_BASE + href_clean,
                "organisation_number": org,
            }
        )
        if len(rows) >= MAX_ROWS:
            break
    return rows


def fetch_brreg_announcements(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    org = str(profile.get("organisation_number") or "")
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    url = KUNNGJORING_URL.format(org=org)
    if len(org) != 9:
        return (
            evidence("brreg_announcements", "not_applicable", SOURCE_TYPE, url, note="Missing organisation number"),
            metrics,
        )

    result = _fetch_html(url)
    metrics["requests"] += 1
    metrics["bytes"] += result.bytes_received
    if result.elapsed_ms:
        metrics["latencies_ms"].append(result.elapsed_ms)

    if result.status in {404, 410}:
        return (
            evidence(
                "brreg_announcements",
                "not_found",
                SOURCE_TYPE,
                result.url,
                note=result.error,
                content_sha256=result.content_sha256,
                retrieved_at=result.retrieved_at,
                source_row_key=org,
            ),
            metrics,
        )
    if result.status != 200 or not isinstance(result.body, str):
        return (
            evidence(
                "brreg_announcements",
                "source_error",
                SOURCE_TYPE,
                result.url,
                note=result.error or f"HTTP {result.status}",
                content_sha256=result.content_sha256,
                retrieved_at=result.retrieved_at,
                source_row_key=org,
            ),
            metrics,
        )

    rows = parse_kunngjoringer(result.body, org)
    value = {
        "announcements": rows,
        "announcement_count": len(rows),
        "source": "brreg_public_html_oppslag",
        "note": "Parsed from public HTML lookup; no official open JSON kunngjøringer API",
    }
    if rows:
        return (
            evidence(
                "brreg_announcements",
                "available",
                SOURCE_TYPE,
                result.url,
                value=value,
                content_sha256=result.content_sha256,
                retrieved_at=result.retrieved_at,
                source_row_key=org,
                as_of=rows[0]["date"],
                note=f"{len(rows)} announcement(s) on public Brreg kunngjøring list",
            ),
            metrics,
        )
    return (
        evidence(
            "brreg_announcements",
            "not_found",
            SOURCE_TYPE,
            result.url,
            value=value,
            content_sha256=result.content_sha256,
            retrieved_at=result.retrieved_at,
            source_row_key=org,
            note="Public kunngjøring list returned no parseable rows for this orgnr",
        ),
        metrics,
    )
