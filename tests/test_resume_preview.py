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
    parse_kimi_wire, load_turns, format_turn_body,
    find_session_jsonl, find_claude_jsonl,
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

    def test_parse_grok_skips_system_reminder_user(self):
        recs = [
            {"type": "user", "content": [{"type": "text", "text": "move the sim hands."}]},
            {"type": "assistant", "content": "ok",
             "tool_calls": [{"name": "run_terminal_command"}]},
            {"type": "user", "content": [{
                "type": "text",
                "text": "<system-reminder>\nBackground task \"term_d976b6dc3d\" "
                        "completed (terminated by signal SIGTERM).\n</system-reminder>",
            }]},
            {"type": "assistant", "tool_calls": [
                {"name": "run_terminal_command"},
                {"name": "run_terminal_command"},
            ]},
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
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt"], "move the sim hands.")
        self.assertEqual(turns[0]["reply"], "ok")
        self.assertEqual(
            turns[0]["tools"],
            ["run_terminal_command", "run_terminal_command", "run_terminal_command"],
        )
        body = format_turn_body(turns[0])
        self.assertIn("⚙ run_terminal_command ×3", body)
        self.assertNotIn("system-reminder", body)
        self.assertNotIn("term_d976b6dc3d", body)

    def test_parse_kimi_wire(self):
        path = os.path.join(_ROOT, "tests", "fixtures", "kimi_wire_preview.jsonl")
        turns = parse_kimi_wire(path)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt"], "what's the performance?")
        self.assertEqual(turns[0]["tools"], ["Read"])
        self.assertEqual(turns[0]["reply"], "Let me check RichLabel.")
        self.assertEqual(turns[1]["prompt"], "so richlabel has no cache?")
        self.assertEqual(turns[1]["tools"], ["Grep"])
        self.assertIn("no cache", turns[1]["reply"])

    def test_parse_kimi_wire_drops_cancelled_prompt(self):
        recs = [
            {"type": "turn.prompt", "input": "keep me", "origin": {"kind": "user"}},
            {"type": "context.append_loop_event",
             "event": {"type": "content.part",
                       "part": {"type": "text", "text": "ok"}}},
            {"type": "turn.prompt", "input": "go", "origin": {"kind": "user"}},
            {"type": "turn.cancel"},
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            turns = parse_kimi_wire(path)
        finally:
            os.remove(path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["prompt"], "keep me")
        self.assertEqual(turns[0]["reply"], "ok")

    def test_load_turns_kimi_empty_without_store(self):
        self.assertEqual(load_turns("session_missing", "kimi"), [])


    def test_find_session_jsonl_missing(self):
        self.assertIsNone(find_session_jsonl("", "grok"))
        self.assertIsNone(find_session_jsonl("no-such-session", "kimi"))
        self.assertIsNone(find_claude_jsonl(""))


if __name__ == "__main__":
    unittest.main()
