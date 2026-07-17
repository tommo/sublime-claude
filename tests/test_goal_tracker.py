#!/usr/bin/env python3
"""Unit tests for plugin-native GoalTracker + plan host + slash/verdict parse."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goal_tracker import (
    GoalTracker,
    parse_goal_slash,
    BLOCKED_STREAK_TO_PAUSE,
)
from goal_prompts import (
    parse_verifier_verdict,
    continuation_directive,
    goal_rules,
    verifier_prompt,
    implementer_kickoff,
)
from goal_plan import (
    materialize_plan,
    plan_has_required_sections,
    parse_plan_sections,
    extract_acceptance_criteria,
    extract_verification_steps,
    write_plan_file,
    default_plan_path,
)


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


class TestHostPlan(unittest.TestCase):
    def test_materialize_has_required_sections(self):
        md = materialize_plan("ship the widget", goal_id="abc123")
        self.assertTrue(plan_has_required_sections(md))
        secs = parse_plan_sections(md)
        self.assertIn("Acceptance criteria", secs)
        self.assertIn("Verification plan", secs)
        self.assertIn("ship the widget", md)
        ac = extract_acceptance_criteria(md)
        self.assertGreaterEqual(len(ac), 1)
        self.assertTrue(any("ship the widget" in c for c in ac))
        vs = extract_verification_steps(md)
        self.assertGreaterEqual(len(vs), 1)

    def test_write_and_default_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = default_plan_path(td, "gid99")
            self.assertIn(".claude/goals/gid99/plan.md", path.replace("\\", "/"))
            md = materialize_plan("obj X", goal_id="gid99")
            written = write_plan_file(path, md)
            self.assertTrue(os.path.isfile(written))
            with open(written) as f:
                body = f.read()
            self.assertTrue(plan_has_required_sections(body))


class TestTrackerLifecycle(unittest.TestCase):
    def test_create_enters_planning_not_execute(self):
        g = GoalTracker()
        g.create("fix it", token_budget=1000, tokens_baseline=50)
        self.assertTrue(g.is_active())
        self.assertTrue(g.is_open())
        self.assertEqual(g.phase, "planning")
        self.assertFalse(g.has_plan())
        self.assertFalse(g.should_continue())  # blocked until plan ready
        self.assertEqual(g.objective, "fix it")
        self.assertEqual(g.token_budget, 1000)

    def test_accept_plan_then_continue(self):
        g = GoalTracker()
        g.create("obj")
        md = materialize_plan("obj", goal_id=g.goal_id)
        ack = g.accept_plan(md, plan_path="/tmp/plan.md")
        self.assertTrue(ack["ok"])
        self.assertEqual(g.phase, "executing")
        self.assertTrue(g.has_plan())
        self.assertTrue(g.should_continue())

    def test_reject_bad_plan(self):
        g = GoalTracker()
        g.create("obj")
        ack = g.accept_plan("# not a real plan\n")
        self.assertFalse(ack["ok"])
        self.assertEqual(g.phase, "planning")

    def test_complete_rejected_without_plan(self):
        g = GoalTracker()
        g.create("obj")
        r = g.apply_update(completed=True, message="done")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("rejected"))

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
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
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
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
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
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
        g.pause("user", "esc")
        self.assertEqual(g.status, "user_paused")
        self.assertFalse(g.should_continue())
        self.assertTrue(g.resume())
        self.assertTrue(g.is_active())
        self.assertEqual(g.phase, "executing")
        g.clear()
        self.assertFalse(g.is_open())
        self.assertEqual(g.status, "cleared")
        self.assertFalse(g.has_plan())  # stale plan dropped

    def test_resume_without_plan_stays_planning(self):
        g = GoalTracker()
        g.create("obj")
        g.pause("user", "later")
        g.resume()
        self.assertEqual(g.phase, "planning")
        self.assertFalse(g.should_continue())

    def test_budget(self):
        g = GoalTracker()
        g.create("obj", token_budget=100, tokens_baseline=0)
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
        g.set_tokens_used(50)
        self.assertFalse(g.budget_exceeded())
        g.set_tokens_used(100)
        self.assertTrue(g.enforce_budget())
        self.assertEqual(g.status, "budget_limited")

    def test_verify_achieved(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
        g.apply_update(completed=True, message="done", mid_turn=True)
        claim = g.begin_verify()
        self.assertEqual(claim, "done")
        self.assertEqual(g.phase, "verifying")
        g.apply_verdict(True, detail="ok")
        self.assertEqual(g.status, "complete")
        self.assertFalse(g.is_open())
        self.assertFalse(g.has_plan())  # plan cleared on complete

    def test_verify_not_achieved_gaps(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
        g.apply_update(completed=True, message="done", mid_turn=True)
        g.begin_verify()
        g.apply_verdict(False, gaps=["missing test"])
        self.assertTrue(g.is_active())
        self.assertEqual(g.gaps, ["missing test"])
        self.assertTrue(g.should_continue())

    def test_verify_cap(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
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
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
        data = g.to_json()
        g2 = GoalTracker.from_json(data)
        self.assertEqual(g2.status, "user_paused")
        self.assertTrue(g2.is_open())

    def test_full_product_lifecycle_phases(self):
        """create → planning → accept → execute → complete claim → verify → complete."""
        g = GoalTracker()
        g.create("lifecycle obj")
        phases = [g.phase]
        self.assertEqual(g.phase, "planning")
        md = materialize_plan("lifecycle obj", goal_id=g.goal_id)
        g.accept_plan(md, plan_path="/proj/.claude/goals/x/plan.md")
        phases.append(g.phase)
        self.assertEqual(g.phase, "executing")
        self.assertTrue(g.should_continue())
        g.note_continue()
        g.apply_update(completed=True, message="shipped", mid_turn=True)
        claim = g.begin_verify()
        phases.append(g.phase)
        self.assertEqual(g.phase, "verifying")
        g.apply_verdict(True, detail=claim)
        phases.append(g.status)
        self.assertEqual(g.status, "complete")
        self.assertEqual(phases, ["planning", "executing", "verifying", "complete"])


class TestPrompts(unittest.TestCase):
    def test_rules_contain_objective(self):
        t = goal_rules("ship widget")
        self.assertIn("ship widget", t)
        self.assertIn("update_goal", t)

    def test_continuation_includes_plan_criteria(self):
        g = GoalTracker()
        g.create("ship widget")
        g.accept_plan(materialize_plan("ship widget", goal_id=g.goal_id))
        c = continuation_directive(g)
        self.assertIn("NOT complete", c)
        self.assertIn("Acceptance criteria", c)
        self.assertIn("ship widget", c)
        self.assertIn("HOST PLAN CONTRACT", c)

    def test_verifier_includes_plan_not_claim_only(self):
        g = GoalTracker()
        g.create("ship widget")
        md = materialize_plan("ship widget", goal_id=g.goal_id)
        g.accept_plan(md)
        p = verifier_prompt(
            g.objective, "I did it", plan_body=g.plan_body)
        self.assertIn("PLAN ACCEPTANCE CRITERIA", p)
        self.assertIn("PLAN VERIFICATION STEPS", p)
        self.assertIn("prose claim alone is insufficient", p.lower())
        # criteria text derived from plan, not only free-form claim
        self.assertIn("ship widget", p)

    def test_implementer_kickoff_has_plan(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
        k = implementer_kickoff(g)
        self.assertIn("HOST PLAN CONTRACT", k)
        self.assertIn("finished planning", k)

    def test_continuation(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(materialize_plan("obj", goal_id=g.goal_id))
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
