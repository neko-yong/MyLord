import ast
import copy
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit import rerun as streamlit_rerun
from streamlit.testing.v1 import AppTest

from dispute_map_view import render_dispute_map, render_mediation_context
from tests.test_app_chat_fragment import ChatDatabase, find, inline_dialog
from tests.test_dispute_map_view import BODIES, TITLES, fictional_map


APP = Path(__file__).resolve().parents[1] / "app.py"


def approved_baseline():
    """Reconstruct the three-line prior view, then verify its pinned source hash.

    SHA-256 is of app.py at 74d1ae1902cb69e933363ee10b3679686f422c2f,
    UTF-8 with LF. This fails on any other app change, including business logic.
    """
    source = APP.read_text(encoding="utf-8")
    changes = (
        ("from dispute_map_view import render_dispute_map, render_mediation_context\n", ""),
        ('            render_dispute_map(dispute["content"])', '            st.markdown(dispute["content"])'),
        ('        render_mediation_context(page_snapshot["artifacts"].get("DISPUTE_MAP"))\n', ""),
    )
    for new, old in changes:
        assert source.count(new) == 1
        source = source.replace(new, old, 1)
    assert hashlib.sha256(source.encode()).hexdigest() == (
        "635f8161f239028dbb77f17b3814f1a8b374bd285e0deada14aa99ff8db33919"
    )
    return source


class ContextDatabase(ChatDatabase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.artifacts = {
            case: {"id": index, "content": fictional_map(preamble=case + "\n\n").replace("虚构核心", case + "核心"),
                   "evidence_hash": "f" * 64, "generation_failed_at": None}
            for index, case in enumerate(("FICTIONAL-ONE", "FICTIONAL-TWO"), 1)
        }
        self.snapshot_args = []
        self.backfill = False

    def get_case_view_snapshot(self, case_id, role, view, last_message_id=0):
        self.snapshot_args.append((case_id, role, view, last_message_id))
        snapshot = super().get_case_view_snapshot(case_id, role, view, last_message_id)
        artifact = self.artifacts[case_id]
        snapshot["artifacts"] = {"DISPUTE_MAP": artifact} if artifact is not None else {}
        return snapshot

    def add_message(self, case_id, sender, content):
        if self.backfill:
            self.messages.append({"id": 80, "case_id": case_id, "sender": "B",
                                  "content": "虚构并发消息", "created_at": "fictional"})
        return super().add_message(case_id, sender, content)


def environment(database):
    return patch.dict(os.environ, {
        "DATABASE_URL": f"postgresql://fictional-offline/{id(database)}",
        "LLM_ENDPOINT": "", "LLM_MODEL": "", "LLM_API_KEY": "", "DEV_MODE": "false",
    })


def client(role="A", case_id="FICTIONAL-ONE", view="③ 调解室", baseline=False):
    app = AppTest.from_string(approved_baseline()) if baseline else AppTest.from_file(str(APP))
    app.session_state["auth"] = {"case_id": case_id, "role": role}
    app.session_state["case_tab"] = view
    return app


def text(app):
    return "\n".join(item.value for item in app.markdown)


class DisputeMapAppTests(unittest.TestCase):
    def test_both_actual_render_contacts_use_same_snapshot_without_writes(self):
        database = ContextDatabase()
        before = copy.deepcopy(database.artifacts)
        with (environment(database), patch("db.Database", return_value=database),
              patch("llm.call_llm") as llm,
              patch("dispute_map_view.render_dispute_map", wraps=render_dispute_map) as map_view,
              patch("dispute_map_view.render_mediation_context", wraps=render_mediation_context) as context_view):
            for role in ("A", "B"):
                app = client(role, view="② 争议地图").run(timeout=15)
                self.assertFalse(app.exception)
                self.assertEqual(map_view.call_args.args, (database.artifacts["FICTIONAL-ONE"]["content"],))
                columns = app.tabs[1].get("column")
                self.assertEqual([column.markdown[0].value for column in columns], [
                    f"# {TITLES[3]}\n{BODIES[3]}", f"# {TITLES[4]}\n{BODIES[4]}",
                ])
                app.session_state["case_tab"] = "③ 调解室"
                app.run(timeout=15)
                self.assertFalse(app.exception)
                self.assertIs(context_view.call_args.args[0], database.artifacts["FICTIONAL-ONE"])
                visible = text(app)
                self.assertEqual(visible.count("FICTIONAL-ONE核心"), 1)
                self.assertIn(BODIES[-1], visible)
                self.assertNotIn(BODIES[3], visible)
                self.assertNotIn(BODIES[6], visible)
                tab_nodes = list(app.tabs[2])
                core_index = next(i for i, node in enumerate(tab_nodes)
                                  if node.type == "markdown" and TITLES[5] in node.value)
                chat_index = next(i for i, node in enumerate(tab_nodes) if node.type == "chat_input")
                self.assertLess(core_index, chat_index)
            self.assertEqual(map_view.call_count, 2)
            self.assertEqual(context_view.call_count, 2)
            llm.assert_not_called()
        self.assertEqual(database.artifacts, before)
        self.assertEqual(database.snapshot_views, ["dispute", "mediation"] * 2)
        self.assertEqual(database.revision_calls, 4)
        self.assertEqual(database.write_calls, 0)

    def test_case_change_artifact_update_logout_and_reentry_do_not_keep_context(self):
        database = ContextDatabase()
        with environment(database), patch("db.Database", return_value=database), patch("llm.call_llm") as llm:
            app = client().run(timeout=15)
            self.assertIn("FICTIONAL-ONE核心", text(app))
            app.session_state["auth"] = {"case_id": "FICTIONAL-TWO", "role": "B"}
            app.run(timeout=15)
            self.assertFalse(app.exception)
            self.assertIn("FICTIONAL-TWO核心", text(app))
            self.assertNotIn("FICTIONAL-ONE核心", text(app))
            database.artifacts["FICTIONAL-TWO"]["content"] = fictional_map().replace("虚构核心", "更新核心")
            app.run(timeout=15)
            self.assertIn("更新核心", text(app))
            self.assertNotIn("FICTIONAL-TWO核心", text(app))
            find(app.button, "退出案件").click().run(timeout=15)
            self.assertFalse(app.exception)
            self.assertNotIn("更新核心", text(app))
            app.session_state["auth"] = {"case_id": "FICTIONAL-ONE", "role": "A"}
            app.session_state["case_tab"] = "③ 调解室"
            app.run(timeout=15)
            self.assertIn("FICTIONAL-ONE核心", text(app))
            self.assertNotIn("更新核心", text(app))
            llm.assert_not_called()

    def test_pending_failed_and_legacy_maps_keep_existing_wait_or_fallback(self):
        for artifact in (None, {"content": ""}, {"content": "", "generation_failed_at": "fictional"},
                         {"content": "# 虚构旧格式\n- 核心分歧：分类\n- 待确认：试摆\n"}):
            database = ContextDatabase()
            database.artifacts["FICTIONAL-ONE"] = artifact
            with environment(database), patch("db.Database", return_value=database), patch("llm.call_llm") as llm:
                app = client().run(timeout=15)
                self.assertFalse(app.exception)
                if artifact and artifact.get("content"):
                    self.assertIn(artifact["content"].strip(), text(app))
                    self.assertEqual(len(app.chat_input), 1)
                    self.assertNotIn(TITLES[-1], text(app))
                else:
                    self.assertEqual(len(app.chat_input), 0)
                    self.assertIn("请先完成争议地图。", [item.value for item in app.info])
                llm.assert_not_called()

    def test_lifecycle_keeps_context_and_existing_chat_availability(self):
        for status in ("MAP_READY", "MEDIATING", "PAUSED", "ARBITRATION_PENDING", "ARBITRATING", "CLOSED"):
            with self.subTest(status=status):
                database = ContextDatabase(status=status)
                database.paused_by = "A" if status == "PAUSED" else None
                with environment(database), patch("db.Database", return_value=database), patch("llm.call_llm") as llm:
                    app = client().run(timeout=15)
                    self.assertFalse(app.exception)
                    self.assertIn("FICTIONAL-ONE核心", text(app))
                    self.assertEqual(len(app.chat_input), int(status in {"MAP_READY", "MEDIATING", "ARBITRATION_PENDING"}))
                    self.assertEqual(database.snapshot_views, ["mediation"])
                    self.assertEqual(database.write_calls, 0)
                    llm.assert_not_called()

    def test_navigation_then_chat_regression(self):
        for baseline in (True, False):
            with self.subTest(baseline=baseline):
                database = ContextDatabase(status="MAP_READY", initial_message_ids=(), inserted_message_ids=(41, 57, 88))
                with (environment(database), patch("db.Database", return_value=database),
                      patch("streamlit.dialog", new=inline_dialog), patch("llm.call_llm") as llm,
                      patch("streamlit.rerun", wraps=streamlit_rerun) as rerun):
                    app = client(view="② 争议地图", baseline=baseline).run(timeout=15)
                    for view in ("③ 调解室", "① 独立陈述", "② 争议地图", "③ 调解室"):
                        app.session_state["case_tab"] = view
                        app.run(timeout=15)
                        self.assertFalse(app.exception)
                        self.assertEqual(app.session_state["case_tab"], view)
                    self.assertEqual(database.snapshot_views, ["dispute", "mediation", "statement", "dispute", "mediation"])
                    self.assertEqual(database.revision_calls, 5)
                    for message in ("虚构首条", "虚构普通消息"):
                        # AppTest 1.62 has no Tab interaction/serialization API.
                        # Supply the active tab alongside each simulated submission.
                        app.session_state["case_tab"] = "③ 调解室"
                        app.chat_input[0].set_value(message).run(timeout=15)
                        self.assertFalse(app.exception)
                        self.assertEqual(app.session_state["case_tab"], "③ 调解室")
                        self.assertIn(message, text(app))
                        self.assertFalse(app.chat_input[0].disabled)
                    database.backfill = True
                    app.session_state["case_tab"] = "③ 调解室"
                    app.chat_input[0].set_value("虚构并发回填后的消息").run(timeout=15)
                    self.assertFalse(app.exception)
                    self.assertEqual(app.session_state["case_tab"], "③ 调解室")
                    self.assertIn("虚构并发消息", text(app))
                    self.assertEqual([message["id"] for message in database.messages], [41, 57, 80, 88])
                    self.assertEqual(database.write_calls, 3)
                    self.assertEqual(len(database.snapshot_views), 9)
                    self.assertEqual(database.revision_calls, 9)
                    self.assertEqual(database.snapshot_args[-1][-1], 57)
                    llm.assert_not_called()
                    rerun.assert_not_called()

    def test_fresh_chat_budgets_match_approved_baseline(self):
        outcomes = []
        for baseline in (True, False):
            database = ContextDatabase(status="MAP_READY", initial_message_ids=(), inserted_message_ids=(41, 57, 88))
            with (environment(database), patch("db.Database", return_value=database),
                  patch("streamlit.dialog", new=inline_dialog), patch("llm.call_llm") as llm,
                  patch("streamlit.rerun") as rerun):
                app = client(baseline=baseline).run(timeout=15)
                for message in ("虚构首条", "虚构普通消息"):
                    app.chat_input[0].set_value(message).run(timeout=15)
                    self.assertFalse(app.exception)
                    self.assertIn(message, text(app))
                    self.assertFalse(app.chat_input[0].disabled)
                database.backfill = True
                app.chat_input[0].set_value("虚构并发回填后的消息").run(timeout=15)
                self.assertFalse(app.exception)
                self.assertIn("虚构并发消息", text(app))
                self.assertEqual([message["id"] for message in database.messages], [41, 57, 80, 88])
                self.assertEqual(database.write_calls, 3)
                self.assertEqual(len(database.snapshot_views), 5)
                self.assertEqual(database.revision_calls, 5)
                self.assertEqual(database.snapshot_args[-1][-1], 57)
                llm.assert_not_called()
                rerun.assert_not_called()
                outcomes.append((database.snapshot_args, database.revision_calls, database.write_calls, llm.call_count, rerun.call_count))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_context_is_outside_fragment_and_business_code_is_byte_equivalent(self):
        approved_baseline()  # Hash gate also covers navigation, auth, timers and writes.
        tree = ast.parse(APP.read_text(encoding="utf-8"))
        fragment = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "shared_mediation_room")
        self.assertFalse(any(isinstance(node, ast.Name) and node.id == "render_mediation_context"
                             for node in ast.walk(fragment)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "render_mediation_context"]
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0].lineno, fragment.lineno)
