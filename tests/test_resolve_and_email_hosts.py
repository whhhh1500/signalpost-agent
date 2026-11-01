from norway_company_agent.resolve import digits_orgnr
from norway_company_agent.site_discovery import email_domain_hosts


def test_digits_orgnr():
    assert digits_orgnr("982942942") == "982942942"
    assert digits_orgnr("982 942 942") == "982942942"
    assert digits_orgnr("BRANDMAKER") is None


def test_email_domain_hosts_skips_free_mail():
    assert email_domain_hosts("post@brandmaker.no") == ["brandmaker.no"]
    assert email_domain_hosts("me@gmail.com") == []
    assert email_domain_hosts(None) == []
