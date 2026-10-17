from __future__ import annotations

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.contract import (
    ALLOWED_AVAILABILITY_STATES,
    build_submission_envelope,
    validate_output_contract_envelope,
)


class TestOutputContract(unittest.TestCase):
    def setUp(self):
        self.mock_profile = {
            "organisation_number": "123456789",
            "name": "TESTSELSKAP AS",
            "legal_form": "AS",
            "employees": 5,
            "municipality": "OSLO",
            "evidence": {
                "registry": {
                    "field": "registry",
                    "status": "available",
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
                    "retrieved_at": "2026-08-24T06:00:00Z",
                    "content_sha256": "a" * 64,
                },
                "website": {
                    "field": "website",
                    "status": "available",
                    "source_url": "https://example.no/",
                    "retrieved_at": "2026-08-24T06:00:04Z",
                    "content_sha256": "b" * 64,
                    "value": {
                        "final_url": "https://example.no/",
                        "title": "Example Company",
                        "description": "Example description",
                        "identity_assessment": {"publishable": True},
                        "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/example"}],
                    },
                },
            },
            "run_metrics": {
                "requests": 4,
                "latencies_ms": [200, 300],
                "third_party_cost_usd": 0.005,
            },
        }

    def test_build_envelope_structure(self):
        envelope = build_submission_envelope(
            self.mock_profile,
            run_id="run-test-001",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:08Z",
        )
        self.assertEqual(envelope["organisation_number"], "123456789")
        self.assertEqual(envelope["run"]["run_id"], "run-test-001")
        self.assertEqual(envelope["run"]["terminal_status"], "completed")
        self.assertIsInstance(envelope["claims"], list)
        self.assertIsInstance(envelope["evidence"], list)
        self.assertIsInstance(envelope["changes"], list)
        self.assertIsInstance(envelope["errors"], list)
        self.assertIsInstance(envelope["operations"], dict)
        self.assertEqual(envelope["operations"]["requests"], 4)
        self.assertEqual(envelope["operations"]["runtime_ms"], 500)
        self.assertEqual(envelope["operations"]["third_party_cost_usd"], 0.005)

    def test_validation_passes_on_valid_envelope(self):
        envelope = build_submission_envelope(
            self.mock_profile,
            run_id="run-test-001",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:08Z",
        )
        violations = validate_output_contract_envelope(envelope)
        self.assertEqual(violations, [])

    def test_validation_catches_invalid_availability(self):
        envelope = build_submission_envelope(
            self.mock_profile,
            run_id="run-test-001",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:08Z",
        )
        envelope["claims"][0]["availability"] = "invalid_status"
        violations = validate_output_contract_envelope(envelope)
        self.assertTrue(any("invalid availability 'invalid_status'" in v for v in violations))

    def test_validation_catches_unknown_evidence_ref(self):
        envelope = build_submission_envelope(
            self.mock_profile,
            run_id="run-test-001",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:08Z",
        )
        envelope["claims"][0]["evidence_ids"] = ["ev-ghost"]
        violations = validate_output_contract_envelope(envelope)
        self.assertTrue(any("unknown evidence_id: 'ev-ghost'" in v for v in violations))

    def test_validation_catches_invalid_sha256(self):
        envelope = build_submission_envelope(
            self.mock_profile,
            run_id="run-test-001",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:08Z",
        )
        envelope["evidence"][0]["content_sha256"] = "short_hash"
        violations = validate_output_contract_envelope(envelope)
        self.assertTrue(any("invalid content_sha256" in v for v in violations))


if __name__ == "__main__":
    unittest.main()
