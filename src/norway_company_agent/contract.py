from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ALLOWED_AVAILABILITY_STATES = {
    "available",
    "not_available",
    "blocked",
    "not_applicable",
    "ambiguous",
    "failed",
}


def _ensure_sha256(value: str | None, fallback_payload: Any) -> str:
    """Ensure a valid 64-character hexadecimal SHA-256 digest is returned."""
    if value and len(str(value)) == 64 and all(c in "0123456789abcdefABCDEF" for c in str(value)):
        return str(value).lower()
    encoded = json.dumps(fallback_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _format_financial_claim_span(org: str, period: Any, record: dict[str, Any]) -> str:
    parts = [f"Foretak {org}"]
    if period:
        fra = period.get("fraDato") if isinstance(period, dict) else None
        til = period.get("tilDato") if isinstance(period, dict) else None
        if fra and til:
            parts.append(f"Regnskapsperiode {fra} til {til}")
    rev = record.get("revenue")
    curr = record.get("currency") or "NOK"
    if rev is not None:
        parts.append(f"Salgsinntekter: {rev} {curr}")
    op = record.get("operating_result")
    if op is not None:
        parts.append(f"Driftsresultat: {op} {curr}")
    res = record.get("annual_result")
    if res is not None:
        parts.append(f"Årsresultat: {res} {curr}")
    assets = record.get("assets")
    if assets is not None:
        parts.append(f"Sum eiendeler: {assets} {curr}")
    debt = record.get("debt")
    if debt is not None:
        parts.append(f"Sum gjeld: {debt} {curr}")
    return "; ".join(parts)


def build_submission_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    third_party_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Serialize an enriched company profile into the minimal output contract JSON."""
    org = str(profile.get("organisation_number") or "").strip()
    evidence_dict = profile.get("evidence") or {}

    evidence_list: list[dict[str, Any]] = []
    evidence_id_set: set[str] = set()

    def add_evidence(ev_id: str, source_url: str, source_class: str, retrieved_at: str, sha256: str, claim_span: str) -> str:
        if ev_id not in evidence_id_set:
            evidence_list.append({
                "id": ev_id,
                "source_url": source_url,
                "source_class": source_class,
                "retrieved_at": retrieved_at,
                "content_sha256": sha256,
                "claim_span": str(claim_span).strip() or f"Evidence anchor for {org}",
            })
            evidence_id_set.add(ev_id)
        return ev_id

    claims: list[dict[str, Any]] = []

    # 1. Base Registry Evidence & Identity Claims
    reg_ev = evidence_dict.get("registry") or evidence_dict.get("registry_live") or {}
    reg_retrieved = reg_ev.get("retrieved_at") or started_at
    reg_url = reg_ev.get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
    reg_sha256 = _ensure_sha256(reg_ev.get("content_sha256"), f"registry:{org}:{profile.get('name')}")
    reg_span = f"{profile.get('name') or 'Enhet'}, organisasjonsnummer {org}, form {profile.get('legal_form') or 'ukjent'}, kommune {profile.get('municipality') or 'ukjent'}"
    add_evidence("ev-registry", reg_url, "official_registry", reg_retrieved, reg_sha256, reg_span)

    claims.append({
        "field": "organisation_number",
        "value": org,
        "availability": "available",
        "confidence": 1.0,
        "evidence_ids": ["ev-registry"],
    })
    claims.append({
        "field": "legal_name",
        "value": profile.get("name"),
        "availability": "available" if profile.get("name") else "not_available",
        "confidence": 1.0,
        "evidence_ids": ["ev-registry"],
    })
    claims.append({
        "field": "legal_form",
        "value": profile.get("legal_form"),
        "availability": "available" if profile.get("legal_form") else "not_available",
        "confidence": 1.0,
        "evidence_ids": ["ev-registry"],
    })
    claims.append({
        "field": "registered_municipality",
        "value": profile.get("municipality"),
        "availability": "available" if profile.get("municipality") else "not_available",
        "confidence": 1.0,
        "evidence_ids": ["ev-registry"],
    })
    claims.append({
        "field": "registered_employees",
        "value": profile.get("employees"),
        "availability": "available" if profile.get("employees") is not None else "not_available",
        "confidence": 1.0,
        "evidence_ids": ["ev-registry"],
    })

    # 2. Accounting Obligation
    acc_ev = evidence_dict.get("accounting_obligation") or {}
    if acc_ev:
        acc_retrieved = acc_ev.get("retrieved_at") or started_at
        acc_url = acc_ev.get("source_url") or "https://www.brreg.no/en/submission-of-annual-accounts/"
        acc_sha = _ensure_sha256(acc_ev.get("content_sha256"), f"accounting_obligation:{org}")
        acc_val = acc_ev.get("value") or {}
        acc_span = f"Regnskapsplikt: {acc_val.get('classification')}; Årsak: {acc_val.get('reason')}"
        add_evidence("ev-accounting-obligation", acc_url, "official_rule", acc_retrieved, acc_sha, acc_span)
        claims.append({
            "field": "accounting_obligation",
            "value": acc_val.get("classification"),
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": ["ev-accounting-obligation"],
        })

    # 3. Official Financials
    fin_ev = evidence_dict.get("financials") or {}
    fin_status = fin_ev.get("status")
    if fin_status == "available":
        fin_val = fin_ev.get("value") or {}
        records = fin_val.get("records") or []
        if records:
            latest = records[0]
            fin_retrieved = fin_ev.get("retrieved_at") or started_at
            fin_url = fin_ev.get("source_url") or f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
            fin_sha = _ensure_sha256(fin_ev.get("content_sha256"), latest)
            fin_span = _format_financial_claim_span(org, latest.get("period"), latest)
            add_evidence("ev-financials", fin_url, "official_annual_accounts", fin_retrieved, fin_sha, fin_span)

            for key in ("revenue", "operating_result", "annual_result", "assets", "debt"):
                val = latest.get(key)
                claims.append({
                    "field": key,
                    "value": val,
                    "availability": "available" if val is not None else "not_available",
                    "confidence": 1.0,
                    "evidence_ids": ["ev-financials"],
                })
        else:
            for key in ("revenue", "operating_result", "annual_result", "assets", "debt"):
                claims.append({
                    "field": key,
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [],
                })
    elif fin_status == "blocked":
        for key in ("revenue", "operating_result", "annual_result", "assets", "debt"):
            claims.append({
                "field": key,
                "value": None,
                "availability": "blocked",
                "confidence": 1.0,
                "evidence_ids": [],
            })
    elif fin_status == "source_error":
        for key in ("revenue", "operating_result", "annual_result", "assets", "debt"):
            claims.append({
                "field": key,
                "value": None,
                "availability": "failed",
                "confidence": 1.0,
                "evidence_ids": [],
            })
    else:
        # Not found or not applicable
        acc_class = (acc_ev.get("value") or {}).get("classification")
        avail = "not_applicable" if acc_class in {"special_rule_or_review_required"} else "not_available"
        for key in ("revenue", "operating_result", "annual_result", "assets", "debt"):
            claims.append({
                "field": key,
                "value": None,
                "availability": avail,
                "confidence": 1.0,
                "evidence_ids": [],
            })

    # 4. Roles
    roles_ev = evidence_dict.get("roles") or {}
    roles_status = roles_ev.get("status")
    if roles_status == "available":
        roles_val = roles_ev.get("value") or {}
        roles_list = roles_val.get("roles") or []
        active_roles = [r for r in roles_list if not r.get("inactive")]
        roles_url = roles_ev.get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
        roles_retrieved = roles_ev.get("retrieved_at") or started_at
        roles_sha = _ensure_sha256(roles_ev.get("content_sha256"), roles_list)
        roles_span = ", ".join(f"{r.get('role', 'Rolle')}: {r.get('name')}" for r in active_roles[:8]) or "Ingen aktive roller"
        add_evidence("ev-roles", roles_url, "official_roles", roles_retrieved, roles_sha, roles_span)
        claims.append({
            "field": "roles",
            "value": active_roles,
            "availability": "available" if active_roles else "not_available",
            "confidence": 1.0,
            "evidence_ids": ["ev-roles"],
        })
    elif roles_status == "source_error":
        claims.append({
            "field": "roles",
            "value": [],
            "availability": "failed",
            "confidence": 1.0,
            "evidence_ids": [],
        })
    else:
        claims.append({
            "field": "roles",
            "value": [],
            "availability": "not_available",
            "confidence": 1.0,
            "evidence_ids": [],
        })

    # 5. Operating Locations / Subunits
    loc_ev = evidence_dict.get("locations") or {}
    loc_status = loc_ev.get("status")
    if loc_status == "available":
        loc_val = loc_ev.get("value") or {}
        locations_list = loc_val.get("locations") or []
        loc_url = loc_ev.get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
        loc_retrieved = loc_ev.get("retrieved_at") or started_at
        loc_sha = _ensure_sha256(loc_ev.get("content_sha256"), locations_list)
        loc_span = ", ".join(f"{loc.get('name')} ({loc.get('organisation_number')})" for loc in locations_list[:8]) or "Ingen underenheter"
        add_evidence("ev-locations", loc_url, "official_subunits", loc_retrieved, loc_sha, loc_span)
        claims.append({
            "field": "locations",
            "value": locations_list,
            "availability": "available" if locations_list else "not_available",
            "confidence": 1.0,
            "evidence_ids": ["ev-locations"],
        })
    elif loc_status == "source_error":
        claims.append({
            "field": "locations",
            "value": [],
            "availability": "failed",
            "confidence": 1.0,
            "evidence_ids": [],
        })
    else:
        claims.append({
            "field": "locations",
            "value": [],
            "availability": "not_available",
            "confidence": 1.0,
            "evidence_ids": [],
        })

    # 6. Official Website & Social Links
    web_ev = evidence_dict.get("website") or {}
    web_status = web_ev.get("status")
    web_val = web_ev.get("value") or {}
    ident = web_val.get("identity_assessment") or {}
    is_publishable = bool(ident.get("publishable"))

    if web_status == "available":
        web_url = web_ev.get("source_url") or web_val.get("final_url") or web_val.get("requested_url") or ""
        web_retrieved = web_ev.get("retrieved_at") or started_at
        web_sha = _ensure_sha256(web_ev.get("content_sha256"), web_val)
        web_title = web_val.get("title") or ""
        web_desc = web_val.get("description") or ""
        web_span = f"Title: {web_title}. Description: {web_desc}" if (web_title or web_desc) else (web_val.get("main_text_excerpt") or "")[:200]
        add_evidence("ev-website", web_url, "company_owned", web_retrieved, web_sha, web_span)

        if is_publishable:
            claims.append({
                "field": "official_website",
                "value": web_url,
                "availability": "available",
                "confidence": 0.99,
                "evidence_ids": ["ev-website"],
            })
            socials = web_val.get("social_links") or []
            claims.append({
                "field": "social_profiles",
                "value": socials,
                "availability": "available" if socials else "not_available",
                "confidence": 0.95,
                "evidence_ids": ["ev-website"],
            })
        else:
            # Ambiguous entity match on fetched domain
            claims.append({
                "field": "official_website",
                "value": None,
                "availability": "ambiguous",
                "confidence": 0.85,
                "evidence_ids": ["ev-website"],
            })
            claims.append({
                "field": "social_profiles",
                "value": [],
                "availability": "ambiguous",
                "confidence": 0.85,
                "evidence_ids": ["ev-website"],
            })
    elif web_status == "blocked":
        blocked_url = web_ev.get("source_url") or str(profile.get("website") or "")
        blocked_sha = _ensure_sha256(web_ev.get("content_sha256"), f"blocked:{blocked_url}")
        add_evidence("ev-website", blocked_url or "https://example.test", "company_owned", started_at, blocked_sha, "Website fetch was blocked by robots.txt or access policy.")
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "blocked",
            "confidence": 1.0,
            "evidence_ids": ["ev-website"],
        })
        claims.append({
            "field": "social_profiles",
            "value": [],
            "availability": "blocked",
            "confidence": 1.0,
            "evidence_ids": ["ev-website"],
        })
    elif web_status == "source_error":
        err_url = web_ev.get("source_url") or str(profile.get("website") or "")
        err_sha = _ensure_sha256(web_ev.get("content_sha256"), f"error:{err_url}")
        add_evidence("ev-website", err_url or "https://example.test", "company_owned", started_at, err_sha, web_ev.get("note") or "Website fetch failed.")
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "failed",
            "confidence": 1.0,
            "evidence_ids": ["ev-website"],
        })
        claims.append({
            "field": "social_profiles",
            "value": [],
            "availability": "failed",
            "confidence": 1.0,
            "evidence_ids": ["ev-website"],
        })
    else:
        # Not found / not registered
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "not_available",
            "confidence": 1.0,
            "evidence_ids": [],
        })
        claims.append({
            "field": "social_profiles",
            "value": [],
            "availability": "not_available",
            "confidence": 1.0,
            "evidence_ids": [],
        })

    # 7. Extracted Jobs (website_jobs)
    jobs_ev = evidence_dict.get("website_jobs") or {}
    jobs_val = jobs_ev.get("value") or {}
    jobs_list = jobs_val.get("jobs") or []
    if jobs_list and is_publishable:
        jobs_url = jobs_ev.get("source_url") or web_ev.get("source_url") or ""
        jobs_sha = _ensure_sha256(jobs_ev.get("content_sha256"), jobs_list)
        jobs_span = "; ".join(f"{j.get('title')}: {j.get('claim_span')}" for j in jobs_list[:5])
        add_evidence("ev-website-jobs", jobs_url, "company_owned", jobs_ev.get("retrieved_at") or started_at, jobs_sha, jobs_span)
        claims.append({
            "field": "jobs",
            "value": jobs_list,
            "availability": "available",
            "confidence": 0.90,
            "evidence_ids": ["ev-website-jobs"],
        })
    else:
        claims.append({
            "field": "jobs",
            "value": [],
            "availability": "not_available" if is_publishable else "not_applicable",
            "confidence": 0.90,
            "evidence_ids": [],
        })

    # 8. Extracted People (website_people)
    people_ev = evidence_dict.get("website_people") or {}
    people_val = people_ev.get("value") or {}
    people_list = people_val.get("people") or []
    if people_list and is_publishable:
        people_url = people_ev.get("source_url") or web_ev.get("source_url") or ""
        people_sha = _ensure_sha256(people_ev.get("content_sha256"), people_list)
        people_span = "; ".join(f"{p.get('name')} ({p.get('role')}): {p.get('claim_span')}" for p in people_list[:5])
        add_evidence("ev-website-people", people_url, "company_owned", people_ev.get("retrieved_at") or started_at, people_sha, people_span)
        claims.append({
            "field": "key_personnel",
            "value": people_list,
            "availability": "available",
            "confidence": 0.90,
            "evidence_ids": ["ev-website-people"],
        })
    else:
        claims.append({
            "field": "key_personnel",
            "value": [],
            "availability": "not_available" if is_publishable else "not_applicable",
            "confidence": 0.90,
            "evidence_ids": [],
        })

    # 9. Changes
    changes = profile.get("change_history") or []

    # 10. Errors
    errors: list[str] = []
    crawl_errors = web_val.get("crawl_errors") or []
    for err in crawl_errors:
        if isinstance(err, dict):
            errors.append(f"Crawl error on {err.get('url')}: {err.get('error')}")
        else:
            errors.append(str(err))
    for mod_name, mod_data in evidence_dict.items():
        if isinstance(mod_data, dict) and mod_data.get("status") == "source_error":
            note = mod_data.get("note")
            if note:
                errors.append(f"{mod_name}: {note}")

    # 11. Operations metrics
    run_metrics = profile.get("run_metrics") or {}
    latencies = run_metrics.get("latencies_ms") or []
    runtime_ms = sum(latencies) if latencies else 0
    requests_count = int(run_metrics.get("requests") or 0)
    cost = float(run_metrics.get("third_party_cost_usd") or third_party_cost_usd)

    operations = {
        "requests": requests_count,
        "runtime_ms": runtime_ms,
        "third_party_cost_usd": round(cost, 6),
    }

    # 12. Determine terminal status
    has_fatal_error = reg_ev.get("status") == "source_error"
    terminal_status = "failed" if has_fatal_error else "completed"

    envelope = {
        "organisation_number": org,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": terminal_status,
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": changes,
        "errors": errors,
        "operations": operations,
    }

    return envelope


def validate_output_contract_envelope(envelope: dict[str, Any]) -> list[str]:
    """Validate a single envelope strictly against OUTPUT_CONTRACT.md rules.

    Returns a list of violation messages (empty list if valid).
    """
    violations: list[str] = []

    # Required top-level keys
    required_keys = {"organisation_number", "run", "claims", "evidence", "changes", "errors", "operations"}
    missing_keys = required_keys - set(envelope.keys())
    if missing_keys:
        violations.append(f"Missing required envelope keys: {sorted(missing_keys)}")

    # 1. organisation_number
    org = str(envelope.get("organisation_number") or "")
    if len(org) != 9 or not org.isdigit():
        violations.append(f"Invalid organisation_number: {org!r} (must be 9 digits)")

    # 2. run
    run = envelope.get("run")
    if not isinstance(run, dict):
        violations.append("Field 'run' must be a JSON object")
    else:
        for rk in ("run_id", "started_at", "completed_at", "terminal_status"):
            if not run.get(rk):
                violations.append(f"Missing run field: {rk!r}")

    # 3. evidence
    evidence = envelope.get("evidence")
    evidence_ids: set[str] = set()
    if not isinstance(evidence, list):
        violations.append("Field 'evidence' must be a list")
    else:
        for idx, ev in enumerate(evidence):
            if not isinstance(ev, dict):
                violations.append(f"Evidence item #{idx} must be an object")
                continue
            ev_id = ev.get("id")
            if not ev_id or not isinstance(ev_id, str):
                violations.append(f"Evidence item #{idx} missing string 'id'")
            elif ev_id in evidence_ids:
                violations.append(f"Duplicate evidence id: {ev_id!r}")
            else:
                evidence_ids.add(ev_id)

            if not ev.get("source_url"):
                violations.append(f"Evidence item {ev_id!r} missing 'source_url'")
            if not ev.get("source_class"):
                violations.append(f"Evidence item {ev_id!r} missing 'source_class'")
            if not ev.get("retrieved_at"):
                violations.append(f"Evidence item {ev_id!r} missing 'retrieved_at'")

            sha = str(ev.get("content_sha256") or "")
            if len(sha) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sha):
                violations.append(f"Evidence item {ev_id!r} invalid content_sha256 (must be 64-char hex, got {sha!r})")

            span = str(ev.get("claim_span") or "").strip()
            if not span:
                violations.append(f"Evidence item {ev_id!r} missing or empty 'claim_span'")

    # 4. claims
    claims = envelope.get("claims")
    if not isinstance(claims, list):
        violations.append("Field 'claims' must be a list")
    else:
        for idx, cl in enumerate(claims):
            if not isinstance(cl, dict):
                violations.append(f"Claim #{idx} must be an object")
                continue
            field = cl.get("field")
            if not field or not isinstance(field, str):
                violations.append(f"Claim #{idx} missing string 'field'")

            avail = cl.get("availability")
            if avail not in ALLOWED_AVAILABILITY_STATES:
                violations.append(f"Claim {field!r} has invalid availability {avail!r} (must be one of {sorted(ALLOWED_AVAILABILITY_STATES)})")

            conf = cl.get("confidence")
            if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
                violations.append(f"Claim {field!r} confidence {conf!r} must be float between 0.0 and 1.0")

            ev_refs = cl.get("evidence_ids")
            if not isinstance(ev_refs, list):
                violations.append(f"Claim {field!r} evidence_ids must be a list")
            else:
                for ref in ev_refs:
                    if ref not in evidence_ids:
                        violations.append(f"Claim {field!r} references unknown evidence_id: {ref!r}")

    # 5. changes
    if not isinstance(envelope.get("changes"), list):
        violations.append("Field 'changes' must be a list")

    # 6. errors
    if not isinstance(envelope.get("errors"), list):
        violations.append("Field 'errors' must be a list")

    # 7. operations
    ops = envelope.get("operations")
    if not isinstance(ops, dict):
        violations.append("Field 'operations' must be an object")
    else:
        if not isinstance(ops.get("requests"), int) or ops["requests"] < 0:
            violations.append(f"operations.requests must be non-negative int, got {ops.get('requests')!r}")
        if not isinstance(ops.get("runtime_ms"), (int, float)) or ops["runtime_ms"] < 0:
            violations.append(f"operations.runtime_ms must be non-negative number, got {ops.get('runtime_ms')!r}")
        if not isinstance(ops.get("third_party_cost_usd"), (int, float)) or ops["third_party_cost_usd"] < 0:
            violations.append(f"operations.third_party_cost_usd must be non-negative number, got {ops.get('third_party_cost_usd')!r}")

    return violations
