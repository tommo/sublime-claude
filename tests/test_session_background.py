"""Detach live sessions instead of killing the bridge on sheet close."""
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings(dict):
    def get(self, k, d=None):
        return super().get(k, d)

    def set(self, k, v):
        self[k] = v


class _View:
    def __init__(self, vid):
        self._id = vid
        self._settings = _Settings({"claude_output": True})

    def id(self):
        return self._id

    def is_valid(self):
        return True

    def settings(self):
        return self._settings


class _Output:
    def __init__(self, view):
        self.view = view
        self._input_mode = True


class _Session:
    def __init__(self, view, aid="agent-1", sid="sess-1"):
        self.agent_id = aid
        self.session_id = sid
        self.output = _Output(view)
        self.client = object()
        self.initialized = True
        self.working = False
        self.quick_mode = False
        self.is_sleeping = False
        self.backgrounded = False

    def reset_phantoms_for_new_view(self):
        pass


class TestSessionBackground(unittest.TestCase):
    def setUp(self):
        sublime = types.SimpleNamespace(
            _claude_sessions={},
            _claude_agents={},
            _claude_background={},
        )
        sublime.load_settings = lambda _n: _Settings()
        sys.modules["sublime"] = sublime
        if "session_registry" in sys.modules:
            del sys.modules["session_registry"]
        import session_registry
        self.reg = session_registry
        self.sublime = sublime

    def test_detach_keeps_session_findable(self):
        v = _View(7)
        s = _Session(v)
        self.reg.register_session(s)
        self.assertIs(self.sublime._claude_sessions[7], s)
        self.assertTrue(self.reg.keep_running_on_close(s))
        self.assertTrue(self.reg.detach_session(s))
        self.assertNotIn(7, self.sublime._claude_sessions)
        self.assertIs(self.reg.find_live_by_session_id("sess-1"), s)
        self.assertIs(self.reg.get_session_by_agent_id("agent-1"), s)
        self.assertTrue(s.backgrounded)
        self.assertIsNone(s.output.view)
        self.assertEqual(len(self.reg.iter_sessions()), 1)

    def test_sleeping_does_not_keep_running(self):
        v = _View(8)
        s = _Session(v)
        s.client = None
        s.initialized = False
        s.is_sleeping = True
        self.assertFalse(self.reg.keep_running_on_close(s))

    def test_close_or_detach_live(self):
        v = _View(9)
        s = _Session(v)
        self.reg.register_session(s)
        self.assertEqual(self.reg.close_or_detach_session(s, v), "detach")
        self.assertTrue(s.backgrounded)
        self.assertTrue(v.settings().get("claude_soft_close"))
        self.assertIs(self.reg.find_live_by_session_id("sess-1"), s)

    def test_close_or_detach_sleeping_stops(self):
        v = _View(10)
        s = _Session(v)
        s.client = None
        s.initialized = False
        s.is_sleeping = True
        s.stopped = False
        s.stop = lambda: setattr(s, "stopped", True)
        self.reg.register_session(s)
        self.assertEqual(self.reg.close_or_detach_session(s, v), "stop")
        self.assertTrue(s.stopped)
        self.assertNotIn(10, self.sublime._claude_sessions)

    def test_sessions_for_window_includes_background(self):
        win = object()
        v = _View(11)
        s = _Session(v)
        s.window = win
        self.reg.register_session(s)
        self.reg.detach_session(s)
        found = self.reg.sessions_for_window(win)
        self.assertEqual(found, [s])


if __name__ == "__main__":
    unittest.main()
