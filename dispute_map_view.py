"""Read-only views of saved dispute-map Markdown; uncertain formats stay whole."""

import re
from typing import NamedTuple

import streamlit as st


SECTION_TITLES = (
    "事件地图",
    "双方一致的事实",
    "存在争议的事实",
    "A 的结构化信息",
    "B 的结构化信息",
    "真正的冲突核心",
    "当前证据不足之处",
    "下一阶段最值得确认的 3 个问题",
)
ATX = re.compile(r"^( {0,3})(#{1,6})(?:[ \t\u3000]+(.*)|$)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


class Section(NamedTuple):
    title: str
    start: int
    end: int


def parse_dispute_map(content):
    """Return original-order spans (including the preamble), or None.

    Only eight unique, nonempty, same-level chapters in contract order are
    accepted. Deeper subheadings remain in their parent chapter. This is not a
    Markdown engine: ambiguous nesting and cross-block markup fall back intact.
    """
    headings = []
    offset = 0
    fence = None
    container_seen = False
    for line in content.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        marker = FENCE.fullmatch(text)
        if fence:
            if (marker and marker[1][0] == fence[0]
                    and len(marker[1]) >= len(fence) and not marker[2].strip()):
                fence = None
        elif marker:
            if marker[1][0] == "`" and "`" in marker[2]:
                return None
            fence = marker[1]
        else:
            # Splitting these constructs can change Markdown scope or link targets.
            if re.match(r"^ {0,3}(?:<|\[[^\]]+\]:|=+\s*$|[-]+\s*$)", text):
                return None
            if re.match(r"^ {0,3}(?:>|[-+*] |\d+[.)] )", text):
                container_seen = True
            heading = ATX.fullmatch(text)
            if heading:
                if heading[1] and container_seen:
                    return None
                title = (heading[3] or "").strip(" \t\u3000")
                title = re.sub(r"[ \t\u3000]+#+$", "", title)
                title = re.sub(r"[ \t\u3000]+", " ", title).strip()
                headings.append((title, len(heading[2]), offset, offset + len(line)))
        offset += len(line)

    if fence:
        return None
    chapters = [heading for heading in headings if heading[0] in SECTION_TITLES]
    if tuple(heading[0] for heading in chapters) != SECTION_TITLES:
        return None
    level = chapters[0][1]
    if any(heading[1] != level for heading in chapters):
        return None
    for title, depth, start, _ in headings:
        if title not in SECTION_TITLES:
            if start < chapters[0][2]:
                if depth >= level:
                    return None
            elif depth <= level:
                return None

    sections = [Section("", 0, chapters[0][2])]
    for index, (title, _, start, body_start) in enumerate(chapters):
        end = chapters[index + 1][2] if index + 1 < len(chapters) else len(content)
        if not content[body_start:end].strip():
            return None
        sections.append(Section(title, start, end))
    return tuple(sections)


def render_dispute_map(content):
    st.caption("双方说法一致不代表已独立核实。")
    sections = parse_dispute_map(content)
    if sections is None:
        st.info("无法可靠分段，以下保留原始争议地图。")
        st.markdown(content)
        return

    for section in sections:
        if section.title == SECTION_TITLES[3]:
            a_column, b_column = st.columns(
                2, gap="medium", vertical_alignment="top", wrap=True,
            )
            with a_column:
                st.markdown(content[section.start:section.end])
            b_section = sections[5]
            with b_column:
                st.markdown(content[b_section.start:b_section.end])
        elif section.title != SECTION_TITLES[4] and section.start < section.end:
            st.markdown(content[section.start:section.end])


def render_mediation_context(dispute):
    if not dispute or not dispute.get("content"):
        return
    content = dispute["content"]
    # A static container always starts visible, without client fold state to leak
    # across cases. It remains in normal flow, outside the chat fragment.
    with st.container(border=True):
        st.caption("来自争议地图，供继续讨论，不是最终裁决。")
        sections = parse_dispute_map(content)
        if sections is None:
            st.info("无法可靠提取冲突核心及问题，以下保留原始争议地图。")
            st.caption("双方说法一致不代表已独立核实。")
            st.markdown(content)
        else:
            for section in sections:
                if section.title in (SECTION_TITLES[5], SECTION_TITLES[7]):
                    st.markdown(content[section.start:section.end])
