import copy
import secrets
import string
from datetime import datetime, timezone

from db import (
    ARBITRATION_REQUEST_ALLOWED_STATUSES,
    ARTIFACT_KINDS,
    CHECKPOINT_ARTIFACT_KINDS,
    MESSAGE_ALLOWED_STATUSES,
    MESSAGE_SENDERS,
    PUBLIC_ARTIFACT_KINDS,
    ROLES,
    CaseStateError,
    StatementAlreadySubmitted,
    hash_token,
)
from evidence import (
    EvidenceIntegrityError,
    build_evidence_snapshot,
    canonicalize_evidence,
    evidence_hash,
    load_evidence_snapshot,
)


DEV_TITLE_PREFIX = "[DEV_TEST] "
STORE_VERSION = 1


def require_dev_local(settings, database_mode=None):
    if not getattr(settings, "dev_mode", False):
        raise PermissionError("Fast Local is disabled outside DEV_MODE.")
    effective_mode = database_mode or getattr(
        settings, "dev_database_mode", None
    )
    if effective_mode != "local":
        raise PermissionError("Fast Local requires DEV_DATABASE_MODE=local.")


def new_dev_local_store(settings):
    require_dev_local(settings, "local")
    return {
        "version": STORE_VERSION,
        "cases": {},
        "statements": {},
        "messages": {},
        "artifacts": {},
        "case_sequence": 0,
        "statement_sequence": 0,
        "message_sequence": 0,
        "artifact_sequence": 0,
    }


def _now():
    return datetime.now(timezone.utc)


def _role(role):
    if role not in ROLES:
        raise ValueError("无效的案件身份。")


def _hash(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in string.hexdigits for character in value)
    ):
        raise ValueError("证据 Hash 无效。")
    return value.lower()


class DevMemoryDatabase:
    """Session-local DEV_TEST backend with production-observable semantics."""

    def __init__(self, settings, store, database_mode="local"):
        require_dev_local(settings, database_mode)
        if not isinstance(store, dict) or store.get("version") != STORE_VERSION:
            raise ValueError("Fast Local store 版本无效，请 Reset Local Playground。")
        self.settings = settings
        self.store = store
        self.database_mode = database_mode

    def _guard(self):
        require_dev_local(self.settings, self.database_mode)

    def _case(self, case_id):
        self._guard()
        case = self.store["cases"].get(case_id)
        if not case:
            raise CaseStateError("案件不存在。")
        return case

    def _next(self, name):
        self.store[name] += 1
        return self.store[name]

    def _touch(self, case):
        case["updated_at"] = _now()

    def init_db(self):
        self._guard()

    def close(self):
        self._guard()

    def health_check(self):
        self._guard()
        return True

    def reset(self):
        self._guard()
        self.store.clear()
        self.store.update(new_dev_local_store(self.settings))

    def create_case(self, title):
        self._guard()
        clean_title = str(title).strip()
        if not clean_title.startswith(DEV_TITLE_PREFIX):
            raise PermissionError("Fast Local 只能创建 DEV_TEST 案件。")

        case_id = None
        for _ in range(10):
            candidate = "DEV-" + "".join(
                secrets.choice(string.ascii_uppercase + string.digits)
                for _ in range(6)
            )
            if candidate not in self.store["cases"]:
                case_id = candidate
                break
        if case_id is None:
            raise RuntimeError("无法生成唯一的本地测试案件编号。")

        a_token = f"A-{secrets.token_urlsafe(24)}"
        b_token = f"B-{secrets.token_urlsafe(24)}"
        created_at = _now()
        self.store["case_sequence"] += 1
        self.store["cases"][case_id] = {
            "case_id": case_id,
            "title": clean_title,
            "a_token_hash": hash_token(a_token),
            "b_token_hash": hash_token(b_token),
            "status": "COLLECTING",
            "paused_by": None,
            "arbitration_requested_by": None,
            "arbitration_requested_at": None,
            "arbitration_started_at": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        self.store["messages"][case_id] = []
        return case_id, a_token, b_token

    def delete_case_if_title_prefix(self, case_id, title_prefix):
        self._guard()
        if title_prefix != DEV_TITLE_PREFIX:
            return False
        case = self.store["cases"].get(case_id)
        if not case or not case["title"].startswith(DEV_TITLE_PREFIX):
            return False
        del self.store["cases"][case_id]
        self.store["messages"].pop(case_id, None)
        self.store["statements"] = {
            key: value
            for key, value in self.store["statements"].items()
            if key[0] != case_id
        }
        self.store["artifacts"] = {
            key: value
            for key, value in self.store["artifacts"].items()
            if key[0] != case_id
        }
        return True

    def authenticate(self, case_id, token):
        self._guard()
        case = self.store["cases"].get(str(case_id).strip().upper())
        if not case or not isinstance(token, str):
            return None
        candidate = hash_token(token)
        if secrets.compare_digest(candidate, case["a_token_hash"]):
            return "A"
        if secrets.compare_digest(candidate, case["b_token_hash"]):
            return "B"
        return None

    def get_case(self, case_id):
        self._guard()
        case = self.store["cases"].get(case_id)
        if not case:
            return None
        visible = {
            key: value
            for key, value in case.items()
            if key not in {"a_token_hash", "b_token_hash"}
        }
        return copy.deepcopy(visible)

    def get_submission_status(self, case_id):
        self._guard()
        return {
            role: (case_id, role) in self.store["statements"]
            for role in ("A", "B")
        }

    def get_case_overview(self, case_id):
        self._guard()
        case = self.get_case(case_id)
        if not case:
            return None
        return {
            "case": case,
            "submitted": self.get_submission_status(case_id),
        }

    def get_statement(self, case_id, role):
        self._guard()
        _role(role)
        statement = self.store["statements"].get((case_id, role))
        return copy.deepcopy(statement) if statement else None

    def get_statements_for_llm(self, case_id):
        self._guard()
        statements = {
            role: self.store["statements"][(case_id, role)]["content"]
            for role in ROLES
            if (case_id, role) in self.store["statements"]
        }
        if set(statements) != ROLES:
            raise CaseStateError("双方独立陈述尚未全部提交。")
        return statements

    def save_statement(self, case_id, role, content):
        self._guard()
        _role(role)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("独立陈述不能为空。")
        case = self._case(case_id)
        if case["status"] != "COLLECTING":
            raise CaseStateError("当前案件状态不允许提交独立陈述。")
        key = (case_id, role)
        if key in self.store["statements"]:
            raise StatementAlreadySubmitted("你的独立陈述已经提交并冻结。")
        submitted_at = _now()
        self.store["statements"][key] = {
            "id": self._next("statement_sequence"),
            "case_id": case_id,
            "role": role,
            "content": content.strip(),
            "submitted_at": submitted_at,
        }
        if all((case_id, item) in self.store["statements"] for item in ROLES):
            case["status"] = "READY_FOR_MAP"
        self._touch(case)

    def both_submitted(self, case_id):
        submitted = self.get_submission_status(case_id)
        return submitted["A"] and submitted["B"]

    def request_arbitration(self, case_id, role):
        self._guard()
        _role(role)
        case = self._case(case_id)
        if case["status"] == "ARBITRATION_PENDING":
            return self.get_case(case_id)
        if case["status"] not in ARBITRATION_REQUEST_ALLOWED_STATUSES:
            raise CaseStateError("当前案件状态不允许申请最终仲裁。")
        case["status"] = "ARBITRATION_PENDING"
        case["paused_by"] = None
        case["arbitration_requested_by"] = role
        case["arbitration_requested_at"] = _now()
        case["arbitration_started_at"] = None
        self._touch(case)
        return self.get_case(case_id)

    def cancel_arbitration_request(self, case_id, role):
        self._guard()
        _role(role)
        case = self._case(case_id)
        if case["status"] != "ARBITRATION_PENDING":
            raise CaseStateError("当前没有待确认的最终仲裁申请。")
        case["status"] = (
            "MEDIATING" if self.store["messages"].get(case_id) else "MAP_READY"
        )
        case["arbitration_requested_by"] = None
        case["arbitration_requested_at"] = None
        case["arbitration_started_at"] = None
        self._touch(case)
        return self.get_case(case_id)

    def confirm_arbitration(self, case_id, role):
        self._guard()
        _role(role)
        case = self._case(case_id)
        requester = case["arbitration_requested_by"]
        if requester == role:
            raise CaseStateError("申请方不能确认自己的最终仲裁申请。")
        if case["status"] in {"ARBITRATING", "CLOSED"}:
            existing = self.get_arbitration_evidence(case_id)
            if existing:
                return existing
            raise CaseStateError("案件缺少冻结证据。")
        if case["status"] != "ARBITRATION_PENDING" or requester not in ROLES:
            raise CaseStateError("当前案件状态不允许确认最终仲裁。")

        statements = self.get_statements_for_llm(case_id)
        dispute = self.get_artifact(case_id, "DISPUTE_MAP")
        if not dispute or not dispute["content"]:
            raise CaseStateError("争议地图尚未完成。")
        messages = self.get_messages(case_id)
        frozen_at = _now()
        cutoff = messages[-1]["id"] if messages else 0
        snapshot = build_evidence_snapshot(
            case_id=case_id,
            created_at=frozen_at,
            requester=requester,
            confirmer=role,
            statements=statements,
            dispute_map=dispute["content"],
            messages=messages,
            message_cutoff_id=cutoff,
        )
        content = canonicalize_evidence(snapshot)
        snapshot_hash = evidence_hash(snapshot)
        key = (case_id, "ARBITRATION_EVIDENCE")
        existing = self.store["artifacts"].get(key)
        if existing and (
            existing["content"] != content
            or existing["evidence_hash"] != snapshot_hash
        ):
            raise CaseStateError("案件已经存在不同的冻结证据。")
        if not existing:
            self.store["artifacts"][key] = {
                "id": self._next("artifact_sequence"),
                "case_id": case_id,
                "kind": "ARBITRATION_EVIDENCE",
                "content": content,
                "evidence_hash": snapshot_hash,
                "created_at": frozen_at,
            }
        case["status"] = "ARBITRATING"
        case["paused_by"] = None
        case["arbitration_started_at"] = frozen_at
        self._touch(case)
        return self.get_arbitration_evidence(case_id)

    def get_arbitration_evidence(self, case_id):
        self._guard()
        record = self.store["artifacts"].get(
            (case_id, "ARBITRATION_EVIDENCE")
        )
        if not record:
            return None
        try:
            snapshot = load_evidence_snapshot(
                record["content"], record["evidence_hash"]
            )
        except EvidenceIntegrityError as exc:
            raise CaseStateError("冻结证据完整性校验失败。") from exc
        result = copy.deepcopy(record)
        result["snapshot"] = snapshot
        return result

    def get_artifact(self, case_id, kind):
        self._guard()
        if kind not in ARTIFACT_KINDS:
            raise ValueError("无效的仲裁产物类型。")
        artifact = self.store["artifacts"].get((case_id, kind))
        return copy.deepcopy(artifact) if artifact else None

    def save_checkpoint(self, case_id, kind, content, snapshot_hash):
        self._guard()
        if kind not in CHECKPOINT_ARTIFACT_KINDS:
            raise ValueError("无效的仲裁检查点类型。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("仲裁检查点内容不能为空。")
        snapshot_hash = _hash(snapshot_hash)
        self._case(case_id)
        key = (case_id, kind)
        existing = self.store["artifacts"].get(key)
        if existing:
            if (
                existing["content"] == content
                and existing["evidence_hash"] == snapshot_hash
            ):
                return existing["id"]
            raise CaseStateError("该仲裁检查点已由其他请求完成。")
        artifact_id = self._next("artifact_sequence")
        self.store["artifacts"][key] = {
            "id": artifact_id,
            "case_id": case_id,
            "kind": kind,
            "content": content,
            "evidence_hash": snapshot_hash,
            "created_at": _now(),
        }
        return artifact_id

    def claim_artifact(self, case_id, kind):
        self._guard()
        if kind not in PUBLIC_ARTIFACT_KINDS:
            raise ValueError("无效的仲裁产物类型。")
        case = self._case(case_id)
        key = (case_id, kind)
        if key in self.store["artifacts"]:
            return None
        required_status = (
            "READY_FOR_MAP" if kind == "DISPUTE_MAP" else "ARBITRATING"
        )
        if case["status"] != required_status:
            return None
        snapshot_hash = None
        if kind == "FINAL_JUDGMENT":
            evidence = self.get_arbitration_evidence(case_id)
            if not evidence:
                return None
            snapshot_hash = evidence["evidence_hash"]
        artifact_id = self._next("artifact_sequence")
        self.store["artifacts"][key] = {
            "id": artifact_id,
            "case_id": case_id,
            "kind": kind,
            "content": "",
            "evidence_hash": snapshot_hash,
            "created_at": _now(),
        }
        return artifact_id

    def complete_artifact(self, case_id, artifact_id, kind, content):
        self._guard()
        if kind not in PUBLIC_ARTIFACT_KINDS:
            raise ValueError("无效的仲裁产物类型。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("仲裁结果不能为空。")
        case = self._case(case_id)
        allowed_statuses = (
            {"READY_FOR_MAP", "MAP_READY"}
            if kind == "DISPUTE_MAP"
            else {"ARBITRATING", "CLOSED"}
        )
        if case["status"] not in allowed_statuses:
            raise CaseStateError("当前案件状态不允许完成该结果。")
        artifact = self.store["artifacts"].get((case_id, kind))
        if not artifact or artifact["id"] != artifact_id:
            raise CaseStateError("该结果已被其他请求完成或取消。")
        if artifact["content"] and artifact["content"] != content:
            raise CaseStateError("该结果已被其他请求完成或取消。")
        if kind == "FINAL_JUDGMENT":
            evidence = self.get_arbitration_evidence(case_id)
            if not evidence or artifact["evidence_hash"] != evidence["evidence_hash"]:
                raise CaseStateError("案件缺少冻结证据。")
        artifact["content"] = content.strip()
        case["status"] = "MAP_READY" if kind == "DISPUTE_MAP" else "CLOSED"
        case["paused_by"] = None
        self._touch(case)

    def release_artifact(self, case_id, artifact_id, kind):
        self._guard()
        artifact = self.store["artifacts"].get((case_id, kind))
        if artifact and artifact["id"] == artifact_id and not artifact["content"]:
            del self.store["artifacts"][(case_id, kind)]

    def get_messages(self, case_id):
        self._guard()
        return copy.deepcopy(self.store["messages"].get(case_id, []))

    def get_mediation_snapshot(self, case_id, last_message_id=0):
        self._guard()
        if not isinstance(last_message_id, int) or last_message_id < 0:
            raise ValueError("无效的消息游标。")
        case = self.get_case(case_id)
        if not case:
            return None
        return {
            "case": case,
            "artifact": self.get_artifact(case_id, "DISPUTE_MAP"),
            "messages": [
                message
                for message in self.get_messages(case_id)
                if message["id"] > last_message_id
            ],
        }

    def add_message(self, case_id, sender, content):
        self._guard()
        if sender not in MESSAGE_SENDERS:
            raise ValueError("无效的消息发送者。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("消息不能为空。")
        case = self._case(case_id)
        if case["status"] not in MESSAGE_ALLOWED_STATUSES:
            raise CaseStateError("当前案件状态不允许发送新消息。")
        message = {
            "id": self._next("message_sequence"),
            "case_id": case_id,
            "sender": sender,
            "content": content.strip(),
            "created_at": _now(),
        }
        self.store["messages"].setdefault(case_id, []).append(message)
        if case["status"] == "MAP_READY":
            case["status"] = "MEDIATING"
        self._touch(case)

    def ensure_judge_intervention_allowed(self, case_id):
        self._guard()
        case = self._case(case_id)
        if case["status"] not in MESSAGE_ALLOWED_STATUSES:
            raise CaseStateError("当前案件状态不允许请法官介入。")
        return True

    def pause_case(self, case_id, role):
        self._guard()
        _role(role)
        case = self._case(case_id)
        if case["status"] not in {"MAP_READY", "MEDIATING"}:
            return False
        case["status"] = "PAUSED"
        case["paused_by"] = role
        self._touch(case)
        return True

    def resume_case(self, case_id, role):
        self._guard()
        _role(role)
        case = self._case(case_id)
        if case["status"] != "PAUSED" or case["paused_by"] != role:
            return False
        case["status"] = "MEDIATING"
        case["paused_by"] = None
        self._touch(case)
        return True
