"""Kimi auto-compact phrases must light the compact hint."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import _acp_is_compact_text  # noqa: E402


class TestCompactPhrases(unittest.TestCase):
    def test_kimi_auto_start(self):
        msg = "Compacting conversation context"
        self.assertTrue(_acp_is_compact_text(msg))
        with open(os.path.join(_ROOT, "session.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("compacting conversation context", src.lower())
        self.assertIn("*Compacting conversation context…*", src)

    def test_kimi_auto_done(self):
        msg = (
            "Compaction completed.\n"
            "- Messages compacted: 1,204\n"
            "- Tokens before: 98,000\n"
        )
        self.assertTrue(_acp_is_compact_text(msg))
        self.assertFalse(_acp_is_compact_text("weaker grain"))

    def test_load_replay_keeps_compact_chunks(self):
        path = os.path.join(_ROOT, "bridge", "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("def _forward_load_replay")
        end = src.find("\n    def ", start + 10)
        body = src[start:end]
        self.assertIn("_acp_is_compact_text", body)
        self.assertIn("text_delta", body)


if __name__ == "__main__":
    unittest.main()
