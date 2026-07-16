#!/usr/bin/env python3
"""Unit tests for plugin-native GoalTracker + slash parse + verdict parse."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goal_tracker import (
    GoalTracker,
    parse_goal_slash,
    BLOCKED_STREAK_TO_PAUSE,
)
from goal_prompts import parse_verifier_verdict, continuation_directive, goal_rules


class TestParseSlash(unittest.TestCase):
    def test_status_empty(self):
        self.assertEqual(parse_goal_slash(""), ("status", None))
        self.assertEqual(parse_goal_slash("status"), ("status", None))

    def test_reserved(self):
        self.assertEqual(parse_goal_slash("pause"), ("pause", None))
        self.assertEqual(parse_goal_slash("resume"), ("resume", None))
        self.assertEqual(parse_goal_slash("clear"), ("clear", None))

    def test_set_objective(self):
        act, payload = parse_goal_slash("ship the widget")
        self.assertEqual(act, "set")
        self.assertEqual(payload[0], "ship the widget")
        self.assertIsNone(payload[1])

    def test_budget_trailing(self):
        act, payload = parse_goal_slash("ship it --budget 500000")
        self.assertEqual(act, "set")
        self.assertEqual(payload[0], "ship it")
        self.assertEqual(payload[1], 500000)

    def test_budget_malformed_stays_in_objective(self):
        act, payload = parse_goal_slash("do --budget foo")
        self.assertEqual(act, "set")
        self.assertEqual(payload[0], "do --budget foo")
        self.assertIsNone(payload[1])

    def test_budget_not_trailing(self):
        act, payload = parse_goal_slash("--budget 10 middle text")
        self.assertEqual(act, "set")
        self.assertIsNone(payload[1])


class TestTrackerLifecycle(unittest.TestCase):
    def test_create_active(self):
        g = GoalTracker()
        g.create("fix it", token_budget=1000, tokens_baseline=50)
        self.assertTrue(g.is_active())
        self.assertTrue(g.is_open())
        self.assertEqual(g.objective, "fix it")
        self.assertEqual(g.token_budget, 1000)
        self.assertEqual(g.tokens_baseline, 50)

    def test_no_invent_without_goal(self):
        g = GoalTracker()
        r = g.apply_update(message="hello")
        self.assertFalse(r["ok"])
        r2 = g.apply_update(completed=True, message="done")
        self.assertFalse(r2["ok"])
        self.assertTrue(r2.get("rejected"))

    def test_progress_and_deferred_complete(self):
        g = GoalTracker()
        g.create("obj")
        r = g.apply_update(message="halfway")
        self.assertTrue(r["ok"])
        self.assertEqual(g.message, "halfway")
        r2 = g.apply_update(completed=True, message="all good", mid_turn=True)
        self.assertTrue(r2.get("deferred"))
        self.assertTrue(g.has_pending_complete())
        self.assertTrue(g.is_active())  # not complete yet

    def test_blocked_streak(self):
        g = GoalTracker()
        g.create("obj")
        for i in range(BLOCKED_STREAK_TO_PAUSE - 1):
            r = g.apply_update(blocked_reason=f"fail {i}")
            self.assertTrue(r["ok"])
            self.assertEqual(g.status, "active")
        r = g.apply_update(blocked_reason="final")
        self.assertEqual(g.status, "blocked")
        self.assertTrue(g.is_paused())
        self.assertFalse(g.should_continue())

    def test_pause_resume_clear(self):
        g = GoalTracker()
        g.create("obj")
        g.pause("user", "esc")
        self.assertEqual(g.status, "user_paused")
        self.assertFalse(g.should_continue())
        self.assertTrue(g.resume())
        self.assertTrue(g.is_active())
        g.clear()
        self.assertFalse(g.is_open())
        self.assertEqual(g.status, "cleared")

    def test_budget(self):
        g = GoalTracker()
        g.create("obj", token_budget=100, tokens_baseline=0)
        g.set_tokens_used(50)
        self.assertFalse(g.budget_exceeded())
        g.set_tokens_used(100)
        self.assertTrue(g.enforce_budget())
        self.assertEqual(g.status, "budget_limited")

    def test_verify_achieved(self):
        g = GoalTracker()
        g.create("obj")
        g.apply_update(completed=True, message="done", mid_turn=True)
        claim = g.begin_verify()
        self.assertEqual(claim, "done")
        self.assertEqual(g.phase, "verifying")
        g.apply_verdict(True, detail="ok")
        self.assertEqual(g.status, "complete")
        self.assertFalse(g.is_open())

    def test_verify_not_achieved_gaps(self):
        g = GoalTracker()
        g.create("obj")
        g.apply_update(completed=True, message="done", mid_turn=True)
        g.begin_verify()
        g.apply_verdict(False, gaps=["missing test"])
        self.assertTrue(g.is_active())
        self.assertEqual(g.gaps, ["missing test"])
        self.assertTrue(g.should_continue())

    def test_verify_cap(self):
        g = GoalTracker()
        g.create("obj")
        g.verify_max = 2
        for _ in range(2):
            g.apply_update(completed=True, message="x", mid_turn=True)
            g.begin_verify()
            g.apply_verdict(False, gaps=["nope"])
        g.apply_update(completed=True, message="x", mid_turn=True)
        claim = g.begin_verify()
        self.assertIsNone(claim)
        self.assertTrue(g.is_paused())

    def test_restore_pauses_active(self):
        g = GoalTracker()
        g.create("obj")
        data = g.to_json()
        g2 = GoalTracker.from_json(data)
        self.assertEqual(g2.status, "user_paused")
        self.assertTrue(g2.is_open())


class TestPrompts(unittest.TestCase):
    def test_rules_contain_objective(self):
        t = goal_rules("ship widget")
        self.assertIn("ship widget", t)
        self.assertIn("update_goal", t)

    def test_continuation(self):
        g = GoalTracker()
        g.create("obj")
        g.gaps = ["a"]
        c = continuation_directive(g)
        self.assertIn("NOT complete", c)
        self.assertIn("a", c)

    def test_verdict_achieved(self):
        ok, gaps = parse_verifier_verdict("looks good\nVERDICT: achieved\n")
        self.assertTrue(ok)
        self.assertEqual(gaps, [])

    def test_verdict_not_achieved(self):
        ok, gaps = parse_verifier_verdict(
            "nope\nVERDICT: not_achieved\nGAPS:\n- missing file\n"
        )
        self.assertFalse(ok)
        self.assertIn("missing file", gaps)

    def test_verdict_default(self):
        ok, gaps = parse_verifier_verdict("I think it's fine")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
