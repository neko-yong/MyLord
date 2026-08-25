import unittest
from types import SimpleNamespace

from dev_fixtures import get_fixture
from llm import LLMError
from mock_llm import MockLLM
from prompts import (
    DISPUTE_MAP_PROMPT,
    FINAL_JUDGMENT_PROMPT,
    INTERVENTION_PROMPT,
    META_JUDGE_PROMPT,
)


DEV_SETTINGS = SimpleNamespace(dev_mode=True)
PROD_SETTINGS = SimpleNamespace(dev_mode=False)


class MockLLMTests(unittest.TestCase):
    def setUp(self):
        self.fixture = get_fixture("weekend_plan")
        self.calls = {}
        self.failure = {"stage": "NONE", "triggered": False}
        self.mock = MockLLM(
            DEV_SETTINGS,
            self.fixture,
            self.calls,
            self.failure,
        )

    def call(self, system_prompt, user_prompt="normal input"):
        return self.mock(system_prompt, user_prompt, max_tokens=100)

    def test_production_cannot_construct_or_call_mock(self):
        with self.assertRaises(PermissionError):
            MockLLM(PROD_SETTINGS, self.fixture, {}, {})

        self.mock.settings = PROD_SETTINGS
        with self.assertRaises(PermissionError):
            self.call(DISPUTE_MAP_PROMPT)

    def test_all_mock_stages_return_formal_llm_results(self):
        cases = (
            (
                "DISPUTE_MAP",
                DISPUTE_MAP_PROMPT,
                "input",
                self.fixture.mock_dispute_map,
            ),
            (
                "JUDGE_INTERVENTION",
                INTERVENTION_PROMPT,
                "input",
                self.fixture.mock_judge_intervention,
            ),
            (
                "JUDGMENT_NORMAL",
                FINAL_JUDGMENT_PROMPT,
                "===== A 独立陈述 =====",
                self.fixture.mock_judgment_normal,
            ),
            (
                "JUDGMENT_SWAPPED",
                FINAL_JUDGMENT_PROMPT,
                "===== 临时 A（原始 B）=====",
                self.fixture.mock_judgment_swapped,
            ),
            (
                "META_JUDGMENT",
                META_JUDGE_PROMPT,
                "===== Judgment 1：正常 A/B =====",
                self.fixture.mock_meta_judgment,
            ),
        )

        for stage, system_prompt, user_prompt, expected in cases:
            with self.subTest(stage=stage):
                result = self.call(system_prompt, user_prompt)
                self.assertEqual(result.content, expected)
                self.assertEqual(result.model, "dev-mock")
                self.assertEqual(result.finish_reason, "stop")
                self.assertEqual(result.total_tokens, 0)
                self.assertEqual(result.latency_ms, 0.0)

        self.assertEqual(self.calls, {stage: 1 for stage, *_rest in cases})

    def test_failure_injection_is_one_shot_and_counted(self):
        for stage, system_prompt, user_prompt in (
            ("JUDGMENT_NORMAL", FINAL_JUDGMENT_PROMPT, "normal"),
            (
                "JUDGMENT_SWAPPED",
                FINAL_JUDGMENT_PROMPT,
                "===== 临时 A（原始 B）=====",
            ),
            (
                "META_JUDGMENT",
                META_JUDGE_PROMPT,
                "===== Judgment 1：正常 A/B =====",
            ),
        ):
            with self.subTest(stage=stage):
                calls = {}
                failure = {"stage": stage, "triggered": False}
                mock = MockLLM(
                    DEV_SETTINGS,
                    self.fixture,
                    calls,
                    failure,
                )
                with self.assertRaises(LLMError) as raised:
                    mock(system_prompt, user_prompt, max_tokens=100)
                self.assertEqual(
                    raised.exception.category,
                    "dev_injected_failure",
                )
                self.assertEqual(calls[stage], 1)
                self.assertTrue(failure["triggered"])

                mock(system_prompt, user_prompt, max_tokens=100)
                self.assertEqual(calls[stage], 2)

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(LLMError) as raised:
            self.call("unknown prompt")

        self.assertEqual(raised.exception.category, "dev_mock_stage")


if __name__ == "__main__":
    unittest.main()
