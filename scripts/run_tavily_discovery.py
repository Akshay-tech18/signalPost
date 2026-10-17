#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.discovery import (
    BLOCKED_DISCOVERY_HOSTS,
    build_company_search_query,
    choose_search_candidate,
)
from norway_company_agent.evidence import evidence, utc_now
from norway_company_agent.identity import apply_website_identity_gate
from norway_company_agent.website import fetch_website

TAVILY_ENDPOINT = "https://api.tavily.com/search"

GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "yahoo.no", "yahoo.com", "online.no",
    "outlook.com", "live.no", "icloud.com", "me.com", "mac.com",
    "frisurf.no", "broadpark.no", "lyse.net", "ntebb.no", "enivest.net",
    "getmail.no", "vikenfiber.no", "altibox.no", "styrerommet.no",
    "styrepost.no", "obos.no", "bbl.no", "usbl.no",
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def extract_corporate_email_domain(profile: dict) -> str | None:
    reg_val = (profile.get("evidence", {}).get("registry") or {}).get("value") or {}
    email = reg_val.get("epostadresse") or reg_val.get("email") or profile.get("epostadresse")
    if email and "@" in str(email):
        domain = str(email).split("@")[1].strip().lower()
        if domain and "." in domain and domain not in GENERIC_EMAIL_DOMAINS:
            return domain
    return None


def is_non_commercial_entity(profile: dict) -> bool:
    legal_form = str(profile.get("legal_form") or "").upper()
    name = str(profile.get("name") or "").upper()
    if legal_form in {"BRL", "ESEK"}:
        return True
    if any(w in name for w in ("BORETTSLAG", "BURETTSLAG", "SAMEIET ", "SAMEIE ")):
        return True
    employees = profile.get("employees")
    if (employees is None or employees == 0) and any(w in name for w in ("HOLDING", "INVEST", "EIENDOM")):
        return True
    return False


def parse_tavily_web_results(payload: dict, *, query: str) -> list[dict]:
    results = payload.get("results") or []
    parsed = []
    for rank, result in enumerate(results, start=1):
        if not isinstance(result, dict) or not result.get("url"):
            continue
        parsed.append({
            "url": result.get("url"),
            "title": result.get("title") or "",
            "snippet": result.get("content") or "",
            "rank": rank,
            "provider": "tavily_search_api",
            "query": query,
        })
    return parsed


def tavily_search(profile: dict, api_key: str, *, timeout: float, count: int) -> tuple[list[dict], dict]:
    query = build_company_search_query(profile, mode="clean")
    payload_dict = {
        "query": query,
        "search_depth": "basic",
        "include_images": False,
        "include_answer": False,
        "max_results": count,
        "exclude_domains": sorted(BLOCKED_DISCOVERY_HOSTS),
    }
    data = json.dumps(payload_dict).encode("utf-8")

    request = urllib.request.Request(TAVILY_ENDPOINT, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
    })

    max_retries = 3
    backoff = 2.0
    for attempt in range(max_retries):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            payload = json.loads(raw)
            return parse_tavily_web_results(payload, query=query), {
                "status": response.status,
                "latency_ms": elapsed_ms,
                "bytes": len(raw),
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if exc.code == 429 and attempt < max_retries - 1:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            return [], {
                "status": exc.code,
                "latency_ms": elapsed_ms,
                "bytes": 0,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "error": type(exc).__name__,
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return [], {
                "status": getattr(exc, "code", 0),
                "latency_ms": elapsed_ms,
                "bytes": 0,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "error": type(exc).__name__,
            }

    return [], {
        "status": 500,
        "latency_ms": 0,
        "bytes": 0,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "error": "MaxRetriesExceeded",
    }


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transient Tavily discovery with native domain exclusions, free BRREG email pre-harvest, and exact-entity site crawling."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--limit", type=int, default=20, help="Maximum missing-website profiles to query via Tavily")
    parser.add_argument("--count", type=int, default=10, choices=range(1, 21), metavar="1..20")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--min-interval", type=float, default=0.1)
    parser.add_argument("--promote-verified", action="store_true", help="Copy exact-entity discovered sites into canonical website evidence")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output without repeating search calls")
    parser.add_argument("--skip-non-commercial", dest="skip_non_commercial", action="store_true", default=True, help="Skip housing co-ops and passive holding shells to save API credits (default: True)")
    parser.add_argument("--no-skip-non-commercial", dest="skip_non_commercial", action="store_false", help="Do not skip non-commercial entities")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without sending live API calls or crawler requests")
    parser.add_argument("--api-key-env", default="TAVILY_API_KEY")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not args.dry_run and not api_key:
        parser.error(f"Missing API key in environment variable {args.api_key_env}")

    rows = read_jsonl(Path(args.input))
    counts: Counter[str] = Counter()
    provider_latencies: list[int] = []
    started_at = utc_now()
    queried = 0

    existing_by_org: dict[str, dict] = {}
    if args.resume and Path(args.output).exists():
        for r in read_jsonl(Path(args.output)):
            existing_by_org[str(r.get("organisation_number"))] = r

    # Dry-Run Simulation Mode
    if args.dry_run:
        print("\n========================================================")
        print("🔍 TAVILY DISCOVERY PRE-FLIGHT DRY RUN (Zero API Calls)")
        print("========================================================")
        dry_web_present = 0
        dry_email_harvestable = []
        dry_non_commercial = []
        dry_tavily_needed = []

        for row in rows:
            org = str(row.get("organisation_number"))
            name = str(row.get("name") or "")
            if row.get("website"):
                dry_web_present += 1
                continue
            email_domain = extract_corporate_email_domain(row)
            if email_domain:
                dry_email_harvestable.append((org, name, email_domain))
                continue
            if args.skip_non_commercial and is_non_commercial_entity(row):
                dry_non_commercial.append((org, name, row.get("legal_form", "AS")))
                continue
            dry_tavily_needed.append((org, name, build_company_search_query(row, mode="clean")))

        print(f"Total input profiles:                 {len(rows)}")
        print(f"Already have website in registry:     {dry_web_present}")
        print(f"Free BRREG email corporate domains:   {len(dry_email_harvestable)} ($0 cost, 0 Tavily calls)")
        print(f"Non-commercial shells skipped:        {len(dry_non_commercial)} (Credits saved)")
        print(f"Tavily queries that would be sent:    {len(dry_tavily_needed)} (within limit {args.limit})")

        print("\nSample Free Corporate Email Candidates (Pre-harvested without Tavily):")
        for org, name, dom in dry_email_harvestable[:5]:
            print(f"  • {org}: {name} -> https://www.{dom}")

        print("\nSample Tavily Clean Search Queries (with exclude_domains active):")
        for org, name, q in dry_tavily_needed[:5]:
            print(f"  • {org}: {name} -> '{q}'")

        print("\n✅ Dry run completed successfully. 0 API credits used, 0 network requests dispatched.")
        return

    # Live Execution Mode
    for idx, row in enumerate(rows):
        org = str(row.get("organisation_number"))

        # 1. Resume check from cache
        if args.resume and org in existing_by_org:
            prev = existing_by_org[org]
            ev = prev.get("evidence", {})
            if "website_discovery" in ev or "website_discovered" in ev or (prev.get("website") and not row.get("website")):
                row.setdefault("evidence", {}).update(ev)
                if prev.get("website"):
                    row["website"] = prev["website"]
                counts["resumed_from_cache"] += 1
                continue

        # 2. Skip if website already present
        if row.get("website"):
            counts["registry_website_present_skipped"] += 1
            continue

        # 3. Free Pre-Harvest from BRREG corporate email domain
        corp_domain = extract_corporate_email_domain(row)
        if corp_domain:
            candidate_urls = [
                f"https://www.{corp_domain}",
                f"https://{corp_domain}",
                f"http://www.{corp_domain}",
                f"http://{corp_domain}",
            ]
            verified_site = None
            verified_assessment = None
            for cand_url in candidate_urls:
                try:
                    site, ops = fetch_website(cand_url, timeout=min(args.timeout, 8.0))
                    gated = apply_website_identity_gate(row, site)
                    if gated["assessment"] and gated["assessment"].get("publishable") and site.get("status") == "available":
                        verified_site = gated["website"]
                        verified_assessment = gated["assessment"]
                        counts["crawl_requests"] += ops.get("requests", 0)
                        break
                except Exception:
                    continue

            if verified_site and verified_assessment:
                verified_site["source_type"] = "registry_email_discovered_company_website"
                row.setdefault("evidence", {})["website_discovery"] = evidence(
                    "website_discovery",
                    "available",
                    "registry_email_domain_independent_crawl",
                    verified_site.get("source_url"),
                    value={
                        "provider": "brreg_epostadresse",
                        "discovered_domain": corp_domain,
                        "independent_page_url": verified_site.get("source_url"),
                    },
                    note="Verified directly from official BRREG corporate email domain; Tavily API call avoided.",
                )
                row["evidence"]["website_discovered"] = verified_site
                counts["registry_email_verified"] += 1
                counts["independent_crawls"] += 1
                counts["verified_sites"] += 1
                if args.promote_verified:
                    row["evidence"]["website"] = verified_site
                    row["website"] = verified_site.get("source_url")
                    counts["promoted_sites"] += 1
                continue

        # 4. Skip non-commercial entities to save API credits
        if args.skip_non_commercial and is_non_commercial_entity(row):
            row.setdefault("evidence", {})["website_discovery"] = evidence(
                "website_discovery",
                "not_found",
                "non_commercial_entity_filter",
                "local_policy",
                value={"legal_form": row.get("legal_form"), "name": row.get("name")},
                note="Non-commercial housing cooperative or passive holding shell; abstained to preserve search budget.",
            )
            counts["non_commercial_skipped"] += 1
            continue

        # 5. Check limit for paid Tavily queries
        if queried >= args.limit:
            break

        # 6. Execute Tavily search
        queried += 1
        results, operation = tavily_search(row, api_key, timeout=args.timeout, count=args.count)
        provider_latencies.append(operation["latency_ms"])
        counts["provider_requests"] += 1
        counts["provider_bytes"] += operation["bytes"]
        if operation.get("error"):
            counts["provider_errors"] += 1

        decision = choose_search_candidate(row, results)
        selected = decision.get("selected")
        discovery_summary = {
            "provider": "tavily_search_api",
            "query_sha256": operation["query_sha256"],
            "candidate_count": len(results),
            "selected_for_independent_crawl": bool(selected),
            "provider_status": operation["status"],
            "retention_policy": "Search titles, snippets, ranks, query text, and raw response are not persisted.",
        }

        if not selected:
            counts["abstained_before_crawl"] += 1
            row.setdefault("evidence", {})["website_discovery"] = evidence(
                "website_discovery",
                "not_found",
                "transient_tavily_search",
                TAVILY_ENDPOINT,
                value=discovery_summary,
                note="No result passed the deterministic crawl-candidate gate; raw search output was discarded.",
            )
            time.sleep(args.min_interval)
            continue

        # 7. Independent Exact-Entity Crawl
        website, web_ops = fetch_website(selected["url"], timeout=args.timeout)
        gated = apply_website_identity_gate(row, website)
        website = gated["website"]
        assessment = gated["assessment"]
        value = website.get("value") or {}

        website["source_type"] = "search_discovered_company_website"
        website["value"] = value
        row.setdefault("evidence", {})["website_discovery"] = evidence(
            "website_discovery",
            "available" if assessment and assessment.get("publishable") else "not_found",
            "transient_tavily_search_then_independent_crawl",
            TAVILY_ENDPOINT,
            value={**discovery_summary, "independent_page_url": website.get("source_url") if assessment and assessment.get("publishable") else None},
            note="Search output was transient. Publication depends only on independently fetched exact-entity page evidence.",
        )
        row["evidence"]["website_discovered"] = website
        counts["independent_crawls"] += 1
        counts["crawl_requests"] += web_ops.get("requests", 0)

        if assessment and assessment.get("publishable") and website.get("status") == "available":
            counts["verified_sites"] += 1
            if args.promote_verified:
                row["evidence"]["website"] = website
                row["website"] = website.get("source_url") or selected["url"]
                counts["promoted_sites"] += 1
        else:
            counts["quarantined_sites"] += 1

        time.sleep(args.min_interval)

        # Incremental checkpoint flush
        if queried % 5 == 0:
            write_jsonl(Path(args.output), rows)

    # Final guaranteed write of all 1,000 rows
    write_jsonl(Path(args.output), rows)

    report_path = Path(args.report)
    if queried == 0 and report_path.exists():
        try:
            prev_report = json.loads(report_path.read_text(encoding="utf-8"))
            report = {
                **prev_report,
                "resumed_from_cache": True,
                "last_checked_at": utc_now(),
                "counts": {**prev_report.get("counts", {}), **dict(counts)},
            }
        except Exception:
            report = {
                "generated_at": utc_now(),
                "started_at": started_at,
                "provider": "Tavily Search API",
                "provider_endpoint": TAVILY_ENDPOINT,
                "input_profiles": len(rows),
                "queried_missing_website_profiles": queried,
                "counts": dict(counts),
                "provider_latency_ms": {"p50": percentile(provider_latencies, 0.5), "p95": percentile(provider_latencies, 0.95)},
                "raw_search_results_persisted": False,
                "promote_verified_enabled": args.promote_verified,
                "qualification": "not_evaluated_on_500_org_external_final_corpus",
            }
    else:
        report = {
            "generated_at": utc_now(),
            "started_at": started_at,
            "provider": "Tavily Search API",
            "provider_endpoint": TAVILY_ENDPOINT,
            "input_profiles": len(rows),
            "queried_missing_website_profiles": queried,
            "counts": dict(counts),
            "provider_latency_ms": {"p50": percentile(provider_latencies, 0.5), "p95": percentile(provider_latencies, 0.95)},
            "raw_search_results_persisted": False,
            "promote_verified_enabled": args.promote_verified,
            "qualification": "not_evaluated_on_500_org_external_final_corpus",
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
