"""NAV Arbeidsplassen public jobs matcher by organisation number.

Light, fail-soft path:

1. Search the public stillinger API by legal name (cheap candidate shortlist).
2. For each candidate UUID, fetch the authenticated public feed entry
   (https://pam-stilling-feed.nav.no) which carries employer.orgnr.
3. Keep only ACTIVE ads whose employer.orgnr exactly equals this company or
   one of its Brreg-registered sub-units (underenheter from the locations module).

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
MAX_CANDIDATES = 25
MAX_DETAIL_FETCHES = 15
SUFFIX = re.compile(r"\b(AS|ASA|ANS|DA|ENK|SA|BA|NUF|KS|IKS|SF|STI|FLI|SAM)\b\.?", re.I)

_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None


def _utc_now() -> str:
    from .evidence import utc_now

    return utc_now()


def normalize_name(name: str) -> str:
    text = SUFFIX.sub(" ", name or "")
    text = re.sub(r"[\W_]+", " ", text.casefold())
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


GENERIC_QUERY_TOKENS = {
    "kommune", "fylkeskommune", "hotel", "hotell", "norge", "norway", "bygg", "service", "services",
    "holding", "eiendom", "invest", "drift", "gruppen", "group", "senter", "barnehage", "sokn",
    "helse", "og", "the", "sektor", "avdeling",
}


def search_queries(name: str) -> list[str]:
    """NAV search is AND-like over businessName/title text, so legal suffixes ("AS") and
    long organisational-unit names return zero hits. Try the suffix-free name first, then its
    first two tokens, then a single distinctive first token. Recall only: every candidate
    is still verified by exact employer.orgnr before anything is published."""
    tokens = normalize_name(name).split()
    out: list[str] = []
    if tokens:
        out.append(" ".join(tokens))
    if len(tokens) > 2:
        out.append(" ".join(tokens[:2]))
    if len(tokens) >= 2 and len(tokens[0]) >= 4 and tokens[0] not in GENERIC_QUERY_TOKENS:
        out.append(tokens[0])
    seen: list[str] = []
    for q in out:
        if q not in seen:
            seen.append(q)
    return seen


def _query_compatible(query: str, employer: str) -> bool:
    q = set(query.split())
    return bool(q) and q.issubset(set(normalize_name(employer).split()))


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


def registered_subunits(profile: dict[str, Any]) -> set[str]:
    """Sub-unit (underenhet) orgnrs already fetched from Brreg for this company.

    NAV ads usually name the employer by its sub-unit number, not the legal
    entity. The locations module reads Brreg underenheter filtered by
    overordnetEnhet, so these numbers are officially tied to the company and
    cost no extra requests.
    """
    record = (profile.get("evidence") or {}).get("locations") or {}
    if record.get("status") != "available":
        return set()
    units = ((record.get("value") or {}).get("locations")) or []
    out: set[str] = set()
    for unit in units:
        num = re.sub(r"\D", "", str((unit or {}).get("organisation_number") or ""))
        if len(num) == 9:
            out.add(num)
    return out


AGGREGATE_TITLE = re.compile(
    r"\b(flere|ulike|diverse|mange|nye)\s+(ledige\s+)?stillinger\b|\bledige\s+stillinger\b|"
    r"\b(talentbank|talent\s*pool|åpen\s+søknad|apen\s+soknad|generell\s+søknad|open\s+application|"
    r"multiple\s+positions|several\s+positions|various\s+positions|sommerjobb(er)?\s+\d{4}\s*-\s*flere)\b|"
    # an explicit head-count in the title ("2 faste 100%-stillinger", "3 sykepleiere")
    r"(?<![\d%,.])\b([2-9]|[1-9]\d)\s+(?:nye\s+|faste\s+|midlertidige\s+|ledige\s+)*"
    r"(?:\d+\s*%-?\s*)?(?:\w+-)?(stillinger|stillingar|vikariater|vikariat|vikarer|vikarar|sykepleiere|sjukepleiarar|medarbeidere|medarbeidarar|lærlinger|lærlingar|lærlingplasser)\b|"
    # recruitment pools for several people, not one vacancy
    r"\b(tilkallingsvikarer|tilkallingsvikarar|ringevikarer|ringevikarar|sommervikarer|sommarvikarar|ekstravakter|ekstrahjelper|vikarer|vikarar|vikarpool|vikarbank|helgestillinger|helgestillingar)\b",
    re.I,
)


OSLO_TZ_NAME = "Europe/Oslo"


def _oslo_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(OSLO_TZ_NAME)


def _parse_dt(value: Any, *, end_of_day: bool = False) -> "datetime | None":
    """Parse a NAV timestamp to an aware datetime.

    Naive timestamps are NAV local time (Europe/Oslo). A date-only value is a whole day in
    Oslo: as a deadline (`end_of_day=True`) it means 23:59:59.999999 Oslo time that day.
    """
    from datetime import datetime, time

    text = str(value or "").strip()
    if not text:
        return None
    date_only = len(text) == 10
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            date_only = True
        except ValueError:
            return None
    if date_only and end_of_day:
        parsed = datetime.combine(parsed.date(), time.max)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_oslo_tz())


def single_current_ad_check(content: dict[str, Any], now: "datetime | None" = None) -> str | None:
    """Return a rejection reason, or None if this is ONE current ad of its own.

    Rules (full timestamps, timezone-aware; audit item 6):
    * `expires` (or a dated `applicationDue`) must exist and be later than now; a date-only
      deadline lasts until the end of that day in Europe/Oslo;
    * a dated `applicationDue` that has passed rejects the ad even if `expires` is later;
    * `published` must not be in the future;
    * `positioncount` must be present and exactly 1 (missing/0/non-numeric cannot prove a single vacancy);
    * the title must not be an aggregate/multi-role/talent-pool posting.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_oslo_tz())
    expires = _parse_dt(content.get("expires"), end_of_day=True)
    due = _parse_dt(content.get("applicationDue"), end_of_day=True)
    deadline = expires or due
    if deadline is None:
        return "no expiry/application deadline: cannot prove the ad is current"
    if deadline <= now:
        return f"ad expired {deadline.isoformat()}"
    if due is not None and due <= now:
        return f"application deadline passed {due.isoformat()}"
    published = _parse_dt(content.get("published"))
    if published is not None and published > now:
        return f"ad published in the future ({published.isoformat()})"
    raw_count = content.get("positioncount")
    count = str(raw_count if raw_count is not None else "").strip()
    if not count.isdigit() or int(count) != 1:
        return f"positioncount is not exactly 1 (positioncount={raw_count!r}); cannot prove a single vacancy"
    title = " ".join(str(content.get("title") or content.get("jobtitle") or "").split())
    if not title:
        return "ad has no title"
    if AGGREGATE_TITLE.search(title):
        return "title indicates an aggregate or multi-role posting"
    return None


def jobs_from_entry(body: Any, org: str, subunits: set[str] | frozenset[str] = frozenset(), now: Any = None, rejected: list | None = None) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or body.get("status") != "ACTIVE":
        return []
    content = body.get("ad_content") or body.get("json") or {}
    employer = content.get("employer") or {}
    orgnr = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
    if orgnr == org:
        relation = "legal_entity"
    elif orgnr in subunits:
        relation = "registered_subunit"
    else:
        return []
    reason = single_current_ad_check(content, now)
    if reason:
        if rejected is not None:
            rejected.append({"uuid": content.get("uuid") or body.get("uuid"), "title": content.get("title"), "reason": reason})
        return []
    uuid = content.get("uuid") or body.get("uuid")
    # Always publish the canonical single-ad NAV page, never a list/search page.
    link = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
    return [
        {
            "uuid": uuid,
            "title": " ".join(re.sub(r"[\ufeff\u200b\u200c\u200d]", "", str(content.get("title") or content.get("jobtitle") or "")).split()),
            "published": content.get("published"),
            "expires": content.get("expires"),
            "application_due": content.get("applicationDue"),
            "positioncount": content.get("positioncount"),
            "employer_name": employer.get("name"),
            "employer_orgnr": orgnr,
            "employer_relation": relation,
            "employer_homepage": employer.get("homepage"),
            "link": link,
            "original_source_url": content.get("sourceurl") or content.get("link"),
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
    if token is None:
        token = get_public_token(force=True)

    subunits = registered_subunits(profile)
    queries = search_queries(name)
    search_result = None
    compatible: list[dict[str, Any]] = []
    queries_used: list[str] = []
    search_errors: list[str] = []
    matched: list[dict[str, Any]] = []
    hashes: list[str] = []
    skipped_unverifiable = 0
    detail_errors: list[dict[str, Any]] = []
    detail_ok = 0
    rejected_ads: list[dict[str, Any]] = []
    fetched: set[str] = set()
    # Audit item 10: keep going through the fallback queries until at least one ad is
    # orgnr-VERIFIED (not merely name-compatible), within a fixed detail-fetch budget.
    for query in queries or [name]:
        found, result = search_job_candidates(query)
        _add(result)
        queries_used.append(query)
        if search_result is None or (search_result.status != 200 and result.status == 200):
            search_result = result
        if result.status != 200:
            search_errors.append(f"{query}: {result.error or 'HTTP ' + str(result.status)}")
            continue
        new_items = []
        for item in found:
            emp = item.get("employer_name") or ""
            biz = item.get("business_name") or ""
            ok = names_compatible(name, emp) or names_compatible(name, biz) or _query_compatible(query, emp) or _query_compatible(query, biz)
            if ok and item["uuid"] not in {c["uuid"] for c in compatible}:
                compatible.append(item)
                new_items.append(item)
        if token is None:
            continue
        for item in new_items:
            if len(fetched) >= MAX_DETAIL_FETCHES:
                break
            fetched.add(item["uuid"])
            entry = fetch_feed_entry(item["uuid"], token)
            _add(entry)
            if entry.content_sha256:
                hashes.append(entry.content_sha256)
            if entry.status == 404:
                skipped_unverifiable += 1
                continue
            if entry.status != 200:
                detail_errors.append({"uuid": item["uuid"], "status": entry.status, "error": entry.error})
                continue
            detail_ok += 1
            matched.extend(jobs_from_entry(entry.body, org, subunits, rejected=rejected_ads))
        if matched or len(fetched) >= MAX_DETAIL_FETCHES:
            break
    assert search_result is not None
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
    if token is None and compatible:
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

    # Dedupe by uuid
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for job in matched:
        # Audit item 11: one NAV ad UUID = one ad. Two ads with the same title/place/deadline are
        # distinct vacancies unless they share the UUID.
        uid = str(job.get("uuid") or "")
        if uid and uid not in seen:
            seen.add(uid)
            unique.append(job)

    combined_hash = hashlib.sha256("".join(hashes).encode()).hexdigest() if hashes else search_result.content_sha256
    value = {
        "jobs": unique,
        "job_count": len(unique),
        "candidates_considered": len(compatible),
        "detail_fetches": len(fetched),
        "detail_fetches_ok": detail_ok,
        "detail_fetch_errors": detail_errors,
        "search_errors": search_errors,
        "search_queries": queries_used,
        "skipped_unverifiable_feed_entries": skipped_unverifiable,
        "match_rule": "exact_employer_orgnr_on_active_feed_entry (legal entity or Brreg-registered sub-unit)",
        "subunits_checked": len(subunits),
        "rejected_own_ads": rejected_ads,
        "ad_rule": "single current ad: ACTIVE, not expired, positioncount=1, non-aggregate title, canonical NAV ad URL",
    }
    # Round-2 audit P1-G: "verified ads found" is not "complete count". The count is complete
    # only if every query ran successfully, every name-compatible candidate was detail-checked,
    # and no detail fetch failed. Otherwise the count is unknown -- never an asserted 0.
    unchecked = [c["uuid"] for c in compatible if c["uuid"] not in fetched]
    count_complete = not search_errors and not unchecked and not detail_errors
    value["verified_ad_count"] = len(unique)
    value["unchecked_candidates"] = len(unchecked)
    value["count_complete"] = count_complete
    if not count_complete:
        value["verification_incomplete"] = True
        value["job_count"] = None
        value["job_count_note"] = (
            f"{len(unique)} ad(s) verified; count incomplete ({len(search_errors)} failed search(es), "
            f"{len(unchecked)} unchecked candidate(s), {len(detail_errors)} failed detail fetch(es))"
        )
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
                note=f"{len(unique)} active ad(s) with employer.orgnr={org} or one of its registered sub-units",
            ),
            metrics,
        )
    if not count_complete and not unique:
        # All verification attempts failed: this is NOT "no jobs".
        return (
            evidence(
                "nav_jobs",
                "source_error",
                SOURCE_TYPE,
                source_url,
                value={**value, "job_count": None},
                content_sha256=combined_hash,
                retrieved_at=search_result.retrieved_at,
                source_row_key=org,
                note=value["job_count_note"] + "; cannot claim 'no jobs', job count unknown",
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
