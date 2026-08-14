"""Last-active session split: resume lands in that group."""
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
    def __init__(self, vid, output=True):
        self._id = vid
        self._settings = _Settings()
        if output:
            self._settings.set("claude_output", True)

    def id(self):
        return self._id

    def is_valid(self):
        return True

    def settings(self):
        return self._settings


class _Window:
    def __init__(self, groups=2):
        self._settings = _Settings()
        self._groups = groups
        self._index = {}
        self._views = []

    def settings(self):
        return self._settings

    def views(self):
        return list(self._views)

    def num_groups(self):
        return self._groups

    def get_view_index(self, view):
        return self._index.get(view.id(), (0, 0))

    def views_in_group(self, group):
        return [v for v in self._views if self._index.get(v.id(), (0, 0))[0] == group]

    def set_view_index(self, view, group, index):
        self._index[view.id()] = (group, index)

    def add(self, view, group, index=0):
        self._views.append(view)
        self._index[view.id()] = (group, index)


class TestSessionSplit(unittest.TestCase):
    def _load(self):
        import session_split
        return session_split

    def test_last_group_from_active_view(self):
        core = self._load()
        win = _Window()
        sess = _View(11)
        win.add(sess, 1)
        win.settings().set("claude_active_view", 11)
        self.assertEqual(core.last_session_group(win), 1)

    def test_place_moves_into_last_split(self):
        core = self._load()
        win = _Window()
        sess = _View(11)
        win.add(sess, 1)
        win.settings().set("claude_active_view", 11)
        win.settings().set("claude_active_group", 1)
        newbie = _View(99)
        win.add(newbie, 0)
        self.assertTrue(core.place_in_last_session_split(win, newbie))
        self.assertEqual(win.get_view_index(newbie)[0], 1)


if __name__ == "__main__":
    unittest.main()
