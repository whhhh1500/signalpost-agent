#!/usr/bin/env python3
"""Simple local page: search a Norwegian company by name or orgnr and show facts with sources."""
from __future__ import annotations

import argparse
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.enrich import enrich_profile  # noqa: E402
from norway_company_agent.identity import website_identity_publishable  # noqa: E402
from norway_company_agent.resolve import resolve_company  # noqa: E402

PAGE_CSS = """
body{font:16px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#122}
main{max-width:920px;margin:0 auto;padding:24px}
h1{font-size:1.4rem;margin:0 0 8px}
.lead{color:#445;margin:0 0 18px}
form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
input[name=q]{flex:1;min-width:220px;padding:10px 12px;border:1px solid #ccd;border-radius:8px}
button{padding:10px 16px;border:0;border-radius:8px;background:#0b5;color:#fff;font-weight:600;cursor:pointer}
.card{background:#fff;border:1px solid #e2e5ea;border-radius:12px;padding:16px 18px;margin:0 0 14px}
.card h2{font-size:1.05rem;margin:0 0 10px}
.muted{color:#667}
.blank{color:#888;font-style:italic}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;vertical-align:top;padding:6px 8px;border-top:1px solid #eef}
th{width:28%;color:#456;font-weight:600}
a{color:#06c}
.tag{display:inline-block;background:#eef7f0;color:#163;border-radius:999px;padding:2px 8px;font-size:12px;margin-right:6px}
.err{background:#fff4f4;border-color:#f3c}
ul{margin:6px 0;padding-left:18px}
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _source_cell(record: dict | None) -> str:
    if not record:
        return '<span class="blank">—</span>'
    url = record.get("source_url") or ""
    status = record.get("status") or ""
    retrieved = record.get("retrieved_at") or ""
    note = record.get("note") or ""
    bits = [f'<span class="tag">{_esc(status)}</span>']
    if url:
        bits.append(f'<a href="{_esc(url)}" rel="noopener noreferrer" target="_blank">{_esc(url[:80])}</a>')
    if retrieved:
        bits.append(f'<span class="muted">{_esc(retrieved)}</span>')
    if note:
        bits.append(f'<div class="muted">{_esc(note)}</div>')
    return "<br>".join(bits)


def render_profile(profile: dict) -> str:
    evidence = profile.get("evidence") or {}
    website = evidence.get("website") or {}
    website_value = website.get("value") or {}
    publishable = website_identity_publishable(website)
    socials = website_value.get("social_links") if publishable else []
    discovery = evidence.get("website_discovery") or {}
    news = evidence.get("news") or {}
    news_items = ((news.get("value") or {}).get("items") or []) if news.get("status") == "available" else []
    jobs = evidence.get("nav_jobs") or {}
    job_rows = ((jobs.get("value") or {}).get("jobs") or []) if jobs.get("status") == "available" else []
    anns = evidence.get("brreg_announcements") or {}
    ann_rows = ((anns.get("value") or {}).get("announcements") or []) if anns.get("status") == "available" else []
    fins = evidence.get("financials") or {}
    latest = ((fins.get("value") or {}).get("latest") or {}) if fins.get("status") == "available" else {}

    website_url = ""
    if publishable:
        website_url = website_value.get("final_url") or website_value.get("requested_url") or profile.get("website") or ""
    website_reason = (website_value.get("identity_assessment") or {}).get("reason") or (discovery.get("value") or {}).get("reason") or ""

    rows = [
        ("Organisation number", _esc(profile.get("organisation_number"))),
        ("Legal name", _esc(profile.get("name"))),
        ("Legal form", _esc(profile.get("legal_form"))),
        ("Municipality", _esc(profile.get("municipality"))),
        ("Employees (registry)", _esc(profile.get("employees"))),
        ("Industry", _esc(f"{profile.get('industry_code') or ''} {profile.get('industry_label') or ''}".strip() or None)),
    ]
    body = ['<div class="card"><h2>Identity (Brreg)</h2><table>']
    for label, value in rows:
        body.append(f"<tr><th>{_esc(label)}</th><td>{value or '<span class=\"blank\">blank</span>'}</td></tr>")
    body.append(f"<tr><th>Source</th><td>{_source_cell(evidence.get('registry_live') or evidence.get('registry'))}</td></tr>")
    body.append("</table></div>")

    body.append('<div class="card"><h2>Official website</h2>')
    if website_url:
        body.append(f'<p><a href="{_esc(website_url)}" rel="noopener noreferrer" target="_blank">{_esc(website_url)}</a></p>')
        if website_reason:
            body.append(f'<p class="muted">Verified: {_esc(website_reason)}</p>')
        origin = profile.get("website_origin") or ("registry" if profile.get("website") else "unknown")
        body.append(f'<p><span class="tag">{_esc(origin)}</span></p>')
    else:
        body.append('<p class="blank">blank — no verified official website</p>')
    body.append(f"<p>{_source_cell(website if website else discovery)}</p></div>")

    body.append('<div class="card"><h2>Social pages</h2>')
    if socials:
        body.append("<ul>")
        for link in socials:
            body.append(
                f'<li><span class="tag">{_esc(link.get("platform"))}</span> '
                f'<a href="{_esc(link.get("url"))}" rel="noopener noreferrer" target="_blank">{_esc(link.get("url"))}</a></li>'
            )
        body.append("</ul>")
        body.append('<p class="muted">Only handles linked from a verified company website and matching the legal name.</p>')
    else:
        body.append('<p class="blank">blank — no verified social accounts</p>')
    body.append(f"<p>{_source_cell(website)}</p></div>")

    body.append('<div class="card"><h2>Recent news</h2>')
    if news_items:
        body.append("<ul>")
        for item in news_items:
            pub = item.get("publisher") or item.get("channel")
            when = item.get("published_at") or ""
            body.append(
                f'<li><a href="{_esc(item.get("url"))}" rel="noopener noreferrer" target="_blank">{_esc(item.get("title"))}</a>'
                f' <span class="muted">({_esc(pub)}{_esc(" · " + when if when else "")})</span></li>'
            )
        body.append("</ul>")
    else:
        body.append('<p class="blank">blank — no exact-entity news titles or company-site news pages</p>')
    body.append(f"<p>{_source_cell(news)}</p></div>")

    body.append('<div class="card"><h2>Open jobs (NAV)</h2>')
    if job_rows:
        body.append("<ul>")
        for job in job_rows:
            body.append(
                f'<li><a href="{_esc(job.get("url") or job.get("ad_url"))}" rel="noopener noreferrer" target="_blank">'
                f'{_esc(job.get("title"))}</a> <span class="muted">{_esc(job.get("employer_relation"))}</span></li>'
            )
        body.append("</ul>")
    else:
        body.append('<p class="blank">blank — no orgnr-verified ACTIVE single-position NAV ads</p>')
    body.append(f"<p>{_source_cell(jobs)}</p></div>")

    body.append('<div class="card"><h2>Company announcements (Brreg)</h2>')
    if ann_rows:
        body.append("<ul>")
        for row in ann_rows[:8]:
            body.append(
                f'<li>{_esc(row.get("date") or "")} · {_esc(row.get("type") or row.get("title") or "announcement")} '
                f'<a href="{_esc(row.get("url"))}" rel="noopener noreferrer" target="_blank">source</a></li>'
            )
        body.append("</ul>")
    else:
        body.append('<p class="blank">blank</p>')
    body.append(f"<p>{_source_cell(anns)}</p></div>")

    body.append('<div class="card"><h2>Latest accounts</h2>')
    if latest:
        body.append("<table>")
        for key in ("fiscal_year", "period_from", "period_to", "basis"):
            if latest.get(key) is not None:
                body.append(f"<tr><th>{_esc(key)}</th><td>{_esc(latest.get(key))}</td></tr>")
        body.append("</table>")
    else:
        reason = ((fins.get("value") or {}).get("latest_withheld_reason") if isinstance(fins.get("value"), dict) else None) or "blank"
        body.append(f'<p class="blank">{_esc(reason)}</p>')
    body.append(f"<p>{_source_cell(fins)}</p></div>")
    return "".join(body)


def render_page(query: str = "", result: dict | None = None, error: str | None = None) -> bytes:
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Signalpost company search</title>",
        f"<style>{PAGE_CSS}</style></head><body><main>",
        "<h1>Signalpost company search</h1>",
        "<p class='lead'>Resolve by organisation number or legal name, then show only verified facts with sources. Blank beats a wrong match.</p>",
        "<form method='get' action='/'><input name='q' value='" + _esc(query) + "' placeholder='e.g. 982942942 or BRANDMAKER AS' autofocus>",
        "<button type='submit'>Search</button></form>",
    ]
    if error:
        parts.append(f"<div class='card err'><strong>Error</strong><p>{_esc(error)}</p></div>")
    if result:
        status = result.get("status")
        if status == "ambiguous":
            parts.append("<div class='card'><h2>Several matches</h2><p class='muted'>Pick one organisation number (we do not auto-pick).</p><ul>")
            for match in result.get("matches") or []:
                org = match.get("organisation_number")
                parts.append(
                    f"<li><a href='/?q={_esc(org)}'>{_esc(org)}</a> — {_esc(match.get('name'))} "
                    f"<span class='muted'>{_esc(match.get('municipality') or '')}</span></li>"
                )
            parts.append("</ul></div>")
        elif status == "resolved" and result.get("profile"):
            parts.append(render_profile(result["profile"]))
        else:
            parts.append(f"<div class='card'><p class='blank'>{_esc(result.get('note') or status)}</p></div>")
    parts.append("<p class='muted'>Local demo UI for Signalpost. Sources are fetched live from Brreg, company sites, NAV and Google News RSS.</p>")
    parts.append("</main></body></html>")
    return "".join(parts).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "SignalpostSearch/0.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/", "/search"}:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        params = parse_qs(parsed.query)
        query = (params.get("q") or [""])[0].strip()
        want_json = (params.get("format") or [""])[0] == "json" or self.headers.get("Accept") == "application/json"
        result = None
        error = None
        if query:
            try:
                resolved = resolve_company(query)
                if resolved.get("status") == "resolved" and resolved.get("profile") is not None:
                    profile, _metric = enrich_profile(resolved["profile"])
                    resolved = {**resolved, "profile": profile}
                result = resolved
            except Exception as exc:  # fail soft for the UI
                error = f"{type(exc).__name__}: {exc}"
        if want_json:
            payload = {"query": query, "error": error, "result": result}
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        body = render_page(query=query, result=result, error=error)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Signalpost company search page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Open http://{args.host}:{args.port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
