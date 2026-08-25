from dataclasses import dataclass

from arbitration import run_final_arbitration
from db import DatabaseUnavailable
from dev_fixtures import get_fixture
from dev_memory_db import DEV_TITLE_PREFIX
from mock_llm import MockLLM, require_dev_mode
from validation import build_statement_content, validate_statement_fields


SCENARIOS = (
    "EMPTY",
    "A_SUBMITTED",
    "STATEMENTS_SUBMITTED",
    "MAP_READY",
    "MEDIATING",
    "PAUSED",
    "ARBITRATION_PENDING_A",
    "ARBITRATION_PENDING_B",
    "ARBITRATING",
    "CLOSED",
)
FAILURE_STAGES = (
    "NONE",
    "DISPUTE_MAP",
    "JUDGE_INTERVENTION",
    "JUDGMENT_NORMAL",
    "JUDGMENT_SWAPPED",
    "META_JUDGMENT",
    "FINAL_DB_COMPLETE",
)


@dataclass(frozen=True)
class DevCase:
    case_id: str
    a_token: str
    b_token: str
    fixture_key: str
    scenario: str
    seed_mock_calls: dict


def _valid_statement(role, fields):
    cleaned, errors = validate_statement_fields(fields)
    if errors:
        raise ValueError(f"Fixture {role} statement is invalid: {errors}")
    return build_statement_content(role, cleaned)


def is_dev_case(settings, database, case_id):
    require_dev_mode(settings)
    case = database.get_case(case_id)
    return bool(
        case
        and isinstance(case.get("title"), str)
        and case["title"].startswith(DEV_TITLE_PREFIX)
    )


def _require_dev_case(settings, database, case_id):
    if not is_dev_case(settings, database, case_id):
        raise PermissionError("Developer action is limited to DEV_TEST cases.")


def _submit_both(database, case_id, fixture):
    database.save_statement(
        case_id,
        "A",
        _valid_statement("A", fixture.a_statement_fields),
    )
    database.save_statement(
        case_id,
        "B",
        _valid_statement("B", fixture.b_statement_fields),
    )


def _seed_map(database, case_id, fixture):
    reservation_id = database.claim_artifact(case_id, "DISPUTE_MAP")
    if reservation_id is None:
        raise RuntimeError("Developer dispute-map reservation failed.")
    database.complete_artifact(
        case_id,
        reservation_id,
        "DISPUTE_MAP",
        fixture.mock_dispute_map,
    )


def _seed_mediating(database, case_id, fixture):
    for sender, content in fixture.default_messages:
        database.add_message(case_id, sender, content)


def _seed_arbitrating(database, case_id, requester="A"):
    confirmer = "B" if requester == "A" else "A"
    database.request_arbitration(case_id, requester)
    database.confirm_arbitration(case_id, confirmer)


def seed_dev_case(settings, database, fixture_key, scenario):
    require_dev_mode(settings)
    if scenario not in SCENARIOS:
        raise ValueError("未知的开发 Scenario。")
    fixture = get_fixture(fixture_key)
    case_id = None
    try:
        case_id, a_token, b_token = database.create_case(
            f"{DEV_TITLE_PREFIX}{fixture.title} · {scenario}"
        )
        if scenario == "EMPTY":
            pass
        elif scenario == "A_SUBMITTED":
            database.save_statement(
                case_id,
                "A",
                _valid_statement("A", fixture.a_statement_fields),
            )
        else:
            _submit_both(database, case_id, fixture)
            if scenario not in {"STATEMENTS_SUBMITTED"}:
                _seed_map(database, case_id, fixture)
                if scenario not in {"MAP_READY"}:
                    _seed_mediating(database, case_id, fixture)
                    if scenario == "PAUSED":
                        if not database.pause_case(case_id, "B"):
                            raise RuntimeError("Developer pause seeding failed.")
                    elif scenario == "ARBITRATION_PENDING_A":
                        database.request_arbitration(case_id, "A")
                    elif scenario == "ARBITRATION_PENDING_B":
                        database.request_arbitration(case_id, "B")
                    elif scenario in {"ARBITRATING", "CLOSED"}:
                        _seed_arbitrating(database, case_id)

        seed_mock_calls = {}
        if scenario == "CLOSED":
            reservation_id = database.claim_artifact(
                case_id,
                "FINAL_JUDGMENT",
            )
            if reservation_id is None:
                raise RuntimeError("Developer final reservation failed.")
            mock = MockLLM(
                settings,
                fixture,
                seed_mock_calls,
                {"stage": "NONE", "triggered": False},
            )
            run_final_arbitration(
                database=database,
                ask_llm=mock,
                case_id=case_id,
                reservation_id=reservation_id,
                dual_review=True,
                sleep=lambda _seconds: None,
            )

        return DevCase(
            case_id=case_id,
            a_token=a_token,
            b_token=b_token,
            fixture_key=fixture_key,
            scenario=scenario,
            seed_mock_calls=seed_mock_calls,
        )
    except BaseException:
        if case_id is not None:
            database.delete_case_if_title_prefix(case_id, DEV_TITLE_PREFIX)
        raise


def get_dev_state(settings, database, case_id):
    require_dev_mode(settings)
    _require_dev_case(settings, database, case_id)
    case = database.get_case(case_id)
    submitted = database.get_submission_status(case_id)
    messages = database.get_messages(case_id)
    evidence = database.get_arbitration_evidence(case_id)

    def exists(kind):
        artifact = database.get_artifact(case_id, kind)
        return bool(artifact and artifact.get("content"))

    evidence_hash = evidence.get("evidence_hash") if evidence else None
    snapshot = evidence.get("snapshot") if evidence else None
    return {
        "case_id": case_id,
        "status": case["status"],
        "a_submitted": submitted["A"],
        "b_submitted": submitted["B"],
        "dispute_map": exists("DISPUTE_MAP"),
        "message_count": len(messages),
        "paused_by": case.get("paused_by"),
        "arbitration_request": case.get("arbitration_requested_by"),
        "evidence": bool(evidence),
        "evidence_cutoff": (
            snapshot.get("message_cutoff_id") if snapshot else None
        ),
        "evidence_hash_preview": (
            evidence_hash[:10] if evidence_hash else None
        ),
        "judgment_normal": exists("JUDGMENT_NORMAL"),
        "judgment_swapped": exists("JUDGMENT_SWAPPED"),
        "meta": exists("META_JUDGMENT"),
        "final": exists("FINAL_JUDGMENT"),
    }


def switch_dev_role(settings, database, case_id, role):
    require_dev_mode(settings)
    if role not in {"A", "B"}:
        raise ValueError("无效的开发查看身份。")
    _require_dev_case(settings, database, case_id)
    return role


def delete_dev_case(settings, database, case_id):
    require_dev_mode(settings)
    _require_dev_case(settings, database, case_id)
    if not database.delete_case_if_title_prefix(case_id, DEV_TITLE_PREFIX):
        raise RuntimeError("Developer case deletion failed.")
    return True


def recreate_dev_case(
    settings,
    database,
    case_id,
    fixture_key,
    scenario,
):
    require_dev_mode(settings)
    delete_dev_case(settings, database, case_id)
    return seed_dev_case(settings, database, fixture_key, scenario)


class FinalCompleteFailureDatabase:
    def __init__(self, settings, database, failure_state):
        require_dev_mode(settings)
        self.settings = settings
        self.database = database
        self.failure_state = failure_state

    def __getattr__(self, name):
        require_dev_mode(self.settings)
        return getattr(self.database, name)

    def complete_artifact(self, case_id, artifact_id, kind, content):
        require_dev_mode(self.settings)
        if (
            kind == "FINAL_JUDGMENT"
            and self.failure_state.get("stage") == "FINAL_DB_COMPLETE"
            and not self.failure_state.get("triggered", False)
        ):
            attempts = self.failure_state.get("attempts", 0) + 1
            self.failure_state["attempts"] = attempts
            if attempts >= 3:
                self.failure_state["triggered"] = True
            raise DatabaseUnavailable(
                "开发模式模拟最终数据库写入暂时失败。"
            )
        return self.database.complete_artifact(
            case_id,
            artifact_id,
            kind,
            content,
        )
