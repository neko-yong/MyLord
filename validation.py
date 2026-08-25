STATEMENT_FIELDS = (
    ("start", "事情如何开始"),
    ("timeline", "关键时间线"),
    ("complaint", "对方让我不满的具体行为"),
    ("own", "我当时的具体行为"),
    ("emotion", "我的情绪"),
    ("need", "我的核心需要"),
    ("request", "我希望对方做什么 / 希望解决什么"),
    ("self_reflect", "我认为自己可能做得不好的地方"),
    ("evidence", "原话 / 聊天记录 / 其他证据"),
)

REQUIRED_STATEMENT_FIELDS = {
    "start": ("事情经过", 10),
    "complaint": ("对方的具体行为", 5),
    "own": ("你当时具体做了什么", 5),
    "need": ("你的核心需要", 2),
    "request": ("你希望解决什么", 2),
}


def _clean_statement_values(values):
    source = values if isinstance(values, dict) else {}
    return {
        key: str(source.get(key) or "").strip()
        for key, _heading in STATEMENT_FIELDS
    }


def validate_statement_fields(values):
    cleaned = _clean_statement_values(values)
    errors = {}
    for key, (label, minimum_length) in REQUIRED_STATEMENT_FIELDS.items():
        value = cleaned[key]
        if not value:
            errors[key] = f"{label}：未填写"
        elif len(value) < minimum_length:
            errors[key] = f"{label}：至少需要 {minimum_length} 个字符"
    return cleaned, errors


def build_statement_content(role, values):
    if role not in {"A", "B"}:
        raise ValueError("无效的案件身份。")
    cleaned, errors = validate_statement_fields(values)
    if errors:
        raise ValueError("独立陈述仍有必填内容未完成。")

    sections = []
    for key, heading in STATEMENT_FIELDS:
        value = cleaned[key] or "（未提供）"
        sections.append(f"## {heading}\n{value}")
    return f"# {role} 的独立陈述\n\n" + "\n\n".join(sections) + "\n"
