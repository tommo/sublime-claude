"""ACP session/fork is real — do not open an empty session and shrug."""
import asyncio
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402
from grok_main import GrokBridge  # noqa: E402


class TestParseForkId(unittest.TestCase):
    def test_prefers_new_id(self):
        sid = AcpBridge._parse_fork_session_id(
            {"sessionId": "new-1"}, "old-1")
        self.assertEqual(sid, "new-1")

    def test_rejects_source_id(self):
        self.assertIsNone(AcpBridge._parse_fork_session_id(
            {"sessionId": "old-1"}, "old-1"))

    def test_grok_newSessionId(self):
        self.assertEqual(
            AcpBridge._parse_fork_session_id(
                {"newSessionId": "abc", "sessionId": "old-1"}, "old-1"),
            "abc")


class TestTryForkSession(unittest.TestCase):
    def test_tries_acp_then_xai(self):
        b = GrokBridge()
        b.cwd = "/Volumes/prj/eb"
        b._additional_dirs = []
        calls = []

        async def _send(method, params, timeout=None):
            calls.append((method, params))
            if method == "session/fork":
                raise RuntimeError("Method not found")
            if method == "_x.ai/session/fork":
                return {"sessionId": "forked-9"}
            raise RuntimeError("nope")

        b._send_acp = _send
        b._ingest_session_result = lambda r: None
        ok = asyncio.run(b._try_fork_session("src-1", [{"name": "sublime"}]))
        self.assertTrue(ok)
        self.assertEqual(b.session_id, "forked-9")
        self.assertEqual(calls[0][0], "session/fork")
        self.assertEqual(calls[0][1]["sessionId"], "src-1")
        self.assertEqual(calls[1][0], "_x.ai/session/fork")
        self.assertEqual(calls[1][1]["sourceSessionId"], "src-1")

    def test_empty_on_total_failure(self):
        b = GrokBridge()
        b.cwd = "/p"

        async def _send(method, params, timeout=None):
            raise RuntimeError("Method not found")

        b._send_acp = _send
        self.assertFalse(asyncio.run(b._try_fork_session("src-1", [])))

    def test_init_calls_fork_not_the_old_lie(self):
        path = os.path.join(_BRIDGE, "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_try_fork_session", src)
        self.assertNotIn("ACP has no fork", src)
        self.assertIn("session/fork", src)
        self.assertIn("_x.ai/session/fork", src)

    def test_host_paints_fork_preview(self):
        path = os.path.join(_ROOT, "session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("def _on_init")
        end = src.find("\n    def _load_env", start)
        body = src[start:end]
        self.assertIn("_paint_resume_preview", body)
        self.assertNotIn("self.resume_id and not self.fork", body)


if __name__ == "__main__":
    unittest.main()
