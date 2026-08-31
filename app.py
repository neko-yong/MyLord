import logging
import os
import time

import streamlit as st
import state_trace

from admin_console import is_admin_route, render_admin_console
from arbitration import retry_database_write, run_final_arbitration
from config import load_settings, secure_secret_matches
from database_resources import get_database
from db import (
    CaseStateError,
    DatabaseError,
    StatementAlreadySubmitted,
)
from dev_fixtures import fixture_options, get_fixture
from dev_memory_db import DevMemoryDatabase, new_dev_local_store
from dev_tools import (
    FAILURE_STAGES,
    SCENARIOS,
    FinalCompleteFailureDatabase,
    delete_dev_case,
    get_dev_state,
    is_dev_case,
    recreate_dev_case,
    seed_dev_case,
    switch_dev_role,
)
from dispute_map_view import render_dispute_map, render_mediation_context
from llm import LLMError, TASK_MAX_TOKENS, call_llm
from mock_llm import MockLLM
from performance import (
    finish_current_trace,
    finish_trace,
    instrument_database,
    observe_fragment as measure_fragment,
    record_llm_call,
    start_trace,
)
from prompts import (
    CORE_SYSTEM_PROMPT,
    DISPUTE_MAP_PROMPT,
    INTERVENTION_PROMPT,
)
from validation import build_statement_content, validate_statement_fields


def _load_server_settings():
    secret_values = st.secrets if st.secrets.load_if_toml_exists() else {}
    return load_settings(secret_values)


settings = _load_server_settings()
admin_route = is_admin_route(
    st.query_params,
    settings.admin_console_route_key,
)
st.set_page_config(
    page_title=(
        "Case maintenance" if admin_route else "双向关系仲裁员"
    ),
    page_icon="⚖️",
    layout="wide" if admin_route else "centered",
    initial_sidebar_state="collapsed" if admin_route else "auto",
)


STATUS_LABELS = {
    "COLLECTING": "等待双方独立陈述",
    "READY_FOR_MAP": "正在整理争议地图",
    "MAP_READY": "争议地图已就绪",
    "MEDIATING": "共享调解中",
    "PAUSED": "调解已暂停",
    "ARBITRATION_PENDING": "最终仲裁等待双方确认",
    "ARBITRATING": "最终仲裁进行中",
    "CLOSED": "最终仲裁已完成",
}
TAB_LABELS = (
    "① 独立陈述",
    "② 争议地图",
    "③ 调解室",
    "④ 最终仲裁",
)
TAB_VIEWS = dict(zip(TAB_LABELS, ("statement", "dispute", "mediation", "final")))


logger = logging.getLogger(__name__)


def state_trace_enabled():
    return os.environ.get("RERUN_STATE_TRACE") == "true"


def runtime_state():
    auth_state = st.session_state.get("auth") or {}
    snapshot = globals().get("page_snapshot") or {}
    case_state = snapshot.get("case") or {}
    rendered_tabs = globals().get("tabs", ())
    return dict(
        case_status=case_state.get("status", "unknown"),
        arbitration_state=case_state.get("status", "unknown"),
        snapshot_present=bool(snapshot),
        revision_after=state_trace.revision_fingerprint(snapshot.get("revision")),
        selected_tab_open_flags="".join("1" if tab.open else "0" for tab in rendered_tabs),
        authenticated=bool(auth_state),
        case_id_present=bool(auth_state.get("case_id")),
        role_present=auth_state.get("role") in {"A", "B"},
        selected_tab=TAB_VIEWS.get(st.session_state.get("case_tab", TAB_LABELS[0]), "invalid"),
        render_branch="none", llm_started=False, llm_finished=False,
    )


def trace_event(name, **fields):
    state_trace.event(name, **fields)


def trace_fragment(name):
    return state_trace.observe_fragment(name, state_trace_enabled, runtime_state)


def stop():
    if state_trace_enabled():
        trace_event("stop", **runtime_state())
    state_trace.finish_current()
    st.stop()


def show_database_error(error):
    st.error(str(error), icon=":material/database:")
    if settings.development_mode:
        with st.expander("开发信息", icon=":material/code:"):
            st.code(type(error).__name__)


def show_llm_error(error):
    st.error("AI 法官暂时无法响应，请稍后重试。", icon=":material/error:")
    st.toast(
        "本次 AI 内容没有写入，已保存的案件数据不受影响。",
        icon=":material/error:",
    )
    if settings.development_mode:
        with st.expander("开发信息", icon=":material/code:"):
            st.code(error.debug_summary())


CONFIRMATION_COPY = {
    "statement": {
        "heading": "确认提交并冻结？",
        "body": (
            "提交后，本次独立陈述将不能修改。\n\n"
            "对方不会直接看到你的陈述正文，但 AI 法官会读取双方陈述，"
            "用于争议地图与后续仲裁。"
        ),
        "cancel": "取消",
        "confirm": "确认提交",
        "icon": ":material/lock:",
    },
    "arbitration_request": {
        "heading": "确认申请进入最终仲裁？",
        "body": (
            "对方还需要确认。\n\n"
            "在对方确认以前，双方仍可继续调解。\n\n"
            "如果双方都确认，当前证据会被冻结；最终仲裁期间不能继续发言、"
            "暂停或请法官介入。"
        ),
        "cancel": "取消",
        "confirm": "确认申请",
        "icon": ":material/gavel:",
    },
    "arbitration_accept": {
        "heading": "确认进入最终仲裁？",
        "body": (
            "确认后，本轮证据将立即冻结。\n\n"
            "最终仲裁开始后：\n"
            "- 双方不能继续发送消息\n"
            "- 不能再请法官介入\n"
            "- 不能暂停或恢复\n"
            "- AI 将基于当前冻结材料完成双向复核"
        ),
        "cancel": "返回",
        "confirm": "确认并冻结证据",
        "icon": ":material/lock:",
    },
    "arbitration_decline": {
        "heading": "确认继续调解？",
        "body": (
            "这将拒绝对方当前的最终仲裁申请。\n\n"
            "案件会返回共享调解状态，双方可以继续沟通。"
        ),
        "cancel": "取消",
        "confirm": "确认继续调解",
        "icon": ":material/forum:",
    },
    "pause": {
        "heading": "确认暂停当前调解？",
        "body": (
            "暂停后，当前共享调解将暂时停止。\n\n"
            "双方不能继续发送普通调解消息，直到调解被恢复。\n\n"
            "按照当前规则，只有请求暂停的一方可以恢复调解。"
        ),
        "cancel": "取消",
        "confirm": "确认暂停",
        "icon": ":material/pause:",
    },
    "resume": {
        "heading": "确认恢复调解？",
        "body": "恢复后，双方可以继续发送调解消息并继续当前案件。",
        "cancel": "取消",
        "confirm": "确认恢复",
        "icon": ":material/play_arrow:",
    },
    "judge_intervention": {
        "heading": "确认请 AI 法官介入？",
        "body": (
            "AI 法官会读取当前共享调解上下文，生成一条中立调解意见，"
            "并将其加入双方可见的共享记录。\n\n"
            "该操作会生成新的 AI 调解内容。"
        ),
        "cancel": "取消",
        "confirm": "确认介入",
        "icon": ":material/gavel:",
    },
}


def queue_confirmation(action, case_id, role, payload=None):
    if action not in CONFIRMATION_COPY:
        raise ValueError("无效的确认操作。")
    st.session_state["_pending_confirmation"] = {
        "action": action,
        "case_id": case_id,
        "role": role,
        "payload": payload or {},
    }


def clear_pending_confirmation():
    st.session_state.pop("_pending_confirmation", None)


def build_dispute_map_prompt(statements):
    return f"""以下是两份独立陈述。

===== A =====
{statements['A']}

===== B =====
{statements['B']}

{DISPUTE_MAP_PROMPT}
"""


def run_reserved_dispute_map(case_id, reservation_id):
    statements = database.get_statements_for_llm(case_id)
    with st.spinner("AI 法官正在整理双方事实、分歧和待确认事项…"):
        result = ask(
            DISPUTE_MAP_PROMPT,
            build_dispute_map_prompt(statements),
            max_tokens=TASK_MAX_TOKENS["DISPUTE_MAP"],
        )
    database.complete_artifact(
        case_id,
        reservation_id,
        "DISPUTE_MAP",
        result.content,
    )


def mark_dispute_map_failed(case_id, reservation_id):
    try:
        database.fail_artifact(case_id, reservation_id, "DISPUTE_MAP")
    except DatabaseError:
        # Keep the unfinished reservation. The stale-reservation guard prevents storms.
        pass


def finish_dispute_map_generation(case_id, reservation_id):
    try:
        run_reserved_dispute_map(case_id, reservation_id)
    except LLMError as error:
        mark_dispute_map_failed(case_id, reservation_id)
        show_llm_error(error)
        st.warning(
            "独立陈述已经成功冻结，争议地图尚未生成。请稍后重新尝试。"
        )
        return False
    except DatabaseError as error:
        mark_dispute_map_failed(case_id, reservation_id)
        show_database_error(error)
        return False
    return True


def render_automatic_dispute_map(case_id, current_case, submitted, dispute):
    if not (submitted["A"] and submitted["B"]):
        return
    if current_case["status"] != "READY_FOR_MAP":
        return

    st.success("双方独立陈述已提交并冻结。", icon=":material/check_circle:")
    if dispute and dispute["content"]:
        return
    if dispute and dispute.get("generation_failed_at"):
        st.warning(
            "AI 法官暂时未能完成争议地图整理。独立陈述仍保持冻结。"
        )
        if st.button(
            "重新尝试整理争议地图",
            icon=":material/refresh:",
            key=f"retry_dispute_map_{case_id}",
        ):
            try:
                reservation_id = database.retry_failed_artifact(
                    case_id,
                    "DISPUTE_MAP",
                )
            except DatabaseError as error:
                show_database_error(error)
                return
            if reservation_id is None:
                st.info("另一请求已经开始重试，请稍后查看。")
                return
            if finish_dispute_map_generation(case_id, reservation_id):
                rerun()
        return
    if dispute:
        st.info(
            "AI 法官正在整理双方事实、分歧与待确认事项……",
            icon=":material/hourglass_top:",
        )
        return
    if not llm_available():
        st.error("AI 法官尚未由网站管理员配置，争议地图暂时无法整理。")
        return

    try:
        reservation_id = database.claim_artifact(case_id, "DISPUTE_MAP")
    except DatabaseError as error:
        show_database_error(error)
        return
    if reservation_id is None:
        st.info(
            "AI 法官正在整理双方事实、分歧与待确认事项……",
            icon=":material/hourglass_top:",
        )
        return
    if finish_dispute_map_generation(case_id, reservation_id):
        rerun()


def run_judge_intervention(case_id):
    database.ensure_judge_intervention_allowed(case_id)
    statements = database.get_statements_for_llm(case_id)
    dispute = database.get_artifact(case_id, "DISPUTE_MAP")
    if not dispute or not dispute["content"]:
        raise CaseStateError("争议地图尚未完成。")
    history = "\n\n".join(
        f"{message['sender']}: {message['content']}"
        for message in database.get_messages(case_id)
    ) or "（目前尚无共享消息）"
    prompt = f"""===== A 独立陈述 =====
{statements['A']}

===== B 独立陈述 =====
{statements['B']}

===== 争议地图 =====
{dispute['content']}

===== 共享调解历史 =====
{history}

{INTERVENTION_PROMPT}
"""
    with st.spinner("法官正在聚焦当前争议…"):
        result = ask(
            INTERVENTION_PROMPT,
            prompt,
            max_tokens=TASK_MAX_TOKENS["INTERVENTION"],
        )
    database.add_message(case_id, "JUDGE", result.content)


def release_reservation(case_id, artifact_id, kind):
    try:
        retry_database_write(
            lambda: database.release_artifact(case_id, artifact_id, kind)
        )
    except DatabaseError:
        # A later page refresh will still show the unfinished reservation.
        pass


def start_or_resume_final_arbitration(case_id):
    if not llm_available():
        st.error("AI 法官尚未由网站管理员配置。")
        return False
    try:
        reservation_id = database.claim_artifact(case_id, "FINAL_JUDGMENT")
    except DatabaseError as error:
        show_database_error(error)
        return False
    if reservation_id is None:
        st.info("最终仲裁已由另一请求执行，或当前执行尚未超过安全恢复时间。")
        return False

    try:
        arbitration_database = database
        failure_state = st.session_state.get("dev_failure_state", {})
        if (
            settings.dev_mode
            and failure_state.get("stage") == "FINAL_DB_COMPLETE"
        ):
            arbitration_database = FinalCompleteFailureDatabase(
                settings,
                database,
                failure_state,
            )
        with st.spinner("正在依据冻结证据进行双向复核仲裁…"):
            run_final_arbitration(
                database=arbitration_database,
                ask_llm=ask,
                case_id=case_id,
                reservation_id=reservation_id,
                dual_review=True,
            )
    except LLMError as error:
        release_reservation(case_id, reservation_id, "FINAL_JUDGMENT")
        show_llm_error(error)
        return False
    except DatabaseError as error:
        release_reservation(case_id, reservation_id, "FINAL_JUDGMENT")
        show_database_error(error)
        return False
    return True


def execute_confirmed_action(pending):
    action = pending["action"]
    pending_case_id = pending["case_id"]
    pending_role = pending["role"]
    payload = pending.get("payload", {})

    if action == "statement":
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("待提交的独立陈述无效。")
        database.save_statement(pending_case_id, pending_role, content)
    elif action == "arbitration_request":
        trace_event("db_request_started")
        database.request_arbitration(pending_case_id, pending_role)
        trace_event("db_request_finished")
        trace_event("state_transition", arbitration_state="ARBITRATION_PENDING")
    elif action == "arbitration_accept":
        trace_event("evidence_freeze_started")
        database.confirm_arbitration(pending_case_id, pending_role)
        trace_event("evidence_freeze_finished")
        trace_event("state_transition", arbitration_state="ARBITRATING")
        if not start_or_resume_final_arbitration(pending_case_id):
            st.session_state["_interaction_notice"] = (
                "证据已冻结；模型流程可在服务恢复后从同一 Snapshot 继续。"
            )
    elif action == "arbitration_decline":
        database.cancel_arbitration_request(pending_case_id, pending_role)
    elif action == "pause":
        if not database.pause_case(pending_case_id, pending_role):
            st.session_state["_interaction_notice"] = "案件状态已经变化。"
    elif action == "resume":
        if not database.resume_case(pending_case_id, pending_role):
            st.session_state["_interaction_notice"] = "案件状态已经变化。"
    elif action == "judge_intervention":
        run_judge_intervention(pending_case_id)
    else:
        raise ValueError("无效的确认操作。")


@st.dialog(
    "确认操作",
    dismissible=False,
    icon=":material/warning:",
)
@measure_fragment("confirmation_dialog", lambda: settings.perf_debug)
@trace_fragment("confirmation_dialog")
def confirmation_dialog(pending):
    copy = CONFIRMATION_COPY[pending["action"]]
    st.markdown(f"### {copy['heading']}")
    st.markdown(copy["body"])
    with st.container(horizontal=True, horizontal_alignment="right"):
        cancel = st.button(
            copy["cancel"],
            key=f"cancel_{pending['action']}",
        )
        confirm = st.button(
            copy["confirm"],
            type="primary",
            icon=copy["icon"],
            key=f"confirm_{pending['action']}",
        )
    if cancel:
        clear_pending_confirmation()
        rerun()
    if not confirm:
        return

    trace_event("button_click")
    try:
        execute_confirmed_action(pending)
    except StatementAlreadySubmitted:
        clear_pending_confirmation()
        st.session_state["_interaction_notice"] = "独立陈述已经提交并冻结。"
        rerun()
    except CaseStateError as error:
        trace_event("action_failed")
        st.warning(str(error))
    except DatabaseError as error:
        trace_event("action_failed")
        show_database_error(error)
    except LLMError as error:
        trace_event("action_failed")
        show_llm_error(error)
    else:
        clear_pending_confirmation()
        rerun()


def render_pending_confirmation(case_id, role):
    pending = st.session_state.get("_pending_confirmation")
    if not pending:
        return False
    if (
        pending.get("action") not in CONFIRMATION_COPY
        or pending.get("case_id") != case_id
        or pending.get("role") != role
    ):
        clear_pending_confirmation()
        return False
    confirmation_dialog(pending)
    return True


@st.dialog(
    "案件进展",
    dismissible=False,
    icon=":material/notifications:",
)
@measure_fragment("notification_dialog", lambda: settings.perf_debug)
@trace_fragment("notification_dialog")
def notification_dialog(notification, case_id, role):
    actor = notification["actor_role"]
    if notification["event_type"] == "ARBITRATION_ACCEPTED":
        st.markdown(f"### {actor} 已同意进入最终仲裁")
        st.markdown("当前证据已冻结。\n\nAI 法官正在进行双向复核。")
    elif notification["event_type"] == "ARBITRATION_DECLINED":
        st.markdown(f"### {actor} 选择继续调解")
        st.markdown(
            "本次最终仲裁申请已取消。\n\n"
            "你们可以继续在共享调解室沟通。"
        )
    else:
        st.error("收到无法识别的案件通知。")
        return

    if st.button(
        "知道了",
        type="primary",
        icon=":material/check:",
        width="stretch",
        key=f"ack_notification_{notification['id']}",
    ):
        try:
            database.mark_notification_read(
                case_id,
                notification["id"],
                role,
            )
            trace_event("notification_ack")
        except DatabaseError as error:
            show_database_error(error)
        else:
            rerun()


def ask(
    system_extra,
    user_text,
    temperature=0.2,
    max_tokens=TASK_MAX_TOKENS["DISPUTE_MAP"],
):
    llm_mode = selected_llm_mode()
    started = time.perf_counter()
    trace_event("llm_started", llm_started=True, llm_finished=False)
    try:
        if llm_mode == "mock":
            result = active_mock_llm()(
                system_extra,
                user_text,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            if settings.dev_mode and not st.session_state.get(
                "dev_real_llm_confirmed",
                False,
            ):
                raise LLMError(
                    "dev_real_confirmation",
                    "Real LLM use requires developer confirmation.",
                )
            result = call_llm(
                endpoint=settings.llm_endpoint,
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                system_prompt=CORE_SYSTEM_PROMPT + "\n\n" + system_extra,
                user_prompt=user_text,
                temperature=temperature,
                max_tokens=max_tokens,
            )
    except LLMError:
        trace_event("llm_failed")
        raise
    else:
        trace_event("llm_finished", llm_finished=True)
    finally:
        record_llm_call(
            llm_mode,
            (time.perf_counter() - started) * 1000,
            "ask",
        )
    logger.info(
        "llm_result model=%s finish_reason=%s prompt_tokens=%s "
        "completion_tokens=%s total_tokens=%s latency_ms=%.2f",
        result.model,
        result.finish_reason,
        result.prompt_tokens,
        result.completion_tokens,
        result.total_tokens,
        result.latency_ms,
    )
    return result


def observe_fragment(name):
    enabled = (
        settings.perf_debug
        or settings.development_mode
        or os.environ.get("REALTIME_GATE_OBSERVE") == "true"
    )
    return measure_fragment(name, enabled)


def rerun(scope="app", reason="explicit"):
    trace_event("rerun_requested", requested_scope=scope)
    if state_trace_enabled():
        st.session_state["_rerun_trace_reason"] = reason
    state_trace.finish_current()
    finish_current_trace()
    st.rerun(scope=scope)


performance_trace = start_trace(settings.perf_debug, "full_rerun", "app")
st.session_state.setdefault("auth", None)
runtime_trace = None
if state_trace_enabled():
    current_view = runtime_state()["selected_tab"]
    trace_reason = st.session_state.pop("_rerun_trace_reason", "initial_or_widget")
    if current_view != st.session_state.get("_rerun_trace_tab", current_view):
        trace_reason = "tab_change"
    st.session_state["_rerun_trace_tab"] = current_view
    runtime_trace = state_trace.start(True, "app", trace_reason, **runtime_state())
if admin_route:
    render_admin_console(settings)
    stop()
if settings.dev_mode:
    st.session_state.setdefault(
        "dev_database_mode",
        settings.dev_database_mode,
    )


def selected_llm_mode():
    if not settings.dev_mode:
        return "real"
    mode = st.session_state.get("dev_llm_mode", settings.llm_mode)
    if mode not in {"mock", "real"}:
        raise LLMError("dev_llm_mode", "Invalid developer LLM mode.")
    return mode


def selected_database_mode():
    if not settings.dev_mode:
        return "postgres"
    mode = st.session_state.get(
        "dev_database_mode",
        settings.dev_database_mode,
    )
    if mode == "real":
        mode = "postgres"
    if mode not in {"local", "postgres"}:
        raise PermissionError("Invalid developer database mode.")
    return mode


def active_dev_case():
    if not settings.dev_mode:
        return None
    dev_case = st.session_state.get("dev_case")
    case_id = getattr(dev_case, "case_id", None)
    if not case_id:
        return None
    if not is_dev_case(settings, database, case_id):
        return None
    return dev_case


def active_mock_llm():
    if not settings.dev_mode or selected_llm_mode() != "mock":
        raise PermissionError("Developer Mock LLM is disabled.")
    dev_case = active_dev_case()
    if not dev_case:
        raise LLMError(
            "dev_mock_case",
            "Mock LLM is limited to the active DEV_TEST case.",
        )
    calls = st.session_state.setdefault("dev_mock_calls", {})
    failure_state = st.session_state.setdefault(
        "dev_failure_state",
        {"stage": "NONE", "triggered": False, "attempts": 0},
    )
    return MockLLM(
        settings,
        get_fixture(dev_case.fixture_key),
        calls,
        failure_state,
    )


def llm_available():
    if not settings.dev_mode:
        return settings.llm_ready
    if selected_llm_mode() == "mock":
        return active_dev_case() is not None
    return settings.llm_ready and st.session_state.get(
        "dev_real_llm_confirmed",
        False,
    )


def clear_dev_case_state():
    for key in (
        "dev_case",
        "dev_view_role",
        "dev_role_selector",
        "dev_mock_calls",
        "dev_failure_state",
        "_pending_confirmation",
        "_interaction_notice",
    ):
        st.session_state.pop(key, None)
    for key in tuple(st.session_state):
        if str(key).startswith("_message_cache_"):
            st.session_state.pop(key, None)


def prepare_database_mode():
    mode = selected_database_mode()
    if not settings.dev_mode:
        return mode
    previous = st.session_state.get("_dev_database_mode_active")
    if previous is not None and previous != mode:
        clear_dev_case_state()
        st.session_state["_dev_database_mode_changed"] = True
    st.session_state["_dev_database_mode_active"] = mode
    return mode


def render_developer_playground():
    if not settings.dev_mode:
        return

    fixture_by_label = fixture_options()
    if st.session_state.pop("_dev_reset_pending", False):
        st.session_state.dev_failure_stage = "NONE"
    st.session_state.setdefault("dev_llm_mode", settings.llm_mode)
    st.session_state.setdefault("dev_fixture_label", next(iter(fixture_by_label)))
    st.session_state.setdefault("dev_scenario", "MEDIATING")
    st.session_state.setdefault("dev_failure_stage", "NONE")
    st.session_state.setdefault("dev_real_llm_confirmed", False)

    with st.sidebar.expander("🛠 Developer Playground", expanded=False):
        database_mode = st.segmented_control(
            "Database Mode",
            ("local", "postgres"),
            format_func=lambda value: (
                "⚡ Fast Local" if value == "local" else "🌐 Real PostgreSQL"
            ),
            key="dev_database_mode",
            required=True,
            width="stretch",
        )
        if database_mode not in {"local", "postgres"}:
            st.error("数据库模式无效。")
            stop()

        with st.container(horizontal=True):
            st.badge("DEV_MODE: ON", color="green")
            st.badge(
                f"LLM: {selected_llm_mode().title()}",
                color="blue",
            )
            st.badge(
                "Database: Fast Local"
                if database_mode == "local"
                else "Database: Real PostgreSQL",
                color="green" if database_mode == "local" else "orange",
            )

        if st.session_state.pop("_dev_database_mode_changed", False):
            st.warning("切换数据库模式后，需要创建新的测试案件。")
        if database_mode == "local":
            st.info(
                "Fast Local：仅当前开发 Session；无公网数据库请求，"
                "用于 UI / workflow development only。"
            )
        else:
            st.warning(
                "⚠ 当前操作将访问远程 PostgreSQL，响应速度会明显慢于 Fast Local。"
            )

        fixture_label = st.selectbox(
            "Fixture",
            tuple(fixture_by_label),
            key="dev_fixture_label",
        )
        scenario = st.selectbox(
            "Scenario",
            SCENARIOS,
            key="dev_scenario",
        )
        llm_mode = st.selectbox(
            "LLM Mode",
            ("mock", "real"),
            format_func=lambda value: value.title(),
            key="dev_llm_mode",
        )
        failure_stage = st.selectbox(
            "模拟失败阶段",
            FAILURE_STAGES,
            format_func=lambda value: "无" if value == "NONE" else value,
            key="dev_failure_stage",
        )
        previous_failure = st.session_state.get("dev_failure_state", {})
        if previous_failure.get("stage") != failure_stage:
            st.session_state.dev_failure_state = {
                "stage": failure_stage,
                "triggered": False,
                "attempts": 0,
            }

        if llm_mode == "real":
            st.warning("⚠ 将产生真实模型调用与费用。")
            st.checkbox(
                "确认使用真实 LLM",
                key="dev_real_llm_confirmed",
            )
        else:
            st.session_state.dev_real_llm_confirmed = False

        if database_mode == "local" and st.button(
            "Reset Local Playground",
            icon=":material/restart_alt:",
            width="stretch",
            key="dev_reset_local",
        ):
            database.reset()
            clear_dev_case_state()
            st.session_state._dev_reset_pending = True
            rerun()

        if st.button(
            "创建测试案件",
            type="primary",
            icon=":material/add:",
            width="stretch",
            key="dev_create_case",
        ):
            if llm_mode == "real" and not st.session_state.get(
                "dev_real_llm_confirmed",
                False,
            ):
                st.error("请先确认真实 LLM 调用与费用。")
            else:
                try:
                    dev_case = seed_dev_case(
                        settings,
                        database,
                        fixture_by_label[fixture_label],
                        scenario,
                    )
                except (DatabaseError, LLMError, RuntimeError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.session_state.dev_case = dev_case
                    st.session_state.dev_view_role = "A"
                    st.session_state.dev_mock_calls = dict(
                        dev_case.seed_mock_calls
                    )
                    st.session_state.dev_failure_state = {
                        "stage": failure_stage,
                        "triggered": False,
                        "attempts": 0,
                    }
                    rerun()

        dev_case = active_dev_case()
        if not dev_case:
            if st.session_state.get("dev_case") is not None:
                clear_dev_case_state()
            return

        st.divider()
        st.markdown("#### 当前 Dev Case")
        state = get_dev_state(settings, database, dev_case.case_id)
        view_role = st.segmented_control(
            "查看身份",
            ("A", "B"),
            default=st.session_state.get("dev_view_role", "A"),
            key="dev_role_selector",
        )
        st.session_state.dev_view_role = switch_dev_role(
            settings,
            database,
            dev_case.case_id,
            view_role,
        )
        st.markdown(
            "\n".join(
                (
                    f"- Case ID: `{state['case_id']}`",
                    f"- Status: `{state['status']}`",
                    f"- Role View: `{st.session_state.dev_view_role}`",
                    f"- A Submitted: `{'YES' if state['a_submitted'] else 'NO'}`",
                    f"- B Submitted: `{'YES' if state['b_submitted'] else 'NO'}`",
                    f"- Dispute Map: `{'YES' if state['dispute_map'] else 'NO'}`",
                    f"- Messages: `{state['message_count']}`",
                    f"- Paused By: `{state['paused_by'] or 'NONE'}`",
                    f"- Arbitration Request: `{state['arbitration_request'] or 'NONE'}`",
                    f"- Evidence Snapshot: `{'YES' if state['evidence'] else 'NO'}`",
                    f"- Evidence Cutoff: `{state['evidence_cutoff'] if state['evidence_cutoff'] is not None else 'N/A'}`",
                    f"- Evidence Hash: `{state['evidence_hash_preview'] or 'N/A'}`",
                    f"- J1: `{'DONE' if state['judgment_normal'] else 'PENDING'}`",
                    f"- J2: `{'DONE' if state['judgment_swapped'] else 'PENDING'}`",
                    f"- Meta: `{'DONE' if state['meta'] else 'PENDING'}`",
                    f"- Final: `{'DONE' if state['final'] else 'PENDING'}`",
                )
            )
        )

        st.markdown("##### Mock Calls")
        calls = st.session_state.get("dev_mock_calls", {})
        for stage in (
            "DISPUTE_MAP",
            "JUDGE_INTERVENTION",
            "JUDGMENT_NORMAL",
            "JUDGMENT_SWAPPED",
            "META_JUDGMENT",
        ):
            st.caption(f"{stage}: {calls.get(stage, 0)}")

        with st.container(horizontal=True):
            if st.button(
                "刷新状态",
                icon=":material/refresh:",
                key="dev_refresh",
            ):
                rerun()
            if st.button(
                "重新创建相同场景",
                icon=":material/replay:",
                key="dev_recreate",
            ):
                try:
                    replacement = recreate_dev_case(
                        settings,
                        database,
                        dev_case.case_id,
                        dev_case.fixture_key,
                        dev_case.scenario,
                    )
                except (DatabaseError, LLMError, RuntimeError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.session_state.dev_case = replacement
                    st.session_state.dev_view_role = "A"
                    st.session_state.dev_mock_calls = dict(
                        replacement.seed_mock_calls
                    )
                    st.session_state.dev_failure_state = {
                        "stage": failure_stage,
                        "triggered": False,
                        "attempts": 0,
                    }
                    rerun()

        if st.button(
            "删除当前测试案件",
            icon=":material/delete:",
            width="stretch",
            key="dev_delete",
        ):
            try:
                delete_dev_case(settings, database, dev_case.case_id)
            except (DatabaseError, RuntimeError, ValueError) as error:
                st.error(str(error))
            else:
                clear_dev_case_state()
                rerun()


def current_auth():
    dev_case = active_dev_case()
    if dev_case:
        role = switch_dev_role(
            settings,
            database,
            dev_case.case_id,
            st.session_state.get("dev_view_role", "A"),
        )
        return {"case_id": dev_case.case_id, "role": role, "dev": True}
    return st.session_state.auth

st.title("⚖️ 双向关系仲裁员")
st.caption("独立陈述 → 争议地图 → 共享调解 → 暂停 / 恢复 → 双向复核仲裁")

service_status = st.sidebar.container()

database_mode = prepare_database_mode()
AUTO_REFRESH_INTERVAL = None if database_mode == "local" else "2s"

if database_mode == "postgres" and not settings.database_url:
    with service_status:
        st.subheader("服务状态")
        st.badge("数据库未配置", color="red", icon=":material/database:")
        if settings.llm_ready:
            st.badge("AI 法官已就绪", color="green", icon=":material/smart_toy:")
        else:
            st.badge("AI 法官未配置", color="orange", icon=":material/smart_toy:")
    st.error(
        "数据库尚未配置。请由网站管理员设置 DATABASE_URL。",
        icon=":material/database:",
    )
    st.caption("生产环境不会自动回落到本地 SQLite。")
    stop()

if database_mode == "local":
    if "_dev_local_store" not in st.session_state:
        st.session_state._dev_local_store = new_dev_local_store(settings)
    database = DevMemoryDatabase(
        settings,
        st.session_state._dev_local_store,
        database_mode=database_mode,
    )
else:
    try:
        with st.spinner("正在连接共享数据库…"):
            database = get_database(settings.database_url)
    except DatabaseError as error:
        with service_status:
            st.subheader("服务状态")
            st.badge("数据库连接失败", color="red", icon=":material/database:")
        show_database_error(error)
        stop()

database = instrument_database(database, settings.perf_debug)

render_developer_playground()
auth = current_auth()

with service_status:
    st.subheader("服务状态")
    if database_mode == "local":
        st.badge("Fast Local 已就绪", color="green", icon=":material/bolt:")
    else:
        st.badge("数据库已连接", color="green", icon=":material/database:")
    if settings.llm_ready:
        st.badge("AI 法官已就绪", color="green", icon=":material/smart_toy:")
    else:
        st.badge("AI 法官未配置", color="orange", icon=":material/smart_toy:")
    st.caption("AI 法官用于关系调解与结构化分析，不是法律裁判。")

    if auth:
        st.caption(f"当前案件：{auth['case_id']}")
        st.caption(f"当前身份：{auth['role']}")
        if st.button("退出案件", icon=":material/logout:", width="stretch"):
            if auth.get("dev"):
                clear_dev_case_state()
            else:
                st.session_state.auth = None
            rerun()


if not auth:
    with st.container(border=True):
        st.subheader("进入已有案件")
        with st.form("login_form"):
            login_case_id = st.text_input(
                "Case ID",
                placeholder="CASE-XXXXXX",
                max_chars=32,
            )
            login_token = st.text_input(
                "个人密钥",
                type="password",
                placeholder="A-… 或 B-…",
            )
            login_submitted = st.form_submit_button(
                "进入案件",
                type="primary",
                icon=":material/login:",
                width="stretch",
            )

        if login_submitted:
            try:
                role = database.authenticate(login_case_id, login_token)
            except DatabaseError as error:
                show_database_error(error)
            else:
                if not role:
                    st.error("Case ID 或个人密钥不正确。")
                else:
                    st.session_state.auth = {
                        "case_id": login_case_id.strip().upper(),
                        "role": role,
                    }
                    rerun()

    create_expander = st.expander(
        "创建新案件",
        icon=":material/add_circle:",
    )
    with create_expander:
        if not settings.admin_create_ready:
            st.warning("创建功能尚未由网站管理员配置。")
        else:
            with st.form("create_case_form"):
                admin_secret = st.text_input(
                    "管理员创建口令",
                    type="password",
                )
                title = st.text_input(
                    "案件标题",
                    placeholder="例如：关于周末安排的一次争吵",
                    max_chars=200,
                )
                create_submitted = st.form_submit_button(
                    "创建案件",
                    type="primary",
                    icon=":material/add:",
                    width="stretch",
                )

            if create_submitted:
                if not secure_secret_matches(
                    admin_secret,
                    settings.admin_create_secret,
                ):
                    st.error("创建口令不正确。")
                else:
                    try:
                        new_case_id, a_token, b_token = database.create_case(title)
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        st.success("案件创建成功。请立即保存以下信息。")
                        st.markdown("**Case ID**")
                        st.code(new_case_id, language=None)
                        st.markdown("**A 私密密钥**")
                        st.code(a_token, language=None)
                        st.markdown("**B 私密密钥**")
                        st.code(b_token, language=None)
                        st.warning(
                            "服务器只保存密钥 Hash，无法恢复原始 A/B 密钥。\n\n"
                            "不要把 A、B 两个密钥一起发给同一个人；"
                            "每个人只保留自己的密钥。"
                        )

    st.info(
        "双方的独立陈述默认不会直接展示给对方。\n\n"
        "AI 法官会读取双方陈述，用于生成结构化争议地图与仲裁分析。",
        icon=":material/privacy_tip:",
    )
    stop()


case_id = auth.get("case_id", "")
role = auth.get("role")
if role not in {"A", "B"}:
    if auth.get("dev"):
        clear_dev_case_state()
    else:
        st.session_state.auth = None
    st.error("当前登录状态无效，请重新进入案件。")
    stop()

selected_tab = st.session_state.get("case_tab", TAB_LABELS[0])
if selected_tab not in TAB_VIEWS:
    selected_tab = TAB_LABELS[0]
    st.session_state["case_tab"] = selected_tab
selected_view = TAB_VIEWS[selected_tab]
message_cache_key = f"_message_cache_{case_id}"
cached_messages = st.session_state.get(message_cache_key, [])
if not isinstance(cached_messages, list):
    cached_messages = []
last_message_id = (
    cached_messages[-1]["id"]
    if selected_view == "mediation" and cached_messages
    else 0
)

try:
    if state_trace_enabled():
        trace_event("snapshot_started", selected_tab=selected_view,
                    snapshot_present=False, revision_before=state_trace.revision_fingerprint(
                        st.session_state.get(f"_case_revision_{case_id}_{role}")))
    page_snapshot = database.get_case_view_snapshot(
        case_id,
        role,
        selected_view,
        last_message_id,
    )
except DatabaseError as error:
    trace_event("snapshot_failed")
    show_database_error(error)
    stop()

if not page_snapshot:
    trace_event("snapshot_missing", snapshot_present=False)
    if auth.get("dev"):
        clear_dev_case_state()
    else:
        st.session_state.auth = None
    st.error("案件不存在或已不可用，请重新进入。")
    stop()

case = page_snapshot["case"]
if state_trace_enabled():
    trace_event("snapshot_refreshed", snapshot_present=True, case_status=case["status"],
                arbitration_state=case["status"],
                revision_after=state_trace.revision_fingerprint(page_snapshot["revision"]))
other = "B" if role == "A" else "A"
st.subheader(case["title"])
if interaction_notice := st.session_state.pop("_interaction_notice", None):
    st.warning(interaction_notice)

render_pending_confirmation(case_id, role)
if (
    not st.session_state.get("_pending_confirmation")
    and page_snapshot["unread_notifications"]
):
    notification_dialog(
        page_snapshot["unread_notifications"][0],
        case_id,
        role,
    )

submitted = page_snapshot["submitted"]
label = STATUS_LABELS.get(case["status"], case["status"])
st.markdown(f"**案件状态：** {label}")
with st.container(horizontal=True):
    st.badge(
        "A 已提交" if submitted["A"] else "A 待提交",
        color="green" if submitted["A"] else "gray",
    )
    st.badge(
        "B 已提交" if submitted["B"] else "B 待提交",
        color="green" if submitted["B"] else "gray",
    )
if database_mode == "local":
    st.caption(
        "Fast Local 通过当前 Session 的页面操作刷新状态；"
        "对方的独立陈述正文不会显示。"
    )
else:
    st.caption("页面每 2 秒同步案件状态；对方的独立陈述正文不会显示。")
render_automatic_dispute_map(
    case_id,
    case,
    submitted,
    page_snapshot["artifacts"].get("DISPUTE_MAP"),
)

revision_key = f"_case_revision_{case_id}_{role}"
st.session_state[revision_key] = page_snapshot["revision"]
sync_skip_key = f"_skip_case_sync_{case_id}_{role}"
st.session_state[sync_skip_key] = True


@st.fragment(run_every=AUTO_REFRESH_INTERVAL)
@observe_fragment("case_sync")
@trace_fragment("case_sync")
def live_case_sync():
    if st.session_state.pop(sync_skip_key, False):
        trace_event("poll_skipped")
        return
    try:
        trace_event("poll_started")
        fresh_revision = database.get_case_revision(case_id, role)
    except DatabaseError as error:
        trace_event("poll_failed")
        show_database_error(error)
        return
    if state_trace_enabled():
        trace_event("poll_finished",
                    revision_before=state_trace.revision_fingerprint(st.session_state.get(revision_key)),
                    revision_after=state_trace.revision_fingerprint(fresh_revision))
    if fresh_revision != st.session_state.get(revision_key):
        rerun(reason="revision_changed")


live_case_sync()

if selected_view == "mediation":
    st.session_state[message_cache_key] = (
        cached_messages + page_snapshot["messages"]
    )

tabs = st.tabs(
    TAB_LABELS,
    key="case_tab",
    on_change="rerun",
)
trace_event("tabs_registered", selected_tab_open_flags="".join("1" if tab.open else "0" for tab in tabs))

if tabs[0].open:
    trace_event("render_branch_entered", render_branch="statement")
    with tabs[0]:
        st.markdown("### 你的独立陈述")
        st.caption("这一阶段互相不可见。提交后冻结，避免看到对方版本后修改自己的叙述。")

        my_statement = page_snapshot["statement"]

        if my_statement:
            st.success("你已经提交，当前版本已冻结。")
            st.markdown(my_statement["content"])
        else:
            validation_feedback = st.container()
            with st.form("statement_form"):
                start = st.text_area("1. 事情是怎么开始 / 发生的？（必填）")
                timeline = st.text_area("2. 关键时间线（选填）")
                complaint = st.text_area(
                    "3. 对方哪些具体行为让你不满？（必填）",
                    placeholder=(
                        "尽量描述具体行为、原话或事件，而不是只评价对方是什么样的人。"
                    ),
                )
                own = st.text_area("4. 你当时具体做了什么？（必填）")
                emotion = st.text_area("5. 当时的情绪（选填）")
                need = st.text_area("6. 你真正需要 / 在意的是什么？（必填）")
                request = st.text_area(
                    "7. 你希望对方做什么 / 希望这次解决什么？（必填）"
                )
                self_reflect = st.text_area(
                    "8. 你认为自己可能哪里做得不好？（选填）"
                )
                evidence = st.text_area(
                    "9. 原话 / 聊天记录 / 其他补充（选填）"
                )
                submitted_form = st.form_submit_button(
                    "提交并冻结",
                    type="primary",
                    icon=":material/lock:",
                    width="stretch",
                )

            if submitted_form:
                values = {
                    "start": start,
                    "timeline": timeline,
                    "complaint": complaint,
                    "own": own,
                    "emotion": emotion,
                    "need": need,
                    "request": request,
                    "self_reflect": self_reflect,
                    "evidence": evidence,
                }
                cleaned, validation_errors = validate_statement_fields(values)
                if validation_errors:
                    details = "\n".join(
                        f"- {message}" for message in validation_errors.values()
                    )
                    validation_feedback.error(
                        "还有必填内容没有完成，请补充后再提交。\n\n"
                        f"缺少或内容过短：\n{details}"
                    )
                else:
                    content = build_statement_content(role, cleaned)
                    queue_confirmation(
                        "statement",
                        case_id,
                        role,
                        {"content": content},
                    )
                    rerun()

        st.info(
            f"你只能看到自己的陈述正文。页面顶部会同步 {other} 是否已经提交。",
            icon=":material/visibility_off:",
        )

if tabs[1].open:
    trace_event("render_branch_entered", render_branch="dispute")
    with tabs[1]:
        st.markdown("### 争议地图")
        submission_status = page_snapshot["submitted"]
        dispute = page_snapshot["artifacts"].get("DISPUTE_MAP")

        if dispute and dispute["content"]:
            render_dispute_map(dispute["content"])
        elif dispute and dispute.get("generation_failed_at"):
            st.warning(
                "争议地图整理失败，但双方独立陈述已经安全冻结。"
                "请使用页面上方的“重新尝试整理争议地图”。"
            )
        elif dispute:
            st.info(
                "AI 法官正在整理双方事实、分歧与待确认事项……",
                icon=":material/hourglass_top:",
            )
        elif not (submission_status["A"] and submission_status["B"]):
            st.info(f"等待 {other} 完成独立陈述。双方都提交后系统会自动整理争议地图。")
        else:
            st.info("双方独立陈述已提交，系统将自动开始整理争议地图。")

if tabs[2].open:
    trace_event("render_branch_entered", render_branch="mediation")
    with tabs[2]:
        st.markdown("### 共享调解室")
        render_mediation_context(page_snapshot["artifacts"].get("DISPUTE_MAP"))

        @st.fragment
        @observe_fragment("mediation_room")
        @trace_fragment("mediation_room")
        def shared_mediation_room():
            current_case = page_snapshot["case"]
            dispute = page_snapshot["artifacts"].get("DISPUTE_MAP")
            messages = st.session_state.get(message_cache_key, [])

            if not dispute or not dispute["content"]:
                st.info("请先完成争议地图。")
                return

            current_status = current_case["status"]
            paused = current_status == "PAUSED"
            pending = current_status == "ARBITRATION_PENDING"
            arbitrating = current_status == "ARBITRATING"
            closed = current_status == "CLOSED"
            can_write = current_status in {
                "MAP_READY",
                "MEDIATING",
                "ARBITRATION_PENDING",
            }

            if closed:
                st.info("案件已经完成最终仲裁，共享调解消息已冻结。")
            elif arbitrating:
                st.warning(
                    "🔒 本轮证据已冻结\n\n"
                    "最终仲裁正在进行。本次仲裁仅依据冻结时已经提交的"
                    "独立陈述、争议地图和共享调解记录。"
                )
                if current_case.get("arbitration_started_at"):
                    st.caption(
                        f"证据冻结时间：{current_case['arbitration_started_at']}"
                    )
            elif paused:
                paused_by = current_case["paused_by"]
                st.warning(f"{paused_by} 请求暂停当前调解。暂停期间关闭新消息和法官介入。")
                if paused_by == role:
                    if st.button(
                        "我准备好了，恢复调解",
                        icon=":material/play_arrow:",
                        width="stretch",
                    ):
                        queue_confirmation("resume", case_id, role)
                        rerun()
                else:
                    st.caption(f"只有请求暂停的 {paused_by} 可以恢复调解。")
            elif pending:
                requester = current_case.get("arbitration_requested_by")
                st.info(
                    f"{requester} 已申请进入最终仲裁。对方确认前仍可继续调解；"
                    "当前阶段不允许再请求暂停。"
                )
            else:
                if st.button(
                    "请求暂停",
                    icon=":material/pause:",
                ):
                    queue_confirmation("pause", case_id, role)
                    rerun()

            message_history = st.container()
            if can_write:
                text = st.chat_input(
                    f"以 {role} 身份发言",
                    key=f"chat_input_{case_id}_{role}",
                    max_chars=5000,
                    submit_mode="disable",
                )
                if text:
                    try:
                        message = database.add_message(case_id, role, text)
                    except CaseStateError as error:
                        st.warning(str(error))
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        latest_cached_id = (
                            messages[-1]["id"] if messages else 0
                        )
                        if (
                            message.get("previous_message_id")
                            == latest_cached_id
                        ):
                            st.session_state[message_cache_key] = [
                                *messages,
                                message,
                            ]
                            revision = dict(
                                st.session_state.get(revision_key, {})
                            )
                            revision.update(
                                {
                                    "status": message["case_status"],
                                    "updated_at": message[
                                        "case_updated_at"
                                    ],
                                    "latest_message_id": message["id"],
                                }
                            )
                            st.session_state[revision_key] = revision
                            page_snapshot["revision"] = revision
                            current_case["status"] = message["case_status"]
                            current_case["updated_at"] = message[
                                "case_updated_at"
                            ]
                            messages = [*messages, message]
                        else:
                            try:
                                refreshed = database.get_case_view_snapshot(
                                    case_id,
                                    role,
                                    "mediation",
                                    latest_cached_id,
                                )
                            except DatabaseError as error:
                                show_database_error(error)
                            else:
                                if refreshed:
                                    messages = [
                                        *messages,
                                        *refreshed["messages"],
                                    ]
                                    st.session_state[message_cache_key] = messages
                                    st.session_state[revision_key] = refreshed[
                                        "revision"
                                    ]
                                    page_snapshot["revision"] = refreshed[
                                        "revision"
                                    ]
                                    current_case.update(refreshed["case"])

            with message_history:
                for message in messages:
                    if message["sender"] in {"JUDGE", "SYSTEM"}:
                        st.chat_message("assistant").markdown(
                            f"**{message['sender']}**\n\n{message['content']}"
                        )
                    else:
                        st.chat_message("user").markdown(
                            f"**{message['sender']}**\n\n{message['content']}"
                        )

            if can_write:
                if not llm_available():
                    st.caption("AI 法官尚未由网站管理员配置，双方仍可继续共享对话。")
                if st.button(
                    "请法官介入",
                    icon=":material/gavel:",
                    disabled=not llm_available(),
                ):
                    queue_confirmation("judge_intervention", case_id, role)
                    rerun()

        shared_mediation_room()

if tabs[3].open:
    trace_event("render_branch_entered", render_branch="final")
    with tabs[3]:
        st.markdown("### 最终仲裁")
        current_case = page_snapshot["case"]
        artifacts = page_snapshot["artifacts"]
        dispute = artifacts.get("DISPUTE_MAP")
        final_artifact = artifacts.get("FINAL_JUDGMENT")
        normal_checkpoint = artifacts.get("JUDGMENT_NORMAL")
        swapped_checkpoint = artifacts.get("JUDGMENT_SWAPPED")
        meta_checkpoint = artifacts.get("META_JUDGMENT")
        evidence = page_snapshot["evidence"]

        if not current_case:
            st.error("案件已不可用。")
        elif not dispute or not dispute["content"]:
            st.info("请先完成争议地图。")
        else:
            status = current_case["status"]
            requester = current_case.get("arbitration_requested_by")

            if status == "CLOSED":
                if final_artifact and final_artifact["content"]:
                    st.markdown(final_artifact["content"])
                    st.caption("本案件已经正式结束，最终证据与仲裁结果保持冻结。")
                else:
                    st.error("案件已关闭，但最终仲裁结果不可用。")
            elif status == "ARBITRATION_PENDING":
                if requester == role:
                    st.warning(
                        "已申请进入最终仲裁。\n\n"
                        f"等待 {other} 确认。\n\n"
                        "在对方确认前仍可继续调解；确认后当前证据将被冻结。"
                    )
                    if st.button(
                        "取消最终仲裁申请",
                        icon=":material/undo:",
                        width="stretch",
                    ):
                        try:
                            database.cancel_arbitration_request(case_id, role)
                        except DatabaseError as error:
                            show_database_error(error)
                        else:
                            rerun()
                else:
                    st.warning(
                        f"{requester} 希望结束当前调解并进入最终仲裁。\n\n"
                        "最终仲裁开始后，当前证据将被冻结，双方不能继续发言，"
                        "AI 将只依据冻结前的材料完成双向复核。"
                    )
                    with st.container(horizontal=True):
                        continue_mediation = st.button(
                            "继续调解",
                            icon=":material/forum:",
                        )
                        confirm_arbitration = st.button(
                            "同意进入最终仲裁",
                            type="primary",
                            icon=":material/lock:",
                            disabled=not llm_available(),
                        )
                    if not llm_available():
                        st.caption("AI 法官尚未配置，暂时不能冻结并启动最终仲裁。")
                    if continue_mediation:
                        queue_confirmation(
                            "arbitration_decline",
                            case_id,
                            role,
                        )
                        rerun()
                    if confirm_arbitration:
                        queue_confirmation(
                            "arbitration_accept",
                            case_id,
                            role,
                        )
                        rerun()
            elif status == "ARBITRATING":
                st.warning(
                    "🔒 本轮证据已冻结\n\n"
                    "最终仲裁正在进行。\n\n"
                    "从现在起，本轮仲裁只依据冻结时已经存在的：\n"
                    "- A/B 独立陈述\n"
                    "- 争议地图\n"
                    "- 共享调解记录"
                )
                frozen_at = (
                    evidence["snapshot"]["created_at"] if evidence else None
                )
                if frozen_at:
                    st.caption(f"证据冻结时间：{frozen_at}")

                if meta_checkpoint and meta_checkpoint["content"]:
                    st.success("双向复核与 Meta Judge 已完成。")
                elif swapped_checkpoint and swapped_checkpoint["content"]:
                    st.info("两次审理已完成，正在进行 Meta Judge。")
                elif normal_checkpoint and normal_checkpoint["content"]:
                    st.info("已完成第一次审理，正在进行交换身份复核。")
                else:
                    st.info("正在进行第一次审理。")

                if (
                    final_artifact
                    and not final_artifact["content"]
                    and meta_checkpoint
                    and meta_checkpoint["content"]
                ):
                    st.warning("模型结果已安全保存，只需完成最终数据库写入。")
                    if st.button(
                        "重试完成最终仲裁",
                        type="primary",
                        icon=":material/save:",
                        width="stretch",
                    ):
                        try:
                            retry_database_write(
                                lambda: database.complete_artifact(
                                    case_id,
                                    final_artifact["id"],
                                    "FINAL_JUDGMENT",
                                    meta_checkpoint["content"],
                                )
                            )
                        except DatabaseError as error:
                            show_database_error(error)
                        else:
                            rerun()
                elif not final_artifact:
                    if st.button(
                        "继续最终仲裁",
                        type="primary",
                        icon=":material/gavel:",
                        width="stretch",
                    ):
                        if start_or_resume_final_arbitration(case_id):
                            rerun()
                elif not final_artifact["content"]:
                    st.caption("已有执行正在进行；超过安全恢复时间后可重新检查。")
                    if st.button(
                        "检查并继续",
                        icon=":material/refresh:",
                        width="stretch",
                    ):
                        if start_or_resume_final_arbitration(case_id):
                            rerun()
            elif status == "PAUSED":
                st.info("请先由暂停申请方恢复调解，再申请进入最终仲裁。")
            elif status in {"MAP_READY", "MEDIATING"}:
                st.info(
                    "最终仲裁需要双方确认。\n\n"
                    "双方确认后，本轮独立陈述、争议地图和共享调解记录将被冻结；"
                    "最终仲裁期间不能继续发送消息或请法官介入。"
                )
                if st.button(
                    "申请进入最终仲裁",
                    type="primary",
                    icon=":material/gavel:",
                    width="stretch",
                ):
                    queue_confirmation(
                        "arbitration_request",
                        case_id,
                        role,
                    )
                    rerun()
            else:
                st.info("当前案件尚未进入可以申请最终仲裁的阶段。")

st.caption(
    "这是关系调解原型，不是法律裁判，也不能替代现实中的安全判断。"
    "如果涉及现实的人身威胁、暴力或胁迫，应优先处理现实安全。"
)
finish_trace(performance_trace)
trace_event("render_complete")
state_trace.finish(runtime_trace)
