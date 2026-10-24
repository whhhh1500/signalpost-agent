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
                "positioncount": 1,
            "published": "2026-10-17T00:00:00+02:00",
            "expires": "2099-01-01T00:00:00+01:00",
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
    return {"status": "ACTIVE", "ad_content": {"uuid": "u1", "title": "Kokk", "expires": "2099-01-01", "positioncount": 1, "employer": {"name": "X", "orgnr": orgnr}}}


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


def _own(**extra):
    content = {"uuid": "u2", "title": "Kokk", "expires": "2099-01-01", "positioncount": 1, "employer": {"name": "X", "orgnr": "985589003"}}
    content.update(extra)
    return {"status": "ACTIVE", "ad_content": content}


def test_expired_or_undated_ads_are_rejected():
    rejected = []
    assert jobs_from_entry(_own(expires="2020-01-01"), "985589003", rejected=rejected) == []
    assert "expired" in rejected[0]["reason"]
    assert jobs_from_entry(_own(expires=None), "985589003") == []
    assert jobs_from_entry(_own(applicationDue="2020-01-01"), "985589003") == []


def test_multi_position_and_aggregate_ads_are_rejected():
    assert jobs_from_entry(_own(positioncount="3"), "985589003") == []
    assert jobs_from_entry(_own(title="Flere ledige stillinger i butikk"), "985589003") == []
    assert jobs_from_entry(_own(title="Åpen søknad"), "985589003") == []
    ok = jobs_from_entry(_own(positioncount="1", link="https://example.no/jobs"), "985589003")
    assert ok and ok[0]["link"] == "https://arbeidsplassen.nav.no/stillinger/stilling/u2"


def test_headcount_and_pool_titles_are_rejected_but_single_roles_pass():
    for bad in ["2 faste 100%-stillinger som fagarbeider", "Træleborg omsorgsbolig søker tilkallingsvikarer!",
                "Tilkallingsvikarar/helgestillingar ved sjukeheimane", "Nattevakter – 3 midlertidige stillinger"]:
        assert jobs_from_entry(_own(title=bad), "985589003") == [], bad
    for ok in ["Servicetekniker / montør – industriporter og automatikk", "Tilkallingsvikar til Musti Molde!",
               "Sykepleier / Vernepleier", "Renholder - 60% fast stilling"]:
        assert jobs_from_entry(_own(title=ok), "985589003"), ok
