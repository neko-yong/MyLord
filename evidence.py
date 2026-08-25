import hashlib
import json
from datetime import datetime


EVIDENCE_VERSION = 1
EVIDENCE_ROLES = {"A", "B"}
EVIDENCE_SENDERS = {"A", "B", "JUDGE", "SYSTEM"}


class EvidenceIntegrityError(ValueError):
    pass


def _timestamp(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("证据时间不能为空。")


def canonicalize_evidence(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("证据快照必须是对象。")
    return json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def evidence_hash(snapshot):
    canonical = canonicalize_evidence(snapshot)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_evidence_snapshot(
    *,
    case_id,
    created_at,
    requester,
    confirmer,
    statements,
    dispute_map,
    messages,
    message_cutoff_id=None,
):
    if requester not in EVIDENCE_ROLES or confirmer not in EVIDENCE_ROLES:
        raise ValueError("仲裁申请方或确认方无效。")
    if requester == confirmer:
        raise ValueError("最终仲裁必须由另一方确认。")
    if not isinstance(statements, dict) or set(statements) != EVIDENCE_ROLES:
        raise ValueError("证据快照需要双方独立陈述。")
    if not isinstance(dispute_map, str) or not dispute_map.strip():
        raise ValueError("证据快照需要争议地图。")
    if not isinstance(messages, list):
        raise ValueError("共享调解记录格式无效。")

    message_ids = [
        message.get("id")
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("id"), int)
    ]
    if message_cutoff_id is None:
        message_cutoff_id = max(message_ids, default=0)
    if not isinstance(message_cutoff_id, int) or message_cutoff_id < 0:
        raise ValueError("消息截止 ID 无效。")

    frozen_messages = []
    seen_ids = set()
    for message in sorted(messages, key=lambda item: item.get("id", -1)):
        if not isinstance(message, dict):
            raise ValueError("共享调解记录格式无效。")
        message_id = message.get("id")
        if not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("共享调解消息 ID 无效。")
        if message_id > message_cutoff_id:
            continue
        if message_id in seen_ids:
            raise ValueError("共享调解消息 ID 重复。")
        sender = message.get("sender")
        content = message.get("content")
        if sender not in EVIDENCE_SENDERS:
            raise ValueError("共享调解消息发送者无效。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("共享调解消息内容无效。")
        frozen_messages.append(
            {
                "id": message_id,
                "sender": sender,
                "content": content,
                "created_at": _timestamp(message.get("created_at")),
            }
        )
        seen_ids.add(message_id)

    return {
        "version": EVIDENCE_VERSION,
        "created_at": _timestamp(created_at),
        "case_id": str(case_id),
        "a_statement": str(statements["A"]),
        "b_statement": str(statements["B"]),
        "dispute_map": dispute_map,
        "messages": frozen_messages,
        "message_cutoff_id": message_cutoff_id,
        "requester": requester,
        "confirmer": confirmer,
    }


def load_evidence_snapshot(content, expected_hash):
    if not isinstance(content, str) or not content:
        raise EvidenceIntegrityError("冻结证据不存在。")
    try:
        snapshot = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EvidenceIntegrityError("冻结证据格式无效。") from exc
    if canonicalize_evidence(snapshot) != content:
        raise EvidenceIntegrityError("冻结证据不是 canonical JSON。")
    actual_hash = evidence_hash(snapshot)
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise EvidenceIntegrityError("冻结证据 Hash 校验失败。")
    return snapshot


def format_evidence_history(snapshot):
    messages = snapshot.get("messages", [])
    return "\n\n".join(
        f"{message['sender']}: {message['content']}" for message in messages
    ) or "（无共享调解消息）"
