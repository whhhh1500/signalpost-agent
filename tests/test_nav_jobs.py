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
