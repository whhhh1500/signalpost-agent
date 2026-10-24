from norway_company_agent.site_discovery import (
    domain_is_full_name, legal_name_on_page, names_other_legal_entity, side_evidence_match,
)

OWN = "<html><body><h1>Webdesign Bergen</h1><p>Kontakt Brandmaker AS for logo og nettsider.</p></body></html>"


def test_brandmaker_like_site_passes_on_domain_plus_exact_legal_name():
    assert side_evidence_match(OWN, "brandmaker.no", "BRANDMAKER AS", "982942942", None)


def test_registry_email_domain_is_enough_with_full_name_domain():
    page = "<p>Webdesign i Bergen</p>"
    assert side_evidence_match(page, "brandmaker.no", "BRANDMAKER AS", "982942942", "post@brandmaker.no")
    assert not side_evidence_match(page, "brandmaker.no", "BRANDMAKER AS", "982942942", "post@gmail.com")
    assert not side_evidence_match(page, "brandmaker.no", "BRANDMAKER AS", "982942942", None)


def test_parent_or_brand_site_is_rejected():
    parent = OWN + "<footer>© 2026 Brandmaker Group AS</footer>"
    assert names_other_legal_entity(parent, "BRANDMAKER AS")
    assert not side_evidence_match(parent, "brandmaker.no", "BRANDMAKER AS", "982942942", None)
    jsonld = OWN + '<script type="application/ld+json">{"legalName": "Nordic Brands Holding AS"}</script>'
    assert not side_evidence_match(jsonld, "brandmaker.no", "BRANDMAKER AS", "982942942", None)
    other_org = OWN + "<p>Org.nr: 912 345 678</p>"
    assert not side_evidence_match(other_org, "brandmaker.no", "BRANDMAKER AS", "982942942", None)


def test_domain_must_be_the_full_name_on_no():
    assert domain_is_full_name("brandmaker.no", "BRANDMAKER AS")
    assert domain_is_full_name("nordic-bygg.no", "NORDIC BYGG AS")
    assert not domain_is_full_name("nordic.no", "NORDIC BYGG AS")      # token dropped: parent/brand risk
    assert not domain_is_full_name("brandmaker.com", "BRANDMAKER AS")  # foreign namesakes
    assert not domain_is_full_name("abc.no", "ABC AS")                 # too short to be distinctive


def test_legal_name_requires_suffix_and_word_boundary():
    assert not legal_name_on_page("<p>Brandmaker hjelper deg</p>", "BRANDMAKER AS")
    assert not legal_name_on_page("<p>Superbrandmaker AS</p>", "BRANDMAKER AS")
    assert not legal_name_on_page("<p>Brandmaker ASA</p>", "BRANDMAKER AS")
    assert legal_name_on_page("<p>BRANDMAKER&nbsp;AS</p>", "BRANDMAKER AS")


def test_own_copyright_line_is_not_a_foreign_entity():
    assert not names_other_legal_entity(OWN + "<footer>© 2024 Brandmaker AS</footer>", "BRANDMAKER AS")
