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


def _period_bounds(period: Any) -> "tuple[Any, Any] | None":
    from datetime import date

    if not isinstance(period, dict):
        return None
    try:
        start = date.fromisoformat(str(period.get("fraDato"))[:10])
        end = date.fromisoformat(str(period.get("tilDato"))[:10])
    except ValueError:
        return None
    return (start, end) if end >= start else None


def _period_months(period: Any) -> int | None:
    """Whole months covered by a Brreg regnskapsperiode {fraDato, tilDato}, by the REAL boundaries.

    A period counts as N whole months only if the day after `tilDato` is exactly N calendar
    months after `fraDato` (2025-01-01..2025-12-31 -> 12). 2025-01-01..2025-12-01 is NOT
    12 months (it stops 30 days short) and returns None, so it is never compared. Month-end
    starts (e.g. 02-29/02-28 -> next 02-28) are aligned via the last-day-of-month rule.
    Leap years: 2024-01-01..2024-12-31 (366 days) and 2023-01-01..2023-12-31 (365 days) are
    both 12 whole months; the day difference is reported separately (`period_days`).
    """
    import calendar
    from datetime import timedelta

    bounds = _period_bounds(period)
    if bounds is None:
        return None
    start, end = bounds
    after = end + timedelta(days=1)
    months = (after.year - start.year) * 12 + (after.month - start.month)
    if months <= 0:
        return None
    start_is_month_end = start.day == calendar.monthrange(start.year, start.month)[1]
    after_is_month_end = after.day == calendar.monthrange(after.year, after.month)[1]
    if after.day == start.day or (start_is_month_end and after_is_month_end):
        return months
    return None


COMPARABLE_FIELDS = ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt")
OWN_ACCOUNT_TYPE = "SELSKAP"
GROUP_ACCOUNT_TYPE = "KONSERN"


def _version_key(record: dict[str, Any]) -> tuple[int, int]:
    """Deterministic version order for several filings of the same period (original vs correction).

    Brreg's `journalnr` is the registration journal number (later filing = higher number);
    the record `id` breaks ties. Higher wins.
    """
    def as_int(value: Any) -> int:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return int(digits) if digits else -1

    return as_int(record.get("journal_number")), as_int(record.get("record_id"))


def normalize_financials(body: Any, expected_latest_year: Any = None) -> dict[str, Any]:
    """Audit item 4.

    * Several filings for the same (account type, period) are reduced to ONE by a deterministic
      version rule (highest journalnr, then highest id); the others are listed as superseded.
    * `latest` is the company's OWN account (regnskapstype exactly SELSKAP) with the most recent
      period end. Missing/unknown account types are never treated as own accounts. Group
      (KONSERN) accounts are used only when no SELSKAP account exists, and are then labelled
      `basis="group_konsern"` everywhere they are output.
    * If the registry says a newer year was filed than the newest normalized record, the stale
      record is NOT published as latest (`latest` is None, `latest_is_current` False).
    * `comparison` pairs latest with the preceding record of the same account type, the same
      real period boundaries (whole months, same start month/day) and the same, present currency.
    """
    records = body if isinstance(body, list) else []
    if not records:
        return {"records": [], "latest": None, "comparison": None}
    normalized = []
    for item in records:
        period = item.get("regnskapsperiode") or {}
        bounds = _period_bounds(period)
        normalized.append({
            "record_id": item.get("id"),
            "journal_number": item.get("journalnr"),
            "account_type": item.get("regnskapstype"),
            "period": period,
            "period_start": str(period.get("fraDato") or "")[:10] or None,
            "period_end": str(period.get("tilDato") or "")[:10] or None,
            "fiscal_year": (str(period.get("tilDato") or "")[:4] or None),
            "period_months": _period_months(period),
            "period_days": ((bounds[1] - bounds[0]).days + 1) if bounds else None,
            "currency": item.get("valuta"),
            "revenue": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "operating_result": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsresultat"),
            "profit_before_tax": _get(item, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "annual_result": _get(item, "resultatregnskapResultat", "aarsresultat"),
            "assets": _get(item, "eiendeler", "sumEiendeler"),
            "equity": _get(item, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
            "debt": _get(item, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
        })
    # Version arbitration per (type, start, end).
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in normalized:
        key = (str(record.get("account_type") or "").upper(), record.get("period_start") or "", record.get("period_end") or "")
        groups.setdefault(key, []).append(record)
    chosen: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for items in groups.values():
        items = sorted(items, key=_version_key, reverse=True)
        winner = dict(items[0])
        if len(items) > 1:
            winner["superseded_versions"] = [{"record_id": r.get("record_id"), "journal_number": r.get("journal_number")} for r in items[1:]]
            superseded.extend(items[1:])
        chosen.append(winner)
    for record in chosen:
        kind = str(record.get("account_type") or "").upper()
        record["basis"] = "own_selskap" if kind == OWN_ACCOUNT_TYPE else "group_konsern" if kind == GROUP_ACCOUNT_TYPE else "unknown_type"
    def by_end(r: dict[str, Any]) -> tuple[str, tuple[int, int]]:
        return (r.get("period_end") or "", _version_key(r))
    own = sorted([r for r in chosen if r["basis"] == "own_selskap"], key=by_end, reverse=True)
    group = sorted([r for r in chosen if r["basis"] == "group_konsern"], key=by_end, reverse=True)
    unknown = sorted([r for r in chosen if r["basis"] == "unknown_type"], key=by_end, reverse=True)
    pool = own or group
    ordered = pool + [r for r in sorted(chosen, key=by_end, reverse=True) if r not in pool]
    result: dict[str, Any] = {
        "records": ordered[:6],
        "latest": None,
        "basis": None,
        "latest_rule": "own (regnskapstype=SELSKAP) account with the most recent period end; KONSERN only as labelled fallback; unknown types never used; one version per period (highest journalnr, then id)",
        "version_rule": "highest journalnr, then highest record id, per (account type, period start, period end)",
        "superseded_record_count": len(superseded),
    }
    if not pool:
        result["comparison"] = None
        result["latest_withheld_reason"] = "no account with a known type (SELSKAP or KONSERN); unknown or missing types are not treated as the company's own accounts"
        if unknown:
            result["unknown_type_record_count"] = len(unknown)
        return result
    latest = pool[0]
    result["basis"] = latest["basis"]
    if latest["basis"] == "group_konsern":
        result["basis_note"] = "Only group (KONSERN) accounts were returned; figures are consolidated group figures, not the company's own accounts."
    expected = str(expected_latest_year or "").strip()
    if expected.isdigit() and latest.get("fiscal_year"):
        result["registry_latest_submitted_year"] = expected
        result["latest_is_current"] = latest["fiscal_year"] >= expected
        if not result["latest_is_current"]:
            result["latest_gap_note"] = f"registry lists FY{expected} as filed but only FY{latest['fiscal_year']} figures were returned"
            result["stale_latest"] = latest
            result["latest_withheld_reason"] = "known-stale: a newer filed year exists; the older record is not published as latest"
            result["comparison"] = None
            result["comparison_note"] = "latest filed accounts not available in structured form; no comparison"
            return result
    result["latest"] = latest
    previous = next((r for r in pool[1:] if (r.get("period_end") or "") < (latest.get("period_start") or latest.get("period_end") or "")
                     and r.get("account_type") == latest.get("account_type")), None)
    same_boundaries = bool(previous) and (latest.get("period_start") or "")[5:] == (previous.get("period_start") or "")[5:] and (latest.get("period_end") or "")[5:] == (previous.get("period_end") or "")[5:]
    if previous is None:
        result["comparison"] = None
        result["comparison_note"] = "no earlier period of the same account type"
    elif not latest.get("period_months") or latest.get("period_months") != previous.get("period_months") or not same_boundaries:
        result["comparison"] = None
        result["comparison_note"] = (
            f"periods have different, non-whole-month or unknown lengths/boundaries ({latest.get('period_start')}..{latest.get('period_end')} = {latest.get('period_months')} vs "
            f"{previous.get('period_start')}..{previous.get('period_end')} = {previous.get('period_months')} months); not compared"
        )
    elif not latest.get("currency") or not previous.get("currency") or latest.get("currency") != previous.get("currency"):
        result["comparison"] = None
        result["comparison_note"] = "currency missing or different; not compared"
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
            "latest_period_days": latest.get("period_days"),
            "previous_period_days": previous.get("period_days"),
            "leap_day_difference": latest.get("period_days") != previous.get("period_days"),
            "account_type": latest.get("account_type"),
            "basis": latest.get("basis"),
            "currency": latest.get("currency"),
            "deltas": deltas,
        }
        if result["comparison"]["leap_day_difference"]:
            result["comparison_note"] = f"same whole-month period; day counts differ by leap day ({latest.get('period_days')} vs {previous.get('period_days')})"
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
    # Audit item 8: the financials staleness check must use the LIVE registry's
    # sisteInnsendteAarsregnskap, not the frozen bulk value. Fetch registry_live first.
    order = sorted(endpoints, key=lambda m: m != "registry_live")
    for module in order:
        url, source_type = endpoints[module]
        helper_only = module == "registry_live" and module not in modules and "financials" in modules
        if module not in modules and not helper_only:
            continue
        result = _fetch_history(url) if module == "financial_history" and fetcher is fetch_json else fetcher(url)
        metrics.append(result)
        normalized = None
        if result.status == 200:
            normalized = normalize_entity(result.body) if module == "registry_live" else normalize_financials(result.body, expected_latest_year) if module == "financials" else normalize_financial_history(result.body, org) if module == "financial_history" else normalize_roles(result.body) if module == "roles" else normalize_locations(result.body) if module == "locations" else result.body
        if module == "registry_live" and isinstance(normalized, dict):
            live_year = str(normalized.get("latest_submitted_accounts") or "").strip()
            frozen_year = str(expected_latest_year or "").strip()
            if live_year.isdigit() and (not frozen_year.isdigit() or live_year > frozen_year):
                expected_latest_year = live_year
        if helper_only:
            continue
        records[module] = _classified(module, source_type, result, value=normalized)
    return records, metrics
