from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any


# Bump whenever any publication gate gets stricter: stored profiles validated under an
# older version are re-validated (never re-published as-is) on --resume.
GATE_VERSION = "signalpost-gates-v6-2026-10-24"

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


# Wording that introduces OTHER entities (subsidiaries, sister companies, partners, owners):
# an organisation number listed after it is a mention, not the site operator's own number.
GROUP_LISTING_CONTEXT = re.compile(
    r"(?:datter\w*|s\u00f8ster\w*|soster\w*|konsernselskap\w*|selskaper\s+i\s+konsernet|v\u00e5re\s+selskaper|vare\s+selskaper|"
    r"tilknyttede|subsidiar\w*|sister\s+compan\w*|group\s+compan\w*|our\s+companies|eier\w*\s+av|eies\s+av|owned\s+by|"
    r"morselskap\w*|parent\s+company|partner\w*|samarbeids\w*|kunde\w*|referanse\w*|leverand\u00f8r\w*|leverandor\w*|"
    r"forhandler\w*|webdesign|utviklet\s+av|levert\s+av)",
    re.I,
)


def orgnr_mentions(text: str) -> list[tuple[str, bool]]:
    """(orgnr, operator_context) for every labelled org.nr. operator_context is False when the
    number is introduced as some OTHER entity's ("Vare datterselskaper: X AS org.nr ...")."""
    out = []
    text = text or ""
    for match in ORGNR_LABELLED.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        before = text[max(0, match.start() - 120):match.start()]
        # Only the clause the number belongs to: cut at the last sentence/line break.
        clause = re.split(r"[.!?\n\u2022|]\s|\n", before)[-1]
        out.append((digits, not GROUP_LISTING_CONTEXT.search(clause)))
    return out


def operator_orgnrs(text: str) -> set[str]:
    return {digits for digits, operator in orgnr_mentions(text) if operator}


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


# Credit lines for the agency that BUILT the site ("Webdesign (c) 2026 Pixel Studio AS",
# "Utviklet av Pixel Studio AS") name the web agency, not the site operator.
WEB_CREDIT_CONTEXT = re.compile(
    r"(?:web\s*-?\s*design|webdesign|nettside|hjemmeside|nettsted|design|designet|utvikl\w*|levert|laget|produsert|"
    r"powered|developed|made|built|website|site|hosting|publisert)(?:\s+(?:av|by|og|and|i|med))?\s*[:\-|\u2013]?\s*$",
    re.I,
)


def _copyright_holders(text: str) -> list[str]:
    names = []
    for match in COPYRIGHT_HOLDER.finditer(text or ""):
        before = (text or "")[max(0, match.start() - 40):match.start()]
        if WEB_CREDIT_CONTEXT.search(before):
            continue
        names.append(" ".join(match.group(1).split()))
    return names


def _fold(value: Any) -> str:
    text = str(value or "").translate(str.maketrans({"\u00f8": "o", "\u00d8": "O", "\u00e5": "a", "\u00c5": "A", "\u00e6": "ae", "\u00c6": "AE"}))
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


LEGAL_SUFFIX_RE = r"(?:AS|ASA|ANS|DA|SA|NUF|KS|BA)"


def extended_entity_names(our_name: Any, texts: list[Any]) -> list[str]:
    """Names in identity texts that EXTEND our legal name into another entity.

    'Nordic Bygg Service AS' / 'Nordic Bygg Holding' are not 'Nordic Bygg AS': our full name
    followed by capitalised extra word(s) and a legal suffix, or followed directly by a
    corporate-structure word (Holding, Gruppen, Eiendom ...). The words must be ATTACHED to our
    name -- 'Vi leverer for Holding og Eiendom kunder' elsewhere on the page is not a conflict.
    """
    core = [t for t in re.findall(r"[a-z0-9]+", _fold(our_name).casefold())]
    while core and core[-1] in LEGAL_AND_GENERIC:
        core.pop()
    if not core:
        return []
    name_re = r"\b(?i:" + r"[\s\-&.]+".join(re.escape(t) for t in core) + ")"
    structure = "|".join(sorted(CORPORATE_STRUCTURE_WORDS, key=len, reverse=True))
    with_suffix = re.compile(name_re + r"((?:\s+[A-Z][\w\-]*){1,3})\s+" + LEGAL_SUFFIX_RE + r"\b")
    with_structure = re.compile(name_re + r"\s+(?i:(" + structure + r"))\b")
    found = []
    for text in texts:
        folded = _fold(text)
        for match in with_suffix.finditer(folded):
            if all(w.casefold() in LEGAL_AND_GENERIC for w in match.group(1).split()):
                continue
            found.append(" ".join(match.group(0).split()))
        for match in with_structure.finditer(folded):
            found.append(" ".join(match.group(0).split()))
    return found


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
        names.extend(_copyright_holders(text))
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
    labelled = operator_orgnrs(candidate_text)
    if labelled and org not in labelled:
        signals.append(f"site labels other organisation number(s) {sorted(labelled)[:3]} and not {org}")
    operators = site_operator_names(value)
    # A sole, operator-context org.nr equal to ours outranks a copyright line (copyright lines
    # often carry an old trading name); otherwise a different declared operator blocks.
    sole_own_orgnr = bool(org) and labelled == {org}
    if operators and not sole_own_orgnr and not any(_same_entity_name(profile.get("name"), name) for name in operators):
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
    mentions = orgnr_mentions(candidate_text)
    operator_all = {digits for digits, operator in mentions if operator}
    ours_listed_only_as_other = bool(org_digits) and org_digits in labelled_all and org_digits not in operator_all
    own_operator = any(_same_entity_name(profile.get("name"), name) for name in site_operator_names(value))
    identity_texts = [value.get("title"), value.get("description"), value.get("identity_text_excerpt"), *structured_names, rendered.get("title")]
    extended = extended_entity_names(profile.get("name"), identity_texts)
    if extended and not own_operator:
        # Our exact name standing on its own (followed by its legal suffix or punctuation/end)
        # is still evidence; an extended name only conflicts when it is ALL the site shows.
        core_words = [t for t in re.findall(r"[a-z0-9]+", _fold(profile.get("name")).casefold())]
        while core_words and core_words[-1] in LEGAL_AND_GENERIC:
            core_words.pop()
        plain = re.compile(r"\b(?i:" + r"[\s\-&.]+".join(map(re.escape, core_words)) + r")(?:\s+(?i:" + LEGAL_SUFFIX_RE + r"))?\s*(?:$|[|,.:;!?\u2013\u2014()\-]|\s[a-z\u00e6\u00f8\u00e5])") if core_words else None
        standalone = bool(plain) and any(plain.search(_fold(t)) for t in identity_texts if t)
        if standalone:
            extended = []
    structure_word_conflict = bool(extended)
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
    elif ours_listed_only_as_other and not own_operator:
        score = 0.65
        reasons.append("our organisation number appears only where the site lists OTHER entities (subsidiaries/sister companies/partners); the site is not shown to be operated by us")
    elif org_digits and org_digits in operator_all and len(operator_all) > 1 and not own_operator:
        score = 0.65
        reasons.append(f"site labels several organisation numbers {sorted(operator_all)[:4]} as its own; ours alone does not show that we operate the site")
    elif org_digits and operator_all == {org_digits}:
        score = 1.0
        reasons.append("the site labels our organisation number as the operator's (only operator-context org.nr)")
    elif org_digits and org_digits in operator_all and own_operator:
        score = 1.0
        reasons.append("the site labels our organisation number and declares this exact legal entity as operator")
    elif org_digits and org_digits in compact_homepage_candidate and not labelled_all and not structure_word_conflict:
        score = 0.95
        reasons.append("our organisation number appears in homepage identity evidence and no other organisation number is labelled")
    elif structure_word_conflict:
        score = 0.65
        reasons.append(f"site names an extended entity {extended[0][:60]!r} (our legal name plus extra words), possibly a different company")
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
        "method": "deterministic_name_org_evidence_v5",
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
    base = {**link, "matched_tokens": [], "method": "deterministic_social_handle_identity_v4", "gate_version": GATE_VERSION}
    if not normalized:
        return {**base, "identity_score": 0.0, "publishable": False, "reason": "URL is not an account path on this platform (content/post/video/reserved path)"}
    handle = normalized["handle"]
    # Compare the WHOLE handle: no word is dropped ('the.brandmaker', 'brandmaker.and',
    # 'brandmaker.x', 'brandmaker.1' must keep 'the'/'and'/'x'/'1'); only separators go.
    handle_compact = "".join(re.findall(r"[a-z0-9]+", _fold(handle).casefold()))
    # Legal name: drop only the trailing legal-form suffix; inner words like 'og'/'and' stay
    # (accept both spellings: 'hansenogsonn' and 'hansensonn').
    name_words = re.findall(r"[a-z0-9]+", _fold(profile.get("name")).casefold())
    while name_words and name_words[-1] in LEGAL_AND_GENERIC - {"the", "og", "and"}:
        name_words.pop()
    core_variants = {v for v in ("".join(name_words), "".join(core)) if v}
    core_compact = "".join(core)
    matched = [token for token in core if token in handle_compact]
    location = set(_tokens(profile.get("municipality")))
    score = 0.3
    reason = "social handle lacks strong exact-entity name evidence"
    if not handle_compact or handle_compact in GENERIC_HANDLES:
        reason = "generic or empty social handle"
    elif any(len(v) >= 3 and handle_compact == v for v in core_variants):
        score = 0.98
        reason = "handle is exactly the normalized legal name"
    elif any(len(v) >= 3 and handle_compact.startswith(v) and _neutral_remainder(handle_compact[len(v):], location) for v in core_variants):
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
    # Only candidates that PASSED verification compete; a rejected one ('brandmakerfan') must
    # not veto the verified account ('brandmaker').
    name_bearing = [item for item in social_assessments if assessment["publishable"] and item["publishable"]]
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
