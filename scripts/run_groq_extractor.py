#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Add src to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.evidence import evidence, utc_now

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"
FALLBACK_MODEL = "qwen/qwen3.8-27b"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def find_verbatim_span(span: str, text: str) -> str | None:
    """Verify that the claim span exists verbatim in the source text to prevent hallucinations."""
    if not span or not text:
        return None
    cleaned = span.strip().strip("\"'").strip()
    if not cleaned:
        return None

    # 1. Exact verbatim match
    if cleaned in text:
        return cleaned

    # 2. Case-insensitive exact substring match
    lower_text = text.casefold()
    lower_span = cleaned.casefold()
    idx = lower_text.find(lower_span)
    if idx != -1:
        return text[idx : idx + len(cleaned)]

    # 3. Normalized whitespace match
    norm_span = " ".join(cleaned.split())
    if norm_span in text:
        return norm_span

    # 4. First 60 characters prefix match if model truncated trailing punctuation
    if len(cleaned) > 40:
        prefix = cleaned[:40]
        p_idx = text.find(prefix)
        if p_idx != -1:
            return text[p_idx : p_idx + len(cleaned)]

    return None


RELEVANT_PATTERNS = re.compile(
    r"(karriere|stilling|ledig|søker|rekruttering|jobb|hiring|career|apply|"
    r"daglig leder|styremedlem|styreleder|ledelse|ansatte|kontaktperson|team|founder|ceo|director)",
    re.IGNORECASE,
)


def extract_relevant_snippets(full_text: str, max_chars: int = 4000) -> str:
    """Extract paragraphs and lines containing high-signal keywords to prevent context bloat."""
    paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]
    if not paragraphs:
        return full_text[:max_chars]

    selected: list[str] = []
    # Always keep first introductory paragraph for context
    selected.append(paragraphs[0])
    current_len = len(paragraphs[0])

    for p in paragraphs[1:]:
        if RELEVANT_PATTERNS.search(p):
            selected.append(p)
            current_len += len(p)
            if current_len >= max_chars:
                break

    condensed = "\n\n".join(selected)
    return condensed[:max_chars]


def call_groq_api(
    api_key: str,
    model: str,
    company_name: str,
    text: str,
    *,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call Groq API using standard library urllib with automatic backoff and rate-limit handling."""
    condensed_text = extract_relevant_snippets(text, max_chars=4000)
    prompt = f"""You are a highly precise company research agent. I will provide you with text extracted from the official website of the Norwegian company '{company_name}'.
Your task is to extract any open Job Postings and Key Personnel (e.g., executives, board members, founders, general manager / daglig leder).

CRITICAL RULE: Every extracted fact MUST include a `claim_span` which is an EXACT, verbatim substring from the provided text that proves the claim. If you cannot find an exact quote, do not extract the fact.

Return the results strictly in this JSON format:
{{
  "jobs": [
    {{
      "title": "...",
      "claim_span": "..."
    }}
  ],
  "people": [
    {{
      "name": "...",
      "role": "...",
      "claim_span": "..."
    }}
  ]
}}

If none are found, return empty lists.

Website Text:
---
{condensed_text}
---
"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "signalpost-groq-extractor/1.0 (+https://builderr.ai)",
    }

    last_error = ""
    for attempt in range(max_retries):
        started = time.monotonic()
        req = urllib.request.Request(GROQ_ENDPOINT, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                body = json.loads(resp.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                usage = body.get("usage", {})
                return parsed, {
                    "status": resp.status,
                    "latency_ms": elapsed_ms,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            err_body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {err_body[:200]}"
            if exc.code == 429:
                # Rate limited: check Retry-After or x-ratelimit-reset-tokens header
                retry_after = exc.headers.get("retry-after")
                reset_tokens = exc.headers.get("x-ratelimit-reset-tokens")
                if retry_after:
                    delay = float(retry_after)
                elif reset_tokens:
                    # e.g. "615ms" or "1.2s"
                    delay = float(re.sub(r"[^\d.]", "", reset_tokens)) / (1000 if "ms" in reset_tokens else 1) + 0.1
                else:
                    delay = 2.0 * (2**attempt)
                print(f"  [Rate limit 429] Waiting {delay:.2f}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(max(0.5, delay))
                continue
            elif exc.code in {500, 502, 503, 504}:
                time.sleep(1.0 * (2**attempt))
                continue
            else:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0 * (2**attempt))

    print(f"  ⚠️ Groq call failed after {max_retries} attempts: {last_error}", file=sys.stderr)
    return {"jobs": [], "people": []}, {"status": 0, "latency_ms": 0, "error": last_error, "total_tokens": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract verified jobs and people from crawled websites using Groq LLM.")
    parser.add_argument("--input", required=True, help="Input JSONL profiles (e.g. out/entry-profiles-tavily.jsonl)")
    parser.add_argument("--output", required=True, help="Output JSONL profiles (e.g. out/entry-profiles-final.jsonl)")
    parser.add_argument("--report", default="out/entry-groq-report.json", help="Path to write extraction operational report")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Groq model identifier (default: {DEFAULT_MODEL})")
    parser.add_argument("--api-key-env", default="GROQ_API_KEY", help="Environment variable for Groq API key")
    parser.add_argument("--min-interval", type=float, default=0.25, help="Seconds to sleep between calls to respect rate limits")
    parser.add_argument("--checkpoint-every", type=int, default=25, help="Save progress every N profiles")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output file if present")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        # Check .env file directly
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{args.api_key_env}="):
                    api_key = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if not api_key:
        sys.exit(f"Error: Missing API key in environment variable {args.api_key_env} and .env file")

    rows = read_jsonl(Path(args.input))
    output_path = Path(args.output)

    # Resume handling
    state: dict[str, dict] = {}
    if args.resume and output_path.exists():
        prior = read_jsonl(output_path)
        for r in prior:
            org = str(r.get("organisation_number"))
            ev = r.get("evidence", {})
            if ("website_jobs" in ev and "website_people" in ev) or ev.get("website", {}).get("status") != "available":
                state[org] = r
        print(f"Resuming: found {len(state)} previously processed profiles in {output_path}")

    started_at = utc_now()
    latencies: list[int] = []
    total_tokens = 0
    extracted_jobs = 0
    extracted_people = 0
    rejected_hallucinations = 0
    calls_made = 0

    for idx, row in enumerate(rows, start=1):
        org = str(row.get("organisation_number"))
        if org in state:
            rows[idx - 1] = state[org]
            continue

        website_evidence = row.get("evidence", {}).get("website", {})
        if website_evidence.get("status") != "available":
            continue

        value = website_evidence.get("value", {})
        source_url = website_evidence.get("source_url") or value.get("final_url") or "https://example.test"
        pages = value.get("pages", [])
        full_text = "\n\n".join(page.get("main_text_excerpt", "") for page in pages if page.get("main_text_excerpt"))

        if not pages or not full_text.strip():
            retrieved = utc_now()
            row["evidence"]["website_jobs"] = evidence(
                "website_jobs",
                "not_available",
                "empty_website_crawl",
                source_url,
                note="No crawlable text found on website pages.",
                retrieved_at=retrieved,
            )
            row["evidence"]["website_people"] = evidence(
                "website_people",
                "not_available",
                "empty_website_crawl",
                source_url,
                note="No crawlable text found on website pages.",
                retrieved_at=retrieved,
            )
            state[org] = row
            rows[idx - 1] = row
            continue

        # Fast pre-filter: skip API call if no job or leadership keywords appear in crawled text
        if not RELEVANT_PATTERNS.search(full_text):
            retrieved = utc_now()
            row["evidence"]["website_jobs"] = evidence(
                "website_jobs",
                "not_available",
                "keyword_prefilter",
                source_url,
                note="No career or job keywords found in website text.",
                retrieved_at=retrieved,
            )
            row["evidence"]["website_people"] = evidence(
                "website_people",
                "not_available",
                "keyword_prefilter",
                source_url,
                note="No leadership or team keywords found in website text.",
                retrieved_at=retrieved,
            )
            state[org] = row
            rows[idx - 1] = row
            continue

        company_name = str(row.get("name") or "")
        print(f"[{idx}/{len(rows)}] Extracting facts for {company_name} ({org})...")
        facts, meta = call_groq_api(api_key, args.model, company_name, full_text)
        calls_made += 1
        if meta.get("latency_ms"):
            latencies.append(meta["latency_ms"])
        total_tokens += meta.get("total_tokens", 0)

        # Base website hash for cryptographic provenance
        source_url = website_evidence.get("source_url") or value.get("final_url") or "https://example.test"
        base_sha = website_evidence.get("content_sha256") or hashlib.sha256(full_text.encode("utf-8")).hexdigest()

        # Filter & verify jobs against source text
        verified_jobs = []
        for j in facts.get("jobs", []):
            if not isinstance(j, dict):
                continue
            span = find_verbatim_span(j.get("claim_span", ""), full_text)
            if span:
                verified_jobs.append({"title": j.get("title", "Stilling"), "claim_span": span})
            else:
                rejected_hallucinations += 1

        # Filter & verify people against source text
        verified_people = []
        for p in facts.get("people", []):
            if not isinstance(p, dict):
                continue
            span = find_verbatim_span(p.get("claim_span", ""), full_text)
            if span:
                verified_people.append({
                    "name": p.get("name", ""),
                    "role": p.get("role", "Nøkkelperson"),
                    "claim_span": span,
                })
            else:
                rejected_hallucinations += 1

        if meta.get("error"):
            print(f"  ⚠️ Skipping saving unextracted state for {org} due to API error: {meta['error']}")
            continue

        # Attach verified evidence
        retrieved = utc_now()
        if verified_jobs:
            jobs_sha = hashlib.sha256(f"{base_sha}:jobs:{json.dumps(verified_jobs, sort_keys=True)}".encode("utf-8")).hexdigest()
            row["evidence"]["website_jobs"] = evidence(
                "website_jobs",
                "available",
                "llm_extracted_website_jobs",
                source_url,
                value={"jobs": verified_jobs},
                note=f"Extracted via Groq {args.model} from verified website text.",
                retrieved_at=retrieved,
                content_sha256=jobs_sha,
            )
            extracted_jobs += len(verified_jobs)
        else:
            row["evidence"]["website_jobs"] = evidence(
                "website_jobs",
                "not_available",
                "llm_extracted_website_jobs",
                source_url,
                note=f"No verified job postings found via Groq {args.model}.",
                retrieved_at=retrieved,
            )

        if verified_people:
            people_sha = hashlib.sha256(f"{base_sha}:people:{json.dumps(verified_people, sort_keys=True)}".encode("utf-8")).hexdigest()
            row["evidence"]["website_people"] = evidence(
                "website_people",
                "available",
                "llm_extracted_website_people",
                source_url,
                value={"people": verified_people},
                note=f"Extracted via Groq {args.model} from verified website text.",
                retrieved_at=retrieved,
                content_sha256=people_sha,
            )
            extracted_people += len(verified_people)
        else:
            row["evidence"]["website_people"] = evidence(
                "website_people",
                "not_available",
                "llm_extracted_website_people",
                source_url,
                note=f"No verified leadership profiles found via Groq {args.model}.",
                retrieved_at=retrieved,
            )

        # Update metrics in profile
        metrics = row.setdefault("run_metrics", {"requests": 0, "bytes": 0, "latencies_ms": []})
        metrics["requests"] = metrics.get("requests", 0) + 1
        if meta.get("latency_ms"):
            metrics.setdefault("latencies_ms", []).append(meta["latency_ms"])

        state[org] = row
        rows[idx - 1] = row

        state[org] = row

        # Checkpoint
        if calls_made % args.checkpoint_every == 0:
            write_jsonl(output_path, rows)

        time.sleep(args.min_interval)

    write_jsonl(output_path, rows)
    completed_at = utc_now()

    ordered = sorted(latencies)
    p50 = ordered[len(ordered) // 2] if ordered else 0
    p95 = ordered[int(len(ordered) * 0.95)] if ordered else 0

    report_path = Path(args.report)
    if calls_made == 0 and report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["resumed_from_cache"] = True
            report["last_checked_at"] = completed_at
        except Exception:
            report = {
                "model": args.model,
                "started_at": started_at,
                "completed_at": completed_at,
                "total_profiles": len(rows),
                "api_calls_made": calls_made,
                "total_tokens": total_tokens,
                "verified_jobs_extracted": extracted_jobs,
                "verified_people_extracted": extracted_people,
                "hallucinations_rejected": rejected_hallucinations,
                "latency_ms": {"p50": p50, "p95": p95},
            }
    else:
        report = {
            "model": args.model,
            "started_at": started_at,
            "completed_at": completed_at,
            "total_profiles": len(rows),
            "api_calls_made": calls_made,
            "total_tokens": total_tokens,
            "verified_jobs_extracted": extracted_jobs,
            "verified_people_extracted": extracted_people,
            "hallucinations_rejected": rejected_hallucinations,
            "latency_ms": {"p50": p50, "p95": p95},
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("\n✅ Groq Extraction Complete!")
    print(f"   Model: {args.model}")
    print(f"   API calls: {calls_made}, Tokens: {total_tokens:,}")
    print(f"   Verified facts: {extracted_jobs} jobs, {extracted_people} people")
    print(f"   Hallucinations rejected: {rejected_hallucinations}")
    print(f"   Latency: p50={p50}ms, p95={p95}ms")
    print(f"   Report: {args.report}")


if __name__ == "__main__":
    main()
