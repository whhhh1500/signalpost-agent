from norway_company_agent.identity import apply_website_identity_gate, assess_social_identity, assess_website_identity


def _profile(name="Vikøren Arkitekter AS", org="985589003", **value):
    base = {"final_url": "https://vikoren.no/", "requested_url": "https://vikoren.no/", "title": "Vikøren Arkitekter",
            "main_text_excerpt": "Vikøren Arkitekter tegner hus i Vik. " * 5}
    base.update(value)
    return {"name": name, "organisation_number": org, "evidence": {"website": {"status": "available", "value": base}}}


def test_own_site_with_own_orgnr_passes():
    p = _profile(identity_text_excerpt="Org.nr 985 589 003")
    assert assess_website_identity(p)["publishable"]


def test_parent_site_with_other_orgnr_is_rejected():
    p = _profile(identity_text_excerpt="Vikøren Arkitekter er en del av Storkonsern ASA, org.nr. 912 345 678")
    a = assess_website_identity(p)
    assert not a["publishable"] and a["foreign_entity_signals"]


def test_redirect_to_brand_domain_is_rejected():
    p = _profile(final_url="https://brandgroup.com/norge", requested_url="https://vikoren.no/")
    assert not assess_website_identity(p)["publishable"]


def test_structured_legal_name_of_other_entity_is_rejected():
    p = _profile(structured_organisations=[{"name": "Vikøren Arkitekter", "legalName": "Storkonsern ASA"}])
    assert not assess_website_identity(p)["publishable"]


def test_social_handle_rules():
    prof = {"name": "Vikøren Arkitekter AS"}
    assert assess_social_identity(prof, {"platform": "facebook", "url": "https://facebook.com/vikorenarkitekter"})["publishable"]
    assert not assess_social_identity(prof, {"platform": "facebook", "url": "https://facebook.com/vikorengruppen"})["publishable"]
    single = {"name": "Brandmaker AS"}
    assert assess_social_identity(single, {"platform": "x", "url": "https://x.com/brandmaker"})["publishable"]
    assert not assess_social_identity({"name": "Nor AS"}, {"platform": "x", "url": "https://x.com/norwegian"})["publishable"]


def test_rejected_candidate_does_not_veto_verified_account():
    # Round-2 audit P1-E: 'vikorenarkitekterbergen' fails verification (Bergen is not the
    # registered municipality), so it must not cancel the verified exact-name account.
    p = _profile(identity_text_excerpt="Org.nr 985 589 003", social_links=[
        {"platform": "facebook", "url": "https://facebook.com/vikorenarkitekter"},
        {"platform": "facebook", "url": "https://facebook.com/vikorenarkitekterbergen"},
        {"platform": "linkedin", "url": "https://linkedin.com/company/vikoren-arkitekter"},
    ])
    out = apply_website_identity_gate(p, p["evidence"]["website"])["website"]["value"]["social_links"]
    assert out == [{"platform": "facebook", "url": "https://facebook.com/vikorenarkitekter"},
                   {"platform": "linkedin", "url": "https://linkedin.com/company/vikoren-arkitekter"}]


def test_two_verified_accounts_on_one_platform_publish_neither():
    p = _profile(identity_text_excerpt="Org.nr 985 589 003", social_links=[
        {"platform": "facebook", "url": "https://facebook.com/vikorenarkitekter"},
        {"platform": "facebook", "url": "https://facebook.com/vikorenarkitekterofficial"},
        {"platform": "linkedin", "url": "https://linkedin.com/company/vikoren-arkitekter"},
    ])
    out = apply_website_identity_gate(p, p["evidence"]["website"])["website"]["value"]["social_links"]
    assert out == [{"platform": "linkedin", "url": "https://linkedin.com/company/vikoren-arkitekter"}]
