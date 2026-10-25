"""JSON repair receives the original error when an incomplete object is cropped."""
import json
import unittest

from agents.json_utils import extract_json


class JsonErrorReportingTests(unittest.TestCase):
    def test_unterminated_final_string_keeps_the_original_failure(self):
        text = '{"items": [{"reason": "complete"}, {"reason": "unfinished'
        with self.assertRaises(json.JSONDecodeError) as expected:
            json.loads(text)
        with self.assertRaises(json.JSONDecodeError) as actual:
            extract_json(text)
        self.assertEqual(actual.exception.msg, expected.exception.msg)
        self.assertEqual(actual.exception.pos, expected.exception.pos)
        self.assertEqual(actual.exception.doc, text)

    def test_valid_object_with_trailing_explanation_can_still_be_extracted(self):
        self.assertEqual(extract_json('{"value": 1}\nExtra text'), {"value": 1})

    def test_valid_object_with_prefix_can_still_be_extracted(self):
        self.assertEqual(extract_json('Here is the response: {"value": 1}'), {"value": 1})

    def test_fenced_incomplete_object_keeps_the_unterminated_string_error(self):
        with self.assertRaisesRegex(json.JSONDecodeError, "Unterminated string"):
            extract_json('```json\n{"items": [{"ok": 1}, {"reason": "unfinished\n```')
