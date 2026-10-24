"""Regression tests for the second Codex review of 8c5044b (2026-10-24): every test uses the
reviewer's concrete counterexample, plus a 'still accepted' twin to guard against over-blocking."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from norway_company_agent import nav_jobs
from norway_company_agent.batch import profile_complete_for_modules, time_sensitive_staleness
from norway_company_agent.evidence import evidence
from norway_company_agent.http import FetchResult
from norway_company_agent.identity import (
    GATE_VERSION,
    apply_website_identity_gate,
    assess_social_identity,
    assess_website_identity,
)
from norway_company_agent.official import normalize_financials
from norway_company_agent.research import answer_profile, screen_profiles

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_prototype", ROOT / "scripts" / "build_prototype.py")
build_prototype = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_prototype)

BRAND = {"organisation_number": "923609016", "name": "BRANDMAKER AS", "municipality": "OSLO"}
NB_ORG = "915637353"


def _site(name, org, **value):
    base = {"final_url": "https://example.no/", "requested_url": "https://example.no/",
            "main_text_excerpt": "Vi bygger hus og hytter for private og bedrifter i hele regionen. " * 3}
    base.update(value)
    return {"name": name, "organisation_number": org, "legal_form": "AS",
            "evidence": {"website": evidence("website", "available", "company_site", "https://example.no/", value=base)}}


def _row(name="Nordic Bygg AS", org=NB_ORG, website=None, financials=None):
    return {"organisation_number": org, "name": name, "legal_form": "AS", "employees": 5, "municipality": "OSLO",
            "industry_code": "41.200", "industry_label": "Bygg", "website": "https://example.no", "bankrupt": False,
            "liquidating": False, "evidence": {k: v for k, v in (("website", website), ("financials", financials)) if v}}


def _rec(kind, start, end, revenue, currency="NOK", **extra):
    return {"regnskapstype": kind, "valuta": currency, "regnskapsperiode": {"fraDato": start, "tilDato": end},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": revenue}}}, **extra}


# ---------------- P0-A display layer ----------------
def test_prototype_does_not_show_stale_record_when_latest_withheld():
    value = normalize_financials([_rec("SELSKAP", "2024-01-01", "2024-12-31", 100)], "2025")
    assert value["latest"] is None
    fin = evidence("financials", "available", "official_annual_accounts", "https://brreg", value=value)
    out = build_prototype.compact(_row(financials=fin))["financial"]
    assert out["records"] == []
    assert "100" not in json.dumps(out["records"])


def test_prototype_unknown_type_and_konsern_fallback_use_gate():
    unknown = normalize_financials([_rec(None, "2025-01-01", "2025-12-31", 100)], "2025")
    out = build_prototype.compact(_row(financials=evidence("financials", "available", "x", "u", value=unknown)))["financial"]
    assert out["records"] == []
    group = normalize_financials([_rec("KONSERN", "2025-01-01", "2025-12-31", 100)], "2025")
    out = build_prototype.compact(_row(financials=evidence("financials", "available", "x", "u", value=group)))["financial"]
    assert out["records"][0]["revenue"] == 100 and out["basis"] == "group_konsern"


def test_prototype_legacy_financials_without_gate_fail_closed():
    legacy = evidence("financials", "available", "x", "u", value={"records": [{"revenue": 100}]})
    assert build_prototype.compact(_row(financials=legacy))["financial"]["records"] == []


def test_prototype_unassessed_website_withholds_description_and_socials():
    web = evidence("website", "available", "company_site", "https://example.no", value={
        "title": "Brandmaker", "description": "Wrong company",
        "social_links": [{"platform": "instagram", "url": "https://instagram.com/brandmakerfan"}]})
    out = build_prototype.compact(_row(name="Brandmaker AS", website=web))["web"]["value"]
    assert out["description"] != "Wrong company"
    assert out["social_links"] == []
    assert "brandmakerfan" not in json.dumps(out)


def test_prototype_still_shows_gated_website():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg", identity_text_excerpt="Org.nr 915 637 353",
              social_links=[{"platform": "facebook", "url": "https://facebook.com/nordicbygg"}])
    gated = apply_website_identity_gate(p, p["evidence"]["website"])["website"]
    out = build_prototype.compact(_row(website=gated))["web"]["value"]
    assert out["title"] == "Nordic Bygg" and out["social_links"] == [{"platform": "facebook", "url": "https://facebook.com/nordicbygg"}]


# ---------------- P0-B website ownership ----------------
def test_parent_page_listing_subsidiary_orgnr_is_not_its_site():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Gruppen", description="Våre datterselskaper: Nordic Bygg AS org.nr 915637353")
    a = assess_website_identity(p)
    assert not a["publishable"], a
    gated = apply_website_identity_gate(p, p["evidence"]["website"])["website"]
    row = _row(website=gated)
    assert screen_profiles([row], "companies with an official website")["result_count"] == 0


def test_extended_legal_name_without_orgnr_is_not_ours():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg Service AS")
    a = assess_website_identity(p)
    assert not a["publishable"], a
    gated = apply_website_identity_gate(p, p["evidence"]["website"])["website"]
    assert screen_profiles([_row(website=gated)], "companies with an official website")["result_count"] == 0


def test_plain_name_sites_still_pass():
    assert assess_website_identity(_site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg AS | Tømrer i Oslo"))["publishable"]
    assert assess_website_identity(_site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg - Hjem"))["publishable"]
    assert assess_website_identity(_site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg",
                                         identity_text_excerpt="Nordic Bygg AS | Org.nr 915 637 353"))["publishable"]


# ---------------- P0-C social handle impostors ----------------
def test_handle_words_are_not_dropped_before_comparison():
    for bad in ("the.brandmaker", "brandmaker.and", "brandmaker.x", "brandmaker.1", "brandmaker_the", "a.brandmaker"):
        r = assess_social_identity(BRAND, {"platform": "instagram", "url": f"https://instagram.com/{bad}"})
        assert not r["publishable"], bad
    for good in ("brandmaker", "brandmaker.as", "brandmaker_no", "brandmaker.oslo", "brandmakernorge"):
        assert assess_social_identity(BRAND, {"platform": "instagram", "url": f"https://instagram.com/{good}"})["publishable"], good


def test_social_impostors_not_in_answer_profile():
    p = _site("BRANDMAKER AS", "923609016", title="Brandmaker", identity_text_excerpt="Org.nr 923 609 016",
              social_links=[{"platform": "instagram", "url": f"https://instagram.com/{h}"} for h in ("the.brandmaker", "brandmaker.and", "brandmaker.x", "brandmaker.1")])
    gated = apply_website_identity_gate(p, p["evidence"]["website"])["website"]
    answer = answer_profile({"organisation_number": "923609016", "name": "BRANDMAKER AS", "evidence": {"website": gated}}, "social")
    assert "instagram.com" not in json.dumps(answer)


def test_inner_connector_words_in_legal_name_still_match():
    prof = {"name": "Hansen og Sønn AS"}
    assert assess_social_identity(prof, {"platform": "facebook", "url": "https://facebook.com/hansenogsonn"})["publishable"]
    assert assess_social_identity(prof, {"platform": "facebook", "url": "https://facebook.com/hansensonn"})["publishable"]


# ---------------- P1-D over-blocking ----------------
def test_holding_word_in_description_is_not_group_conflict():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg", identity_text_excerpt="Org.nr 915 637 353",
              description="Vi leverer for Holding og Eiendom kunder.")
    assert assess_website_identity(p)["publishable"]


def test_webdesign_copyright_line_is_not_operator():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg",
              identity_text_excerpt="Org.nr 915 637 353 Webdesign © 2026 Pixel Studio AS")
    assert assess_website_identity(p)["publishable"]
    p2 = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg", identity_text_excerpt="Org.nr 915 637 353. Utviklet av: © Pixel Studio AS")
    assert assess_website_identity(p2)["publishable"]


def test_real_parent_copyright_still_blocks():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg", identity_text_excerpt="© 2026 Nordic Bygg Holding AS")
    assert not assess_website_identity(p)["publishable"]


def test_holding_attached_to_name_still_blocks():
    p = _site("Nordic Bygg AS", NB_ORG, title="Nordic Bygg Holding")
    assert not assess_website_identity(p)["publishable"]


# ---------------- P1-E rejected candidates do not veto ----------------
def test_rejected_fan_account_does_not_veto_official_account():
    p = _site("BRANDMAKER AS", "923609016", title="Brandmaker", identity_text_excerpt="Org.nr 923 609 016",
              social_links=[{"platform": "instagram", "url": "https://instagram.com/brandmaker"},
                            {"platform": "instagram", "url": "https://instagram.com/brandmakerfan"}])
    out = apply_website_identity_gate(p, p["evidence"]["website"])["website"]["value"]["social_links"]
    assert out == [{"platform": "instagram", "url": "https://instagram.com/brandmaker"}]


# ---------------- P1-F resume re-checks time-sensitive fields ----------------
def test_resume_refetches_expired_nav_ad():
    now = datetime.now(timezone.utc)
    nav = evidence("nav_jobs", "available", "nav", "u", value={"jobs": [{"uuid": "a", "expires": "2026-10-23T01:00:00Z"}]},
                   retrieved_at=now.isoformat())
    profile = {"gate_version": GATE_VERSION, "evidence": {"registry": {"status": "available"}, "nav_jobs": nav}}
    assert not profile_complete_for_modules(profile, ["registry", "nav_jobs"])
    fresh = dict(nav, value={"jobs": [{"uuid": "a", "expires": (now + timedelta(days=10)).isoformat()}]})
    profile["evidence"]["nav_jobs"] = fresh
    assert profile_complete_for_modules(profile, ["registry", "nav_jobs"])
    old = dict(fresh, retrieved_at=(now - timedelta(days=3)).isoformat())
    profile["evidence"]["nav_jobs"] = old
    assert time_sensitive_staleness(profile, ["nav_jobs"])


def test_resume_refetches_financials_behind_registry_year():
    value = normalize_financials([_rec("SELSKAP", "2024-01-01", "2024-12-31", 100)], None)
    profile = {"gate_version": GATE_VERSION, "latest_submitted_accounts": "2025",
               "evidence": {"financials": evidence("financials", "available", "x", "u", value=value)}}
    assert not profile_complete_for_modules(profile, ["financials"])
    profile["latest_submitted_accounts"] = "2024"
    assert profile_complete_for_modules(profile, ["financials"])


# ---------------- P1-G incomplete NAV search is not 0 ----------------
def _hit(uuid, employer):
    return {"uuid": uuid, "employer_name": employer, "business_name": employer}


def test_nav_fallback_503_is_unknown_not_zero():
    profile = {"organisation_number": "985589003", "name": "Brandmaker Oslo AS"}
    ok = FetchResult(url=nav_jobs.SEARCH_URL, status=200, elapsed_ms=1, bytes_received=1, body={})
    bad = FetchResult(url=nav_jobs.SEARCH_URL, status=503, elapsed_ms=1, bytes_received=1, error="HTTP 503")
    results = iter([([], ok), ([], bad)])
    with mock.patch.object(nav_jobs, "get_public_token", return_value="t"), \
         mock.patch.object(nav_jobs, "search_queries", return_value=["Brandmaker Oslo AS", "Brandmaker Oslo"]), \
         mock.patch.object(nav_jobs, "search_job_candidates", side_effect=lambda q, **_: next(results)):
        record, _ = nav_jobs.fetch_nav_jobs(profile)
    assert record["status"] != "not_found"
    assert record["value"]["job_count"] is None and record["value"]["count_complete"] is False


def test_nav_detail_budget_exhausted_is_unknown_not_zero():
    profile = {"organisation_number": "985589003", "name": "Brandmaker AS"}
    ok = FetchResult(url=nav_jobs.SEARCH_URL, status=200, elapsed_ms=1, bytes_received=1, body={})
    hits = [_hit(f"other{i}", "Brandmaker AS") for i in range(15)] + [_hit("ours", "Brandmaker AS")]

    def entry(uuid, token):
        orgnr = "985589003" if uuid == "ours" else "111111111"
        return FetchResult(url=uuid, status=200, elapsed_ms=1, bytes_received=1, body={"status": "ACTIVE", "ad_content": {
            "uuid": uuid, "title": "Kokk", "positioncount": 1, "expires": "2099-01-01", "employer": {"orgnr": orgnr}}})

    with mock.patch.object(nav_jobs, "get_public_token", return_value="t"), \
         mock.patch.object(nav_jobs, "search_queries", return_value=["Brandmaker"]), \
         mock.patch.object(nav_jobs, "search_job_candidates", return_value=(hits, ok)), \
         mock.patch.object(nav_jobs, "fetch_feed_entry", side_effect=entry):
        record, _ = nav_jobs.fetch_nav_jobs(profile)
    assert record["status"] != "not_found"
    assert record["value"]["job_count"] is None and record["value"]["unchecked_candidates"] == 1


def test_nav_complete_search_still_reports_zero():
    profile = {"organisation_number": "985589003", "name": "Brandmaker AS"}
    ok = FetchResult(url=nav_jobs.SEARCH_URL, status=200, elapsed_ms=1, bytes_received=1, body={})
    with mock.patch.object(nav_jobs, "get_public_token", return_value="t"), \
         mock.patch.object(nav_jobs, "search_job_candidates", return_value=([], ok)):
        record, _ = nav_jobs.fetch_nav_jobs(profile)
    assert record["status"] == "not_found" and record["value"]["job_count"] == 0


# ---------------- P2-H same-period conflict ----------------
def test_same_period_conflict_without_version_signal_is_withheld_in_both_orders():
    a = _rec("SELSKAP", "2025-01-01", "2025-12-31", 100)
    b = _rec("SELSKAP", "2025-01-01", "2025-12-31", 120)
    for order in ([a, b], [b, a]):
        out = normalize_financials(order, "2025")
        assert out["latest"] is None and "conflict" in out["latest_withheld_reason"]
        assert out["superseded_record_count"] == 0


def test_same_period_versions_with_journalnr_still_resolve():
    a = _rec("SELSKAP", "2025-01-01", "2025-12-31", 100, journalnr="2026001")
    b = _rec("SELSKAP", "2025-01-01", "2025-12-31", 120, journalnr="2026050")
    assert normalize_financials([a, b], "2025")["latest"]["revenue"] == 120
    same = _rec("SELSKAP", "2025-01-01", "2025-12-31", 100)
    assert normalize_financials([same, dict(same)], "2025")["latest"]["revenue"] == 100  # identical duplicates


# ---------------- P2-I leap-year February-end fiscal years ----------------
def test_february_end_fiscal_years_compare_across_leap_day():
    out = normalize_financials([_rec("SELSKAP", "2024-03-01", "2025-02-28", 120), _rec("SELSKAP", "2023-03-01", "2024-02-29", 100)], "2025")
    comp = out["comparison"]
    assert comp is not None and comp["period_months"] == 12
    assert comp["latest_period_days"] == 365 and comp["previous_period_days"] == 366 and comp["leap_day_difference"]


# ---------------- P2-J standalone normalizer uses the same gate ----------------
def test_normalize_social_links_script_uses_publication_gate(tmp_path):
    unassessed = _row(name="BRANDMAKER AS", org="923609016", website=evidence("website", "available", "company_site", "https://x.no", value={
        "title": "Other", "social_links": [{"platform": "instagram", "url": "https://instagram.com/brandmaker"}]}))
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps(unassessed) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "normalize_social_links.py"), "--input", str(path)], check=True, capture_output=True)
    out = json.loads(path.read_text(encoding="utf-8"))
    value = out["evidence"]["website"]["value"]
    assert value["identity_gate_version"] == GATE_VERSION
    assert value["social_links"] == []  # site identity not established -> nothing published
