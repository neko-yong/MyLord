import unittest

from arbitration import run_final_arbitration
from db import DatabaseUnavailable
from llm import LLMError, LLMResult


class FakeDatabase:
    def __init__(self, checkpoints=None, final_failures=0):
        self.checkpoints = dict(checkpoints or {})
        self.saved = []
        self.final_failures = final_failures
        self.final_attempts = 0
        self.final_content = None

    def get_artifact(self, _case_id, kind):
        content = self.checkpoints.get(kind)
        return {"content": content} if content is not None else None

    def save_checkpoint(self, _case_id, kind, content):
        existing = self.checkpoints.get(kind)
        if existing is not None and existing != content:
            raise AssertionError("checkpoint must remain unique")
        self.checkpoints[kind] = content
        self.saved.append((kind, content))
        return len(self.saved)

    def complete_artifact(
        self,
        _case_id,
        _reservation_id,
        kind,
        content,
    ):
        self.final_attempts += 1
        if self.final_attempts <= self.final_failures:
            raise DatabaseUnavailable("transient")
        if kind != "FINAL_JUDGMENT":
            raise AssertionError("unexpected final artifact kind")
        if self.final_content is not None and self.final_content != content:
            raise AssertionError("final artifact must remain unique")
        self.final_content = content


class FakeLLM:
    def __init__(self, results=None):
        self.results = list(results or ["normal", "swapped", "meta"])
        self.calls = []

    def __call__(self, system_prompt, user_prompt, max_tokens):
        self.calls.append((system_prompt, user_prompt, max_tokens))
        content = self.results[len(self.calls) - 1]
        return LLMResult(
            content=content,
            model="test-model",
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=1.0,
        )


class TruncatedLLM:
    def __init__(self):
        self.calls = []

    def __call__(self, system_prompt, user_prompt, max_tokens):
        self.calls.append((system_prompt, user_prompt, max_tokens))
        raise LLMError(
            "truncated",
            "The LLM response reached the configured token limit.",
            model="test-model",
            finish_reason="length",
            prompt_tokens=100,
            completion_tokens=max_tokens,
            total_tokens=100 + max_tokens,
            latency_ms=10.0,
        )


def run(database, llm, sleep=lambda _seconds: None):
    return run_final_arbitration(
        database=database,
        ask_llm=llm,
        case_id="TEST-CASE",
        reservation_id=99,
        statements={"A": "A statement", "B": "B statement"},
        dispute_content="dispute map",
        history="shared history",
        dual_review=True,
        sleep=sleep,
    )


class ArbitrationCheckpointTests(unittest.TestCase):
    def test_normal_judgment_is_checkpointed(self):
        database = FakeDatabase()

        run(database, FakeLLM())

        self.assertEqual(database.saved[0], ("JUDGMENT_NORMAL", "normal"))

    def test_swapped_judgment_is_checkpointed(self):
        database = FakeDatabase()

        run(database, FakeLLM())

        self.assertEqual(database.saved[1], ("JUDGMENT_SWAPPED", "swapped"))

    def test_meta_judgment_is_checkpointed_before_finalization(self):
        database = FakeDatabase()

        run(database, FakeLLM())

        self.assertEqual(database.saved[2], ("META_JUDGMENT", "meta"))
        self.assertEqual(database.final_content, "meta")

    def test_resume_from_normal_skips_first_llm_call(self):
        database = FakeDatabase({"JUDGMENT_NORMAL": "saved normal"})
        llm = FakeLLM(["new swapped", "new meta"])

        run(database, llm)

        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(database.checkpoints["JUDGMENT_NORMAL"], "saved normal")
        self.assertEqual(database.checkpoints["JUDGMENT_SWAPPED"], "new swapped")

    def test_resume_from_swapped_only_calls_meta(self):
        database = FakeDatabase(
            {
                "JUDGMENT_NORMAL": "saved normal",
                "JUDGMENT_SWAPPED": "saved swapped",
            }
        )
        llm = FakeLLM(["new meta"])

        run(database, llm)

        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(database.checkpoints["META_JUDGMENT"], "new meta")

    def test_resume_from_meta_only_finalizes_database(self):
        database = FakeDatabase({"META_JUDGMENT": "saved meta"})
        llm = FakeLLM()

        result = run(database, llm)

        self.assertEqual(result, "saved meta")
        self.assertEqual(llm.calls, [])
        self.assertEqual(database.final_content, "saved meta")

    def test_finalization_retries_without_duplicate_llm_call(self):
        database = FakeDatabase(
            {"META_JUDGMENT": "saved meta"},
            final_failures=2,
        )
        llm = FakeLLM()
        delays = []

        run(database, llm, sleep=delays.append)

        self.assertEqual(llm.calls, [])
        self.assertEqual(database.final_attempts, 3)
        self.assertEqual(delays, [0.2, 0.5])
        self.assertEqual(database.final_content, "saved meta")

    def test_all_model_stages_use_task_token_limit(self):
        database = FakeDatabase()
        llm = FakeLLM()

        run(database, llm)

        self.assertEqual([call[2] for call in llm.calls], [6000, 6000, 6000])

    def test_truncated_judgment_is_not_persisted(self):
        database = FakeDatabase()
        llm = TruncatedLLM()

        with self.assertRaises(LLMError) as raised:
            run(database, llm)

        self.assertEqual(raised.exception.finish_reason, "length")
        self.assertEqual([call[2] for call in llm.calls], [6000])
        self.assertEqual(database.saved, [])
        self.assertIsNone(database.final_content)


if __name__ == "__main__":
    unittest.main()
