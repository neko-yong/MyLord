from llm import LLMError, LLMResult
from prompts import (
    DISPUTE_MAP_PROMPT,
    FINAL_JUDGMENT_PROMPT,
    INTERVENTION_PROMPT,
    META_JUDGE_PROMPT,
)


MOCK_STAGES = {
    "DISPUTE_MAP",
    "JUDGE_INTERVENTION",
    "JUDGMENT_NORMAL",
    "JUDGMENT_SWAPPED",
    "META_JUDGMENT",
}


def require_dev_mode(settings):
    if not getattr(settings, "dev_mode", False):
        raise PermissionError("Developer tools are disabled.")


class MockLLM:
    def __init__(self, settings, fixture, call_counts, failure_state):
        require_dev_mode(settings)
        self.settings = settings
        self.fixture = fixture
        self.call_counts = call_counts
        self.failure_state = failure_state

    def _stage(self, system_prompt, user_prompt):
        if system_prompt == DISPUTE_MAP_PROMPT:
            return "DISPUTE_MAP"
        if system_prompt == INTERVENTION_PROMPT:
            return "JUDGE_INTERVENTION"
        if system_prompt == META_JUDGE_PROMPT:
            return "META_JUDGMENT"
        if system_prompt == FINAL_JUDGMENT_PROMPT:
            if "临时 A（原始 B）" in user_prompt:
                return "JUDGMENT_SWAPPED"
            return "JUDGMENT_NORMAL"
        raise LLMError(
            "dev_mock_stage",
            "The developer mock could not identify the requested stage.",
        )

    def __call__(
        self,
        system_prompt,
        user_prompt,
        temperature=0.2,
        max_tokens=None,
    ):
        del temperature, max_tokens
        require_dev_mode(self.settings)
        stage = self._stage(system_prompt, user_prompt)
        self.call_counts[stage] = self.call_counts.get(stage, 0) + 1

        if (
            self.failure_state.get("stage") == stage
            and not self.failure_state.get("triggered", False)
        ):
            self.failure_state["triggered"] = True
            raise LLMError(
                "dev_injected_failure",
                f"Developer failure injection at {stage}.",
            )

        content_by_stage = {
            "DISPUTE_MAP": self.fixture.mock_dispute_map,
            "JUDGE_INTERVENTION": self.fixture.mock_judge_intervention,
            "JUDGMENT_NORMAL": self.fixture.mock_judgment_normal,
            "JUDGMENT_SWAPPED": self.fixture.mock_judgment_swapped,
            "META_JUDGMENT": self.fixture.mock_meta_judgment,
        }
        return LLMResult(
            content=content_by_stage[stage],
            model="dev-mock",
            finish_reason="stop",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0.0,
        )
