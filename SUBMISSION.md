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
