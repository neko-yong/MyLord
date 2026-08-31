import streamlit as st

from config import secure_secret_matches
from database_resources import get_database
from db import DatabaseError


ADMIN_QUERY_PARAMETER = "console"
ADMIN_PAGE_SIZE = 25
_ADMIN_AUTH_KEY = "_admin_authenticated"
_ADMIN_PAGE_KEY = "_admin_page"
_ADMIN_SELECTED_CASE_KEY = "_admin_selected_case_id"
_ADMIN_DELETE_CASE_KEY = "_admin_delete_case_id"
_ADMIN_DELETE_NOTICE_KEY = "_admin_delete_notice"


def is_admin_route(query_params, configured_route_key):
    if not configured_route_key:
        return False
    candidate = query_params.get(ADMIN_QUERY_PARAMETER)
    if not isinstance(candidate, str):
        return False
    return secure_secret_matches(candidate, configured_route_key)


def _clear_admin_delete_state():
    case_id = st.session_state.pop(_ADMIN_DELETE_CASE_KEY, None)
    if case_id:
        st.session_state.pop(f"admin_delete_acknowledged_{case_id}", None)


def _clear_admin_case_state():
    _clear_admin_delete_state()
    st.session_state.pop(_ADMIN_SELECTED_CASE_KEY, None)


def _submit_admin_login(expected_secret):
    _clear_admin_case_state()
    candidate = st.session_state.get("_admin_login_secret", "")
    authenticated = secure_secret_matches(candidate, expected_secret)
    st.session_state["_admin_login_secret"] = ""
    st.session_state[_ADMIN_AUTH_KEY] = authenticated
    st.session_state["_admin_login_failed"] = not authenticated


def _render_login(settings):
    with st.container(border=True):
        st.subheader("Maintenance sign in")
        with st.form("admin_login_form"):
            st.text_input(
                "Maintenance secret",
                type="password",
                key="_admin_login_secret",
            )
            st.form_submit_button(
                "Sign in",
                type="primary",
                icon=":material/login:",
                on_click=_submit_admin_login,
                args=(settings.admin_maintenance_secret,),
            )

        if st.session_state.pop("_admin_login_failed", False):
            st.error("Sign-in failed.")


def _safe_case_rows(rows):
    return [
        {
            "Case ID": row["case_id"],
            "Status": row["status"],
            "Created at": row["created_at"],
            "Last activity": row["updated_at"],
        }
        for row in rows
    ]


def _render_case_table(rows, key):
    if not rows:
        st.info("No cases found.")
        return
    st.dataframe(
        _safe_case_rows(rows),
        hide_index=True,
        key=key,
    )


def _render_delete_result():
    result = st.session_state.pop(_ADMIN_DELETE_NOTICE_KEY, None)
    if not result:
        return
    if "error" in result:
        st.error(result["error"])
        return
    if "warning" in result:
        st.warning(result["warning"])
        return
    st.success(f"{result['case_id']} deleted.")
    counts = result["deleted_counts"]
    st.table(
        [
            {
                "Cases": counts["cases"],
                "Statements": counts["statements"],
                "Artifacts": counts["artifacts"],
                "Messages": counts["messages"],
                "Notifications": counts["case_notifications"],
                "Residual": result["residual"],
            }
        ]
    )


def _render_delete_confirmation(database, case_id):
    if st.session_state.get(_ADMIN_SELECTED_CASE_KEY) != case_id:
        _clear_admin_case_state()
        st.warning("Delete target changed. Search for the exact Case ID again.")
        return

    pending_case_id = st.session_state.get(_ADMIN_DELETE_CASE_KEY)
    if pending_case_id != case_id:
        _clear_admin_delete_state()
        if st.button(
            "Permanently delete",
            icon=":material/delete_forever:",
            key=f"admin_start_delete_{case_id}",
        ):
            st.session_state.pop(f"admin_delete_acknowledged_{case_id}", None)
            st.session_state[_ADMIN_DELETE_CASE_KEY] = case_id
            st.rerun()
        return

    with st.container(border=True):
        st.error(f"Permanently delete {case_id} and all linked records?")
        acknowledged = st.checkbox(
            "我确认删除这个案件，且无法恢复",
            key=f"admin_delete_acknowledged_{case_id}",
        )
        with st.container(horizontal=True):
            st.button(
                "Cancel",
                key=f"admin_cancel_delete_{case_id}",
                on_click=_clear_admin_delete_state,
            )
            confirm = st.button(
                "Delete permanently",
                type="primary",
                icon=":material/delete_forever:",
                disabled=not acknowledged,
                key=f"admin_confirm_delete_{case_id}",
            )

        if confirm:
            if (
                not acknowledged
                or not st.session_state.get(_ADMIN_AUTH_KEY)
                or st.session_state.get(_ADMIN_SELECTED_CASE_KEY) != case_id
                or st.session_state.get(_ADMIN_DELETE_CASE_KEY) != case_id
            ):
                _clear_admin_case_state()
                st.session_state[_ADMIN_DELETE_NOTICE_KEY] = {
                    "error": "Delete not confirmed. Search for the exact Case ID again."
                }
                st.rerun()
            # Consume this confirmation even if the database call fails.
            _clear_admin_case_state()
            try:
                result = database.delete_case_exact(case_id)
            except DatabaseError as error:
                result = {"error": str(error)}
            st.session_state[_ADMIN_DELETE_NOTICE_KEY] = result or {
                "warning": "Case not found or already deleted."
            }
            st.rerun()


def _render_exact_search(database):
    st.subheader("Exact Case search")
    with st.form("admin_case_search_form"):
        requested_case_id = st.text_input(
            "Full Case ID",
            placeholder="CASE-XXXXXX",
            max_chars=128,
        )
        submitted = st.form_submit_button(
            "Find case",
            icon=":material/search:",
            on_click=_clear_admin_case_state,
        )

    if submitted:
        metadata = database.get_case_admin_metadata(requested_case_id)
        if metadata is None:
            st.session_state.pop(_ADMIN_SELECTED_CASE_KEY, None)
            st.warning("Exact Case ID not found.")
        else:
            st.session_state[_ADMIN_SELECTED_CASE_KEY] = metadata["case_id"]

    selected_case_id = st.session_state.get(_ADMIN_SELECTED_CASE_KEY)
    if not selected_case_id:
        return
    metadata = database.get_case_admin_metadata(selected_case_id)
    if metadata is None:
        _clear_admin_case_state()
        st.warning("Case not found or already deleted.")
        return
    _render_case_table([metadata], "admin_exact_case_table")
    st.caption(
        "请核对上方案件 ID、状态及时间。当前安全元数据未提供标题和关联记录数量；"
        "永久删除会同时清理该案件的全部关联记录，且无法恢复。"
    )
    _render_delete_confirmation(database, metadata["case_id"])


def _render_case_list(database):
    page = st.session_state.setdefault(_ADMIN_PAGE_KEY, 0)
    result = database.list_case_metadata(
        limit=ADMIN_PAGE_SIZE,
        offset=page * ADMIN_PAGE_SIZE,
    )
    total = result["total"]
    if total and page * ADMIN_PAGE_SIZE >= total:
        st.session_state[_ADMIN_PAGE_KEY] = (total - 1) // ADMIN_PAGE_SIZE
        st.rerun()

    st.metric("Total cases", total, border=True)
    _render_case_table(result["cases"], "admin_case_list")

    last_page = max(0, (total - 1) // ADMIN_PAGE_SIZE)
    with st.container(horizontal=True):
        previous = st.button(
            "Previous",
            icon=":material/chevron_left:",
            disabled=page <= 0,
            key="admin_previous_page",
        )
        st.caption(f"Page {page + 1} of {last_page + 1}")
        next_page = st.button(
            "Next",
            icon=":material/chevron_right:",
            disabled=page >= last_page,
            key="admin_next_page",
        )
    if previous:
        st.session_state[_ADMIN_PAGE_KEY] = page - 1
        st.rerun()
    if next_page:
        st.session_state[_ADMIN_PAGE_KEY] = page + 1
        st.rerun()


def render_admin_console(settings, database_factory=get_database):
    st.title("Case maintenance console")
    st.caption("Operational metadata only. Private case content is not available here.")

    if not settings.admin_console_ready:
        st.error("Maintenance console unavailable.")
        return

    st.session_state.setdefault(_ADMIN_AUTH_KEY, False)
    if not st.session_state[_ADMIN_AUTH_KEY]:
        _clear_admin_case_state()
        _render_login(settings)
        return

    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Sign out", icon=":material/logout:", key="admin_logout"):
            st.session_state[_ADMIN_AUTH_KEY] = False
            _clear_admin_case_state()
            st.rerun()

    if not settings.database_url:
        st.error("Maintenance console unavailable.")
        return

    try:
        with st.spinner("Loading case metadata…"):
            database = database_factory(settings.database_url)
        _render_delete_result()
        _render_case_list(database)
        st.divider()
        _render_exact_search(database)
    except DatabaseError as error:
        _clear_admin_case_state()
        st.error(str(error), icon=":material/database:")
