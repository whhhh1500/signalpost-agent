from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any


# Bump whenever any publication gate gets stricter: stored profiles validated under an
# older version are re-validated (never re-published as-is) on --resume.
GATE_VERSION = "signalpost-gates-v5-2026-10-24"

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


CORPORATE_STRUCTURE_WORDS = {"holding", "holdings", "group", "gruppen", "gruppe", "konsern", "invest", "eiendom", "capital", "partners"}
COPYRIGHT_HOLDER = re.compile(
    r"(?:\u00a9|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-\u2013]\s*\d{4})?)?\s*[,:]?\s*"
    r"([A-Z\u00c6\u00d8\u00c50-9][\w&.\- \u00e6\u00f8\u00e5\u00c6\u00d8\u00c5]{1,80}?\s(?:AS|ASA|ANS|DA|SA))\b",
    re.I,
)


def _same_entity_name(ours: Any, theirs: Any) -> bool:
    """Exact legal-entity name equality after normalization (legal suffixes ignored).

    'Nordic Bygg Holding AS' is NOT 'Nordic Bygg AS': every token must match both ways.
    """
    a, b = set(_tokens(ours)), set(_tokens(theirs))
    return bool(a) and a == b


def site_operator_names(value: dict[str, Any]) -> list[str]:
    """Names the site itself declares as its operator: JSON-LD legalName and '(c) <X> AS' lines."""
    names: list[str] = []
    orgs = list(value.get("structured_organisations") or [])
    for page in value.get("pages") or []:
        orgs.extend(page.get("structured_organisations") or [])
    for item in orgs:
        legal = item.get("legalName") if isinstance(item, dict) else None
        if isinstance(legal, str) and legal.strip():
            names.append(legal.strip())
    texts = [value.get("identity_text_excerpt") or ""] + [page.get("identity_text_excerpt") or "" for page in value.get("pages") or []]
    for text in texts:
        for match in COPYRIGHT_HOLDER.findall(text):
            names.append(" ".join(match.split()))
    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    return out


def foreign_entity_signals(profile: dict[str, Any], value: dict[str, Any], candidate_text: str) -> list[str]:
    """Audit item 1/3: evidence that the site is OPERATED by a parent, sister or brand entity.

    A page MENTIONING our organisation number is not proof that we operate it (a parent's
    site often lists all subsidiaries). Rules (any one blocks publication):
      * the site labels organisation number(s) and none of them is ours;
      * the site declares an operator (JSON-LD legalName or a copyright line) and none of the
        declared operators is exactly our legal entity -- 'Nordic Bygg Holding AS' does not
        pass for 'Nordic Bygg AS' just because they share words;
      * the registry/discovered URL redirected to a different registered domain and the
        landing site does not show our organisation number.
    """
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    signals: list[str] = []
    labelled = labelled_orgnrs(candidate_text)
    if labelled and org not in labelled:
        signals.append(f"site labels other organisation number(s) {sorted(labelled)[:3]} and not {org}")
    operators = site_operator_names(value)
    if operators and not any(_same_entity_name(profile.get("name"), name) for name in operators):
        signals.append(f"site declares operator {operators[0][:80]!r}, which is a different legal entity")
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
    labelled_all = labelled_orgnrs(candidate_text)
    own_operator = any(_same_entity_name(profile.get("name"), name) for name in site_operator_names(value))
    our_words = set(_tokens(profile.get("name")))
    homepage_words = set().union(*homepage_token_sets) if homepage_token_sets else set()
    structure_word_conflict = bool((homepage_words & CORPORATE_STRUCTURE_WORDS) - our_words) and exact_homepage_name
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
    elif org_digits and org_digits in labelled_all and len(labelled_all) > 1 and not own_operator:
        score = 0.65
        reasons.append(f"site lists several organisation numbers {sorted(labelled_all)[:4]}; mentioning ours does not show that we operate the site")
    elif org_digits and org_digits in labelled_all and structure_word_conflict and not own_operator:
        score = 0.65
        reasons.append("our organisation number is listed, but the site presents itself as a holding/group entity")
    elif org_digits and org_digits in labelled_all:
        score = 1.0
        reasons.append("the site labels our organisation number as its own (only labelled org.nr, or declared operator is this exact legal entity)")
    elif org_digits and org_digits in compact_homepage_candidate and not labelled_all and not structure_word_conflict:
        score = 0.95
        reasons.append("our organisation number appears in homepage identity evidence and no other organisation number is labelled")
    elif structure_word_conflict and not own_operator:
        score = 0.65
        reasons.append("site name adds a holding/group word that is not in our legal name (possible parent site)")
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
        "method": "deterministic_name_org_evidence_v4",
        "gate_version": GATE_VERSION,
    }


GENERIC_HANDLES = {"home", "pages", "people", "login", "signup", "watch", "hashtag", "explore", "company"}
# Neutral words that may FOLLOW the legal name in a handle. Nothing may precede it
# ('notbrandmaker'), and nothing else may follow it ('brandmakerfake', 'brandmakerfan').
NEUTRAL_SUFFIXES = ("as", "asa", "no", "norge", "norway", "official", "offisiell", "hq")


def _neutral_remainder(rest: str, extra: set[str]) -> bool:
    words = sorted(set(NEUTRAL_SUFFIXES) | extra, key=len, reverse=True)
    used = 0
    while rest:
        word = next((w for w in words if w and rest.startswith(w)), None)
        if word is None or used >= 2:
            return False
        rest = rest[len(word):]
        used += 1
    return True


def assess_social_identity(profile: dict[str, Any], link: dict[str, str], website_value: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audit item 2: a social account is published only if it provably belongs to this company.

    Required, all of:
      * it was linked from this company's own verified website (enforced by the caller);
      * the URL is an ACCOUNT path under that platform's rules (never a post/video/reel/
        'accounts' path; the handle is the platform's account segment, not the last segment);
      * the handle STARTS with the company's normalized legal name and anything after it is
        only neutral words (as/norge/official ...) or the registered municipality -- no prefix
        ('notbrandmaker') and no other suffix ('brandmakerfake') is accepted.
    """
    from .website import parse_social_account

    core = _tokens(profile.get("name"))
    normalized = parse_social_account(link.get("url") or "")
    base = {**link, "matched_tokens": [], "method": "deterministic_social_handle_identity_v3", "gate_version": GATE_VERSION}
    if not normalized:
        return {**base, "identity_score": 0.0, "publishable": False, "reason": "URL is not an account path on this platform (content/post/video/reserved path)"}
    handle = normalized["handle"]
    handle_tokens = _tokens(handle)
    handle_compact = "".join(handle_tokens)
    core_compact = "".join(core)
    matched = [token for token in core if token in handle_compact]
    location = set(_tokens(profile.get("municipality")))
    score = 0.3
    reason = "social handle lacks strong exact-entity name evidence"
    if not handle_compact or handle_compact in GENERIC_HANDLES:
        reason = "generic or empty social handle"
    elif core_compact and len(core_compact) >= 3 and handle_compact == core_compact:
        score = 0.98
        reason = "handle is exactly the normalized legal name"
    elif core_compact and len(core_compact) >= 3 and handle_compact.startswith(core_compact) and _neutral_remainder(handle_compact[len(core_compact):], location):
        score = 0.95
        reason = "handle is the normalized legal name followed only by neutral words or the registered municipality"
    elif matched:
        reason = "handle adds a prefix or a non-neutral suffix to the legal name, or carries only part of it (possible namesake, fan, fake, parent or brand account)"
    return {
        **base,
        "url": normalized["url"],
        "handle": handle,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
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
    # Candidates carrying the company name (verified or not) compete for "the" account.
    name_bearing = [item for item in social_assessments if assessment["publishable"] and (item["publishable"] or (item.get("matched_tokens") and item.get("handle", "").startswith("".join(_tokens(profile.get("name"))))))]
    per_platform: dict[str, list[dict[str, Any]]] = {}
    for item in name_bearing:
        per_platform.setdefault(item["platform"], []).append(item)
    for platform, items in per_platform.items():
        if len({entry.get("handle") or entry["url"].casefold() for entry in items}) > 1:
            for entry in items:
                entry["publishable"] = False
                entry["reason"] = f"multiple candidate {platform} accounts on the site; ownership ambiguous"
    published: dict[tuple[str, str], dict[str, str]] = {}
    for item in social_assessments:
        if assessment["publishable"] and item["publishable"]:
            published.setdefault((item["platform"], item.get("handle") or item["url"]), {"platform": item["platform"], "url": item["url"]})
    value["social_links"] = list(published.values())
    value["identity_gate_version"] = GATE_VERSION
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }


def website_identity_publishable(record: dict[str, Any] | None) -> bool:
    """Fail closed (audit item 7): website claims are publishable only when the CURRENT identity
    gate assessed them and said yes. Missing assessment, missing or old gate version -> False."""
    value = (record or {}).get("value") or {}
    assessment = value.get("identity_assessment")
    if not isinstance(assessment, dict):
        return False
    if value.get("identity_gate_version") != GATE_VERSION:
        return False
    return assessment.get("publishable") is True


def profile_gate_version_current(profile: dict[str, Any]) -> bool:
    """A resumed profile is reusable only if it was validated by THIS gate version."""
    if profile.get("gate_version") != GATE_VERSION:
        return False
    website = (profile.get("evidence") or {}).get("website") or {}
    if website.get("status") == "available" and (website.get("value") or {}).get("identity_gate_version") != GATE_VERSION:
        return False
    return True
