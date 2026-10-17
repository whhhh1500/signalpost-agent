from norway_company_agent.site_discovery import candidate_slugs, name_and_address_match, orgnr_pattern, shows_other_orgnr


def test_slugs_transliterate_and_drop_suffix():
    slugs = candidate_slugs("ARKITEKTFIRMA JON VIKØREN AS")
    assert slugs[0] == "arkitektfirmajonvikoren"
    assert "arkitektfirma-jon-vikoren" in slugs
    assert all(" " not in s for s in slugs)


def test_orgnr_pattern_accepts_spaced_and_dotted_forms():
    pat = orgnr_pattern("985589003")
    assert pat.search("Org.nr: 985 589 003")
    assert pat.search("orgnr 985.589.003 MVA")
    assert not pat.search("1985589003")


def test_sister_company_guard_rejects_other_orgnr():
    html = "<p>Skya Regnskap AS, Org.nr 918 097 902, Storgata 1, 0155 Oslo</p>"
    assert shows_other_orgnr(html, "999999999")
    assert not name_and_address_match(html, "SKYA AS", "0155", ["storgata 1"], "999999999")


def test_name_and_address_match_requires_both():
    html = "<h1>Brandmaker</h1><p>Rådhusgaten 2, 5014 Bergen</p>"
    assert name_and_address_match(html, "BRANDMAKER AS", "5014", [], "123456789")
    assert not name_and_address_match(html, "BRANDMAKER AS", "0150", ["storgata 9"], "123456789")
