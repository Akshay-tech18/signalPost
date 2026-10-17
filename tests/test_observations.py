from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.external_footprint import (
    PUBLISHABLE_ACQUISITION_MODES,
    publishable_observation,
    validate_observation,
)
from scripts.build_all_observations import build_observations_from_profiles


class ObservationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.sample_profiles = [
            {
                "organisation_number": "915022812",
                "name": "MEDVIND MEDIA AS",
                "employees": 3,
                "evidence": {
                    "registry": {
                        "status": "available",
                        "retrieved_at": "2026-09-18T13:25:00Z",
                        "content_sha256": "a" * 64,
                    },
                    "locations": {
                        "status": "available",
                        "retrieved_at": "2026-09-18T13:25:00Z",
                        "content_sha256": "b" * 64,
                        "value": {"locations": [{"org": "915022812", "name": "Avd Oslo"}]},
                    },
                    "website": {
                        "status": "available",
                        "source_url": "https://medvindmedia.no",
                        "retrieved_at": "2026-09-18T13:25:00Z",
                        "content_sha256": "c" * 64,
                        "value": {
                            "final_url": "https://medvindmedia.no",
                            "identity_assessment": {"publishable": True, "score": 0.95, "status": "exact"},
                            "pages": [
                                {"url": "https://medvindmedia.no/", "content_sha256": "c" * 64},
                                {"url": "https://medvindmedia.no/nyheter/artikkel-1", "title": "Ny kontrakt", "content_sha256": "d" * 64},
                            ],
                            "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/medvindmedia"}],
                        },
                    },
                    "website_jobs": {
                        "status": "available",
                        "retrieved_at": "2026-09-18T14:36:00Z",
                        "value": {
                            "jobs": [{"title": "Journalist", "claim_span": "Vi søker journalist til vårt team."}]
                        },
                    },
                },
            },
            {
                "organisation_number": "927056631",
                "name": "NULL EMPLOYEES AS",
                "employees": None,
                "evidence": {
                    "registry": {
                        "status": "available",
                        "retrieved_at": "2026-09-18T13:25:00Z",
                        "content_sha256": "e" * 64,
                    },
                },
            },
        ]

    def test_build_all_observations_produces_only_publishable_records(self):
        observations, audit_labels = build_observations_from_profiles(self.sample_profiles)
        self.assertGreaterEqual(len(observations), 4)

        for obs in observations:
            errors = validate_observation(obs)
            self.assertEqual(errors, [], f"Observation {obs.get('id')} failed validation: {errors}")
            self.assertTrue(publishable_observation(obs))
            self.assertIn(obs["acquisition_mode"], PUBLISHABLE_ACQUISITION_MODES)
            self.assertEqual(obs["rights_status"], "approved")
            self.assertEqual(len(obs["content_sha256"]), 64)
            self.assertTrue(obs["exact_entity"])

    def test_audit_labels_match_generated_observations(self):
        observations, audit_labels = build_observations_from_profiles(self.sample_profiles)
        obs_ids = {obs["id"] for obs in observations}
        self.assertTrue(all(label["id"] in obs_ids for label in audit_labels))
        self.assertTrue(all(label["exact_entity"] for label in audit_labels))
        self.assertTrue(all(label["metric_correct"] for label in audit_labels))


if __name__ == "__main__":
    unittest.main()
