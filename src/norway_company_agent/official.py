from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .evidence import evidence
from .http import FetchResult, fetch_json

ACCOUNTING_OBLIGATION_SOURCE = "https://www.brreg.no/en/submission-of-annual-accounts/reporting-obligations-to-the-register-of-company-accounts/who-has-an-accounting-obligation/"
ACCOUNTING_RULESET_VERSION = "brreg_accounting_obligation_rules_2024-08-09_v1"
ALWAYS_ACCOUNTING_OBLIGED_FORMS = {"AS", "ASA", "BRL", "BBL", "STI", "SF", "VPFO"}
THRESHOLD_OR_ACTIVITY_FORMS = {"ENK", "ANS", "DA", "SA", "FLI", "ESEK", "NUF", "UTLA", "ORGL", "SAM", "SPA", "KS", "BO"}

BRREG_ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
BRREG_ROLES = BRREG_ENTITY + "/roller"
BRREG_GROUP = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
BRREG_SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
BRREG_ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
BRREG_ACCOUNT_YEARS = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
BRREG_ACCOUNT_PDF = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"

_history_lock = threading.Lock()
_history_last_request = 0.0


def accounting_obligation_assessment(profile: dict[str, Any]) -> dict[str, Any]:
    """Classify the rule path without inventing a definitive exemption.

    Brreg's public rule is categorical for some forms and threshold/activity dependent for others.
    Registry employee counts are not treated as equivalent to statutory man-years or asset/revenue tests.
    """
    legal_form = str(profile.get("legal_form") or "").upper()
    latest = profile.get("latest_submitted_accounts")
    if latest:
        classification = "filing_observed"
        reason = "The registry snapshot reports a latest submitted annual-account year."
    elif legal_form in ALWAYS_ACCOUNTING_OBLIGED_FORMS:
        classification = "required_by_legal_form"
        reason = f"Brreg lists organisation form {legal_form} in an always-obliged category."
    elif legal_form in THRESHOLD_OR_ACTIVITY_FORMS:
        classification = "threshold_or_activity_dependent"
        reason = "Obligation depends on statutory size, partner, activity, tax or supervision tests not fully present in the open entity row."
    else:
        classification = "special_rule_or_review_required"
        reason = "The open entity row is insufficient for a definitive accounting-obligation decision."
    ruleset_hash = __import__("hashlib").sha256(ACCOUNTING_RULESET_VERSION.encode()).hexdigest()
    return evidence(
        "accounting_obligation",
        "available",
        "official_rule_interpretation",
        ACCOUNTING_OBLIGATION_SOURCE,
        value={
            "classification": classification,
            "legal_form": legal_form or None,
            "latest_submitted_accounts": latest or None,
            "reason": reason,
            "ruleset_version": ACCOUNTING_RULESET_VERSION,
        },
        as_of="2024-08-09",
        content_sha256=ruleset_hash,
        source_row_key=str(profile.get("organisation_number") or "") or None,
    )


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _period_months(period: Any) -> int | None:
    """Whole months covered by a Brreg regnskapsperiode {fraDato, tilDato}; None if unknown."""
    from datetime import date

    if not isinstance(period, dict):
        return None
    try:
        start = date.fromisoformat(str(period.get("fraDato"))[:10])
        end = date.fromisoformat(str(period.get("tilDato"))[:10])
    except ValueError:
        return None
    if end < start:
        return None
    months = (end.year - start.year) * 12 + (end.month - start.month) + (1 if end.day >= start.day - 1 else 0)
    return max(months, 0)


COMPARABLE_FIELDS = ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt")


def normalize_financials(body: Any, expected_latest_year: Any = None) -> dict[str, Any]:
    """Audit item 4.

    * `latest` is the company's OWN (SELSKAP) account with the most recent period end
      (tilDato); KONSERN (group) accounts are only used when no SELSKAP account exists.
      Brreg returns records oldest-first, so position in the list is never used.
    * If the registry says a newer year was filed (sisteInnsendteAarsregnskap) than the
      newest normalized record, `latest_is_current` is False and the gap is reported.
    * `comparison` only pairs the latest record with the immediately preceding record of
      the same account type AND the same period length in months; otherwise it is null
      with a reason (e.g. a 9-month short year is never compared with 12 months).
    """
    records = body if isinstance(body, list) else []
    if not records:
        return {"records": [], "latest": None, "comparison": None}
    normalized = []
    for item in records:
        period = item.get("regnskapsperiode") or {}
        normalized.append({
            "record_id": item.get("id"),
            "account_type": item.get("regnskapstype"),
            "period": period,
            "period_end": str(period.get("tilDato") or "")[:10] or None,
            "fiscal_year": (str(period.get("tilDato") or "")[:4] or None),
            "period_months": _period_months(period),
            "currency": item.get("valuta"),
            "revenue": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "operating_result": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsresultat"),
            "profit_before_tax": _get(item, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "annual_result": _get(item, "resultatregnskapResultat", "aarsresultat"),
            "assets": _get(item, "eiendeler", "sumEiendeler"),
            "equity": _get(item, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
            "debt": _get(item, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
        })
    own = [r for r in normalized if str(r.get("account_type") or "").upper() != "KONSERN"]
    pool = own or normalized
    pool = sorted(pool, key=lambda r: r.get("period_end") or "", reverse=True)
    others = [r for r in normalized if r not in pool]
    ordered = pool + sorted(others, key=lambda r: r.get("period_end") or "", reverse=True)
    latest = pool[0]
    result: dict[str, Any] = {
        "records": ordered[:6],
        "latest": latest,
        "latest_rule": "own (SELSKAP) account with the most recent period end; KONSERN only as fallback",
    }
    expected = str(expected_latest_year or "").strip()
    if expected.isdigit() and latest.get("fiscal_year"):
        result["registry_latest_submitted_year"] = expected
        result["latest_is_current"] = latest["fiscal_year"] >= expected
        if not result["latest_is_current"]:
            result["latest_gap_note"] = f"registry lists FY{expected} as filed but only FY{latest['fiscal_year']} figures were returned"
    previous = next((r for r in pool[1:] if (r.get("period_end") or "") < (latest.get("period_end") or "")), None)
    if previous is None:
        result["comparison"] = None
        result["comparison_note"] = "no earlier period of the same account type"
    elif not latest.get("period_months") or latest.get("period_months") != previous.get("period_months"):
        result["comparison"] = None
        result["comparison_note"] = (
            f"periods have different or unknown lengths ({latest.get('period_months')} vs {previous.get('period_months')} months); not compared"
        )
    elif latest.get("currency") != previous.get("currency"):
        result["comparison"] = None
        result["comparison_note"] = "different currencies; not compared"
    else:
        deltas = {}
        for key in COMPARABLE_FIELDS:
            a, b = latest.get(key), previous.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                deltas[key] = {"latest": a, "previous": b, "change": a - b, "change_pct": round((a - b) / abs(b) * 100, 1) if b else None}
        result["comparison"] = {
            "latest_period_end": latest.get("period_end"),
            "previous_period_end": previous.get("period_end"),
            "period_months": latest.get("period_months"),
            "account_type": latest.get("account_type"),
            "deltas": deltas,
        }
    return result


def normalize_financial_history(body: Any, org: str) -> dict[str, Any]:
    years = sorted({str(year) for year in body if str(year).isdigit()}) if isinstance(body, list) else []
    return {
        "years": years,
        "pdfs": [
            {"year": year, "url": BRREG_ACCOUNT_PDF.format(org=org, year=year)}
            for year in reversed(years)
        ],
    }


def _reserve_history_slot(clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Reserve starts at least 2.1 seconds apart without serializing response time."""
    global _history_last_request
    with _history_lock:
        delay = 2.1 - (clock() - _history_last_request)
        if delay > 0:
            sleeper(delay)
        _history_last_request = clock()


def _fetch_history(url: str) -> FetchResult:
    """Keep this endpoint below its observed 30-request-starts/minute allowance."""
    _reserve_history_slot()
    return fetch_json(url)


def normalize_roles(body: Any) -> dict[str, Any]:
    roles = []
    for group in body.get("rollegrupper", []) if isinstance(body, dict) else []:
        changed = group.get("sistEndret")
        for item in group.get("roller", []):
            person = item.get("person") or {}
            name = person.get("navn") or {}
            entity = item.get("enhet") or {}
            display_name = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")])) or entity.get("navn")
            roles.append({
                "name": display_name or None,
                "organisation_number": entity.get("organisasjonsnummer"),
                "role_code": _get(item, "type", "kode"),
                "role": _get(item, "type", "beskrivelse"),
                "group_code": _get(group, "type", "kode"),
                "group": _get(group, "type", "beskrivelse"),
                "last_changed": changed,
                "inactive": bool(item.get("avregistrert")),
            })
    return {"roles": roles}


def normalize_locations(body: Any) -> dict[str, Any]:
    rows = _get(body, "_embedded", "underenheter") or []
    return {"locations": [{
        "organisation_number": item.get("organisasjonsnummer"),
        "name": item.get("navn"),
        "address": item.get("beliggenhetsadresse") or item.get("postadresse"),
        "industry": item.get("naeringskode1"),
        "employees": item.get("antallAnsatte"),
    } for item in rows]}


def normalize_entity(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    return {
        "organisation_number": body.get("organisasjonsnummer"),
        "name": body.get("navn"),
        "legal_form": _get(body, "organisasjonsform", "kode"),
        "employees": body.get("antallAnsatte"),
        "bankrupt": body.get("konkurs"),
        "liquidating": body.get("underAvvikling"),
        "website": body.get("hjemmeside"),
        "email": body.get("epostadresse"),
        "industry": body.get("naeringskode1"),
        "business_address": body.get("forretningsadresse"),
        "postal_address": body.get("postadresse"),
        "latest_submitted_accounts": body.get("sisteInnsendteAarsregnskap"),
    }


def _classified(field: str, source_type: str, result: FetchResult, value: Any = None) -> dict[str, Any]:
    if result.status == 200:
        return evidence(field, "available", source_type, result.url, value=result.body if value is None else value, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)
    if result.status in {404, 410}:
        return evidence(field, "not_found", source_type, result.url, note=result.error, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)
    return evidence(field, "source_error", source_type, result.url, note=result.error, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)


def fetch_official_modules(org: str, modules: set[str], fetcher: Callable[[str], FetchResult] = fetch_json, expected_latest_year: Any = None) -> tuple[dict[str, Any], list[FetchResult]]:
    records: dict[str, Any] = {}
    metrics: list[FetchResult] = []
    endpoints = {
        "registry_live": (BRREG_ENTITY.format(org=org), "official_registry_live"),
        "financials": (BRREG_ACCOUNTS.format(org=org), "official_annual_accounts"),
        "financial_history": (BRREG_ACCOUNT_YEARS.format(org=org), "official_annual_account_copies"),
        "roles": (BRREG_ROLES.format(org=org), "official_roles"),
        "group": (BRREG_GROUP.format(org=org), "official_group_structure"),
        "locations": (BRREG_SUBUNITS.format(org=org), "official_subunits"),
    }
    for module, (url, source_type) in endpoints.items():
        if module not in modules:
            continue
        result = _fetch_history(url) if module == "financial_history" and fetcher is fetch_json else fetcher(url)
        metrics.append(result)
        normalized = None
        if result.status == 200:
            normalized = normalize_entity(result.body) if module == "registry_live" else normalize_financials(result.body, expected_latest_year) if module == "financials" else normalize_financial_history(result.body, org) if module == "financial_history" else normalize_roles(result.body) if module == "roles" else normalize_locations(result.body) if module == "locations" else result.body
        records[module] = _classified(module, source_type, result, value=normalized)
    return records, metrics
