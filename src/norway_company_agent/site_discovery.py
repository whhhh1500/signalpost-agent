"""Official-website discovery for companies whose registry row has no homepage.

Only ~11% of Norwegian registry rows carry a website, so recall depends on finding
the rest without inventing links. Strategy: derive a handful of candidate domains
from the legal name, resolve DNS first (cheap), fetch the homepage and a few
contact/about pages, and accept a candidate only when

  * the page shows this company's organisation number, or
  * every distinctive name token AND the registered postcode or street appear,
    and the page shows no *other* organisation number (sister-company guard), or
  * side evidence for sites that never print the org.nr or address (e.g. a small
    agency's portfolio site): the .no domain IS the full legal-name core (every token,
    nothing dropped or added), the page names the exact legal entity ("<name> AS") or
    the registry e-mail domain is this domain, and the page names no other legal
    entity -- no other labelled org.nr, no different JSON-LD legalName, no
    "(c) <Other> AS" owner line (parent / brand-owner guard).

Anything else is left undiscovered. Precision first: a wrong website poisons
every downstream claim.
"""
from __future__ import annotations

import hashlib
import re
import socket
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

from .evidence import evidence
from .website import SAFE_OPENER, USER_AGENT, assert_public_url

SUFFIX = re.compile(r"\b(AS|ASA|ANS|DA|ENK|SA|BA|NUF|KS|IKS|SF|STI|FLI|SAM)\b\.?", re.I)
TRANSLIT = str.maketrans({"æ": "ae", "ø": "o", "å": "a", "Æ": "ae", "Ø": "o", "Å": "a", "&": " og "})
STOP = {"og", "i", "the", "norge", "norway", "holding", "eiendom", "invest"}
ORGNR_LABELLED = re.compile(r"org(?:anisasjons)?\.?\s*-?\s*(?:nr|nummer)\.?\s*:?\s*((?:\d[\s\u00a0.]?){8}\d)", re.I)
SUBPAGES = ("/kontakt", "/om-oss", "/kontakt-oss")
# Holding / co-ownership shells almost never have their own site; skip to save requests.
SKIP_INDUSTRY_PREFIXES = ("64.2", "68.2", "68.1")


def candidate_slugs(name: str, limit: int = 6) -> list[str]:
    text = SUFFIX.sub(" ", name or "").translate(TRANSLIT).lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    if not tokens:
        return []
    out = ["".join(tokens), "-".join(tokens)]
    core = [t for t in tokens if t not in STOP]
    if core and core != tokens:
        out += ["".join(core), "-".join(core)]
    if len(core) >= 2:
        out += [core[0] + core[1], core[0]]
    seen: list[str] = []
    for slug in out:
        if slug and len(slug) >= 3 and slug not in seen and len(slug) <= 63:
            seen.append(slug)
    return seen[:limit]


def orgnr_pattern(org: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\d)" + r"[\s\u00a0.\-]?".join(org) + r"(?!\d)")


def shows_other_orgnr(html: str, org: str) -> bool:
    found = {re.sub(r"\D", "", m) for m in ORGNR_LABELLED.findall(html)}
    return bool(found) and org not in found


def name_and_address_match(html: str, name: str, postcode: str | None, streets: list[str], org: str) -> bool:
    if shows_other_orgnr(html, org):
        return False
    low = html.lower()
    core = [t for t in re.split(r"[^a-zæøå0-9]+", SUFFIX.sub(" ", name).lower()) if len(t) > 2]
    name_ok = bool(core) and all(t in low for t in core)
    addr_ok = bool(postcode and re.search(r"(?<!\d)" + re.escape(postcode) + r"(?!\d)", low)) or any(s and s in low for s in streets)
    return name_ok and addr_ok


LEGAL_SUFFIXES = r"(?:AS|ASA|ANS|DA|SA|BA|NUF|KS|IKS)"
OWNER_LINE = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?([A-ZÆØÅ0-9][\wÆØÅæøå&.\- ]{1,60}?\s" + LEGAL_SUFFIXES + r")\b", re.I)
LEGALNAME_JSON = re.compile(r'"legalName"\s*:\s*"([^"]{2,120})"')


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-zæøå0-9]+", SUFFIX.sub(" ", name or "").lower()) if t]


def _visible_text(html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


def domain_is_full_name(host: str, name: str) -> bool:
    """The .no domain label is exactly the legal name without suffix (all tokens kept)."""
    if not host.endswith(".no"):
        return False
    label = host.removeprefix("www.")[: -len(".no")]
    slugs = candidate_slugs(name, limit=2)  # first two are the full-token forms
    return len(label.replace("-", "")) >= 5 and label in slugs


def names_other_legal_entity(html: str, name: str) -> bool:
    ours = set(_name_tokens(name))
    owners = [m.group(1) for m in OWNER_LINE.finditer(_visible_text(html))]
    owners += LEGALNAME_JSON.findall(html)
    return any(set(_name_tokens(o)) != ours for o in owners if _name_tokens(o))


def legal_name_on_page(html: str, name: str) -> bool:
    tokens = _name_tokens(name)
    suffix = SUFFIX.findall(name or "")
    if not tokens or not suffix:
        return False
    phrase = r"\s+".join(re.escape(t) for t in tokens) + r"\s+" + re.escape(suffix[-1].lower()) + r"(?![\wæøå])"
    return bool(re.search(r"(?<![\wæøå])" + phrase, _visible_text(html).lower()))


def side_evidence_match(html: str, host: str, name: str, org: str, registry_email: str | None) -> str | None:
    if not domain_is_full_name(host, name) or shows_other_orgnr(html, org) or names_other_legal_entity(html, name):
        return None
    email_domain = (registry_email or "").rpartition("@")[2].lower().strip()
    if email_domain and email_domain == host.removeprefix("www."):
        return "domain is the full legal name and matches the registry e-mail domain; no other legal entity named"
    if legal_name_on_page(html, name):
        return "domain is the full legal name and the page names the exact legal entity; no other legal entity named"
    return None


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def _get(url: str, timeout: float, max_bytes: int) -> tuple[str, str, int]:
    assert_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    with SAFE_OPENER.open(req, timeout=timeout) as resp:
        if "html" not in (resp.headers.get("content-type") or "").lower():
            raise ValueError("not html")
        raw = resp.read(max_bytes)
        final = resp.geturl()
    assert_public_url(final)
    return final, raw.decode("utf-8", errors="replace"), len(raw)


def _address(profile: dict[str, Any]) -> tuple[str | None, list[str]]:
    live = ((profile.get("evidence") or {}).get("registry_live") or {}).get("value") or {}
    addr = live.get("business_address") or live.get("postal_address") or {}
    streets = [s.lower() for s in addr.get("adresse") or [] if s and len(s) > 4]
    return addr.get("postnummer"), streets


def discover_website(profile: dict[str, Any], *, timeout: float = 6.0, max_bytes: int = 1_500_000, max_hosts: int = 8) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """Return (homepage_url_or_None, discovery_evidence, metrics)."""
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": []}
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "")
    src = "https://data.brreg.no/enhetsregisteret/api/enheter/" + org
    if str(profile.get("industry_code") or "").startswith(SKIP_INDUSTRY_PREFIXES):
        return None, evidence("website_discovery", "not_found", "domain_candidate_discovery", src, note="Skipped: holding/real-estate shell industry"), metrics
    postcode, streets = _address(profile)
    registry_email = (((profile.get("evidence") or {}).get("registry_live") or {}).get("value") or {}).get("email")
    pattern = orgnr_pattern(org)
    tried: list[str] = []
    for slug in candidate_slugs(name):
        for tld in (".no", ".com"):
            host = slug + tld
            if len(tried) >= max_hosts:
                break
            tried.append(host)
            if not _resolves(host):
                continue
            for path in ("",) + SUBPAGES:
                url = f"https://{host}{path}"
                started = time.monotonic()
                metrics["requests"] += 1
                try:
                    final, html, size = _get(url, timeout, max_bytes)
                except Exception:
                    if path != "":
                        continue
                    try:  # one slower retry: large portfolio homepages time out under load
                        final, html, size = _get(url, timeout * 2, max_bytes)
                    except Exception:
                        break  # homepage unreachable: do not probe subpages
                metrics["bytes"] += size
                metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
                final_host = (urllib.parse.urlparse(final).hostname or "").lower()
                if final_host.removeprefix("www.") != host:
                    # Candidate redirected to another domain (often Facebook or a web-shop
                    # platform): the evidence would not be company-controlled on this host.
                    break
                reason = None
                if pattern.search(html):
                    reason = "organisation number shown on " + (path or "homepage")
                elif name_and_address_match(html, name, postcode, streets, org):
                    reason = "all name tokens plus registered address on " + (path or "homepage") + "; no other organisation number"
                else:
                    side = side_evidence_match(html, host, name, org, registry_email)
                    if side:
                        reason = side + " (" + (path or "homepage") + ")"
                if reason:
                    homepage = f"https://{host}/" if path else final
                    try:
                        from bs4 import BeautifulSoup

                        from .website import identity_text_excerpt

                        matched_excerpt = identity_text_excerpt(BeautifulSoup(html, "html.parser"))
                    except Exception:
                        matched_excerpt = ""
                    return homepage, evidence(
                        "website_discovery", "available", "domain_candidate_discovery", final,
                        value={"homepage": homepage, "matched_url": final, "reason": reason, "candidates_tried": tried,
                               "matched_identity_text_excerpt": matched_excerpt},
                        content_sha256=hashlib.sha256(html.encode("utf-8", "replace")).hexdigest(),
                        note="Website not in registry; discovered by name-derived domain and verified against registry identifiers",
                    ), metrics
    return None, evidence("website_discovery", "not_found", "domain_candidate_discovery", src,
                          value={"candidates_tried": tried}, note="No candidate domain carried this company's identifiers"), metrics
