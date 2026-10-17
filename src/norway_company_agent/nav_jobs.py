"""NAV Arbeidsplassen public jobs matcher by organisation number.

Light, fail-soft path:

1. Search the public stillinger API by legal name (cheap candidate shortlist).
2. For each candidate UUID, fetch the authenticated public feed entry
   (https://pam-stilling-feed.nav.no) which carries employer.orgnr.
3. Keep only ACTIVE ads whose employer.orgnr exactly equals this company.

Ads present only on Finn.no are excluded from the feed and therefore cannot be
orgnr-verified here; those candidates are skipped rather than published on name
match alone. Token is the public rotating JWT from /api/publicToken (no spend).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .evidence import evidence
from .http import FetchResult

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
PUBLIC_TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEED_ENTRY_URL = "https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}"
SEARCH_URL = "https://arbeidsplassen.nav.no/stillinger/api/search"
SOURCE_TYPE = "nav_arbeidsplassen_jobs"
MAX_CANDIDATES = 8
MAX_DETAIL_FETCHES = 5
SUFFIX = re.compile(r"\b(AS|ASA|ANS|DA|ENK|SA|BA|NUF|KS|IKS|SF|STI|FLI|SAM)\b\.?", re.I)

_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None


def _utc_now() -> str:
    from .evidence import utc_now

    return utc_now()


def normalize_name(name: str) -> str:
    text = SUFFIX.sub(" ", name or "")
    text = re.sub(r"[^0-9a-zæøå]+", " ", text.casefold())
    return " ".join(text.split())


def names_compatible(company: str, employer: str) -> bool:
    """Loose candidate filter before orgnr verification; never publishes on this alone."""
    a = normalize_name(company)
    b = normalize_name(employer)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    a_tokens = [t for t in a.split() if len(t) > 2]
    b_tokens = set(b.split())
    return bool(a_tokens) and all(t in b_tokens for t in a_tokens[:3])


def _request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    attempts: int = 2,
) -> FetchResult:
    last_error = "request failed"
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    for attempt in range(attempts):
        started = time.monotonic()
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                elapsed = int((time.monotonic() - started) * 1000)
                body: Any
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = raw.decode("utf-8", errors="replace")
                return FetchResult(
                    url,
                    response.status,
                    elapsed,
                    len(raw),
                    body,
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    retrieved_at=_utc_now(),
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
                    retrieved_at=_utc_now(),
                )
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = type(exc).__name__
        if attempt + 1 < attempts:
            time.sleep(0.3 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())


def get_public_token(*, force: bool = False) -> str | None:
    """Fetch/cache the rotating public Bearer token. Fail-soft returns None."""
    global _token_cache
    now = time.monotonic()
    with _token_lock:
        if not force and _token_cache and now - _token_cache[1] < 3600:
            return _token_cache[0]
    result = _request(PUBLIC_TOKEN_URL, timeout=15.0)
    if result.status != 200 or not result.body:
        return None
    text = result.body if isinstance(result.body, str) else json.dumps(result.body)
    token = next((part for part in text.replace("\n", " ").split() if part.startswith("eyJ")), None)
    if not token:
        return None
    with _token_lock:
        _token_cache = (token, time.monotonic())
    return token


def search_job_candidates(name: str, *, size: int = MAX_CANDIDATES) -> tuple[list[dict[str, Any]], FetchResult]:
    query = urllib.parse.urlencode({"q": name, "from": 0, "size": size})
    url = f"{SEARCH_URL}?{query}"
    result = _request(url)
    candidates: list[dict[str, Any]] = []
    if result.status == 200 and isinstance(result.body, dict):
        for hit in (result.body.get("hits") or {}).get("hits") or []:
            src = hit.get("_source") or hit
            uuid = src.get("uuid")
            if not uuid:
                continue
            employer_name = ((src.get("employer") or {}).get("name")) or src.get("businessName") or ""
            candidates.append(
                {
                    "uuid": uuid,
                    "title": src.get("title"),
                    "business_name": src.get("businessName"),
                    "employer_name": employer_name,
                    "published": src.get("published"),
                    "status": src.get("status"),
                    "search_url": url,
                }
            )
    return candidates, result


def fetch_feed_entry(uuid: str, token: str) -> FetchResult:
    return _request(
        FEED_ENTRY_URL.format(uuid=uuid),
        headers={"Authorization": f"Bearer {token}"},
    )


def jobs_from_entry(body: Any, org: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or body.get("status") != "ACTIVE":
        return []
    content = body.get("ad_content") or body.get("json") or {}
    employer = content.get("employer") or {}
    orgnr = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
    if orgnr != org:
        return []
    link = content.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{content.get('uuid') or body.get('uuid')}"
    return [
        {
            "uuid": content.get("uuid") or body.get("uuid"),
            "title": content.get("title") or content.get("jobtitle"),
            "published": content.get("published"),
            "expires": content.get("expires"),
            "employer_name": employer.get("name"),
            "employer_orgnr": orgnr,
            "employer_homepage": employer.get("homepage"),
            "link": link,
            "municipal": ((content.get("workLocations") or [{}])[0] or {}).get("municipal"),
            "engagementtype": content.get("engagementtype"),
            "extent": content.get("extent"),
        }
    ]


def fetch_nav_jobs(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (evidence_record, metrics) for one company. Never invents jobs."""
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    source_url = SEARCH_URL

    def _add(result: FetchResult) -> None:
        metrics["requests"] += 1
        metrics["bytes"] += result.bytes_received
        if result.elapsed_ms:
            metrics["latencies_ms"].append(result.elapsed_ms)

    if len(org) != 9 or not name.strip():
        return (
            evidence("nav_jobs", "not_applicable", SOURCE_TYPE, source_url, note="Missing organisation number or name"),
            metrics,
        )

    token = get_public_token()

    candidates, search_result = search_job_candidates(name)
    _add(search_result)
    source_url = search_result.url
    if search_result.status != 200:
        return (
            evidence(
                "nav_jobs",
                "source_error",
                SOURCE_TYPE,
                search_result.url,
                note=search_result.error or f"HTTP {search_result.status}",
                content_sha256=search_result.content_sha256,
                retrieved_at=search_result.retrieved_at,
                source_row_key=org,
            ),
            metrics,
        )

    if token is None:
        token = get_public_token(force=True)
    if token is None:
        return (
            evidence(
                "nav_jobs",
                "source_error",
                SOURCE_TYPE,
                PUBLIC_TOKEN_URL,
                note="Public NAV feed token unavailable; cannot orgnr-verify ads",
                retrieved_at=_utc_now(),
                source_row_key=org,
            ),
            metrics,
        )

    compatible = [
        item
        for item in candidates
        if names_compatible(name, item.get("employer_name") or "")
        or names_compatible(name, item.get("business_name") or "")
    ][:MAX_DETAIL_FETCHES]

    matched: list[dict[str, Any]] = []
    hashes: list[str] = []
    skipped_unverifiable = 0
    for item in compatible:
        entry = fetch_feed_entry(item["uuid"], token)
        _add(entry)
        if entry.content_sha256:
            hashes.append(entry.content_sha256)
        if entry.status == 404:
            skipped_unverifiable += 1
            continue
        if entry.status != 200:
            continue
        matched.extend(jobs_from_entry(entry.body, org))

    # Dedupe by uuid
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for job in matched:
        uid = str(job.get("uuid") or "")
        if uid and uid not in seen:
            seen.add(uid)
            unique.append(job)

    combined_hash = hashlib.sha256("".join(hashes).encode()).hexdigest() if hashes else search_result.content_sha256
    value = {
        "jobs": unique,
        "job_count": len(unique),
        "candidates_considered": len(compatible),
        "skipped_unverifiable_feed_entries": skipped_unverifiable,
        "match_rule": "exact_employer_orgnr_on_active_feed_entry",
    }
    if unique:
        return (
            evidence(
                "nav_jobs",
                "available",
                SOURCE_TYPE,
                source_url,
                value=value,
                content_sha256=combined_hash,
                retrieved_at=search_result.retrieved_at,
                source_row_key=org,
                note=f"{len(unique)} active ad(s) with employer.orgnr={org}",
            ),
            metrics,
        )
    return (
        evidence(
            "nav_jobs",
            "not_found",
            SOURCE_TYPE,
            source_url,
            value=value,
            content_sha256=combined_hash,
            retrieved_at=search_result.retrieved_at,
            source_row_key=org,
            note="No ACTIVE feed ads with exact employer.orgnr for this company",
        ),
        metrics,
    )
