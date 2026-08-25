import atexit
import logging
import os
import time
from functools import wraps

import streamlit as st

from arbitration import retry_database_write, run_final_arbitration
from config import load_settings, secure_secret_matches
from db import (
    CaseStateError,
    Database,
    DatabaseError,
    StatementAlreadySubmitted,
)
from llm import LLMError, TASK_MAX_TOKENS, call_llm
from prompts import (
    CORE_SYSTEM_PROMPT,
    DISPUTE_MAP_PROMPT,
    INTERVENTION_PROMPT,
)
from validation import build_statement_content, validate_statement_fields


st.set_page_config(
    page_title="双向关系仲裁员",
    page_icon="⚖️",
    layout="centered",
)


STATUS_LABELS = {
    "COLLECTING": "等待双方独立陈述",
    "READY_FOR_MAP": "可以生成争议地图",
    "MAP_READY": "争议地图已就绪",
    "MEDIATING": "共享调解中",
    "PAUSED": "调解已暂停",
    "ARBITRATION_PENDING": "最终仲裁等待双方确认",
    "ARBITRATING": "最终仲裁进行中",
    "CLOSED": "最终仲裁已完成",
}


logger = logging.getLogger(__name__)


def _load_server_settings():
    secret_values = st.secrets if st.secrets.load_if_toml_exists() else {}
    return load_settings(secret_values)


@st.cache_resource(show_spinner=False)
def get_database(database_url):
    database = Database(database_url)
    try:
        database.init_db()
    except DatabaseError:
        database.close()
        raise
    atexit.register(database.close)
    return database


def show_database_error(error):
    st.error(str(error), icon=":material/database:")
    if settings.development_mode:
        with st.expander("开发信息", icon=":material/code:"):
            st.code(type(error).__name__)


def show_llm_error(error):
    st.error("AI 法官暂时无法响应，请稍后重试。", icon=":material/error:")
    st.toast("AI 法官调用失败，案件数据没有被写入。", icon=":material/error:")
    if settings.development_mode:
        with st.expander("开发信息", icon=":material/code:"):
            st.code(error.debug_summary())


def release_reservation(case_id, artifact_id, kind):
    try:
        retry_database_write(
            lambda: database.release_artifact(case_id, artifact_id, kind)
        )
    except DatabaseError:
        # A later page refresh will still show the unfinished reservation.
        pass


def start_or_resume_final_arbitration(case_id):
    if not settings.llm_ready:
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
        with st.spinner("正在依据冻结证据进行双向复核仲裁…"):
            run_final_arbitration(
                database=database,
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


def ask(
    system_extra,
    user_text,
    temperature=0.2,
    max_tokens=TASK_MAX_TOKENS["DISPUTE_MAP"],
):
    result = call_llm(
        endpoint=settings.llm_endpoint,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        system_prompt=CORE_SYSTEM_PROMPT + "\n\n" + system_extra,
        user_prompt=user_text,
        temperature=temperature,
        max_tokens=max_tokens,
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
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not (
                settings.development_mode
                or os.environ.get("REALTIME_GATE_OBSERVE") == "true"
            ):
                return function(*args, **kwargs)

            started = time.perf_counter()
            state_key = f"_fragment_started_{name}"
            previous = st.session_state.get(state_key)
            st.session_state[state_key] = started
            try:
                return function(*args, **kwargs)
            finally:
                duration_ms = (time.perf_counter() - started) * 1000
                interval_ms = (
                    (started - previous) * 1000
                    if previous is not None
                    else None
                )
                logger.log(
                    logging.WARNING
                    if os.environ.get("REALTIME_GATE_OBSERVE") == "true"
                    else logging.INFO,
                    "fragment_timing name=%s interval_ms=%s duration_ms=%.2f",
                    name,
                    f"{interval_ms:.2f}" if interval_ms is not None else "first",
                    duration_ms,
                )

        return wrapped

    return decorator


settings = _load_server_settings()
st.session_state.setdefault("auth", None)

st.title("⚖️ 双向关系仲裁员")
st.caption("独立陈述 → 争议地图 → 共享调解 → 暂停 / 恢复 → 双向复核仲裁")

service_status = st.sidebar.container()

if not settings.database_url:
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
    st.stop()

try:
    with st.spinner("正在连接共享数据库…"):
        database = get_database(settings.database_url)
except DatabaseError as error:
    with service_status:
        st.subheader("服务状态")
        st.badge("数据库连接失败", color="red", icon=":material/database:")
    show_database_error(error)
    st.stop()

with service_status:
    st.subheader("服务状态")
    st.badge("数据库已连接", color="green", icon=":material/database:")
    if settings.llm_ready:
        st.badge("AI 法官已就绪", color="green", icon=":material/smart_toy:")
    else:
        st.badge("AI 法官未配置", color="orange", icon=":material/smart_toy:")
    st.caption("AI 法官用于关系调解与结构化分析，不是法律裁判。")

    if st.session_state.auth:
        st.caption(f"当前案件：{st.session_state.auth['case_id']}")
        st.caption(f"当前身份：{st.session_state.auth['role']}")
        if st.button("退出案件", icon=":material/logout:", width="stretch"):
            st.session_state.auth = None
            st.rerun()


if not st.session_state.auth:
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
                    st.rerun()

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
    st.stop()


case_id = st.session_state.auth.get("case_id", "")
role = st.session_state.auth.get("role")
if role not in {"A", "B"}:
    st.session_state.auth = None
    st.error("当前登录状态无效，请重新进入案件。")
    st.stop()

try:
    case = database.get_case(case_id)
except DatabaseError as error:
    show_database_error(error)
    st.stop()

if not case:
    st.session_state.auth = None
    st.error("案件不存在或已不可用，请重新进入。")
    st.stop()

other = "B" if role == "A" else "A"
st.subheader(case["title"])


@st.fragment(run_every="2s")
@observe_fragment("case_overview")
def live_case_overview():
    try:
        snapshot = database.get_case_overview(case_id)
    except DatabaseError as error:
        show_database_error(error)
        return

    if not snapshot:
        st.error("案件已不可用。")
        return

    current_case = snapshot["case"]
    submitted = snapshot["submitted"]

    label = STATUS_LABELS.get(current_case["status"], current_case["status"])
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
    st.caption("页面每 2 秒同步案件状态；对方的独立陈述正文不会显示。")


live_case_overview()

tabs = st.tabs(
    ["① 独立陈述", "② 争议地图", "③ 调解室", "④ 最终仲裁"],
    on_change="rerun",
)

if tabs[0].open:
    with tabs[0]:
        st.markdown("### 你的独立陈述")
        st.caption("这一阶段互相不可见。提交后冻结，避免看到对方版本后修改自己的叙述。")

        try:
            my_statement = database.get_statement(case_id, role)
        except DatabaseError as error:
            show_database_error(error)
            my_statement = None

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
                    try:
                        database.save_statement(case_id, role, content)
                    except StatementAlreadySubmitted:
                        st.warning("你的独立陈述已经提交并冻结。")
                        st.rerun()
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        st.success("已提交并冻结。")
                        st.rerun()

        st.info(
            f"你只能看到自己的陈述正文。页面顶部会同步 {other} 是否已经提交。",
            icon=":material/visibility_off:",
        )

if tabs[1].open:
    with tabs[1]:
        st.markdown("### 争议地图")
        try:
            submission_status = database.get_submission_status(case_id)
            dispute = database.get_artifact(case_id, "DISPUTE_MAP")
        except DatabaseError as error:
            show_database_error(error)
            submission_status = {"A": False, "B": False}
            dispute = None

        if dispute and dispute["content"]:
            st.markdown(dispute["content"])
        elif dispute:
            st.info("争议地图正在生成，请稍后刷新。", icon=":material/hourglass_top:")
        elif not (submission_status["A"] and submission_status["B"]):
            st.info(f"等待 {other} 完成独立陈述。双方都提交后才能生成争议地图。")
        else:
            st.warning("双方都已提交。现在可以让 AI 法官整理事实与争议，但暂时不判输赢。")
            if st.button(
                "生成争议地图",
                type="primary",
                icon=":material/account_tree:",
                width="stretch",
            ):
                if not settings.llm_ready:
                    st.error("AI 法官尚未由网站管理员配置。")
                else:
                    try:
                        reservation_id = database.claim_artifact(
                            case_id,
                            "DISPUTE_MAP",
                        )
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        if reservation_id is None:
                            st.info("争议地图已存在或正在由另一请求生成，请刷新查看。")
                        else:
                            try:
                                statements = database.get_statements_for_llm(case_id)
                                prompt = f"""以下是两份独立陈述。

===== A =====
{statements['A']}

===== B =====
{statements['B']}

{DISPUTE_MAP_PROMPT}
"""
                                with st.spinner("正在整理争议地图…"):
                                    result = ask(
                                        DISPUTE_MAP_PROMPT,
                                        prompt,
                                        max_tokens=TASK_MAX_TOKENS["DISPUTE_MAP"],
                                    )
                                database.complete_artifact(
                                    case_id,
                                    reservation_id,
                                    "DISPUTE_MAP",
                                    result.content,
                                )
                            except LLMError as error:
                                release_reservation(
                                    case_id,
                                    reservation_id,
                                    "DISPUTE_MAP",
                                )
                                show_llm_error(error)
                            except DatabaseError as error:
                                release_reservation(
                                    case_id,
                                    reservation_id,
                                    "DISPUTE_MAP",
                                )
                                show_database_error(error)
                            else:
                                st.rerun()

if tabs[2].open:
    with tabs[2]:
        st.markdown("### 共享调解室")

        @st.fragment(run_every="2s")
        @observe_fragment("mediation_room")
        def shared_mediation_room():
            message_cache_key = f"_message_cache_{case_id}"
            cached_messages = st.session_state.get(message_cache_key, [])
            if not isinstance(cached_messages, list):
                cached_messages = []
            last_message_id = (
                cached_messages[-1]["id"] if cached_messages else 0
            )

            try:
                snapshot = database.get_mediation_snapshot(
                    case_id,
                    last_message_id,
                )
            except DatabaseError as error:
                show_database_error(error)
                return

            if not snapshot:
                st.error("案件已不可用。")
                return

            current_case = snapshot["case"]
            dispute = snapshot["artifact"]
            messages = cached_messages + snapshot["messages"]
            st.session_state[message_cache_key] = messages

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
                        try:
                            resumed = database.resume_case(case_id, role)
                        except DatabaseError as error:
                            show_database_error(error)
                        else:
                            if not resumed:
                                st.info("案件状态已经变化，请稍后查看。")
                            st.rerun(scope="fragment")
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
                    try:
                        paused_now = database.pause_case(case_id, role)
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        if not paused_now:
                            st.info("案件状态已经变化，请稍后查看。")
                        st.rerun(scope="fragment")

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
                text = st.chat_input(
                    f"以 {role} 身份发言",
                    key=f"chat_input_{case_id}_{role}",
                    max_chars=5000,
                    submit_mode="disable",
                )
                if text:
                    try:
                        database.add_message(case_id, role, text)
                    except CaseStateError as error:
                        st.warning(str(error))
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        st.rerun(scope="fragment")

                if not settings.llm_ready:
                    st.caption("AI 法官尚未由网站管理员配置，双方仍可继续共享对话。")
                if st.button(
                    "请法官介入",
                    icon=":material/gavel:",
                    disabled=not settings.llm_ready,
                ):
                    try:
                        database.ensure_judge_intervention_allowed(case_id)
                        statements = database.get_statements_for_llm(case_id)
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
                    except CaseStateError as error:
                        st.warning(str(error))
                    except LLMError as error:
                        show_llm_error(error)
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        st.rerun(scope="fragment")

        shared_mediation_room()

if tabs[3].open:
    with tabs[3]:
        st.markdown("### 最终仲裁")
        try:
            current_case = database.get_case(case_id)
            dispute = database.get_artifact(case_id, "DISPUTE_MAP")
            final_artifact = database.get_artifact(case_id, "FINAL_JUDGMENT")
            normal_checkpoint = database.get_artifact(
                case_id,
                "JUDGMENT_NORMAL",
            )
            swapped_checkpoint = database.get_artifact(
                case_id,
                "JUDGMENT_SWAPPED",
            )
            meta_checkpoint = database.get_artifact(case_id, "META_JUDGMENT")
            evidence = database.get_arbitration_evidence(case_id)
        except DatabaseError as error:
            show_database_error(error)
            current_case = None
            dispute = None
            final_artifact = None
            normal_checkpoint = None
            swapped_checkpoint = None
            meta_checkpoint = None
            evidence = None

        @st.fragment(run_every="2s")
        @observe_fragment("final_arbitration")
        def sync_final_arbitration(rendered_status, rendered_final_ready):
            try:
                fresh_case = database.get_case(case_id)
                fresh_final = database.get_artifact(case_id, "FINAL_JUDGMENT")
            except DatabaseError as error:
                show_database_error(error)
                return

            fresh_status = fresh_case["status"] if fresh_case else None
            fresh_final_ready = bool(fresh_final and fresh_final["content"])
            if (fresh_status, fresh_final_ready) != (
                rendered_status,
                rendered_final_ready,
            ):
                st.rerun()

        if current_case and current_case["status"] != "CLOSED":
            sync_final_arbitration(
                current_case["status"],
                bool(final_artifact and final_artifact["content"]),
            )

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
                            st.rerun()
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
                            disabled=not settings.llm_ready,
                        )
                    if not settings.llm_ready:
                        st.caption("AI 法官尚未配置，暂时不能冻结并启动最终仲裁。")
                    if continue_mediation:
                        try:
                            database.cancel_arbitration_request(case_id, role)
                        except DatabaseError as error:
                            show_database_error(error)
                        else:
                            st.rerun()
                    if confirm_arbitration:
                        try:
                            database.confirm_arbitration(case_id, role)
                        except DatabaseError as error:
                            show_database_error(error)
                        else:
                            if start_or_resume_final_arbitration(case_id):
                                st.rerun()
                            else:
                                st.warning(
                                    "证据已冻结。模型流程可在服务恢复后从同一 Snapshot 继续。"
                                )
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
                            st.rerun()
                elif not final_artifact:
                    if st.button(
                        "继续最终仲裁",
                        type="primary",
                        icon=":material/gavel:",
                        width="stretch",
                    ):
                        if start_or_resume_final_arbitration(case_id):
                            st.rerun()
                elif not final_artifact["content"]:
                    st.caption("已有执行正在进行；超过安全恢复时间后可重新检查。")
                    if st.button(
                        "检查并继续",
                        icon=":material/refresh:",
                        width="stretch",
                    ):
                        if start_or_resume_final_arbitration(case_id):
                            st.rerun()
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
                    try:
                        database.request_arbitration(case_id, role)
                    except DatabaseError as error:
                        show_database_error(error)
                    else:
                        st.rerun()
            else:
                st.info("当前案件尚未进入可以申请最终仲裁的阶段。")

st.caption(
    "这是关系调解原型，不是法律裁判，也不能替代现实中的安全判断。"
    "如果涉及现实的人身威胁、暴力或胁迫，应优先处理现实安全。"
)
