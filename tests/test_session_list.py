"""Session list scratch: render + line index (no Sublime)."""
import importlib.util
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = "cc_session_list_testpkg"


def _load():
    if "sublime" not in sys.modules:
        sys.modules["sublime"] = types.SimpleNamespace(_claude_sessions={})
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
    sess_name = _PKG + ".session"
    if sess_name not in sys.modules:
        sess = types.ModuleType(sess_name)
        sess.load_saved_sessions = lambda: []
        sess.load_bookmarks = lambda p=None: set()
        sys.modules[sess_name] = sess
    be_name = _PKG + ".backends"
    if be_name not in sys.modules:
        be = types.ModuleType(be_name)
        _abbr = {
            "claude": "CL", "codex": "CX", "copilot": "CP", "pi": "Pi",
            "dsr": "DSR", "grok": "GR", "kimi": "KM", "grok_cc": "GCC",
        }
        be.abbrev_for = lambda name, m=_abbr: m.get(
            (name or "claude"), (name or "??")[:2].upper())
        sys.modules[be_name] = be
    name = _PKG + ".session_list"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "session_list.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestRenderSessionList(unittest.TestCase):
    def test_render_assigns_lines(self):
        sl = _load()
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "Skin editor", "backend": "grok", "status": "working",
            "query_count": 3, "same_window": True,
        }]
        here = [{
            "kind": "saved", "session_id": "s2", "view_id": None,
            "name": "old plan", "backend": "kimi", "status": "closed",
            "query_count": 1, "project": "/x/pil", "last_activity": 1700000000,
        }]
        text, index = sl.render_list(live, here, [], starred={"s2"})
        self.assertIn("RUNNING (1)", text)
        self.assertIn("HISTORY (1)", text)
        self.assertIn("Skin editor", text)
        self.assertIn("★", text)
        self.assertIn("r refresh", text)
        self.assertNotIn("c compact", text)
        compact, cidx = sl.render_list(live, here, [], starred={"s2"}, cols=24)
        self.assertIn("GR", compact)
        self.assertIn("KM", compact)
        self.assertNotIn("working", compact.split("Skin editor")[-1][:20])
        self.assertEqual(len(cidx), 2)
        self.assertEqual(len(index), 2)
        self.assertEqual(sl.row_at_line(index, index[0]["line"])["session_id"], "s1")
        self.assertEqual(sl.row_at_line(index, index[1]["line"])["session_id"], "s2")
        self.assertEqual(sl.format_when(100, now=100), "now")
        self.assertEqual(sl.format_when(100, now=130), "30s")
        self.assertEqual(sl.format_when(100, now=100 + 5 * 60), "5m")
        self.assertEqual(sl.format_when(100, now=100 + 3 * 3600), "3h")
        self.assertEqual(sl.format_when(100, now=100 + 2 * 86400), "2d")
        self.assertEqual(sl.format_when(100, now=100 + 21 * 86400), "3w")

    def test_access_ts_prefers_focus(self):
        sl = _load()
        self.assertEqual(sl.access_ts({"last_activity": 10, "last_access": 20}), 20)
        self.assertEqual(sl.access_ts({"last_activity": 100, "last_access": 5}), 5)
        self.assertEqual(sl.access_ts({"last_activity": 10}), 10)
        self.assertEqual(sl.access_ts({}), 0)

    def test_session_title_recovers_truncated_name(self):
        sl = _load()
        conv = types.SimpleNamespace(
            prompt="check pui prop editor change, then wire the inspector")
        s = types.SimpleNamespace(
            name="check pui prop editor change,...",
            output=types.SimpleNamespace(conversations=[conv], current=None),
        )
        self.assertEqual(
            sl.session_title(s),
            "check pui prop editor change, then wire the inspector")
        s.name = "read pml_editor package"
        self.assertEqual(sl.session_title(s), "read pml_editor package")

    def test_one_line_title_escapes_newline(self):
        sl = _load()
        self.assertEqual(
            sl.one_line_title("merge\n- **P1 — `merge` 1"),
            "merge↵- **P1 — `merge` 1")
        self.assertNotIn("\n", sl.one_line_title("a\r\nb\nc"))
        here = [{
            "kind": "saved", "session_id": "n1", "view_id": None,
            "name": "head\n- **P1 — merge", "backend": "grok",
            "status": "closed", "query_count": 0, "project": "/p",
            "last_activity": 1, "last_access": 1,
        }]
        text, _ = sl.render_list([], here, [], cols=80)
        self.assertEqual(text.count("\n- **P1"), 0)
        self.assertIn("↵", text)

    def test_backend_abbrev(self):
        sl = _load()
        self.assertEqual(sl.backend_abbrev("grok"), "GR")
        self.assertEqual(sl.backend_abbrev("kimi"), "KM")
        self.assertEqual(sl.backend_abbrev("claude"), "CL")
        self.assertEqual(sl.backend_abbrev("codex"), "CX")

    def test_header_fits_view_cols(self):
        sl = _load()
        wide = sl.format_header(72)
        self.assertEqual(len(wide), 72)
        self.assertTrue(wide.startswith("SESSIONS"))
        self.assertTrue(wide.endswith("r refresh"))
        self.assertIn("enter", wide)
        narrow = sl.format_header(28)
        self.assertLessEqual(len(narrow), 28)
        self.assertTrue(narrow.startswith("SESSIONS"))

    def test_auto_compact_by_width(self):
        sl = _load()
        self.assertTrue(sl.use_compact(24))
        self.assertFalse(sl.use_compact(80))
        self.assertFalse(sl.use_compact(0))
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "A" * 80, "backend": "grok", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        narrow, _ = sl.render_list(live, [], [], cols=24)
        nrow = [ln for ln in narrow.splitlines() if ln.startswith("○")][0]
        self.assertEqual(len(nrow), 24)
        self.assertIn("GR", nrow)
        self.assertNotIn("ready", nrow)
        self.assertTrue(nrow.rstrip().endswith("…"))
        wide, _ = sl.render_list(live, [], [], cols=80)
        wrow = [ln for ln in wide.splitlines() if ln.startswith("○")][0]
        self.assertGreater(len(wrow), len(nrow))
        self.assertIn("grok", wrow)
        self.assertIn("ready", wrow)
        self.assertEqual(sl.fit_title("short", 10), "short     ")
        self.assertEqual(sl.fit_title("abcdefghij", 6), "abcde…")
        asleep = [{
            "kind": "live", "session_id": "z", "view_id": 2,
            "name": "nap", "backend": "kimi", "status": "sleeping",
            "query_count": 0, "same_window": True,
        }]
        sleep_txt, _ = sl.render_list(asleep, [], [], cols=80)
        srow = [ln for ln in sleep_txt.splitlines() if ln.startswith("⏸")][0]
        self.assertNotIn("sleeping", srow)

    def test_right_cols_align(self):
        sl = _load()
        live = [
            {"kind": "live", "session_id": "a", "view_id": 1,
             "name": "GUEST", "backend": "kimi", "status": "ready",
             "query_count": 0, "same_window": True,
             "last_access": 100, "last_activity": 100},
            {"kind": "live", "session_id": "b", "view_id": 2,
             "name": "inspector layout needs some polish here",
             "backend": "grok", "status": "ready",
             "query_count": 3, "same_window": True,
             "last_access": 50, "last_activity": 50},
        ]
        text, _ = sl.render_list(live, [], [], cols=80)
        rows = [ln for ln in text.splitlines() if ln[:1] in ("○", "●", "⏸")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].rstrip().endswith("ready"))
        self.assertTrue(rows[1].rstrip().endswith("ready"))
        self.assertEqual(len(rows[0].rstrip()), len(rows[1].rstrip()))
        for row in rows:
            self.assertNotRegex(row, r"\b\d+[smhdw]\b")
        asleep = {
            "kind": "live", "session_id": "z", "view_id": 3,
            "name": "nap", "backend": "kimi", "status": "sleeping",
            "query_count": 0, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        mixed, _ = sl.render_list(live + [asleep], [], [], cols=80)
        srow = [ln for ln in mixed.splitlines() if ln.startswith("⏸")][0]
        self.assertRegex(srow, r"\b(\d+[smhdw]|now)\b")
        self.assertNotIn("ready", srow)
        self.assertEqual(len(srow.rstrip()), len(rows[0].rstrip()))

    def test_history_matches_running_cols(self):
        sl = _load()
        live = [{
            "kind": "live", "session_id": "a", "view_id": 1,
            "name": "edui-shader-theme", "backend": "grok",
            "status": "sleeping", "query_count": 0, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }]
        here = [{
            "kind": "saved", "session_id": "b", "view_id": None,
            "name": "add middle button panning", "backend": "grok",
            "status": "closed", "query_count": 1, "project": "/x/pil",
            "last_access": 1, "last_activity": 1,
        }]
        text, _ = sl.render_list(live, here, [], cols=80)
        run = [ln for ln in text.splitlines() if ln.startswith("⏸")][0]
        hist = [ln for ln in text.splitlines() if ln.startswith("·")][0]
        self.assertNotIn("pil", hist)
        # elapsed field ends at the same column
        self.assertEqual(len(run.rstrip()), len(hist.rstrip()))

    def test_live_sorts_by_access_time(self):
        sl = _load()
        import sublime
        older = types.SimpleNamespace(
            session_id="old", name="older", backend="grok",
            working=True, is_sleeping=False, query_count=9,
            last_activity=100, last_access=100,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        newer = types.SimpleNamespace(
            session_id="new", name="newer", backend="kimi",
            working=False, is_sleeping=True, query_count=1,
            last_activity=50, last_access=200,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: older, 2: newer}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["old", "new"])

    def test_history_sorts_by_access_time(self):
        sl = _load()
        prev = sl.load_saved_sessions
        sl.load_saved_sessions = lambda: [
            {"session_id": "a", "name": "old-access", "backend": "grok",
             "project": "/p", "last_activity": 300, "last_access": 300},
            {"session_id": "b", "name": "new-access", "backend": "kimi",
             "project": "/p", "last_activity": 100, "last_access": 400},
        ]
        try:
            here, other = sl.collect_history(set(), "/p")
        finally:
            sl.load_saved_sessions = prev
        self.assertEqual([r["session_id"] for r in here], ["b", "a"])
        self.assertEqual(other, [])


if __name__ == "__main__":
    unittest.main()
