#!/usr/bin/env python3
"""CLI: resolve + enrich one company; print a compact JSON summary with sources."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.enrich import enrich_profile  # noqa: E402
from norway_company_agent.identity import website_identity_publishable  # noqa: E402
from norway_company_agent.resolve import resolve_company  # noqa: E402


def summarize(profile: dict) -> dict:
    evidence = profile.get("evidence") or {}
    website = evidence.get("website") or {}
    value = website.get("value") or {}
    publishable = website_identity_publishable(website)
    news = evidence.get("news") or {}
    jobs = evidence.get("nav_jobs") or {}
    return {
        "organisation_number": profile.get("organisation_number"),
        "name": profile.get("name"),
        "website": (value.get("final_url") or profile.get("website")) if publishable else None,
        "website_publishable": publishable,
        "website_origin": profile.get("website_origin"),
        "website_reason": (value.get("identity_assessment") or {}).get("reason") or ((evidence.get("website_discovery") or {}).get("value") or {}).get("reason"),
        "social_links": value.get("social_links") if publishable else [],
        "news_count": len(((news.get("value") or {}).get("items") or [])) if news.get("status") == "available" else 0,
        "news_titles": [i.get("title") for i in ((news.get("value") or {}).get("items") or [])[:5]] if news.get("status") == "available" else [],
        "jobs_count": len(((jobs.get("value") or {}).get("jobs") or [])) if jobs.get("status") == "available" else 0,
        "news_status": news.get("status"),
        "jobs_status": jobs.get("status"),
        "announcements_status": (evidence.get("brreg_announcements") or {}).get("status"),
        "financials_status": (evidence.get("financials") or {}).get("status"),
        "run_metrics": profile.get("run_metrics"),
        "sources": {
            key: {"status": (evidence.get(key) or {}).get("status"), "source_url": (evidence.get(key) or {}).get("source_url")}
            for key in ("registry_live", "website", "website_discovery", "news", "nav_jobs", "brreg_announcements", "financials")
            if evidence.get(key)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Organisation number or company name")
    parser.add_argument("--full", action="store_true", help="Print full enriched profile")
    args = parser.parse_args()
    resolved = resolve_company(args.query)
    if resolved.get("status") != "resolved" or not resolved.get("profile"):
        print(json.dumps(resolved, ensure_ascii=False, indent=2))
        raise SystemExit(1 if resolved.get("status") != "ambiguous" else 2)
    profile, _ = enrich_profile(resolved["profile"])
    if args.full:
        print(json.dumps(profile, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(summarize(profile), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
