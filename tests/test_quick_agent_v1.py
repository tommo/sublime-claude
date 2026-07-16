#!/usr/bin/env python3
"""Offline tests for Quick Agent v1 pure helpers + tool wiring.

Run:  python3 tests/test_quick_agent_v1.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def _extract_function_source(src: str, name: str) -> str:
    """Extract a top-level function body via AST (shipped source)."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"function {name} not found")


class TestShippedPureHelpers(unittest.TestCase):
    """Drive the real functions from quick_agent.py (mocked sublime only)."""

    @classmethod
    def setUpClass(cls):
        import types
        # Minimal sublime stub so importing quick_agent succeeds
        sublime = types.ModuleType("sublime")
        sublime.Region = lambda *a, **k: type("R", (), {})()
        sublime.Phantom = lambda *a, **k: None
        sublime.PhantomSet = lambda *a, **k: None
        sublime.LAYOUT_BLOCK = 1
        sublime.LAYOUT_BELOW = 2
        sublime.load_settings = lambda n: type("S", (), {
            "get": lambda self, k, d=None: d if d is not None else {},
            "set": lambda self, k, v: None,
        })()
        sublime.save_settings = lambda n: None
        sublime.status_message = lambda m: None
        sublime.error_message = lambda m: None
        sublime.active_window = lambda: None
        sublime.windows = lambda: []
        sublime.set_timeout = lambda f, t=0: None
        sys.modules["sublime"] = sublime
        sp = types.ModuleType("sublime_plugin")
        sp.WindowCommand = object
        sp.TextCommand = object
        sp.EventListener = object
        sys.modules["sublime_plugin"] = sp

        # Package stubs for relative imports inside quick_agent
        pkg = types.ModuleType("pkg")
        pkg.__path__ = [ROOT]
        sys.modules["pkg"] = pkg

        class Session:
            def __init__(self, *a, **k):
                self.quick_mode = True
                self.client = None
                self.output = None
                self.window = None
                self.working = False
                self.initialized = False
                self.name = ""
                self.draft_prompt = ""
                self.backend = "deepseek"
                self.profile = {}
                self.context = type("C", (), {
                    "items": [],
                    "_add_path_ref": lambda *a, **k: None,
                })()

            def start(self):
                pass

            def _enter_input_with_draft(self):
                pass

        sess_mod = types.ModuleType("pkg.session")
        sess_mod.Session = Session
        sys.modules["pkg.session"] = sess_mod

        backends = types.ModuleType("pkg.backends")
        backends.all_backends = lambda: {}
        backends.get = lambda n: type("S", (), {
            "label": n, "default_models": [], "available": None,
        })()
        sys.modules["pkg.backends"] = backends

        import importlib.util
        # Rewrite relative imports for file load
        path = os.path.join(ROOT, "quick_agent.py")
        src = open(path).read()
        src = src.replace("from . import backends", "from pkg import backends")
        src = src.replace("from .session import Session", "from pkg.session import Session")
        src = src.replace("from .context_manager", "from pkg.context_manager")
        # context_manager only used inside attach — stub if imported
        cm = types.ModuleType("pkg.context_manager")
        cm.format_line_range = lambda a, b: f"L{a}-L{b}"
        sys.modules["pkg.context_manager"] = cm

        spec = importlib.util.spec_from_loader("pkg.quick_agent", loader=None)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pkg.quick_agent"] = mod
        exec(compile(src, path, "exec"), mod.__dict__)
        cls.qa = mod

    def test_max_slots_is_3(self):
        self.assertEqual(self.qa.MAX_QUICK_SLOTS, 3)

    def test_can_add_slot(self):
        self.assertTrue(self.qa.can_add_slot(0))
        self.assertTrue(self.qa.can_add_slot(2))
        self.assertFalse(self.qa.can_add_slot(3))

    def test_normalize_done_status(self):
        self.assertEqual(self.qa.normalize_done_status("completed"), "completed")
        self.assertEqual(self.qa.normalize_done_status("blocked"), "blocked")
        self.assertEqual(self.qa.normalize_done_status("FAILED"), "blocked")
        self.assertEqual(self.qa.normalize_done_status(None), "completed")

    def test_system_prompt_self_stop_not_update_goal(self):
        p = self.qa.default_system_prompt()
        self.assertIn("quick_done", p)
        self.assertNotIn("update_goal", p)

    def test_stop_session_bridge(self):
        class FC:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FS:
            def __init__(self):
                self.client = FC()
                self.initialized = True
                self.working = True

        s = FS()
        self.assertTrue(self.qa.stop_session_bridge(s))
        self.assertIsNone(s.client)
        self.assertFalse(s.working)
        self.assertFalse(s.initialized)


class TestToolWiringInRepo(unittest.TestCase):
    def test_quick_done_in_mcp_server_tools_list(self):
        src = _read("mcp/server.py")
        self.assertIn('"name": "quick_done"', src)
        self.assertIn("Quick Agent", src)

    def test_quick_done_in_tool_router(self):
        src = _read("tool_router.py")
        self.assertIn('register("quick_done"', src)
        self.assertIn("return quick_done(", src)

    def test_quick_done_handler_in_mcp_server(self):
        src = _read("mcp_server.py")
        self.assertIn("def _quick_done", src)
        self.assertIn("complete_quick_from_tool", src)

    def test_queue_not_transcript_pollution(self):
        src = _read("session.py")
        # queue_prompt must not write ◎ [queued] into transcript
        # (regression: Grok-style chrome is phantom-only)
        m = re.search(
            r"def queue_prompt\(.*?\n(?:    .*\n)*?    def ",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "queue_prompt not found")
        body = m.group(0)
        self.assertNotIn('◎ [queued]', body)
        self.assertIn("_update_queue_phantom", body)

    def test_host_max_enforced_in_source(self):
        src = _read("quick_agent.py")
        self.assertIn("MAX_QUICK_SLOTS = 3", src)
        self.assertIn("can_add_slot", src)
        self.assertIn("at most", src)


if __name__ == "__main__":
    unittest.main()
