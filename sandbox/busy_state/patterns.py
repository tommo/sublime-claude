"""Pattern suite: leftover after @done must not own busy."""
from __future__ import annotations

import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from turn_state import TurnState  # noqa: E402

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "gitapp_after_done.json")


def _play(events, backend="kimi"):
    t = TurnState()
    log = []
    for ev in events:
        kind = ev.get("t")
        if kind == "begin_query":
            t.begin_query()
        elif kind == "end_turn":
            t.end_live()
        elif kind == "enter_compact":
            t.enter_compacting()
        elif kind == "compact_done":
            t.finish_compact()
        elif kind == "interrupt":
            t.begin_interrupt()
        elif kind == "settle_interrupt":
            t.settle_interrupt()
        elif kind == "terminal_create" and ev.get("synth_bash"):
            log.append(t.inbound_action("synth_bash"))
        elif kind == "tool_use_bg":
            log.append(t.inbound_action("tool_use_bg"))
        elif kind == "text":
            log.append(t.inbound_action("text"))
        elif kind == "thinking":
            log.append(t.inbound_action("thinking"))
        elif kind == "bg_notify":
            log.append(t.notify_action(ev.get("backend") or backend))
        elif kind in ("terminal_output_poll", "terminal_kill", "wait_for_exit"):
            pass
        log.append(t.kind)
    return t, log


class TestGitappAfterDone(unittest.TestCase):
    def test_fixture_stays_idle(self):
        with open(_FIX, encoding="utf-8") as f:
            data = json.load(f)
        t, _ = _play(data["events"], backend="kimi")
        self.assertEqual(t.kind, "idle")
        self.assertFalse(t.working)
        self.assertFalse(t.awaiting_rpc)

    def test_synth_bash_after_end_is_paint_bg(self):
        t = TurnState()
        t.begin_query()
        self.assertTrue(t.end_live())
        self.assertEqual(t.inbound_action("synth_bash"), "paint_bg")
        self.assertEqual(t.kind, "idle")

    def test_kimi_notify_after_end_is_surface_not_query(self):
        t = TurnState()
        t.begin_query()
        t.end_live()
        self.assertEqual(t.notify_action("kimi"), "surface")
        self.assertEqual(t.notify_action("grok"), "surface")
        self.assertEqual(t.notify_action("claude"), "query")
        self.assertFalse(t.working)


class TestLiveCloser(unittest.TestCase):
    def test_query_then_end_turn(self):
        t = TurnState()
        g = t.begin_query()
        self.assertEqual(t.kind, "live")
        self.assertTrue(t.awaiting_rpc)
        self.assertTrue(t.working)
        self.assertTrue(t.end_live(g))
        self.assertEqual(t.kind, "idle")
        self.assertFalse(t.working)

    def test_stale_gen_does_not_idle_new_turn(self):
        t = TurnState()
        g1 = t.begin_query()
        g2 = t.begin_query()
        self.assertNotEqual(g1, g2)
        self.assertFalse(t.end_live(g1))
        self.assertEqual(t.kind, "live")
        self.assertTrue(t.end_live(g2))
        self.assertEqual(t.kind, "idle")

    def test_queue_while_live(self):
        t = TurnState()
        t.begin_query()
        self.assertTrue(t.should_queue_prompt())
        t.end_live()
        self.assertFalse(t.should_queue_prompt())


class TestCompact(unittest.TestCase):
    def test_end_turn_does_not_clear_compacting(self):
        t = TurnState()
        t.begin_query()
        t.enter_compacting()
        self.assertEqual(t.kind, "compacting")
        self.assertTrue(t.working)
        self.assertFalse(t.end_live())
        self.assertEqual(t.kind, "compacting")
        self.assertTrue(t.finish_compact())
        self.assertEqual(t.kind, "idle")

    def test_queue_while_compacting(self):
        t = TurnState()
        t.begin_query()
        t.enter_compacting()
        self.assertTrue(t.should_queue_prompt())


class TestInterrupt(unittest.TestCase):
    def test_interrupt_stays_busy_until_settle(self):
        t = TurnState()
        t.begin_query()
        self.assertTrue(t.begin_interrupt())
        self.assertTrue(t.working)
        self.assertEqual(t.kind, "interrupting")
        self.assertEqual(t.inbound_action("text"), "paint")
        t.settle_interrupt()
        self.assertEqual(t.kind, "idle")
        self.assertFalse(t.working)
        self.assertEqual(t.inbound_action("synth_bash"), "paint_bg")

    def test_resume_stream_after_interrupt_is_busy(self):
        t = TurnState()
        t.begin_query()
        t.begin_interrupt()
        t.settle_interrupt()
        t.resume_stream()
        self.assertEqual(t.kind, "live")
        self.assertTrue(t.working)
        self.assertFalse(t.awaiting_rpc)
        self.assertTrue(t.end_live())
        self.assertFalse(t.working)

    def test_idle_interrupt_is_noop(self):
        t = TurnState()
        self.assertFalse(t.begin_interrupt())
        self.assertEqual(t.kind, "idle")


class TestParentNotify(unittest.TestCase):
    def test_parent_idle_may_query_child_complete(self):
        parent = TurnState()
        self.assertFalse(parent.should_queue_prompt())
        parent.begin_query()
        self.assertTrue(parent.working)
        parent.end_live()
        self.assertFalse(parent.working)

    def test_parent_live_queues(self):
        parent = TurnState()
        parent.begin_query()
        self.assertTrue(parent.should_queue_prompt())
        self.assertEqual(parent.notify_action("kimi"), "hold")


class TestInboundNeverAdopts(unittest.TestCase):
    def test_every_idle_inbound_keeps_idle(self):
        t = TurnState()
        for ev in ("text", "thinking", "synth_bash", "tool_use_bg", "tool_result"):
            t.inbound_action(ev)
            self.assertEqual(t.kind, "idle", ev)
            self.assertFalse(t.working, ev)


if __name__ == "__main__":
    unittest.main()
