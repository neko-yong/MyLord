import copy
import json
import unittest
from datetime import datetime, timezone

from evidence import (
    EvidenceIntegrityError,
    build_evidence_snapshot,
    canonicalize_evidence,
    evidence_hash,
    load_evidence_snapshot,
)


class EvidenceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.created_at = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        self.messages = [
            {
                "id": 1,
                "case_id": "CASE-TEST",
                "sender": "A",
                "content": "A message",
                "created_at": self.created_at,
                "token_hash": "TOKEN_HASH_MUST_NOT_ENTER",
            },
            {
                "id": 2,
                "case_id": "CASE-TEST",
                "sender": "JUDGE",
                "content": "Judge message",
                "created_at": self.created_at,
                "authorization": "AUTHORIZATION_MUST_NOT_ENTER",
            },
            {
                "id": 3,
                "case_id": "CASE-TEST",
                "sender": "B",
                "content": "After cutoff",
                "created_at": self.created_at,
            },
        ]

    def build(self):
        return build_evidence_snapshot(
            case_id="CASE-TEST",
            created_at=self.created_at,
            requester="A",
            confirmer="B",
            statements={"A": "A statement", "B": "B statement"},
            dispute_map="Dispute map",
            messages=self.messages,
            message_cutoff_id=2,
        )

    def test_snapshot_contains_required_evidence_and_cutoff(self):
        snapshot = self.build()

        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["case_id"], "CASE-TEST")
        self.assertEqual(snapshot["a_statement"], "A statement")
        self.assertEqual(snapshot["b_statement"], "B statement")
        self.assertEqual(snapshot["dispute_map"], "Dispute map")
        self.assertEqual(snapshot["message_cutoff_id"], 2)
        self.assertEqual([message["id"] for message in snapshot["messages"]], [1, 2])
        self.assertEqual(snapshot["messages"][1]["sender"], "JUDGE")
        self.assertEqual(snapshot["requester"], "A")
        self.assertEqual(snapshot["confirmer"], "B")

    def test_snapshot_drops_non_evidence_fields_and_post_cutoff_messages(self):
        content = canonicalize_evidence(self.build())

        self.assertNotIn("TOKEN_HASH_MUST_NOT_ENTER", content)
        self.assertNotIn("AUTHORIZATION_MUST_NOT_ENTER", content)
        self.assertNotIn("After cutoff", content)
        self.assertNotIn("token_hash", content.lower())
        self.assertNotIn("authorization", content.lower())

    def test_canonical_hash_is_stable(self):
        snapshot = self.build()
        reordered = json.loads(json.dumps(snapshot, ensure_ascii=False))

        self.assertEqual(canonicalize_evidence(snapshot), canonicalize_evidence(reordered))
        self.assertEqual(evidence_hash(snapshot), evidence_hash(reordered))
        self.assertEqual(len(evidence_hash(snapshot)), 64)

    def test_load_rejects_tampered_snapshot(self):
        snapshot = self.build()
        content = canonicalize_evidence(snapshot)
        expected_hash = evidence_hash(snapshot)
        tampered = copy.deepcopy(snapshot)
        tampered["messages"][0]["content"] = "tampered"

        loaded = load_evidence_snapshot(content, expected_hash)
        self.assertEqual(loaded, snapshot)
        with self.assertRaises(EvidenceIntegrityError):
            load_evidence_snapshot(canonicalize_evidence(tampered), expected_hash)


if __name__ == "__main__":
    unittest.main()
