"""Regression tests for the Codex audit (2026-10-24), one block per audit item,
using the auditor's counterexamples."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from norway_company_agent import nav_jobs
from norway_company_agent.batch import profile_complete_for_modules
from norway_company_agent.evidence import evidence
from norway_company_agent.http import FetchResult
from norway_company_agent.identity import (
    GATE_VERSION,
    apply_website_identity_gate,
    assess_social_identity,
    assess_website_identity,
    website_identity_publishable,
)
from norway_company_agent.official import _period_months, fetch_official_modules, normalize_financials
from norway_company_agent.research import answer_profile
from norway_company_agent.website import _social_links, dedupe_social_links, normalize_social_url, structured_social_links

BRAND = {"organisation_number": "923609016", "name": "BRANDMAKER AS", "municipality": "OSLO"}


# 1. prefix/suffix handle impostors
def test_social_handle_prefix_and_non_neutral_suffix_rejected():
    for bad in ("notbrandmaker", "brandmakerfake", "brandmakerfan", "ibrandmaker"):
        result = assess_social_identity(BRAND, {"platform": "instagram", "url": f"https://instagram.com/{bad}"})
        assert not result["publishable"], bad
    for ok in ("brandmaker", "brandmakeras", "brandmaker_no", "brandmakeroslo", "brandmaker.official"):
        result = assess_social_identity(BRAND, {"platform": "instagram", "url": f"https://instagram.com/{ok}"})
        assert result["publishable"], ok


# 2. content paths are not accounts
def test_content_paths_are_not_accounts():
    for url in ("https://facebook.com/someone/posts/brandmaker", "https://instagram.com/accounts/brandmaker",
                "https://tiktok.com/@someone/video/brandmaker", "https://instagram.com/p/brandmaker",
                "https://x.com/someone/status/1", "https://facebook.com/someone/photos/brandmaker"):
        assert normalize_social_url(url) is None, url
        assert not assess_social_identity(BRAND, {"platform": "facebook", "url": url})["publishable"], url
    assert normalize_social_url("https://www.tiktok.com/@brandmaker") == {"platform": "tiktok", "url": "https://tiktok.com/@brandmaker"}


# 3. parent page listing a subsidiary orgnr; holding company impersonation
def _site(value):
    return {"organisation_number": "915637353", "name": "NORDIC BYGG AS",
            "evidence": {"website": evidence("website", "available", "company_site", "https://nordicbygg.no", value=value)}}


def test_parent_page_listing_several_labelled_orgnrs_is_not_full_identity():
    profile = _site({"final_url": "https://nordicbygg.no/", "title": "Nordic Bygg",
                     "identity_text_excerpt": "Nordic Bygg Holding AS org.nr 999888777. Datterselskaper: Nordic Bygg AS org.nr 915 637 353"})
    assessment = assess_website_identity(profile)
    assert assessment["score"] < 1.0
    assert not assessment["publishable"]


def test_holding_company_legal_name_does_not_pass_as_operating_company():
    profile = _site({"final_url": "https://nordicbygg.no/", "title": "Nordic Bygg",
                     "structured_organisations": [{"@type": "Organization", "legalName": "Nordic Bygg Holding AS"}],
                     "identity_text_excerpt": "© 2026 Nordic Bygg Holding AS"})
    assert not assess_website_identity(profile)["publishable"]


def test_own_sole_labelled_orgnr_still_publishes():
    profile = _site({"final_url": "https://nordicbygg.no/", "title": "Nordic Bygg",
                     "identity_text_excerpt": "© 2026 Nordic Bygg AS - Org.nr 915 637 353"})
    assessment = assess_website_identity(profile)
    assert assessment["publishable"] and assessment["score"] == 1.0


# 4. period months by real boundaries
def test_period_months_uses_real_boundaries_and_leap_years():
    assert _period_months({"fraDato": "2025-01-01", "tilDato": "2025-12-31"}) == 12
    assert _period_months({"fraDato": "2025-01-01", "tilDato": "2025-12-01"}) is None
    assert _period_months({"fraDato": "2024-01-01", "tilDato": "2024-12-31"}) == 12
    assert _period_months({"fraDato": "2024-07-01", "tilDato": "2025-06-30"}) == 12
    assert _period_months({"fraDato": "2025-04-01", "tilDato": "2025-12-31"}) == 9


def _rec(kind, start, end, revenue, currency="NOK", **extra):
    return {"regnskapstype": kind, "valuta": currency, "regnskapsperiode": {"fraDato": start, "tilDato": end},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": revenue}}}, **extra}


def test_comparison_requires_same_type_boundaries_and_present_currency():
    short = normalize_financials([_rec("SELSKAP", "2025-01-01", "2025-12-01", 2), _rec("SELSKAP", "2024-01-01", "2024-12-31", 1)], "2025")
    assert short["comparison"] is None
    no_cur = normalize_financials([_rec("SELSKAP", "2025-01-01", "2025-12-31", 2, None), _rec("SELSKAP", "2024-01-01", "2024-12-31", 1, None)], "2025")
    assert no_cur["comparison"] is None and "currency" in no_cur["comparison_note"]
    leap = normalize_financials([_rec("SELSKAP", "2025-01-01", "2025-12-31", 2), _rec("SELSKAP", "2024-01-01", "2024-12-31", 1)], "2025")
    assert leap["comparison"]["leap_day_difference"] is True


# 5. version arbitration, own vs unknown vs group
def test_correction_wins_deterministically_regardless_of_order():
    original = _rec("SELSKAP", "2025-01-01", "2025-12-31", 100, id=10, journalnr="2026000100")
    correction = _rec("SELSKAP", "2025-01-01", "2025-12-31", 120, id=11, journalnr="2026000200")
    for body in ([original, correction], [correction, original]):
        value = normalize_financials(body, "2025")
        assert value["latest"]["revenue"] == 120
        assert value["superseded_record_count"] == 1


def test_unknown_type_is_not_own_and_group_fallback_is_labelled():
    unknown = normalize_financials([_rec(None, "2025-01-01", "2025-12-31", 5)], "2025")
    assert unknown["latest"] is None
    group = normalize_financials([_rec("KONSERN", "2025-01-01", "2025-12-31", 7)], "2025")
    assert group["latest"]["basis"] == "group_konsern" and group["basis"] == "group_konsern"
    profile = {"organisation_number": "923609016", "name": "X AS",
               "evidence": {"financials": evidence("financials", "available", "official_annual_accounts", "https://x", value=group, content_sha256="a" * 64)}}
    answer = answer_profile(profile, "revenue")
    labels = [fact["label"] if isinstance(fact, dict) and "label" in fact else str(fact) for fact in answer.get("facts", answer.get("claims", []))]
    assert any("KONSERN" in str(label) for label in labels)


# 6. NAV timestamps / published / positioncount
def _ad(**extra):
    content = {"uuid": "u", "title": "Kokk", "positioncount": 1, "expires": "2026-10-24",
               "employer": {"orgnr": "985589003"}}
    content.update(extra)
    return content


def test_nav_deadline_compares_full_timestamps_in_oslo():
    noon_oslo = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
    assert nav_jobs.single_current_ad_check(_ad(expires="2026-10-24"), noon_oslo) is None  # end of day Oslo
    assert nav_jobs.single_current_ad_check(_ad(expires="2026-10-24T09:00:00+02:00"), noon_oslo) is not None
    late = datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc)  # 00:30 25 Oct in Oslo
    assert nav_jobs.single_current_ad_check(_ad(expires="2026-10-24"), late) is not None


def test_nav_future_published_and_missing_positioncount_rejected():
    now = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
    assert "future" in nav_jobs.single_current_ad_check(_ad(expires="2099-01-01", published="2026-11-01T00:00:00+01:00"), now)
    assert nav_jobs.single_current_ad_check(_ad(expires="2099-01-01", positioncount=None), now) is not None
    assert nav_jobs.single_current_ad_check(_ad(expires="2099-01-01", positioncount=0), now) is not None


# 7. resume and fail-closed publication
def test_resume_rejects_profiles_from_older_gate_version():
    old = {"gate_version": "signalpost-gates-v4", "evidence": {"registry": {"status": "available"}}}
    assert not profile_complete_for_modules(old, ["registry"])
    assert not profile_complete_for_modules({"evidence": {"registry": {"status": "available"}}}, ["registry"])
    assert profile_complete_for_modules(dict(old, gate_version=GATE_VERSION), ["registry"])


def test_missing_identity_assessment_fails_closed():
    record = evidence("website", "available", "company_site", "https://x.no", value={"description": "We are great", "social_links": [{"platform": "facebook", "url": "https://facebook.com/x"}]})
    assert not website_identity_publishable(record)
    old_version = dict(record, value=dict(record["value"], identity_assessment={"publishable": True}, identity_gate_version="old"))
    assert not website_identity_publishable(old_version)
    answer = answer_profile({"organisation_number": "1", "name": "X", "evidence": {"website": record}}, "social")
    assert "facebook.com/x" not in str(answer.get("facts", answer.get("claims", "")))


# 8. stale financials and live expected year
def test_stale_latest_is_withheld_and_live_year_overrides_bulk():
    body = [_rec("SELSKAP", "2024-01-01", "2024-12-31", 1)]
    assert normalize_financials(body, "2025")["latest"] is None

    def fetcher(url):
        if "regnskap" in url:
            return FetchResult(url=url, status=200, elapsed_ms=1, bytes_received=1, body=body)
        return FetchResult(url=url, status=200, elapsed_ms=1, bytes_received=1, body={"organisasjonsnummer": "923609016", "navn": "X AS", "sisteInnsendteAarsregnskap": "2025"})

    records, _ = fetch_official_modules("923609016", {"financials"}, fetcher=fetcher, expected_latest_year="2024")
    assert records["financials"]["value"]["latest"] is None
    assert "registry_live" not in records


# 9. identity excerpt / discovery evidence
def test_identity_excerpt_from_secondary_page_reaches_gate():
    profile = _site({"final_url": "https://nordicbygg.no/", "title": "Nordic Bygg",
                     "pages": [{"url": "https://nordicbygg.no/kontakt", "identity_text_excerpt": "Nordic Bygg AS, org.nr. 915 637 353", "origin": "website_discovery"}]})
    assert assess_website_identity(profile)["publishable"]


# 10. NAV verification failures are not "no jobs"; fallback continues past name-compatible
def _search_hit(uuid, employer):
    return {"uuid": uuid, "employer_name": employer, "business_name": employer}


def test_nav_all_detail_failures_are_source_error_not_zero():
    profile = {"organisation_number": "985589003", "name": "Brandmaker AS"}
    ok = FetchResult(url=nav_jobs.SEARCH_URL, status=200, elapsed_ms=1, bytes_received=1, body={})
    with mock.patch.object(nav_jobs, "get_public_token", return_value="t"), \
         mock.patch.object(nav_jobs, "search_job_candidates", return_value=([_search_hit("a", "Brandmaker AS")], ok)), \
         mock.patch.object(nav_jobs, "fetch_feed_entry", return_value=FetchResult(url="u", status=500, elapsed_ms=1, bytes_received=1, error="boom")):
        record, _ = nav_jobs.fetch_nav_jobs(profile)
    assert record["status"] == "source_error"
    assert record["value"]["job_count"] is None


def test_nav_fallback_continues_until_orgnr_verified():
    profile = {"organisation_number": "985589003", "name": "Brandmaker Oslo AS"}
    ok = FetchResult(url=nav_jobs.SEARCH_URL, status=200, elapsed_ms=1, bytes_received=1, body={})
    hits = {0: [_search_hit("wrong", "Brandmaker Oslo AS")], 1: [_search_hit("right", "Brandmaker Oslo AS")]}
    calls = {"n": 0}

    def search(query, **_):
        index = min(calls["n"], 1)
        calls["n"] += 1
        return hits[index], ok

    def entry(uuid, token):
        orgnr = "985589003" if uuid == "right" else "111111111"
        return FetchResult(url=uuid, status=200, elapsed_ms=1, bytes_received=1, body={"status": "ACTIVE", "ad_content": _ad(uuid=uuid, expires="2099-01-01", employer={"orgnr": orgnr})})

    with mock.patch.object(nav_jobs, "get_public_token", return_value="t"), \
         mock.patch.object(nav_jobs, "search_queries", return_value=["Brandmaker Oslo", "Brandmaker"]), \
         mock.patch.object(nav_jobs, "search_job_candidates", side_effect=search), \
         mock.patch.object(nav_jobs, "fetch_feed_entry", side_effect=entry):
        record, _ = nav_jobs.fetch_nav_jobs(profile)
    assert record["status"] == "available"
    assert [job["uuid"] for job in record["value"]["jobs"]] == ["right"]


# 11. dedupe by UUID only
def test_two_distinct_ads_with_same_title_place_deadline_both_kept():
    profile = {"organisation_number": "985589003", "name": "Brandmaker AS"}
    ok = FetchResult(url=nav_jobs.SEARCH_URL, status=200, elapsed_ms=1, bytes_received=1, body={})
    hits = [_search_hit("a", "Brandmaker AS"), _search_hit("b", "Brandmaker AS")]

    def entry(uuid, token):
        return FetchResult(url=uuid, status=200, elapsed_ms=1, bytes_received=1, body={"status": "ACTIVE", "ad_content": _ad(uuid=uuid, expires="2099-01-01")})

    with mock.patch.object(nav_jobs, "get_public_token", return_value="t"), \
         mock.patch.object(nav_jobs, "search_job_candidates", return_value=(hits, ok)), \
         mock.patch.object(nav_jobs, "fetch_feed_entry", side_effect=entry):
        record, _ = nav_jobs.fetch_nav_jobs(profile)
    assert record["value"]["job_count"] == 2


# 12. URL variants of one account are one account; JSON-LD sameAs is used
def test_url_variants_of_one_handle_are_one_account():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup('<a href="https://www.facebook.com/Brandmaker/">f</a><a href="https://m.facebook.com/brandmaker?ref=x">f</a>', "html.parser")
    links = _social_links("https://brandmaker.no/", soup)
    assert len(links) == 1
    assert len(dedupe_social_links(links + structured_social_links([{"sameAs": ["https://facebook.com/BRANDMAKER"]}]))) == 1
    site = evidence("website", "available", "company_site", "https://brandmaker.no", value={
        "final_url": "https://brandmaker.no/", "title": "Brandmaker", "identity_text_excerpt": "Brandmaker AS org.nr 923 609 016",
        "social_links": links + [{"platform": "facebook", "url": "https://facebook.com/BrandMaker/"}]})
    gated = apply_website_identity_gate(dict(BRAND, evidence={"website": site}), site)["website"]
    assert [item["platform"] for item in gated["value"]["social_links"]] == ["facebook"]
    assert gated["value"]["identity_gate_version"] == GATE_VERSION
