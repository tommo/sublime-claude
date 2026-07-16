#!/usr/bin/env python3
"""Offline tests for Quick Agent v1 pure helpers + complete_slot path.

Run:  python3 tests/test_quick_agent_v1.py
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def _load_quick_agent():
    """Import shipped quick_agent.py with sublime stubs."""
    sublime = types.ModuleType("sublime")
    sublime.Region = lambda *a, **k: type("R", (), {})()
    sublime.Phantom = lambda *a, **k: None
    sublime.PhantomSet = lambda *a, **k: None
    sublime.LAYOUT_BLOCK = 1
    sublime.LAYOUT_BELOW = 2
    _settings_store = {}

    class FakeSettings:
        def get(self, k, d=None):
            return _settings_store.get(k, d)

        def set(self, k, v):
            _settings_store[k] = v

        def erase(self, k):
            _settings_store.pop(k, None)

        def has(self, k):
            return k in _settings_store

    sublime.load_settings = lambda n: FakeSettings()
    sublime.save_settings = lambda n: None
    sublime.status_message = lambda m: None
    sublime.error_message = lambda m: None
    sublime.active_window = lambda: None
    sublime.windows = lambda: []
    sublime.set_timeout = lambda f, t=0: None
    sublime._claude_sessions = {}
    sys.modules["sublime"] = sublime

    sp = types.ModuleType("sublime_plugin")
    sp.WindowCommand = object
    sp.TextCommand = object
    sp.EventListener = object
    sys.modules["sublime_plugin"] = sp

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
            self._quick_slot_id = None
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

    cm = types.ModuleType("pkg.context_manager")
    cm.format_line_range = lambda a, b: f"L{a}-L{b}"
    sys.modules["pkg.context_manager"] = cm

    path = os.path.join(ROOT, "quick_agent.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    src = src.replace("from . import backends", "from pkg import backends")
    src = src.replace("from .session import Session", "from pkg.session import Session")
    src = src.replace("from .context_manager", "from pkg.context_manager")

    mod = types.ModuleType("pkg.quick_agent")
    sys.modules["pkg.quick_agent"] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod, sublime, Session, FakeSettings


class TestShippedPureHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qa, cls.sublime, cls.Session, cls.FakeSettings = _load_quick_agent()

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


class TestCompleteSlotHandler(unittest.TestCase):
    """Exercise shipped complete_slot / complete_quick_from_tool with stubs."""

    @classmethod
    def setUpClass(cls):
        cls.qa, cls.sublime, cls.Session, cls.FakeSettings = _load_quick_agent()

    def _make_session(self, slot_id, working=True):
        class FC:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True
                self.client_dead = True

            def is_alive(self):
                return not self.stopped

        class FakeOutput:
            view = None
            _input_mode = False

            def is_input_mode(self):
                return False

        s = self.Session()
        s.client = FC()
        s.working = working
        s.initialized = True
        s.quick_mode = True
        s.output = FakeOutput()
        s._quick_slot_id = slot_id
        return s

    def _make_host(self, slots_working=None):
        """Build a QuickHost with N slots without a real ST window/view."""
        if slots_working is None:
            slots_working = [True, False]

        class FakeWin:
            def __init__(self):
                self._s = self.FakeSettings = type(self.qa.FakeSettings if False else object, (), {})()
                self.settings_obj = type("WS", (), {
                    "_d": {},
                    "get": lambda self, k, d=None: self._d.get(k, d),
                    "set": lambda self, k, v: self._d.__setitem__(k, v),
                    "erase": lambda self, k: self._d.pop(k, None),
                    "has": lambda self, k: k in self._d,
                })()
                self.settings_obj._d = {}

            def settings(self):
                return self.settings_obj

            def id(self):
                return 42

        # Simpler window mock
        win_settings = {}

        class WSettings:
            def get(self, k, d=None):
                return win_settings.get(k, d)

            def set(self, k, v):
                win_settings[k] = v

            def erase(self, k):
                win_settings.pop(k, None)

            def has(self, k):
                return k in win_settings

        class FakeWindow:
            def settings(self):
                return WSettings()

            def id(self):
                return 99

            def folders(self):
                return []

            def views(self):
                return []

            def active_view(self):
                return None

            def focus_view(self, v):
                pass

            def new_file(self):
                return None

        host = self.qa.QuickHost(FakeWindow())
        host.view = None  # no paint path
        for i, working in enumerate(slots_working, 1):
            sid = f"q{i}"
            sess = self._make_session(sid, working=working)
            sess.window = host.window
            slot = self.qa.QuickSlot(slot_id=sid, session=sess, name=f"Q{i}")
            host.slots[sid] = slot
            sess._quick_slot_id = sid
        host.active_id = "q1"
        # Register host
        self.qa._hosts[host.window.id()] = host
        return host, win_settings

    def test_complete_slot_stops_bridge_and_sets_status(self):
        host, _ = self._make_host([True])
        slot = host.slots["q1"]
        sess = slot.session
        client = sess.client
        result = host.complete_slot(sess, status="completed", message="done ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["slot"], "q1")
        self.assertTrue(result["bridge_stopped"])
        self.assertEqual(slot.status, "completed")
        self.assertEqual(slot.status_message, "done ok")
        self.assertIsNone(sess.client)
        self.assertFalse(sess.working)
        # client was stopped
        self.assertTrue(client.stopped)

    def test_complete_slot_blocked(self):
        host, _ = self._make_host([True])
        sess = host.slots["q1"].session
        result = host.complete_slot(sess, status="blocked", message="need human")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(host.slots["q1"].status, "blocked")

    def test_complete_quick_from_tool_uses_executing_slot_not_active(self):
        """Inactive working slot's quick_done must not stop the focused idle slot."""
        host, win_settings = self._make_host([False, True])  # q1 active idle, q2 working
        host.active_id = "q1"
        host.slots["q1"].session.working = False
        host.slots["q2"].session.working = True
        # Pin executing slot to q2 (as Session.query would)
        win_settings["claude_executing_quick_slot"] = "q2"

        # Fake host view id for resolve
        class V:
            def id(self):
                return 1001

            def is_valid(self):
                return True

        host.view = V()
        # complete via tool entry with host view_id
        result = self.qa.complete_quick_from_tool(
            status="completed",
            message="bg done",
            view_id=1001,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("slot"), "q2")
        self.assertEqual(host.slots["q2"].status, "completed")
        # active idle slot must remain live
        self.assertEqual(host.slots["q1"].status, "live")
        self.assertIsNone(host.slots["q2"].session.client)
        # q1 bridge still "alive"
        self.assertIsNotNone(host.slots["q1"].session.client)

    def test_resolve_prefers_sole_working_slot(self):
        host, win_settings = self._make_host([False, True])
        host.active_id = "q1"

        class V:
            def id(self):
                return 2002

            def is_valid(self):
                return True

        host.view = V()
        h, sess = self.qa.resolve_quick_session_for_tool(view_id=2002)
        self.assertIs(h, host)
        self.assertIs(sess, host.slots["q2"].session)

    def tearDown(self):
        self.qa._hosts.clear()


class TestToolWiringInRepo(unittest.TestCase):
    def test_quick_done_in_mcp_server_tools_list(self):
        src = _read("mcp/server.py")
        self.assertIn('"name": "quick_done"', src)

    def test_quick_done_in_tool_router(self):
        src = _read("tool_router.py")
        self.assertIn('register("quick_done"', src)

    def test_quick_done_handler_in_mcp_server(self):
        src = _read("mcp_server.py")
        self.assertIn("def _quick_done", src)
        self.assertIn("complete_quick_from_tool", src)

    def test_queue_not_transcript_pollution(self):
        src = _read("session.py")
        m = re.search(
            r"def queue_prompt\(.*?\n(?:    .*\n)*?    def ",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertNotIn("◎ [queued]", body)
        self.assertIn("_update_queue_phantom", body)

    def test_slot_id_assigned_in_source(self):
        src = _read("quick_agent.py")
        self.assertIn("s._quick_slot_id = sid", src)
        self.assertIn("claude_executing_quick_slot", src)
        self.assertIn("resolve_quick_session_for_tool", src)

    def test_host_max_enforced_in_source(self):
        src = _read("quick_agent.py")
        self.assertIn("MAX_QUICK_SLOTS = 3", src)
        self.assertIn("at most", src)


if __name__ == "__main__":
    unittest.main()
