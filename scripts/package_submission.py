#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.contract import build_submission_envelope, validate_output_contract_envelope
from norway_company_agent.evidence import utc_now


def main():
    parser = argparse.ArgumentParser(description="Package company profiles into official OUTPUT_CONTRACT.md envelopes.")
    parser.add_argument("--profiles", required=True, help="Path to input JSONL profiles")
    parser.add_argument("--output", required=True, help="Path to output JSONL envelopes")
    parser.add_argument("--run-id", required=True, help="Execution run identifier")
    parser.add_argument("--third-party-cost-usd", type=float, default=0.0, help="Total or per-profile third party cost in USD")
    parser.add_argument("--validate", action="store_true", default=True, help="Validate envelopes against OUTPUT_CONTRACT.md rules")
    args = parser.parse_args()

    started_at = utc_now()
    profile_path = Path(args.profiles)
    if not profile_path.exists():
        sys.exit(f"Error: Profiles file not found: {args.profiles}")

    profiles = [json.loads(line) for line in profile_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    completed_at = utc_now()

    envelopes = []
    total_violations = 0

    for idx, profile in enumerate(profiles, start=1):
        envelope = build_submission_envelope(
            profile,
            run_id=args.run_id,
            started_at=started_at,
            completed_at=completed_at,
            third_party_cost_usd=args.third_party_cost_usd,
        )
        if args.validate:
            violations = validate_output_contract_envelope(envelope)
            if violations:
                total_violations += len(violations)
                print(f"Warning: Envelope #{idx} ({envelope.get('organisation_number')}) has {len(violations)} contract violation(s):", file=sys.stderr)
                for v in violations:
                    print(f"  - {v}", file=sys.stderr)
        envelopes.append(envelope)

    if total_violations > 0:
        sys.exit(f"Contract packaging failed with {total_violations} total validation violations!")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for env in envelopes:
            handle.write(json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(f"✅ Successfully packaged {len(envelopes)} envelopes conforming to OUTPUT_CONTRACT.md into {args.output}")


if __name__ == "__main__":
    main()
