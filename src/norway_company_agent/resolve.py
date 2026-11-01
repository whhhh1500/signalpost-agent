"""Resolve a Norwegian company by organisation number or legal name via Brreg."""
from __future__ import annotations

import urllib.parse
from typing import Any

from .http import fetch_json
from .official import normalize_entity

BRREG_ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
BRREG_SEARCH = "https://data.brreg.no/enhetsregisteret/api/enheter"


def digits_orgnr(value: str) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) == 9 else None


def profile_from_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Build a competition-style profile skeleton from a live Brreg entity payload."""
    from .evidence import evidence, utc_now
    from .official import accounting_obligation_assessment

    normalized = normalize_entity(entity)
    org = str(normalized.get("organisation_number") or "")
    industry = normalized.get("industry") or {}
    addr = normalized.get("business_address") or normalized.get("postal_address") or {}
    profile = {
        "organisation_number": org,
        "name": normalized.get("name"),
        "legal_form": normalized.get("legal_form"),
        "employees": normalized.get("employees"),
        "bankrupt": bool(normalized.get("bankrupt")),
        "liquidating": bool(normalized.get("liquidating")),
        "municipality": (addr or {}).get("kommune"),
        "municipality_number": (addr or {}).get("kommunenummer"),
        "industry_code": (industry or {}).get("kode") if isinstance(industry, dict) else None,
        "industry_label": (industry or {}).get("beskrivelse") if isinstance(industry, dict) else None,
        "website": normalized.get("website") or "",
        "latest_submitted_accounts": normalized.get("latest_submitted_accounts"),
        "evidence": {
            "registry_live": evidence(
                "registry_live",
                "available",
                "official_registry_live",
                BRREG_ENTITY.format(org=org),
                value=normalized,
                retrieved_at=utc_now(),
            ),
        },
    }
    profile["evidence"]["accounting_obligation"] = accounting_obligation_assessment(profile)
    profile["evidence"]["registry"] = evidence(
        "registry",
        "available",
        "official_registry_live",
        BRREG_ENTITY.format(org=org),
        value=entity,
        retrieved_at=utc_now(),
        note="Live Brreg entity used for interactive lookup (no frozen bulk row)",
    )
    return profile


def resolve_company(query: str, *, size: int = 8) -> dict[str, Any]:
    """Resolve by 9-digit orgnr or by name search. Never guesses a single hit among many."""
    query = " ".join(str(query or "").split())
    if not query:
        return {"status": "empty", "matches": [], "profile": None, "note": "Enter a company name or 9-digit organisation number"}

    org = digits_orgnr(query)
    compact = "".join(ch for ch in query if not ch.isspace())
    if org and compact.isdigit() and len(compact) == 9:
        result = fetch_json(BRREG_ENTITY.format(org=org))
        if result.status == 200 and isinstance(result.body, dict):
            profile = profile_from_entity(result.body)
            return {"status": "resolved", "matches": [{"organisation_number": org, "name": profile.get("name")}], "profile": profile, "note": None}
        if result.status in {404, 410}:
            return {"status": "not_found", "matches": [], "profile": None, "note": f"No Brreg entity for {org}"}
        return {"status": "source_error", "matches": [], "profile": None, "note": result.error or f"Brreg error {result.status}"}

    # Name search
    url = BRREG_SEARCH + "?" + urllib.parse.urlencode({"navn": query, "size": size})
    result = fetch_json(url)
    if result.status != 200 or not isinstance(result.body, dict):
        return {"status": "source_error", "matches": [], "profile": None, "note": result.error or f"Brreg search error {result.status}"}
    rows = ((result.body.get("_embedded") or {}).get("enheter") or [])
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        matches.append({
            "organisation_number": str(row.get("organisasjonsnummer") or ""),
            "name": row.get("navn"),
            "legal_form": (row.get("organisasjonsform") or {}).get("kode"),
            "municipality": (row.get("forretningsadresse") or row.get("postadresse") or {}).get("kommune"),
        })
    matches = [m for m in matches if len(m["organisation_number"]) == 9]
    if not matches:
        return {"status": "not_found", "matches": [], "profile": None, "note": "No Brreg name matches"}
    # Exact casefold name match wins when unique
    exact = [m for m in matches if str(m.get("name") or "").casefold() == query.casefold()]
    if len(exact) == 1:
        detail = fetch_json(BRREG_ENTITY.format(org=exact[0]["organisation_number"]))
        if detail.status == 200 and isinstance(detail.body, dict):
            return {"status": "resolved", "matches": exact, "profile": profile_from_entity(detail.body), "note": None}
    if len(matches) == 1:
        detail = fetch_json(BRREG_ENTITY.format(org=matches[0]["organisation_number"]))
        if detail.status == 200 and isinstance(detail.body, dict):
            return {"status": "resolved", "matches": matches, "profile": profile_from_entity(detail.body), "note": None}
    return {
        "status": "ambiguous",
        "matches": matches,
        "profile": None,
        "note": "Several companies match — pick an organisation number (blank beats a wrong pick)",
    }
