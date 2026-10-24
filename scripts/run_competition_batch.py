#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from norway_company_agent.site_discovery import discover_website  # noqa: E402
from norway_company_agent.nav_jobs import fetch_nav_jobs  # noqa: E402
from norway_company_agent.brreg_announcements import fetch_brreg_announcements  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--discover-websites", action=argparse.BooleanOptionalAction, default=True, help="Find official sites for companies with no registry homepage")
    parser.add_argument("--nav-jobs", action=argparse.BooleanOptionalAction, default=True, help="Match NAV Arbeidsplassen jobs by employer orgnr (fail-soft)")
    parser.add_argument("--brreg-announcements", action=argparse.BooleanOptionalAction, default=True, help="Fetch Brreg kunngjøringer HTML list by orgnr (fail-soft)")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,nav_jobs,brreg_announcements")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "nav_jobs", "brreg_announcements", "website_discovery"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    def enrich(profile: dict) -> tuple[dict, dict]:
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules, expected_latest_year=profile.get("latest_submitted_accounts"))
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            discovery_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
            if not str(profile.get("website") or "").strip() and args.discover_websites:
                found, discovery_record, discovery_metrics = discover_website(profile)
                profile["evidence"]["website_discovery"] = discovery_record
                if found:
                    profile["website"] = found
                    profile["website_origin"] = "discovered"
            website_record, website_metrics = fetch_website(profile.get("website"))
            if profile.get("website_origin") == "discovered":
                website_record["source_type"] = website_record["source_class"] = "discovered_company_website"
            for key in ("requests", "bytes"):
                website_metrics[key] += discovery_metrics[key]
            website_metrics["latencies_ms"] = website_metrics["latencies_ms"] + discovery_metrics["latencies_ms"]
            profile["evidence"]["website"] = apply_website_identity_gate(profile, website_record)["website"]
        extra_requests = website_metrics["requests"]
        extra_bytes = website_metrics["bytes"]
        extra_latencies = list(website_metrics["latencies_ms"])
        if "nav_jobs" in requested_modules and args.nav_jobs:
            nav_record, nav_metrics = fetch_nav_jobs(profile)
            profile["evidence"]["nav_jobs"] = nav_record
            extra_requests += nav_metrics["requests"]
            extra_bytes += nav_metrics["bytes"]
            extra_latencies.extend(nav_metrics["latencies_ms"])
        elif "nav_jobs" in requested_modules:
            from norway_company_agent.evidence import evidence as _ev
            profile["evidence"]["nav_jobs"] = _ev("nav_jobs", "not_applicable", "nav_arbeidsplassen_jobs", "https://arbeidsplassen.nav.no/stillinger/api/search", note="disabled by --no-nav-jobs")
        if "brreg_announcements" in requested_modules and args.brreg_announcements:
            ann_record, ann_metrics = fetch_brreg_announcements(profile)
            profile["evidence"]["brreg_announcements"] = ann_record
            extra_requests += ann_metrics["requests"]
            extra_bytes += ann_metrics["bytes"]
            extra_latencies.extend(ann_metrics["latencies_ms"])
        elif "brreg_announcements" in requested_modules:
            from norway_company_agent.evidence import evidence as _ev
            profile["evidence"]["brreg_announcements"] = _ev("brreg_announcements", "not_applicable", "brreg_kunngjoringer", "https://w2.brreg.no/kunngjoring/hent_nr.jsp", note="disabled by --no-brreg-announcements")
        metric = {
            "requests": len(metrics) + extra_requests,
            "bytes": sum(item.bytes_received for item in metrics) + extra_bytes,
            "latencies_ms": [item.elapsed_ms for item in metrics] + extra_latencies,
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
