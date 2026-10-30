# Signalpost submission notes

Built on the official Builderr Signalpost starter kit. Main change: an official-website
discovery layer for companies whose Brreg record has no homepage (about 89% of the
public universe).

## What changed
- `src/norway_company_agent/site_discovery.py`: generates candidate domains from the
  company name (.no/.com, Norwegian letter transliteration, legal-suffix stripping,
  max 8 hosts), fetches the home page plus /kontakt, /om-oss, /kontakt-oss, and accepts a
  site only when (1) the page shows this company's organisation number, or (2) every name
  token matches AND the registered postcode/street matches AND no other organisation
  number appears. Cross-domain redirects (e.g. to Facebook) are rejected. Holding /
  real-estate shells (NACE 64.2 / 68.x) are skipped to save requests. Every accepted
  site carries URL, fetch time, sha256, reason and the candidates tried.
- Wired into `scripts/run_competition_batch.py` (`--discover-websites`, default on);
  discovered sites then go through the existing website fetch and identity gate with
  `source_type = discovered_company_website`.
- `tests/test_site_discovery.py`; full suite: 108 passed, 5 subtests passed.

## Run command
```
uv sync
uv run python scripts/run_competition_batch.py \
  --organisations <batch.jsonl> --bulk <brreg-enheter.csv> \
  --profiles-output out/profiles.jsonl --output out/envelopes.jsonl \
  --report out/report.json --run-id official-001 --workers 4
```

## 100-company smoke test (2026-10-17)
`out/smoke100-report.json`, `out/smoke100-envelopes.jsonl`, `out/smoke100-profiles.jsonl`
(batch: `select_entry_batch.py --count 100`, default seed). 100/100 terminal envelopes,
zero silent drops, validation passed, 1,122 requests, ~4 minutes with 4 workers.

## Models / APIs / licences
No LLMs and no paid APIs. Public Brreg open data (NLOD) and the companies' own public
websites, fetched politely with rate limits. Expected cost per official batch: USD 0
(compute only).

Note: `SIGNALPOST_ALLOW_FAKE_IP_DNS=1` is a local-only switch for sandboxes whose DNS
returns 198.18.0.0/15 proxy addresses; it is off by default and not needed for evaluation.

## Revision: NAV jobs + Brreg kunngjøringer (2026-10-17)
- `src/norway_company_agent/nav_jobs.py`: light fail-soft matcher. Searches
  `arbeidsplassen.nav.no/stillinger/api/search` by legal name, then verifies each
  candidate via the public NAV feed entry (`pam-stilling-feed.nav.no`) so only
  ACTIVE ads with exact `employer.orgnr` are published. Finn-only ads (404 on the
  feed) are skipped, never published on name match alone. Uses the rotating public
  Bearer token from `/api/publicToken` (no spend).
- `src/norway_company_agent/brreg_announcements.py`: fetches the public Brreg
  kunngjøring HTML list by orgnr (`w2.brreg.no/kunngjoring/hent_nr.jsp`). No open
  JSON kunngjøringer API exists; paid XML SFTP is out of scope. Parses date/type/URL
  with response sha256.
- Wired into `scripts/run_competition_batch.py` as modules `nav_jobs` and
  `brreg_announcements` (default on; `--no-nav-jobs` / `--no-brreg-announcements`).
- Smoke20 (`smoke20.jsonl`, `--no-discover-websites`, 2 workers): 20/20 envelopes,
  validation passed, 144 requests, ~74s. Brreg announcements available for 20/20
  (435 rows). NAV jobs not_found for all 20 (holding-heavy sample); live check on
  Que Pasa AS `927212021` returned 2 ACTIVE orgnr-verified ads.

## Revision: NAV sub-unit matching + test path fix (2026-10-18)
- `nav_jobs.py`: NAV ads usually name the employer by its *sub-unit* (underenhet)
  number, not the legal entity, so exact legal-entity matching found almost nothing.
  Ads are now also accepted when `employer.orgnr` is one of the company's sub-units
  already returned by the Brreg `underenheter?overordnetEnhet=<orgnr>` call in the
  `locations` module (no extra requests). Each job carries
  `employer_relation = legal_entity | registered_subunit`; anything else is still rejected.
- On 40 random companies with 20+ employees: 0/40 → 3/40 with verified active ads
  (6 ads, all hand-checked correct: ColliCare Logistics, Leirvik, Norges arktiske
  studentsamskipnad).
- `tests/conftest.py` puts `src/` on the path so `uv run --with pytest pytest -q`
  collects every test file cleanly: 116 passed, 5 subtests passed.
- 100-company smoke test re-run with all modules on (`out/smoke100-*`, run id
  `smoke100-rev-20261018`, same default-seed batch): 100/100 terminal envelopes,
  validation passed, 1,314 requests, ~5 minutes with 4 workers, p95 1.5 s.

## 2026-10-24 hardening

- **Jobs validated on real NAV ads**: 45 Norwegian employers with live ads (8 to 8,000+ employees; municipalities, hospitals, staffing, retail, small cafés). 62 ads published: all live-checked ACTIVE, positioncount=1, and employer orgnr confirmed in Brreg as the company or its underenhet. All 43 rejected ads were justified (multi-position, recruitment pool/tilkallingsvikarer incl. nynorsk, or deadline passed). Locked in as `tests/fixtures/nav_real_ads_2026-10-24.json` + `tests/test_nav_real_ads.py`. Search now drops the legal suffix and falls back to short name queries; the crude slash rule (which rejected "Servicetekniker / montør") was replaced by head-count and pool-word rules; identical re-posts are collapsed.
- **Website side evidence** for sites that never print org.nr: the `.no` domain must be the full legal-name core, the page must name the exact legal entity (or the Brreg e-mail domain matches), and the page must name no other legal entity (other org.nr, JSON-LD legalName, "© Other AS"). Homepage fetch gets one slower retry. 100-company smoke rerun: publishable websites 6 -> 7 (only BRANDMAKER AS, brandmaker.no, added); no other change.
- Full suite: 241 passed, 5 subtests passed.


## Audit checklist response (Soham 2026-10-23)

Recorded version at audit time: `fe23684` (v2/5). Hardening since then is on `main` at
`563ce4e8e9ec22cd8f8cf0eef4b2ebaa854a03c2` (gate version `signalpost-gates-v6-2026-10-24`).
Regression suite: `tests/test_audit_*.py`, `tests/test_nav_real_ads.py` — 286 passed.

1. **Own website, not parent/brand** — `assess_website_identity` / `apply_website_identity_gate`
   only publish a site when operator-bound orgnr or strict legal-name + address evidence
   points to *this* entity. Parent/sister/brand operator signals, cross-domain redirects
   without our orgnr, and ambiguous multi-entity pages fail closed (website withheld;
   social/news/jobs that depended on that site are not published for the subsidiary).
2. **Own social accounts** — `assess_social_identity` requires whole-handle legal-name match
   against the company (not a parent, agency, or theme slug). Footer links that fail the
   gate are dropped; shared/ambiguous candidates are not published.
3. **Single current job ads** — `nav_jobs.single_current_ad_check` keeps only ACTIVE,
   non-expired ads with `positioncount=1`, a canonical NAV ad URL (not careers/search
   landings), and `employer.orgnr` equal to the legal entity or a Brreg-registered
   underenhet. Aggregate/pool titles and parent vacancies are rejected.
4. **Latest accounts, equal periods** — `normalize_financials` picks the newest own
   (`SELSKAP`) filing as `latest` (withhold when FY expected is missing/conflicted);
   comparisons require the same whole-month period length, same start month/day, and
   same currency, older → newer. Same-period version conflicts without journalnr/id
   are withheld. Prototype display is fail-closed on ungated/legacy financials.

Payout note received: cash prizes via Wise (any Wise-supported country/currency).
