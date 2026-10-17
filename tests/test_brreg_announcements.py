from norway_company_agent.brreg_announcements import parse_kunngjoringer

SAMPLE = '''
<tr bgcolor="#ffffff">
<td></td><td nowrap="true"><p>12.11.2025</p></td><td></td><td nowrap="true"><p>
<a href="hent_en.jsp?kid=20250000833116&amp;sokeverdi=996375072&amp;spraak=nb">Sletting etter fusjon</a>
</p></td></tr>
<tr>
<td></td><td nowrap="true"><p>04.08.2025</p></td><td></td><td nowrap="true"><p>
<a href="hent_en.jsp?kid=20250000674161&amp;sokeverdi=996375072&amp;spraak=nb">Godkjente &aring;rsregnskap</a>
</p></td></tr>
'''


def test_parse_kunngjoringer_extracts_date_type_url():
    rows = parse_kunngjoringer(SAMPLE, "996375072")
    assert len(rows) == 2
    assert rows[0]["date"] == "2025-11-12"
    assert rows[0]["type"] == "Sletting etter fusjon"
    assert "kid=20250000833116" in rows[0]["url"]
    assert rows[1]["type"] == "Godkjente årsregnskap"


def test_parse_empty_html():
    assert parse_kunngjoringer("<html></html>", "123456789") == []
