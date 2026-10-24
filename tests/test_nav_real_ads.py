"""Regression on 105 real NAV ads (45 Norwegian employers with live ads, captured 2026-10-24).

Every ad's employer orgnr was checked against Brreg (the legal entity itself or an
underenhet whose overordnetEnhet is the company). Expected outcomes were reviewed by hand:
published = one current vacancy of this company; rejected = multi-position, recruitment
pool, or deadline passed.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from norway_company_agent.nav_jobs import jobs_from_entry, search_queries

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nav_real_ads_2026-10-24.json").read_text())
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _run(ad):
    emp = re.sub(r"\D", "", str(ad["entry"]["ad_content"]["employer"]["orgnr"]))
    subunits = {emp} if emp != ad["company_orgnr"] else set()
    rejected = []
    jobs = jobs_from_entry(ad["entry"], ad["company_orgnr"], subunits, now=NOW, rejected=rejected)
    return jobs, rejected


@pytest.mark.parametrize("ad", FIXTURE["ads"], ids=lambda a: a["entry"]["ad_content"]["uuid"][:8])
def test_real_ad_outcome(ad):
    jobs, rejected = _run(ad)
    if ad["expected"] == "published":
        assert len(jobs) == 1 and not rejected
        assert jobs[0]["link"].startswith("https://arbeidsplassen.nav.no/stillinger/stilling/")
        assert "\ufeff" not in jobs[0]["title"]
    else:
        assert jobs == [] and rejected


def test_real_ads_are_never_attributed_to_another_company():
    for ad in FIXTURE["ads"]:
        assert jobs_from_entry(ad["entry"], "999999999", set(), now=NOW) == []


def test_fixture_mix():
    kinds = [a["expected"] for a in FIXTURE["ads"]]
    assert kinds.count("published") >= 50 and kinds.count("rejected") >= 30
    assert len({a["company_orgnr"] for a in FIXTURE["ads"]}) >= 35


def test_search_queries_drop_legal_suffix_and_unit_names():
    assert search_queries("SKEIDAR SKIEN AS") == ["skeidar skien", "skeidar"]
    assert search_queries("TIME KOMMUNE TENESTEOMRÅDE OMSORG")[:2] == ["time kommune tenesteområde omsorg", "time kommune"]
    assert search_queries("FRYD KAFÉ & INTERIØR AS")[0] == "fryd kafé interiør"
    assert search_queries("HOTEL WITH AS") == ["hotel with"]
