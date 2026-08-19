"""Resume after interrupt must not restore asking UI."""
import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bind_drop():
    """Session._drop_resume_asking_if_needed without importing session.py."""
    ns = {}
    # Keep in sync with Session._drop_resume_asking_if_needed
    src = r'''
def _drop_resume_asking_if_needed(self, send_fn):
    if not getattr(self, "_resume_drop_asking", False):
        return False
    try:
        send_fn()
    except Exception:
        pass
    if not getattr(self, "_resume_asking_interrupt_sent", False):
        self._resume_asking_interrupt_sent = True
        if self.client:
            try:
                self.client.send("interrupt", {})
            except Exception:
                pass
    try:
        if self.output:
            self.output.clear_asking_state()
    except Exception:
        pass
    return True
'''
    exec(src, ns)
    return ns["_drop_resume_asking_if_needed"]


class _Out:
    def __init__(self):
        self.pending_question = object()
        self.pending_permission = object()
        self.pending_plan = object()
        self._permission_queue = ["x"]
        self._question_input_mode = True
        self.view = None
        self.q = False
        self.p = False
        self.pl = False

    def _clear_question(self):
        self.q = True

    def _remove_permission_block(self):
        self.p = True

    def _clear_plan_approval(self):
        self.pl = True


def _load_clear_asking():
    """Bind OutputView.clear_asking_state onto a dummy."""
    path = os.path.join(_ROOT, "output_view.py")
    with open(path) as f:
        src = f.read()
    start = src.find("    def clear_asking_state")
    end = src.find("    def interrupted", start)
    body = "def clear_asking_state(self):\n" + "\n".join(
        ln[4:] if ln.startswith("    ") else ln
        for ln in src[start:end].splitlines()[1:]
    )
    ns = {}
    exec(body, ns)
    return ns["clear_asking_state"]


class TestResumeDropAsking(unittest.TestCase):
    def test_drop_cancels_and_interrupts_once(self):
        drop = _bind_drop()
        sent = []

        class _S:
            _resume_drop_asking = True
            _resume_asking_interrupt_sent = False
            client = types.SimpleNamespace(
                send=lambda m, p=None, s=sent: s.append((m, p)))
            output = types.SimpleNamespace(
                clear_asking_state=lambda s=sent: s.append(("cleared", None)))

        s = _S()
        self.assertTrue(drop(s, lambda: s.client.send(
            "question_response", {"id": 3, "answers": None})))
        self.assertTrue(drop(s, lambda: s.client.send(
            "question_response", {"id": 4, "answers": None})))
        methods = [m for m, _ in sent]
        self.assertEqual(methods.count("interrupt"), 1)
        self.assertEqual(methods.count("question_response"), 2)
        self.assertEqual(methods.count("cleared"), 2)

    def test_no_drop_when_flag_off(self):
        drop = _bind_drop()
        s = types.SimpleNamespace(
            _resume_drop_asking=False,
            client=None,
            output=None,
        )
        called = []
        self.assertFalse(drop(s, lambda: called.append(1)))
        self.assertEqual(called, [])

    def test_clear_asking_state_wipes_modals(self):
        fn = _load_clear_asking()
        ov = _Out()
        fn(ov)
        self.assertTrue(ov.q)
        self.assertTrue(ov.p)
        self.assertTrue(ov.pl)
        self.assertIsNone(ov.pending_question)
        self.assertIsNone(ov.pending_permission)
        self.assertIsNone(ov.pending_plan)
        self.assertFalse(ov._question_input_mode)
        self.assertEqual(ov._permission_queue, [])


if __name__ == "__main__":
    unittest.main()
