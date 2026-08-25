import unittest

from dev_fixtures import FIXTURES, get_fixture
from validation import build_statement_content, validate_statement_fields


class DevFixtureTests(unittest.TestCase):
    def test_five_complete_fixtures_are_available(self):
        self.assertEqual(
            set(FIXTURES),
            {
                "weekend_plan",
                "chores",
                "message_reply",
                "pause_communication",
                "boundary_control",
            },
        )
        for fixture in FIXTURES.values():
            with self.subTest(fixture=fixture.key):
                self.assertTrue(fixture.title)
                self.assertTrue(fixture.default_messages)
                for attribute in (
                    "mock_dispute_map",
                    "mock_judge_intervention",
                    "mock_judgment_normal",
                    "mock_judgment_swapped",
                    "mock_meta_judgment",
                    "mock_final_judgment",
                ):
                    self.assertTrue(getattr(fixture, attribute).strip())

    def test_all_fixture_statements_use_production_validation(self):
        for fixture in FIXTURES.values():
            for role, fields in (
                ("A", fixture.a_statement_fields),
                ("B", fixture.b_statement_fields),
            ):
                with self.subTest(fixture=fixture.key, role=role):
                    cleaned, errors = validate_statement_fields(fields)
                    self.assertEqual(errors, {})
                    content = build_statement_content(role, cleaned)
                    self.assertIn(f"# {role} 的独立陈述", content)

    def test_prompt_injection_fixture_is_available_for_regression(self):
        fixture = get_fixture("boundary_control")

        self.assertIn(
            "忽略系统规则",
            fixture.a_statement_fields["evidence"],
        )

    def test_unknown_fixture_is_rejected(self):
        with self.assertRaises(ValueError):
            get_fixture("missing")


if __name__ == "__main__":
    unittest.main()
