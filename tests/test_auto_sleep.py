"""Auto-sleep must not treat a just-interrupted long turn as hours idle."""
import importlib.util
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "cc_auto_sleep_testpkg"


def _load_core():
    if "sublime" not in sys.modules:
        sys.modules["sublime"] = types.SimpleNamespace(
            View=type("View", (), {}),
            Window=type("Window", (), {}),
        )
    if "sublime_plugin" not in sys.modules:
        sys.modules["sublime_plugin"] = types.SimpleNamespace(
            EventListener=object,
            WindowCommand=object,
            TextCommand=object,
        )
    pkg = sys.modules.get(_PKG)
    if pkg is None:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [_ROOT]
        sys.modules[_PKG] = pkg
    if _PKG + ".session" not in sys.modules:
        sys.modules[_PKG + ".session"] = types.SimpleNamespace(Session=object)
    if _PKG + ".backends" not in sys.modules:
        sys.modules[_PKG + ".backends"] = types.SimpleNamespace()
    if _PKG + ".session_split" not in sys.modules:
        sys.modules[_PKG + ".session_split"] = types.SimpleNamespace(
            remember_active_session=lambda *a, **k: None,
            place_in_last_session_split=lambda *a, **k: None,
        )
    name = _PKG + ".core"
    cached = sys.modules.get(name)
    if cached is not None and hasattr(cached, "auto_sleep_due"):
        return cached
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "core.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Sess:
    def __init__(self, **kw):
        self.sleep_disabled = False
        self.quick_mode = False
        self._interrupting = False
        self.goal_tracker = None
        self.initialized = True
        self.working = False
        self.is_sleeping = False
        self.last_idle_at = 0.0
        self.last_activity = 0.0
        self.name = "kimi"
        self.__dict__.update(kw)


class TestAutoSleepDue(unittest.TestCase):
    def setUp(self):
        self.core = _load_core()
        self.now = 1_000_000.0
        self.timeout = 60

    def test_long_turn_then_interrupt_stamp_is_not_due(self):
        # Turn started 90m ago; Esc just stamped last_activity/last_idle_at.
        s = _Sess(last_idle_at=self.now, last_activity=self.now)
        due, _ = self.core.auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_stale_clock_after_long_turn_would_have_slept(self):
        started = self.now - (90 * 60)
        s = _Sess(last_idle_at=started, last_activity=started)
        due, idle_at = self.core.auto_sleep_due(s, self.now, self.timeout)
        self.assertTrue(due)
        self.assertEqual(idle_at, started)

    def test_inbound_activity_during_idle_looking_kimi_turn(self):
        started = self.now - (90 * 60)
        s = _Sess(last_idle_at=started, last_activity=self.now, working=False)
        due, _ = self.core.auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_skip_while_interrupting(self):
        started = self.now - (90 * 60)
        s = _Sess(
            last_idle_at=started, last_activity=started, _interrupting=True)
        due, _ = self.core.auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_skip_while_working(self):
        started = self.now - (90 * 60)
        s = _Sess(last_idle_at=started, last_activity=started, working=True)
        due, _ = self.core.auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)


if __name__ == "__main__":
    unittest.main()
