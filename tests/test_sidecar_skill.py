"""Unqualified sidecar in a Sublime session is SUBLIME SIDECAR."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from sidecar_skill import RULE, SKILL_PATH, additional_skill_dirs  # noqa: E402


class TestSidecarSkill(unittest.TestCase):
    def test_skill_file_exists(self):
        self.assertTrue(os.path.isfile(SKILL_PATH))
        with open(SKILL_PATH, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("name: sidecar", body)
        self.assertIn("SUBLIME SIDECAR", body)
        self.assertIn("spawn_session", body)
        self.assertIn("grok -p", body)
        self.assertIn("Reuse warm sheets", body)
        self.assertIn("Honor the user's model", body)
        self.assertIn("list_profile_docs", body)
        self.assertIn("signal_complete", body)

    def test_rule_points_at_skill(self):
        self.assertIn("SUBLIME SIDECAR", RULE)
        self.assertIn("spawn_session", RULE)
        self.assertIn("list_sessions", RULE)
        self.assertIn("Honor the user's model", RULE)
        self.assertIn("list_profile_docs", RULE)
        self.assertIn("signal_complete", RULE)
        self.assertIn(SKILL_PATH, RULE)

    def test_additional_skill_dirs(self):
        dirs = additional_skill_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertTrue(os.path.isdir(dirs[0]))
        self.assertTrue(os.path.isfile(os.path.join(dirs[0], "sidecar", "SKILL.md")))

    def test_acp_meta_includes_rule(self):
        from acp_base import AcpBridge

        class _B(AcpBridge):
            def __init__(self):
                pass

        meta = _B().build_session_meta()
        self.assertIn("sidecar", meta.get("rules", "").lower())
        self.assertIn("spawn_session", meta.get("rules", ""))

    def test_mcp_initialize_instructions(self):
        import importlib.util
        path = os.path.join(_ROOT, "mcp", "server.py")
        spec = importlib.util.spec_from_file_location("mcp_server_stdio", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        resp = mod.handle_request({"id": 1, "method": "initialize", "params": {}})
        inst = (resp.get("result") or {}).get("instructions") or ""
        self.assertIn("SUBLIME SIDECAR", inst)
        self.assertIn("spawn_session", inst)

    def test_spawn_session_blurb(self):
        path = os.path.join(_ROOT, "mcp", "server.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('says "sidecar"', src)
        self.assertIn("SUBLIME sidecar", src)
        self.assertIn("Reuse warm sheets", src)
        self.assertIn("Honor the user's model", src)
        self.assertIn("list_profile_docs", src)
        self.assertIn("signal_complete", src)


if __name__ == "__main__":
    unittest.main()
