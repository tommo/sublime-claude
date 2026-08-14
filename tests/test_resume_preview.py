"""Resume-preview: last turn, plus earlier if the tail is short."""
import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from resume_preview import (
    display_prompt, select_preview, parse_claude_jsonl, parse_grok_chat,
    format_turn_body,
)


class TestResumePreview(unittest.TestCase):
    def test_display_prompt_unwraps_user_query(self):
        raw = "<user_query>\npush\n</user_query>"
        self.assertEqual(display_prompt(raw), "push")

    def test_select_extends_short_tail(self):
        turns = [
            {"prompt": "aaa " * 80, "reply": "bbb " * 80, "tools": []},
            {"prompt": "push", "reply": "done", "tools": []},
        ]
        chosen = select_preview(turns, min_chars=200, max_turns=8)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(chosen[-1]["prompt"], "push")
        long_last = [
            {"prompt": "old", "reply": "x", "tools": []},
            {"prompt": "new", "reply": "y" * 400, "tools": []},
        ]
        only = select_preview(long_last, min_chars=200, max_turns=8)
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["prompt"], "new")

    def test_parse_claude_jsonl(self):
        recs = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "id": "1"},
                {"type": "text", "text": "world"},
            ]}},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_claude_jsonl(path)
        finally:
            os.remove(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt"], "hello")
        self.assertEqual(turns[0]["reply"], "world")
        self.assertEqual(turns[0]["tools"], ["Read"])

    def test_parse_grok_chat(self):
        recs = [
            {"type": "user", "content": [{"type": "text", "text": "hi"}]},
            {"type": "assistant", "content": "yo",
             "tool_calls": [{"name": "read_file"}]},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_grok_chat(path)
        finally:
            os.remove(path)
        self.assertEqual(turns[0]["prompt"], "hi")
        self.assertEqual(turns[0]["reply"], "yo")
        self.assertIn("⚙ read_file", format_turn_body(turns[0]))
        self.assertIn("yo", format_turn_body(turns[0]))


if __name__ == "__main__":
    unittest.main()
