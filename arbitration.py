import time

from db import DatabaseUnavailable
from llm import TASK_MAX_TOKENS
from prompts import FINAL_JUDGMENT_PROMPT, META_JUDGE_PROMPT


DATABASE_RETRY_DELAYS = (0.2, 0.5)


def retry_database_write(operation, sleep=time.sleep):
    for attempt in range(len(DATABASE_RETRY_DELAYS) + 1):
        try:
            return operation()
        except DatabaseUnavailable:
            if attempt == len(DATABASE_RETRY_DELAYS):
                raise
            sleep(DATABASE_RETRY_DELAYS[attempt])


def _checkpoint_content(
    database,
    case_id,
    kind,
    ask_llm,
    system_prompt,
    user_prompt,
    sleep,
):
    existing = database.get_artifact(case_id, kind)
    if existing and existing["content"]:
        return existing["content"]

    result = ask_llm(
        system_prompt,
        user_prompt,
        max_tokens=TASK_MAX_TOKENS[kind],
    )
    retry_database_write(
        lambda: database.save_checkpoint(case_id, kind, result.content),
        sleep=sleep,
    )
    return result.content


def run_final_arbitration(
    database,
    ask_llm,
    case_id,
    reservation_id,
    statements,
    dispute_content,
    history,
    dual_review=True,
    sleep=time.sleep,
):
    if dual_review:
        meta_checkpoint = database.get_artifact(case_id, "META_JUDGMENT")
        if meta_checkpoint and meta_checkpoint["content"]:
            final_result = meta_checkpoint["content"]
            retry_database_write(
                lambda: database.complete_artifact(
                    case_id,
                    reservation_id,
                    "FINAL_JUDGMENT",
                    final_result,
                ),
                sleep=sleep,
            )
            return final_result

    a = statements["A"]
    b = statements["B"]
    base = f"""===== A 独立陈述 =====
{a}

===== B 独立陈述 =====
{b}

===== 争议地图 =====
{dispute_content}

===== 共享调解历史 =====
{history}
"""
    first_judgment = _checkpoint_content(
        database,
        case_id,
        "JUDGMENT_NORMAL",
        ask_llm,
        FINAL_JUDGMENT_PROMPT,
        base + "\n\n" + FINAL_JUDGMENT_PROMPT,
        sleep,
    )

    if not dual_review:
        final_result = first_judgment
    else:
        swapped = f"""重要：这一次为了检测标签/位置偏差，请把原始 B 暂时视作 A，把原始 A 暂时视作 B。
完成判断后，仍然清楚说明这是“交换标签审理”的结果。

===== 临时 A（原始 B）=====
{b}

===== 临时 B（原始 A）=====
{a}

===== 原始争议地图 =====
{dispute_content}

===== 共享调解历史 =====
{history}

{FINAL_JUDGMENT_PROMPT}
"""
        second_judgment = _checkpoint_content(
            database,
            case_id,
            "JUDGMENT_SWAPPED",
            ask_llm,
            FINAL_JUDGMENT_PROMPT,
            swapped,
            sleep,
        )

        meta = f"""下面有两份仲裁。

===== Judgment 1：正常 A/B =====
{first_judgment}

===== Judgment 2：交换 A/B 标签 =====
{second_judgment}

原始身份始终是：
- 原始 A = 第一份独立陈述中的 A
- 原始 B = 第一份独立陈述中的 B

{META_JUDGE_PROMPT}
"""
        final_result = _checkpoint_content(
            database,
            case_id,
            "META_JUDGMENT",
            ask_llm,
            META_JUDGE_PROMPT,
            meta,
            sleep,
        )

    retry_database_write(
        lambda: database.complete_artifact(
            case_id,
            reservation_id,
            "FINAL_JUDGMENT",
            final_result,
        ),
        sleep=sleep,
    )
    return final_result
