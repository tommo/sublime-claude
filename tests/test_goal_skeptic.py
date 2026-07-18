#!/usr/bin/env python3
"""Goal skeptic: Task-mode POC + legacy sheet helpers (no Sublime runtime)."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goal_skeptic import (
    GOAL_ROLE_SKEPTIC,
    MODE_SESSION,
    MODE_TASK,
    is_goal_skeptic,
    make_skeptic_context,
    parent_view_id_for_skeptic,
    resolve_skeptic_mode,
    resolve_verdict_session,
    skeptic_display_name,
)
from goal_tracker import GoalTracker
from goal_plan import sample_concrete_plan
from goal_prompts import executor_verify_prompt, verifier_prompt


class TestSkepticMode(unittest.TestCase):
    def test_default_task(self):
        class S:
            def get(self, k, d=None):
                return d
        self.assertEqual(resolve_skeptic_mode(S()), MODE_TASK)

    def test_session_opt_in(self):
        class S:
            def get(self, k, d=None):
                if k == "goal_skeptic_mode":
                    return "session"
                return d
        self.assertEqual(resolve_skeptic_mode(S()), MODE_SESSION)

    def test_unknown_falls_back_task(self):
        class S:
            def get(self, k, d=None):
                if k == "goal_skeptic_mode":
                    return "sheets-please"
                return d
        self.assertEqual(resolve_skeptic_mode(S()), MODE_TASK)


class TestSkepticContext(unittest.TestCase):
    def test_context_marks_role_and_parent(self):
        ctx = make_skeptic_context(42, verify_run=3)
        self.assertEqual(ctx["goal_role"], GOAL_ROLE_SKEPTIC)
        self.assertEqual(ctx["parent_view_id"], 42)
        self.assertEqual(ctx["goal_parent_view_id"], 42)
        self.assertEqual(ctx["goal_verify_run"], 3)
        self.assertTrue(str(ctx["subsession_id"]).startswith("goal-skeptic-"))

    def test_display_name(self):
        self.assertEqual(skeptic_display_name(2), "goal-skeptic-2")


class TestIsSkeptic(unittest.TestCase):
    def test_attr(self):
        s = SimpleNamespace(goal_role="skeptic", initial_context=None)
        self.assertTrue(is_goal_skeptic(s))

    def test_from_context(self):
        s = SimpleNamespace(
            goal_role=None,
            initial_context={"goal_role": "skeptic"},
        )
        self.assertTrue(is_goal_skeptic(s))

    def test_implementer_false(self):
        s = SimpleNamespace(goal_role=None, initial_context={})
        self.assertFalse(is_goal_skeptic(s))


class TestVerdictRouting(unittest.TestCase):
    def test_implementer_is_owner(self):
        parent = SimpleNamespace(goal_role=None, initial_context={}, id="p")
        sessions = {1: parent}
        self.assertIs(resolve_verdict_session(parent, sessions), parent)

    def test_skeptic_routes_to_parent(self):
        parent = SimpleNamespace(goal_role=None, initial_context={}, id="p")
        child = SimpleNamespace(
            goal_role="skeptic",
            parent_view_id=99,
            initial_context={"goal_role": "skeptic", "parent_view_id": 99},
        )
        sessions = {99: parent, 7: child}
        self.assertIs(resolve_verdict_session(child, sessions), parent)

    def test_skeptic_orphan_returns_none(self):
        child = SimpleNamespace(
            goal_role="skeptic",
            parent_view_id=404,
            initial_context={},
        )
        self.assertIsNone(resolve_verdict_session(child, {}))

    def test_parent_view_id_helpers(self):
        child = SimpleNamespace(
            goal_role="skeptic",
            parent_view_id=None,
            initial_context={"goal_parent_view_id": "55"},
        )
        self.assertEqual(parent_view_id_for_skeptic(child), 55)


class TestPlannerSchemaPrompts(unittest.TestCase):
    def test_kickoff_embeds_schema(self):
        from goal_prompts import planner_kickoff
        from goal_tracker import GoalTracker
        g = GoalTracker()
        g.create("demo goal usage, no code")
        p = planner_kickoff(g, plan_path="/tmp/x/plan.md")
        self.assertIn("PLAN.MD SCHEMA", p)
        self.assertIn("## Acceptance criteria", p)
        self.assertIn("## Verification plan", p)
        self.assertIn("Minimal valid example", p)
        self.assertIn("/tmp/x/plan.md", p)

    def test_revise_embeds_schema_and_issues(self):
        from goal_prompts import planner_revise
        from goal_tracker import GoalTracker
        g = GoalTracker()
        g.create("demo")
        p = planner_revise(
            g,
            ["need ≥2 acceptance criteria under `## Acceptance criteria` (found 0)"],
            plan_path="/tmp/x/plan.md",
        )
        self.assertIn("REJECTED", p)
        self.assertIn("PLAN.MD SCHEMA", p)
        self.assertIn("need ≥2 acceptance criteria", p)
        self.assertIn("schema/quality", p.lower() or p)


class TestExecutorVerifyPrompt(unittest.TestCase):
    def test_executor_not_self_judge(self):
        p = executor_verify_prompt(
            "ship widget",
            "done with tests",
            plan_body=sample_concrete_plan("ship widget", "g1"),
        )
        self.assertIn("GOAL FLOW EXECUTOR", p)
        self.assertIn("Task", p)
        self.assertIn("spawn_subagent", p)
        self.assertIn("goal achievement skeptic", p)
        self.assertIn("goal_verdict", p)
        self.assertIn("<skeptic-prompt>", p)
        low = p.lower()
        self.assertIn("update_goal(completed=true)", low)
        self.assertIn("do not call update_goal(completed=true)", low)
        # Must forbid sheet path; no POC framing
        self.assertIn("spawn_session", low)
        self.assertIn("never spawn_session", low)
        self.assertNotIn("poc", low)
        self.assertNotIn("POC", p)

    def test_skeptic_body_is_reviewer(self):
        p = verifier_prompt("ship widget", "claim")
        self.assertIn("SKEPTIC / REVIEWER", p)
        self.assertIn("goal_verdict", p)
        self.assertNotIn("POC", p)


class TestBeginVerifyClearsPending(unittest.TestCase):
    def test_pending_cleared_and_phase_verifying(self):
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        self.assertTrue(g.has_pending_complete())
        claim = g.begin_verify()
        self.assertIsNotNone(claim)
        self.assertEqual(g.phase, "verifying")
        # Must not re-fire complete while verifying
        self.assertFalse(g.has_pending_complete())
        self.assertIsNone(g.pending_completed_message)


class TestFireVerifierTaskMode(unittest.TestCase):
    """POC: fire_verifier queries main as executor — never create_session."""

    def test_task_mode_queries_main_not_spawn_sheet(self):
        calls = {"query": 0, "spawn": 0}

        class FakeOutput:
            def __init__(self):
                self.texts = []

            def text(self, t):
                self.texts.append(t)

        class FakeParent:
            def __init__(self):
                self.goal_tracker = GoalTracker()
                self.goal_tracker.create("ship widget")
                plan = sample_concrete_plan(
                    "ship widget", self.goal_tracker.goal_id, checklist_done=True)
                self.goal_tracker.accept_plan(plan)
                self.output = FakeOutput()
                self.working = False
                self._goal_verify_turn = False
                self._goal_verify_mode = None
                self._goal_skeptic_view_id = None
                self._goal_verify_awaiting = False
                self.last_prompt = ""

            def _goal_spawn_skeptic_session(self):
                calls["spawn"] += 1
                raise AssertionError("task mode must not spawn sheet")

            def query(self, prompt, display_prompt=None):
                calls["query"] += 1
                self.last_prompt = prompt
                self.last_display = display_prompt

            def sync_goal_ui(self):
                pass

            def _goal_fire_verifier(self, claim: str) -> bool:
                from goal_prompts import executor_verify_prompt
                from goal_skeptic import MODE_TASK
                gt = self.goal_tracker
                self._goal_verify_mode = MODE_TASK
                self._goal_verify_turn = False
                self._goal_skeptic_view_id = None
                self.working = True
                prompt = executor_verify_prompt(
                    gt.objective,
                    claim,
                    gaps_prior=gt.gaps or None,
                    plan_body=getattr(gt, "plan_body", "") or "",
                )
                self._goal_verify_awaiting = True
                self.output.text(
                    f"*Goal · verifying* ({gt.verify_runs}/{gt.verify_max})")
                self.query(prompt, display_prompt="↻ goal verify")
                return True

        p = FakeParent()
        p.goal_tracker.apply_update(
            completed=True, message="fully done with tests", mid_turn=True)
        claim = p.goal_tracker.begin_verify()
        ok = p._goal_fire_verifier(claim)
        self.assertTrue(ok)
        self.assertEqual(calls["query"], 1)
        self.assertEqual(calls["spawn"], 0)
        self.assertTrue(p._goal_verify_awaiting)
        self.assertEqual(p._goal_verify_mode, MODE_TASK)
        self.assertIn("GOAL FLOW EXECUTOR", p.last_prompt)
        self.assertIn("Task", p.last_prompt)
        self.assertEqual(p.last_display, "↻ goal verify")
        self.assertIsNone(p._goal_skeptic_view_id)
        self.assertTrue(any("verifying" in t for t in p.output.texts))


class TestSkepticCannotOwnComplete(unittest.TestCase):
    def test_verdict_on_parent_while_verifying(self):
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="done with tests", mid_turn=True)
        claim = g.begin_verify()
        self.assertIsNotNone(claim)
        r = g.record_tool_verdict(
            achieved=True,
            evidence=["tests/test_goal_feature.py pytest exit 0"],
            gaps=[],
            message="criteria met",
        )
        self.assertTrue(r.get("ok"))
        self.assertTrue(r.get("achieved"))
        g.take_tool_verdict()
        g.apply_verdict(True, gaps=[], detail="ok")
        self.assertEqual(g.status, "complete")

    def test_child_empty_tracker_cannot_verify(self):
        child_gt = GoalTracker()
        r = child_gt.record_tool_verdict(
            achieved=True,
            evidence=["fake"],
            gaps=[],
            message="should fail",
        )
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("rejected"))


class TestVerdictDedup(unittest.TestCase):
    def test_later_not_achieved_does_not_overwrite_achieved(self):
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        g.begin_verify()
        r1 = g.record_tool_verdict(
            achieved=True,
            evidence=["pytest tests/test_goal_feature.py exit 0"],
            gaps=[],
            message="ok",
        )
        self.assertTrue(r1.get("achieved"))
        r2 = g.record_tool_verdict(
            achieved=False,
            evidence=[],
            gaps=["not achieved (tool)"],
            message="soft",
        )
        self.assertTrue(r2.get("deduped"))
        self.assertTrue(r2.get("achieved"))
        v = g.take_tool_verdict()
        self.assertTrue(v["achieved"])
        self.assertGreaterEqual(len(v["evidence"]), 1)


class TestNoDualBannerRace(unittest.TestCase):
    """apply_verdict(True) mid-turn must not be undone by finish fail-closed."""

    def test_already_complete_not_demoted_by_false_apply(self):
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        g.begin_verify()
        g.apply_verdict(True, gaps=[], detail="via eval")
        self.assertEqual(g.status, "complete")
        # Stale finish path must not demote
        g.apply_verdict(False, gaps=["Verifier did not confirm achievement"])
        self.assertEqual(g.status, "complete")
        self.assertEqual(g.phase, "idle")

    def test_finish_respects_precompleted_status(self):
        """Simulate host finish after eval complete — no not-achieved apply."""
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        g.begin_verify()
        g.apply_verdict(True, gaps=[], detail="eval first")
        self.assertEqual(g.status, "complete")
        # Host apply path when already complete
        if g.status == "complete":
            pass  # skip fail-closed
        else:
            g.apply_verdict(False, gaps=["no tool"])
        self.assertEqual(g.status, "complete")


class TestLifecycleCloses(unittest.TestCase):
    """AC3: after tool verdict applied, leave verifying (complete or executing)."""

    def _ready_verifying(self):
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(
            completed=True, message="fully done with tests", mid_turn=True)
        claim = g.begin_verify()
        self.assertIsNotNone(claim)
        self.assertEqual(g.phase, "verifying")
        self.assertFalse(g.has_pending_complete())
        return g

    def test_achieved_leaves_verifying_complete(self):
        g = self._ready_verifying()
        r = g.record_tool_verdict(
            achieved=True,
            evidence=["pytest tests/test_goal_feature.py -q exit 0"],
            gaps=[],
            message="ok",
        )
        self.assertTrue(r.get("ok") and r.get("achieved"))
        v = g.take_tool_verdict()
        self.assertTrue(v["achieved"])
        g.apply_verdict(True, gaps=[], detail=v.get("message") or "ok")
        self.assertEqual(g.status, "complete")
        self.assertNotEqual(g.phase, "verifying")

    def test_not_achieved_returns_executing_with_gaps(self):
        g = self._ready_verifying()
        r = g.record_tool_verdict(
            achieved=False,
            evidence=[],
            gaps=["missing tests/test_goal_feature.py"],
            message="gaps remain",
        )
        self.assertTrue(r.get("ok"))
        self.assertFalse(r.get("achieved"))
        v = g.take_tool_verdict()
        g.apply_verdict(False, gaps=v.get("gaps") or [], detail="gaps")
        self.assertEqual(g.status, "active")
        self.assertEqual(g.phase, "executing")
        self.assertTrue(any("missing" in x for x in g.gaps))
        self.assertFalse(g.has_pending_complete())

    def test_no_tool_verdict_fail_closed_like_host(self):
        """Host finish with no goal_verdict → not achieved (shipped apply path)."""
        g = self._ready_verifying()
        # Simulate host _goal_apply_verifier_result with no tool_v
        tool_v = g.take_tool_verdict()
        self.assertIsNone(tool_v)
        gaps = ["Skeptic subagent did not call goal_verdict (required)"]
        g.apply_verdict(False, gaps=gaps, detail="no_tool")
        self.assertEqual(g.phase, "executing")
        self.assertEqual(g.status, "active")
        self.assertTrue(g.gaps)


class TestGoalStripUX(unittest.TestCase):
    """Sticky work strip: phase labels + compact ◆ line (not composer ◎)."""

    def test_verifying_label_and_body_from_claim(self):
        from goal_tracker import ui_phase_label, ui_phase_body, GoalTracker
        from goal_plan import sample_concrete_plan
        self.assertEqual(
            ui_phase_label(status="active", phase="verifying"), "verifying")
        body = ui_phase_body(
            status="active",
            phase="verifying",
            message="fully done with tests",
            objective="ship widget",
        )
        self.assertIn("fully done", body)
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        g.begin_verify()
        self.assertEqual(g.ui_phase_label(), "verifying")
        self.assertIn("fully done", g.ui_phase_body())

    def test_executing_label_not_bare_active(self):
        from goal_tracker import ui_phase_label
        self.assertEqual(
            ui_phase_label(status="active", phase="executing"), "executing")

    def test_executing_with_gaps_surfaces_gap_body(self):
        from goal_tracker import ui_phase_label, ui_phase_body, GoalTracker
        from goal_plan import sample_concrete_plan
        body = ui_phase_body(
            status="active",
            phase="executing",
            message="claimed complete",
            objective="ship widget",
            gaps=["missing tests/test_goal_feature.py", "no capture"],
        )
        self.assertEqual(
            ui_phase_label(status="active", phase="executing"), "executing")
        self.assertIn("missing tests", body)
        self.assertIn("+1 more", body)
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        g.begin_verify()
        g.record_tool_verdict(
            achieved=False, evidence=[],
            gaps=["missing tests/test_goal_feature.py", "no capture"],
            message="no",
        )
        v = g.take_tool_verdict()
        g.apply_verdict(False, gaps=v["gaps"], detail="no")
        self.assertEqual(g.phase, "executing")
        self.assertEqual(g.ui_phase_label(), "executing")
        self.assertIn("missing tests", g.ui_phase_body())

    def test_format_strip_compact_no_composer_glyph(self):
        from goal_tracker import format_goal_strip_line
        line = format_goal_strip_line(
            status="active",
            phase="executing",
            objective="don't read file, don't code, just demo the goal flow",
            message="Creating demo_ok.txt and running plan verification gates.",
            compact=True,
        )
        self.assertTrue(line.startswith("  ◆ goal · executing"))
        self.assertNotIn("◎", line)
        # Short obj + progress, not a wall of text
        self.assertIn("Creating demo_ok", line)
        self.assertLess(len(line), 120)
        # Noise "Plan accepted — executing" suppressed
        noise = format_goal_strip_line(
            status="active",
            phase="executing",
            objective="ship widget",
            message="Plan accepted — executing",
        )
        self.assertIn("executing", noise)
        self.assertNotIn("Plan accepted", noise)

    def test_planning_label(self):
        from goal_tracker import ui_phase_label
        self.assertEqual(
            ui_phase_label(status="active", phase="planning"), "planning")


class TestTaskVerifyAbortHandoff(unittest.TestCase):
    """Error/interrupt must not clobber continuation after finish (skeptic gap)."""

    def test_prepare_abort_records_not_achieved(self):
        from goal_skeptic import prepare_task_verify_abort
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        g.apply_update(completed=True, message="fully done with tests", mid_turn=True)
        g.begin_verify()
        self.assertTrue(prepare_task_verify_abort(g, "Host: verify turn error: boom"))
        v = g.pending_tool_verdict
        self.assertIsNotNone(v)
        self.assertFalse(v["achieved"])
        self.assertTrue(any("boom" in x for x in v["gaps"]))

    def test_prepare_abort_skips_non_verifying(self):
        from goal_skeptic import prepare_task_verify_abort
        g = GoalTracker()
        g.create("ship widget")
        plan = sample_concrete_plan("ship widget", g.goal_id, checklist_done=True)
        g.accept_plan(plan)
        self.assertFalse(prepare_task_verify_abort(g, "x"))

    def test_preserve_working_helper(self):
        from goal_skeptic import preserve_working_after_verify_finish
        self.assertTrue(preserve_working_after_verify_finish(True))
        self.assertFalse(preserve_working_after_verify_finish(False))

    def test_fake_session_error_gate_order_does_not_clobber(self):
        """Shipped order: abort → finish → if handoff return before working=False."""
        from goal_skeptic import (
            prepare_task_verify_abort,
            preserve_working_after_verify_finish,
        )

        class FakeOutput:
            def __init__(self):
                self.texts = []
                self.current = SimpleNamespace(working=True)

            def text(self, t):
                self.texts.append(t)

        class FakeSession:
            """Mirrors Session._goal_abort_task_verify + non-success gate."""

            def __init__(self):
                self.goal_tracker = GoalTracker()
                self.goal_tracker.create("ship widget")
                plan = sample_concrete_plan(
                    "ship widget", self.goal_tracker.goal_id, checklist_done=True)
                self.goal_tracker.accept_plan(plan)
                self.goal_tracker.apply_update(
                    completed=True, message="fully done with tests", mid_turn=True)
                self.goal_tracker.begin_verify()
                self._goal_verify_awaiting = True
                self._goal_verify_mode = "task"
                self.working = True
                self.output = FakeOutput()
                self.queries = []
                self.idle_opens = 0

            def sync_goal_ui(self):
                pass

            def _goal_finish_verify_cycle(self):
                # Real tracker path then continuation handoff
                gt = self.goal_tracker
                v = gt.take_tool_verdict()
                if v:
                    gt.apply_verdict(
                        bool(v.get("achieved")),
                        gaps=list(v.get("gaps") or []),
                        detail=v.get("message") or "",
                    )
                else:
                    gt.apply_verdict(
                        False,
                        gaps=["Skeptic subagent did not call goal_verdict (required)"],
                        detail="no_tool",
                    )
                self._goal_verify_awaiting = False
                if gt.phase == "verifying":
                    gt.phase = "executing"
                # Simulate should_continue → query continuation
                self.working = True
                self.queries.append("continuation")
                return True  # next turn started

            def _goal_abort_task_verify(self, gap, message=""):
                # Same control flow as Session._goal_abort_task_verify
                gt = self.goal_tracker
                if not prepare_task_verify_abort(gt, gap, message=message):
                    return False
                self._goal_verify_awaiting = False
                started = bool(self._goal_finish_verify_cycle())
                self.sync_goal_ui()
                return preserve_working_after_verify_finish(started)

            def on_done_error(self):
                """Shipped gate order from Session completion handler."""
                goal_handoff = False
                if (self._goal_verify_awaiting
                        and (self._goal_verify_mode or "task") == "task"):
                    goal_handoff = self._goal_abort_task_verify(
                        "Host: verify turn error: boom", message="verify error")
                # 4b. Goal finish already started next turn
                if goal_handoff:
                    return "handoff"
                # 5. non-success gate
                self.working = False
                if self.output.current:
                    self.output.current.working = False
                self.idle_opens += 1
                return "idle"

        s = FakeSession()
        result = s.on_done_error()
        self.assertEqual(result, "handoff")
        self.assertTrue(s.working, "continuation must keep working=True")
        self.assertEqual(s.idle_opens, 0)
        self.assertEqual(s.queries, ["continuation"])
        self.assertEqual(s.goal_tracker.phase, "executing")
        self.assertTrue(s.goal_tracker.gaps)

    def test_fake_session_no_handoff_forces_idle(self):
        from goal_skeptic import (
            prepare_task_verify_abort,
            preserve_working_after_verify_finish,
        )

        class FakeSession:
            def __init__(self):
                self.goal_tracker = GoalTracker()
                self.goal_tracker.create("ship widget")
                plan = sample_concrete_plan(
                    "ship widget", self.goal_tracker.goal_id, checklist_done=True)
                self.goal_tracker.accept_plan(plan)
                self.goal_tracker.apply_update(
                    completed=True, message="fully done with tests", mid_turn=True)
                self.goal_tracker.begin_verify()
                self._goal_verify_awaiting = True
                self._goal_verify_mode = "task"
                self.working = True
                self.idle_opens = 0

            def sync_goal_ui(self):
                pass

            def _goal_finish_verify_cycle(self):
                gt = self.goal_tracker
                v = gt.take_tool_verdict()
                gt.apply_verdict(
                    False,
                    gaps=list((v or {}).get("gaps") or ["no"]),
                    detail="x",
                )
                self._goal_verify_awaiting = False
                # Cap / no continue
                return False

            def _goal_abort_task_verify(self, gap, message=""):
                if not prepare_task_verify_abort(
                        self.goal_tracker, gap, message=message):
                    return False
                self._goal_verify_awaiting = False
                started = bool(self._goal_finish_verify_cycle())
                return preserve_working_after_verify_finish(started)

            def on_done_error(self):
                goal_handoff = self._goal_abort_task_verify("Host: err")
                if goal_handoff:
                    return "handoff"
                self.working = False
                self.idle_opens += 1
                return "idle"

        s = FakeSession()
        self.assertEqual(s.on_done_error(), "idle")
        self.assertFalse(s.working)
        self.assertEqual(s.idle_opens, 1)


class TestFirePathSourceContract(unittest.TestCase):
    """Static: default fire path is Task executor, sheet is opt-in only."""

    def test_session_source_default_uses_executor_not_create_session(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "session.py")
        src = open(path, encoding="utf-8").read()
        # Locate _goal_fire_verifier body until next def at same indent
        start = src.index("def _goal_fire_verifier(self")
        # next top-level method after fire
        rest = src[start:]
        end = rest.index("\n    def _goal_fire_verifier_session_sheet")
        body = rest[:end]
        self.assertIn("executor_verify_prompt", body)
        self.assertIn("goal verify", body)
        self.assertIn("MODE_SESSION", body)
        self.assertNotIn("POC", body)
        # Default branch must query main; must not import/call create_session
        self.assertIn("self.query(prompt", body)
        self.assertNotIn("from .core import create_session", body)
        self.assertNotIn("create_session(", body)
        # sheet helper still exists for legacy
        self.assertIn("def _goal_fire_verifier_session_sheet", src)
        self.assertIn(
            "create_session",
            src[src.index("def _goal_spawn_skeptic_session"):],
        )


if __name__ == "__main__":
    unittest.main()
