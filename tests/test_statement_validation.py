import unittest

from validation import (
    REQUIRED_STATEMENT_FIELDS,
    build_statement_content,
    validate_statement_fields,
)


def valid_required_values():
    return {
        "start": "事情从周末安排的讨论开始，后来双方发生了争执。",
        "timeline": "",
        "complaint": "对方临时取消约定，而且没有提前说明。",
        "own": "我提高了声音，并且打断了对方说话。",
        "emotion": "",
        "need": "提前告知",
        "request": "希望以后改动共同安排前先沟通。",
        "self_reflect": "",
        "evidence": "",
    }


class StatementValidationTests(unittest.TestCase):
    def test_all_fields_empty_are_rejected(self):
        cleaned, errors = validate_statement_fields({})

        self.assertEqual(set(errors), set(REQUIRED_STATEMENT_FIELDS))
        self.assertTrue(all(value == "" for value in cleaned.values()))

    def test_whitespace_only_is_rejected(self):
        values = {key: " \n   " for key in valid_required_values()}

        _cleaned, errors = validate_statement_fields(values)

        self.assertEqual(set(errors), set(REQUIRED_STATEMENT_FIELDS))

    def test_only_optional_fields_are_rejected(self):
        values = {
            "timeline": "周六晚上",
            "emotion": "失望",
            "self_reflect": "也许语气太急",
            "evidence": "补充记录",
        }

        _cleaned, errors = validate_statement_fields(values)

        self.assertEqual(set(errors), set(REQUIRED_STATEMENT_FIELDS))

    def test_missing_one_required_field_is_rejected(self):
        values = valid_required_values()
        values["own"] = ""

        _cleaned, errors = validate_statement_fields(values)

        self.assertEqual(set(errors), {"own"})
        self.assertIn("你当时具体做了什么", errors["own"])

    def test_all_required_valid_and_optional_empty_are_accepted(self):
        values = valid_required_values()

        cleaned, errors = validate_statement_fields(values)
        content = build_statement_content("A", cleaned)

        self.assertEqual(errors, {})
        self.assertEqual(content.count("（未提供）"), 4)
        self.assertIn(values["start"], content)
        self.assertIn("# A 的独立陈述", content)

    def test_minimum_lengths_reject_obviously_invalid_answers(self):
        values = valid_required_values()
        values.update(
            {
                "start": "不知道",
                "complaint": "没有",
                "own": "无",
                "need": "无",
                "request": "无",
            }
        )

        _cleaned, errors = validate_statement_fields(values)

        self.assertEqual(set(errors), set(REQUIRED_STATEMENT_FIELDS))

    def test_a_and_b_use_identical_validation_rules(self):
        values = valid_required_values()
        values["request"] = " "

        _cleaned_a, errors_a = validate_statement_fields(values)
        _cleaned_b, errors_b = validate_statement_fields(values)

        self.assertEqual(errors_a, errors_b)
        with self.assertRaises(ValueError):
            build_statement_content("A", values)
        with self.assertRaises(ValueError):
            build_statement_content("B", values)


if __name__ == "__main__":
    unittest.main()
