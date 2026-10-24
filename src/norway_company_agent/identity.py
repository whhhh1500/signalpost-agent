from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


ORGNR_LABELLED = re.compile(r"(?:org(?:anisasjons)?\.?\s*-?\s*(?:nr|nummer)|foretaksregisteret|mva)\.?\s*:?\s*(?:no\s*)?((?:\d[\s\u00a0.]?){8}\d)", re.I)


def labelled_orgnrs(text: str) -> set[str]:
    """Organisation numbers a page explicitly labels as its operator (org.nr / foretaksregisteret)."""
    return {re.sub(r"\D", "", match) for match in ORGNR_LABELLED.findall(text or "")}


def _host_domain(url: str) -> str:
    host = (urllib.parse.urlparse(url or "").hostname or "").casefold().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def foreign_entity_signals(profile: dict[str, Any], value: dict[str, Any], candidate_text: str) -> list[str]:
    """Audit item 1: evidence that the site belongs to a parent, sister or brand owner.

    Rules (any one blocks publication):
      * the site labels an organisation number and none of the labelled numbers is ours;
      * the site's JSON-LD legalName names a different legal entity (none of our core tokens);
      * the registry/discovered URL redirected to a different registered domain and the
        landing site does not show our organisation number.
    """
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    core = set(_tokens(profile.get("name")))
    signals: list[str] = []
    labelled = labelled_orgnrs(candidate_text)
    if labelled and org not in labelled:
        signals.append(f"site labels other organisation number(s) {sorted(labelled)[:3]} and not {org}")
    for item in value.get("structured_organisations") or []:
        legal = item.get("legalName") if isinstance(item, dict) else None
        if isinstance(legal, str) and legal.strip() and core and not (core & set(_tokens(legal))):
            signals.append(f"structured legalName '{legal[:80]}' is a different entity")
            break
    requested = value.get("requested_url") or ""
    final = value.get("final_url") or ""
    if requested and final and _host_domain(requested) != _host_domain(final) and org not in labelled:
        signals.append(f"URL redirected from {_host_domain(requested)} to {_host_domain(final)} (brand/parent site) without our orgnr")
    return signals


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    hostname = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    compact_candidate = re.sub(r"\D", "", candidate_text)
    compact_homepage_candidate = re.sub(r"\D", "", homepage_candidate_text)
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []
    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))
    foreign = foreign_entity_signals(profile, value, candidate_text)
    if any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif foreign:
        score = 0.2
        reasons.extend(foreign)
        reasons.append("site appears to belong to a parent, sister or brand entity; not this company's own website")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif org_digits and org_digits in compact_homepage_candidate:
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence with substantive content")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "foreign_entity_signals": foreign,
        "method": "deterministic_name_org_evidence_v3",
    }


GENERIC_HANDLES = {"home", "pages", "people", "login", "signup", "watch", "hashtag", "explore", "company"}


def assess_social_identity(profile: dict[str, Any], link: dict[str, str], website_value: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audit item 2: a social account is published only if it provably belongs to this company.

    Required, all of:
      * it was linked from this company's own verified website (enforced by the caller);
      * the handle is not a generic/platform path;
      * the handle carries the company's own legal-name core: the full normalized name
        sequence, or every distinctive token, or (single-token names) the token as the whole
        handle or a handle prefix -- a token merely embedded inside a longer word is rejected;
      * the handle is not the site's parent/brand: if the website was not itself verified
        this check never runs because nothing is published.
    """
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    segments = [seg for seg in urllib.parse.unquote(parsed.path).split("/") if seg]
    handle_segment = segments[-1] if segments else ""
    handle_tokens = _tokens(handle_segment.lstrip("@"))
    handle_compact = "".join(handle_tokens)
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    distinct = set(core)
    score = 0.3
    reason = "social handle lacks strong exact-entity name evidence"
    allowed_extra = {"as", "asa", "no", "norge", "norway", "official", "the", "hq"}
    affixed = {core_compact} | {core_compact + x for x in allowed_extra} | {x + core_compact for x in allowed_extra}
    extra_tokens = [t for t in handle_tokens if t not in distinct]
    if not handle_compact or handle_compact in GENERIC_HANDLES:
        reason = "generic or empty social handle"
    elif core_compact and len(core_compact) >= 3 and handle_compact in affixed:
        score = 0.98
        reason = "handle is the normalized legal name (optionally with a neutral affix such as 'as'/'norge')"
    elif distinct and distinct.issubset(set(handle_tokens)) and all(t in allowed_extra for t in extra_tokens):
        score = 0.95
        reason = "every legal-name token is a whole handle token and no foreign tokens are present"
    elif len(core_compact) >= 8 and core_compact in handle_compact and len(handle_compact) - len(core_compact) <= 6:
        score = 0.9
        reason = "long legal-name sequence with a short location/affix in the handle"
    elif matched:
        reason = "only part of the legal name, or the name embedded in a longer word, appears in the handle (possible namesake, parent or brand account)"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v2",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    # One account per platform: two distinct verified handles on one platform means we
    # cannot tell which is the company's own (e.g. parent vs local), so publish neither.
    passing = [item for item in social_assessments if assessment["publishable"] and item["publishable"]]
    per_platform: dict[str, list[dict[str, Any]]] = {}
    for item in passing:
        per_platform.setdefault(item["platform"], []).append(item)
    for platform, items in per_platform.items():
        if len({entry["url"].casefold() for entry in items}) > 1:
            for entry in items:
                entry["publishable"] = False
                entry["reason"] = f"multiple candidate {platform} accounts on the site; ownership ambiguous"
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }
