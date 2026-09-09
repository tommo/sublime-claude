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

    def test_list_children_by_parent_agent_id(self):
        parent = _Session(_View(1), aid="agent-p")
        child = _Session(_View(2), aid="agent-c")
        child.parent_agent_id = "agent-p"
        child.parent_view_id = 1
        self.reg.register_session(parent)
        self.reg.register_session(child)
        kids = self.reg.list_children_of(
            parent_view_id=1, parent_agent_id="agent-p")
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])

    def test_list_children_includes_background(self):
        parent = _Session(_View(3), aid="agent-p")
        child = _Session(_View(4), aid="agent-c")
        child.parent_agent_id = "agent-p"
        child.parent_view_id = 3
        self.reg.register_session(parent)
        self.reg.register_session(child)
        self.reg.detach_session(child)
        kids = self.reg.list_children_of(
            parent_view_id=3, parent_agent_id="agent-p")
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])

    def test_list_children_stale_view_still_matches_agent(self):
        parent = _Session(_View(10), aid="agent-p")
        child = _Session(_View(20), aid="agent-c")
        child.parent_agent_id = "agent-p"
        child.parent_view_id = 99
        self.reg.register_session(parent)
        self.reg.register_session(child)
        kids = self.reg.list_children_of(
            parent_view_id=10, parent_agent_id="agent-p")
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])

    def test_list_children_after_parent_agent_id_rotation(self):
        parent = _Session(_View(30), aid="agent-new")
        parent.session_id = "sess-parent"
        parent.agent_id_aliases = ["agent-old"]
        child = _Session(_View(31), aid="agent-c")
        child.parent_agent_id = "agent-old"
        child.parent_view_id = 29
        self.reg.register_session(parent)
        self.reg.register_session(child)
        kids = self.reg.list_children_of(parent=parent)
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])
        self.assertEqual(child.parent_agent_id, "agent-new")
        self.assertEqual(child.parent_view_id, 30)
        self.assertIn("agent-c", parent.child_agent_ids)

    def test_list_children_by_parent_session_id(self):
        parent = _Session(_View(40), aid="agent-p")
        parent.session_id = "sess-p"
        child = _Session(_View(41), aid="agent-c")
        child.parent_agent_id = "agent-gone"
        child.parent_view_id = 7
        child.parent_session_id = "sess-p"
        self.reg.register_session(parent)
        self.reg.register_session(child)
        kids = self.reg.list_children_of(parent=parent)
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])

    def test_list_children_stale_mcp_view_id(self):
        parent = _Session(_View(70), aid="agent-p")
        child = _Session(_View(71), aid="agent-c")
        child.parent_agent_id = "agent-gone"
        child.parent_view_id = 69
        self.reg.register_session(parent)
        self.reg.register_session(child)
        kids = self.reg.list_children_of(
            parent_view_id=70, parent_agent_id="agent-p", extra_view_ids=[69])
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])

    def test_list_children_via_noted_child_ids(self):
        parent = _Session(_View(50), aid="agent-p")
        parent.child_agent_ids = ["agent-c"]
        child = _Session(_View(51), aid="agent-c")
        child.parent_agent_id = "agent-gone"
        child.parent_view_id = 8
        self.reg.register_session(parent)
        self.reg.register_session(child)
        kids = self.reg.list_children_of(parent=parent)
        self.assertEqual([k.agent_id for k in kids], ["agent-c"])

    def test_harvest_mentioned_orphans(self):
        parent = _Session(_View(60), aid="agent-bbbbbbbbbbbb")
        child = _Session(_View(61), aid="agent-cccccccccccc")
        child.parent_agent_id = "agent-aaaaaaaaaaaa"
        other = _Session(_View(62), aid="agent-dddddddddddd")
        other.parent_agent_id = "agent-eeeeeeeeeeee"
        live_other = _Session(_View(63), aid="agent-eeeeeeeeeeee")
        self.reg.register_session(parent)
        self.reg.register_session(child)
        self.reg.register_session(other)
        self.reg.register_session(live_other)
        text = f"spawned {child.agent_id} then mailed {other.agent_id}"
        found = self.reg.harvest_mentioned_orphans(parent, text)
        self.assertEqual([s.agent_id for s in found], ["agent-cccccccccccc"])

    def test_identity_from_saved_entry(self):
        ident = self.reg.identity_from_saved_entry({
            "session_id": "s1",
            "agent_id": "agent-keep",
            "child_agent_ids": ["agent-c"],
            "agent_id_aliases": ["agent-old"],
        })
        self.assertEqual(ident["agent_id"], "agent-keep")
        self.assertEqual(ident["child_agent_ids"], ["agent-c"])
        self.assertEqual(ident["agent_id_aliases"], ["agent-old"])


if __name__ == "__main__":
    unittest.main()
