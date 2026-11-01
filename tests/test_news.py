from norway_company_agent.news import exact_title_match


def test_exact_title_match_requires_full_legal_name():
    assert exact_title_match("EQUINOR ASA", "Equinor ASA øker utbyttene")
    assert exact_title_match("EQUINOR ASA", "Analytiker om Equinor ASA: positiv")
    assert not exact_title_match("EQUINOR ASA", "Equinor Ventures investerer")
    assert not exact_title_match("ROAN VIND AS", "Aneo Roan Vind Holding AS selger")


def test_exact_title_match_allows_norwegian_prepositions():
    assert exact_title_match("BRANDMAKER AS", "Ny kontrakt for Brandmaker AS i Bergen")
    assert not exact_title_match("BRANDMAKER AS", "Brandmaker Group AS")
