"""Session model persists across restart instead of falling back to grok-4.6."""
from __future__ import annotations

import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestResolveInitModel(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("sublime", types.SimpleNamespace())
        import session_registry as sr
        self.resolve = sr.resolve_init_model

    def test_new_session_uses_default(self):
        self.assertEqual(
            self.resolve(default_model="grok-4.6"),
            "grok-4.6",
        )

    def test_profile_beats_default(self):
        self.assertEqual(
            self.resolve(
                profile_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )

    def test_live_session_beats_default(self):
        self.assertEqual(
            self.resolve(
                session_model="deepseek-v4-flash",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash",
        )

    def test_new_session_ignores_view_stamp(self):
        self.assertEqual(
            self.resolve(
                view_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "grok-4.6",
        )

    def test_resume_view_stamp_beats_default(self):
        self.assertEqual(
            self.resolve(
                view_model="deepseek-v4-pro",
                default_model="grok-4.6",
                resume=True,
            ),
            "deepseek-v4-pro",
        )

    def test_saved_beats_default(self):
        self.assertEqual(
            self.resolve(
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )

    def test_resume_uses_default_if_nothing_saved(self):
        self.assertEqual(
            self.resolve(default_model="grok-4.6", resume=True),
            "grok-4.6",
        )

    def test_resume_keeps_saved_deepseek(self):
        self.assertEqual(
            self.resolve(
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
                resume=True,
            ),
            "deepseek-v4-pro",
        )

    def test_profile_beats_live_and_saved(self):
        self.assertEqual(
            self.resolve(
                profile_model="deepseek-v4-flash",
                session_model="grok-4.6",
                saved_model="grok-4.6",
                default_model="grok-4.6",
            ),
            "deepseek-v4-flash",
        )

    def test_blank_strings_are_skipped(self):
        self.assertEqual(
            self.resolve(
                session_model="  ",
                view_model="",
                saved_model="deepseek-v4-pro",
                default_model="grok-4.6",
            ),
            "deepseek-v4-pro",
        )


class _ModelBridge:
    """Minimal stand-in for AcpBridge.normalize/resolve_applied_model."""

    def __init__(self):
        from acp_base import AcpBridge
        self.DEFAULT_MODEL = "grok-4.6"
        self.MODEL_ALIASES = dict(AcpBridge.MODEL_ALIASES)
        self.MODEL_ALIASES.update({
            "grok-4.5": "grok-4.6",
            "deepseek-pro": "deepseek-v4-pro",
        })
        self.normalize_model = AcpBridge.normalize_model.__get__(self, _ModelBridge)
        self.resolve_applied_model = AcpBridge.resolve_applied_model.__get__(
            self, _ModelBridge)


class TestResolveAppliedModel(unittest.TestCase):
    def setUp(self):
        self.b = _ModelBridge()

    def test_trusts_agent_current(self):
        self.assertEqual(
            self.b.resolve_applied_model("deepseek-v4-pro", "grok-4.6"),
            "grok-4.6",
        )

    def test_accepts_alias_of_requested(self):
        self.assertEqual(
            self.b.resolve_applied_model("deepseek-pro", "deepseek-v4-pro"),
            "deepseek-v4-pro",
        )

    def test_empty_current_keeps_requested(self):
        self.assertEqual(
            self.b.resolve_applied_model("deepseek-v4-pro", None),
            "deepseek-v4-pro",
        )


class TestClearDispatch(unittest.TestCase):
    def test_acp_exposes_clear(self):
        from acp_base import AcpBridge

        class _B(AcpBridge):
            def __init__(self):
                pass

        table = AcpBridge.extra_dispatch(_B())
        self.assertIn("clear", table)
        self.assertEqual(table["clear"].__name__, "handle_clear")


class TestGrokSpawnModelFlag(unittest.TestCase):
    def test_spawn_passes_session_model(self):
        from grok_main import GrokBridge

        class _Spawn(GrokBridge):
            def __init__(self):
                self.model = "deepseek-v4-pro"
                self.effort = ""
                self.permission_mode = "default"
                self._always_approve = False

        argv = _Spawn().agent_argv()
        self.assertIn("--model", argv)
        self.assertIn("deepseek-v4-pro", argv)


if __name__ == "__main__":
    unittest.main()
