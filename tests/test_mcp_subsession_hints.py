"""Child completion is signal_complete XOR send_to_session, not both."""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestMcpSubsessionHints(unittest.TestCase):
    def _read(self, *parts):
        with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_spawn_contract_forbids_mailing_parent(self):
        src = self._read("mcp_server.py")
        start = src.find("def _with_subsession_report_contract")
        body = src[start:src.find("\n    def ", start + 10)]
        self.assertIn("signal_complete", body)
        self.assertIn("do not also ", body.lower())
        self.assertIn("send_to_session", body)

    def test_tool_descriptions_xor_mail_and_signal(self):
        src = self._read("mcp", "server.py")
        self.assertIn("send_to_session the parent", src)
        self.assertIn("This is the subscribe path", src)
        self.assertIn("Not for parent/child session completion", src)

    def test_spawn_schema_has_model(self):
        src = self._read("mcp", "server.py")
        self.assertIn('"model"', src)
        self.assertIn("source session's model", src)
        impl = self._read("mcp_server.py")
        self.assertIn("resolve_spawn_model", impl)
        self.assertIn("model=spawn_model", impl)

    def test_chatroom_tool_removed(self):
        mcp = self._read("mcp", "server.py")
        router = self._read("tool_router.py")
        host = self._read("mcp_server.py")
        fmt = self._read("tool_formatters_sublime.py")
        self.assertNotIn('"name": "chatroom"', mcp)
        self.assertNotIn("chatroom_handler", router)
        self.assertNotIn("def chatroom_list", host)
        self.assertNotIn('"chatroom":', fmt)

    def test_list_sessions_gets_caller_view_id(self):
        src = self._read("mcp", "server.py")
        self.assertIn('"list_sessions"', src)
        self.assertIn(
            '"spawn_session", "send_to_session", "list_sessions"', src)
        router = self._read("tool_router.py")
        self.assertIn("list_sessions(_caller_view_id=", router)


if __name__ == "__main__":
    unittest.main()
