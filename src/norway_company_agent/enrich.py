"""Enrich one company profile with official modules, website/social, news, jobs."""
from __future__ import annotations

from typing import Any

from .brreg_announcements import fetch_brreg_announcements
from .evidence import evidence
from .identity import GATE_VERSION, apply_website_identity_gate
from .nav_jobs import fetch_nav_jobs
from .news import fetch_news
from .official import fetch_official_modules
from .site_discovery import discover_website
from .website import fetch_website


DEFAULT_MODULES = (
    "registry",
    "accounting_obligation",
    "registry_live",
    "financials",
    "roles",
    "group",
    "locations",
    "website",
    "nav_jobs",
    "brreg_announcements",
    "news",
)


def enrich_profile(
    profile: dict[str, Any],
    *,
    modules: list[str] | None = None,
    discover_websites: bool = True,
    nav_jobs: bool = True,
    brreg_announcements: bool = True,
    news: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mutate and return profile plus request metrics. Fail-soft on optional modules."""
    requested = list(modules or DEFAULT_MODULES)
    fetch_modules = set(requested) - {
        "registry",
        "accounting_obligation",
        "website",
        "nav_jobs",
        "brreg_announcements",
        "website_discovery",
        "news",
    }
    # registry_live is needed for discovery address/email even when already present
    if "registry_live" not in profile.get("evidence", {}) and "registry_live" not in fetch_modules:
        fetch_modules = set(fetch_modules) | {"registry_live"}

    records, metrics = fetch_official_modules(
        profile["organisation_number"],
        fetch_modules,
        expected_latest_year=profile.get("latest_submitted_accounts"),
    )
    profile.setdefault("evidence", {}).update(records)

    # Keep top-level website in sync with live registry when present
    live = (profile["evidence"].get("registry_live") or {}).get("value") or {}
    if live.get("website") and not str(profile.get("website") or "").strip():
        profile["website"] = live.get("website")
    if live.get("name"):
        profile["name"] = profile.get("name") or live.get("name")
    if live.get("latest_submitted_accounts"):
        profile["latest_submitted_accounts"] = live.get("latest_submitted_accounts")

    website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    if "website" in requested:
        discovery_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if not str(profile.get("website") or "").strip() and discover_websites:
            found, discovery_record, discovery_metrics = discover_website(profile)
            profile["evidence"]["website_discovery"] = discovery_record
            if found:
                profile["website"] = found
                profile["website_origin"] = "discovered"
        website_record, website_metrics = fetch_website(profile.get("website"))
        if profile.get("website_origin") == "discovered":
            website_record["source_type"] = website_record["source_class"] = "discovered_company_website"
            disc_value = (profile["evidence"].get("website_discovery") or {}).get("value") or {}
            if website_record.get("status") == "available" and disc_value.get("matched_identity_text_excerpt"):
                website_record.setdefault("value", {}).setdefault("pages", []).append({
                    "url": disc_value.get("matched_url"),
                    "identity_text_excerpt": disc_value["matched_identity_text_excerpt"],
                    "origin": "website_discovery",
                })
        for key in ("requests", "bytes"):
            website_metrics[key] += discovery_metrics[key]
        website_metrics["latencies_ms"] = website_metrics["latencies_ms"] + discovery_metrics["latencies_ms"]
        profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]

    extra_requests = website_metrics["requests"]
    extra_bytes = website_metrics["bytes"]
    extra_latencies = list(website_metrics["latencies_ms"])

    if "nav_jobs" in requested and nav_jobs:
        nav_record, nav_metrics = fetch_nav_jobs(profile)
        profile["evidence"]["nav_jobs"] = nav_record
        extra_requests += nav_metrics["requests"]
        extra_bytes += nav_metrics["bytes"]
        extra_latencies.extend(nav_metrics["latencies_ms"])
    elif "nav_jobs" in requested:
        profile["evidence"]["nav_jobs"] = evidence(
            "nav_jobs", "not_applicable", "nav_arbeidsplassen_jobs",
            "https://arbeidsplassen.nav.no/stillinger/api/search", note="disabled",
        )

    if "brreg_announcements" in requested and brreg_announcements:
        ann_record, ann_metrics = fetch_brreg_announcements(profile)
        profile["evidence"]["brreg_announcements"] = ann_record
        extra_requests += ann_metrics["requests"]
        extra_bytes += ann_metrics["bytes"]
        extra_latencies.extend(ann_metrics["latencies_ms"])
    elif "brreg_announcements" in requested:
        profile["evidence"]["brreg_announcements"] = evidence(
            "brreg_announcements", "not_applicable", "brreg_kunngjoringer",
            "https://w2.brreg.no/kunngjoring/hent_nr.jsp", note="disabled",
        )

    if "news" in requested and news:
        news_record, news_metrics = fetch_news(profile)
        profile["evidence"]["news"] = news_record
        extra_requests += news_metrics["requests"]
        extra_bytes += news_metrics["bytes"]
        extra_latencies.extend(news_metrics["latencies_ms"])
    elif "news" in requested:
        profile["evidence"]["news"] = evidence(
            "news", "not_applicable", "verified_company_news",
            "https://news.google.com/rss/search", note="disabled",
        )

    metric = {
        "requests": len(metrics) + extra_requests,
        "bytes": sum(item.bytes_received for item in metrics) + extra_bytes,
        "latencies_ms": [item.elapsed_ms for item in metrics] + extra_latencies,
    }
    profile["run_metrics"] = metric
    profile["gate_version"] = GATE_VERSION
    return profile, metric
