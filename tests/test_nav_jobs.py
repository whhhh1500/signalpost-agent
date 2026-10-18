from norway_company_agent.nav_jobs import jobs_from_entry, names_compatible, normalize_name


def test_normalize_strips_suffix_and_case():
    assert normalize_name("Que Pasa AS") == "que pasa"
    assert normalize_name("EQUINOR ASA") == "equinor"


def test_names_compatible_accepts_subset_tokens():
    assert names_compatible("Que Pasa AS", "Que Pasa Hamar As")
    assert names_compatible("BRANDMAKER AS", "Brandmaker")
    assert not names_compatible("ALPHA AS", "BETA AS")


def test_jobs_from_entry_requires_exact_orgnr_and_active():
    body = {
        "status": "ACTIVE",
        "ad_content": {
            "uuid": "abc",
            "title": "Kokk",
            "published": "2026-10-17T00:00:00+02:00",
            "link": "https://arbeidsplassen.nav.no/stillinger/stilling/abc",
            "employer": {"name": "Que Pasa AS", "orgnr": "927212021"},
            "workLocations": [{"municipal": "HAMAR"}],
        },
    }
    jobs = jobs_from_entry(body, "927212021")
    assert len(jobs) == 1
    assert jobs[0]["employer_orgnr"] == "927212021"
    assert jobs_from_entry(body, "999999999") == []
    inactive = dict(body, status="INACTIVE")
    assert jobs_from_entry(inactive, "927212021") == []


def _entry(orgnr):
    return {"status": "ACTIVE", "ad_content": {"uuid": "u1", "title": "Kokk", "employer": {"name": "X", "orgnr": orgnr}}}


def test_subunit_orgnr_is_accepted_and_labelled():
    jobs = jobs_from_entry(_entry("973246534"), "985589003", {"973246534"})
    assert len(jobs) == 1
    assert jobs[0]["employer_relation"] == "registered_subunit"


def test_unrelated_orgnr_is_rejected_even_with_subunits():
    assert jobs_from_entry(_entry("999999999"), "985589003", {"973246534"}) == []
    assert jobs_from_entry(_entry("985589003"), "985589003")[0]["employer_relation"] == "legal_entity"


def test_registered_subunits_reads_locations_evidence():
    from norway_company_agent.nav_jobs import registered_subunits

    profile = {"evidence": {"locations": {"status": "available", "value": {"locations": [{"organisation_number": "973246534"}, {"organisation_number": None}]}}}}
    assert registered_subunits(profile) == {"973246534"}
    assert registered_subunits({"evidence": {}}) == set()
