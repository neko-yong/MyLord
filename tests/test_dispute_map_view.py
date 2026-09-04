import copy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from dispute_map_view import parse_dispute_map, render_dispute_map, render_mediation_context
from evidence import build_evidence_snapshot, evidence_hash


# Entirely fictional; independent of the parser's title constants and saved cases.
TITLES = (
    "事件地图", "双方一致的事实", "存在争议的事实", "A 的结构化信息",
    "B 的结构化信息", "真正的冲突核心", "当前证据不足之处",
    "下一阶段最值得确认的 3 个问题",
)
BODIES = (
    "虚构：先约好整理书架，再讨论标签。", "双方都说书架需要整理。",
    "A 说按颜色；B 说按主题，尚未确认。", "A 希望更容易找到蓝色书。",
    "B 希望按主题查找。", "虚构核心：按颜色还是按主题摆放？",
    "尚不知道书的分类清单。", "1. A 最常找哪类书？\n2. B 最常找哪类书？\n3. 双方愿意试哪层？",
)


def fictional_map(level=1, newline="\n", preamble="", bodies=BODIES, titles=TITLES):
    return (preamble + "".join(
        "#" * level + " " + title + "\n" + body + "\n\n"
        for title, body in zip(titles, bodies)
    )).replace("\n", newline)


class DisputeMapParserTests(unittest.TestCase):
    def assert_lossless(self, source):
        spans = parse_dispute_map(source)
        self.assertIsNotNone(spans)
        self.assertEqual([span.title for span in spans], ["", *TITLES])
        self.assertEqual("".join(source[s.start:s.end] for s in spans), source)
        self.assertEqual(spans[0].start, 0)
        self.assertEqual(spans[-1].end, len(source))
        for left, right in zip(spans, spans[1:]):
            self.assertEqual(left.end, right.start)
        return spans

    def test_h1_through_h6_crlf_preamble_and_no_final_newline(self):
        for level in range(1, 7):
            for newline in ("\n", "\r\n"):
                with self.subTest(level=level, newline=newline):
                    preamble = "前言\n\n" if level == 1 else "# 虚构地图\n前言\n\n"
                    self.assert_lossless(fictional_map(level, newline, preamble).rstrip())

    def test_heading_whitespace_and_legal_closing_hashes(self):
        source = fictional_map(level=2).replace("## ", "  ##\u3000")
        for title in TITLES:
            source = source.replace(title + "\n", title.replace(" ", "\u3000") + "\u3000### \n")
        self.assert_lossless(source)

    def test_fake_headings_in_both_fences_and_deeper_subheadings(self):
        for marker in ("```", "~~~~"):
            bodies = list(BODIES)
            bodies[0] += "\n" + marker + "text\n# A 的结构化信息\n# 未知章节\n" + marker + "\n## 时间补充\n虚构细节"
            bodies[1] += "\n> # B 的结构化信息\n\n    # 真正的冲突核心\n"
            with self.subTest(marker=marker):
                self.assert_lossless(fictional_map(bodies=bodies))

    def test_long_unicode_text_and_question_count_are_never_rewritten(self):
        bodies = list(BODIES)
        bodies[3] = "虚构长文🙂\n" * 10000
        for questions in ("只保存了一个问题？", BODIES[-1] + "\n4. 原文额外问题？"):
            bodies[-1] = questions
            source = fictional_map(bodies=bodies)
            spans = self.assert_lossless(source)
            self.assertIn(questions, source[spans[-1].start:spans[-1].end])

    def test_duplicate_missing_empty_unknown_reordered_and_mixed_levels_fall_back(self):
        good = fictional_map()
        invalid = [
            "", "# 旧地图\n- 核心分歧：摆放方式\n- 待确认：试哪层\n",
            good + "# 真正的冲突核心\n重复\n",
            good.replace("# A 的结构化信息\n", "# A的结构化信息\n"),
            good.replace(BODIES[4], " \t\n"),
            good.replace("# B 的结构化信息", "# 未知插入章\n正文\n# B 的结构化信息"),
            good + "# 未知附录\n正文\n",
            good.replace("# B 的结构化信息", "## B 的结构化信息"),
            good.replace("# A 的结构化信息", "## A 的结构化信息"),
            fictional_map(titles=(*TITLES[:3], TITLES[4], TITLES[3], *TITLES[5:])),
            fictional_map(titles=TITLES[:-1], bodies=BODIES[:-1]),
            good.replace("# 事件地图", "# 事件地图###"),
            good.replace("# 事件地图", "事件地图\n===="),
        ]
        for index, source in enumerate(invalid):
            with self.subTest(index=index):
                self.assertIsNone(parse_dispute_map(source))
                with patch("dispute_map_view.st") as ui:
                    render_dispute_map(source)
                    ui.markdown.assert_called_once_with(source)
                    ui.columns.assert_not_called()

    def test_ambiguous_markup_and_unclosed_fences_fall_back(self):
        good = fictional_map()
        for suffix in (
            "\n```\n# 假标题", "\n~~~\n# 假标题\n```",
            "\n```bad`info\ntext\n```", "\n[ref]: /fictional",
            "\n<div>\n# 假标题\n</div>", "\n- ```\n  # 假标题\n  ```",
        ):
            with self.subTest(suffix=suffix):
                self.assertIsNone(parse_dispute_map(good + suffix))


class DisputeMapRenderTests(unittest.TestCase):
    def test_only_a_and_b_are_in_equal_columns_and_all_other_text_is_full_width(self):
        source = fictional_map(preamble="虚构前言\n\n")
        app = AppTest.from_string(
            "from dispute_map_view import render_dispute_map\n"
            f"render_dispute_map({source!r})"
        ).run()
        self.assertFalse(app.exception)
        columns = app.get("column")
        self.assertEqual(len(columns), 2)
        self.assertEqual([column.weight for column in columns], [0.5, 0.5])
        self.assertEqual([column.gap for column in columns], ["medium", "medium"])
        self.assertEqual([len(column.markdown) for column in columns], [1, 1])
        self.assertEqual([column.markdown[0].value for column in columns], [
            f"# {TITLES[3]}\n{BODIES[3]}", f"# {TITLES[4]}\n{BODIES[4]}",
        ])
        direct = [item.value for item in app.main.children.values() if item.type == "markdown"]
        self.assertEqual(direct, ["虚构前言"] + [
            f"# {title}\n{body}" for i, (title, body) in enumerate(zip(TITLES, BODIES))
            if i not in (3, 4)
        ])
        with patch("dispute_map_view.st") as ui:
            ui.columns.return_value = (ui.container(), ui.container())
            render_dispute_map(source)
            ui.columns.assert_called_once_with(2, gap="medium", vertical_alignment="top", wrap=True)

    def test_context_is_visible_original_core_and_questions_only(self):
        source = fictional_map()
        app = AppTest.from_string(
            "from dispute_map_view import render_mediation_context\n"
            f"render_mediation_context(dict(content={source!r}))"
        ).run()
        self.assertFalse(app.exception)
        self.assertEqual([item.value for item in app.markdown], [
            f"# {TITLES[5]}\n{BODIES[5]}", f"# {TITLES[7]}\n{BODIES[7]}",
        ])
        self.assertEqual(len(app.expander), 0)
        self.assertEqual(len(app.get("column")), 0)
        self.assertIn("不是最终裁决", app.caption[0].value)

    def test_unavailable_artifacts_do_not_render_or_parse_placeholder(self):
        for artifact in (None, {}, {"content": ""}, {"content": "", "generation_failed_at": "fictional"}):
            with patch("dispute_map_view.st") as ui, patch("dispute_map_view.parse_dispute_map") as parser:
                render_mediation_context(artifact)
                self.assertEqual(ui.mock_calls, [])
                parser.assert_not_called()

    def test_fallback_context_and_evidence_remain_byte_exact(self):
        for content in (fictional_map(), "# 虚构旧地图\r\n共同事实、待确认\r\n"):
            artifact = {"id": 1, "content": content, "evidence_hash": "f" * 64}
            original = copy.deepcopy(artifact)
            snapshot = build_evidence_snapshot(
                case_id="FICTIONAL", created_at="2026-01-01", requester="A", confirmer="B",
                statements={"A": "虚构 A", "B": "虚构 B"}, dispute_map=content, messages=[],
            )
            digest = evidence_hash(snapshot)
            with patch("dispute_map_view.st") as ui:
                render_mediation_context(artifact)
                if parse_dispute_map(content) is None:
                    ui.markdown.assert_called_once_with(content)
            self.assertEqual(artifact, original)
            self.assertEqual(snapshot["dispute_map"].encode(), content.encode())
            self.assertEqual(evidence_hash(snapshot), digest)
