"""Grok self-wake must archive the @done sheet, not replace it in-place."""
import os
import textwrap
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OV = os.path.join(_ROOT, "output_view.py")
_SESS = os.path.join(_ROOT, "session.py")


def _extract_method(path, name):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = src.find(f"    def {name}")
    if start < 0:
        raise AssertionError(f"{name} missing in {path}")
    nxt = src.find("\n    def ", start + 10)
    return textwrap.dedent(src[start:nxt])


class _Conv:
    def __init__(self, prompt="", has_meta=False, working=True, events=None):
        self.prompt = prompt
        self.has_meta = has_meta
        self.working = working
        self.events = list(events or [])


class _Out:
    def __init__(self):
        self.current = None
        self.conversations = []
        self.prompted = []
        self.title_n = 0

    def prompt(self, text, context_names=None, context_refs=None):
        self.prompted.append(text)
        if self.current:
            self.current.working = False
            self.conversations.append(self.current)
        self.current = _Conv(prompt=text, working=True)

    def _update_title(self):
        self.title_n += 1


def _bind():
    ns = {}
    exec(_extract_method(_OV, "begin_continued"), ns)
    _Out.begin_continued = ns["begin_continued"]


_bind()


class TestBeginContinued(unittest.TestCase):
    def test_has_meta_archives_then_prompts(self):
        o = _Out()
        old = _Conv(prompt="run editor", has_meta=True, working=False)
        old.events.append("singleton acquired\n")
        o.current = old
        o.begin_continued()
        self.assertEqual(o.prompted, ["(continued)"])
        self.assertIs(o.conversations[0], old)
        self.assertEqual(o.current.prompt, "(continued)")
        self.assertTrue(o.current.working)
        self.assertIn("singleton acquired\n", o.conversations[0].events)

    def test_live_sheet_is_not_replaced(self):
        o = _Out()
        live = _Conv(prompt="still going", has_meta=False, working=False)
        o.current = live
        o.begin_continued()
        self.assertEqual(o.prompted, [])
        self.assertIs(o.current, live)
        self.assertTrue(live.working)
        self.assertEqual(o.conversations, [])

    def test_missing_current_prompts(self):
        o = _Out()
        o.begin_continued()
        self.assertEqual(o.prompted, ["(continued)"])
        self.assertEqual(o.current.prompt, "(continued)")

    def test_resume_stream_does_not_swap_current(self):
        with open(_SESS, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    def _resume_interrupt_stream")
        nxt = src.find("\n    def ", start + 10)
        body = src[start:nxt]
        live = [
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        joined = "\n".join(live)
        self.assertIn("begin_continued", joined)
        self.assertNotIn("Conversation(", joined)
        self.assertNotIn("self.output.current =", joined)


if __name__ == "__main__":
    unittest.main()
