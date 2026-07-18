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
    sample_concrete_plan,
    plan_has_required_sections,
    plan_quality_issues,
    is_generic_template_plan,
    complete_claim_preflight,
    open_checklist_items,
    partial_claim_language,
    parse_plan_sections,
    extract_acceptance_criteria,
    extract_verification_steps,
    write_plan_file,
    default_plan_path,
)

# Tests use concrete plans (templates are rejected by accept_plan)
def _plan(obj="obj", gid="gid"):
    return sample_concrete_plan(obj, gid)

def _done_plan(obj="obj", gid="gid"):
    return sample_concrete_plan(obj, gid, checklist_done=True)


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
        md = sample_concrete_plan("ship the widget", goal_id="abc123")
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

    def test_title_case_headings_still_extract(self):
        """Agent ## Acceptance Criteria must not become 'need ≥2 criteria'."""
        md = """# Plan: demo
## Acceptance Criteria
1. Write `evidence/demo_ok.txt` with one line goal-demo-ok
2. Leave product sources unchanged
## Verification Plan
1. `gating`: `grep -qx goal-demo-ok evidence/demo_ok.txt` exits 0
2. `gating`: `git status --porcelain` clean outside evidence
## Task Checklist
- [ ] write evidence
- [ ] run verification
"""
        self.assertTrue(plan_has_required_sections(md))
        secs = parse_plan_sections(md)
        self.assertIn("Acceptance criteria", secs)
        self.assertEqual(len(extract_acceptance_criteria(md)), 2)
        self.assertEqual(len(extract_verification_steps(md)), 2)
        self.assertEqual(plan_quality_issues(md, "demo"), [])

    def test_docs_demo_plan_with_paths_passes(self):
        md = """# Plan: demo goal usage, no code
## Acceptance criteria
1. **Demo marker**: `/tmp/goal-demo/evidence/demo_ok.txt` exists with line `goal-demo-ok`
2. **No product churn**: git status clean outside the goal evidence dir
## Verification plan
1. `gating`: `grep -qx goal-demo-ok /tmp/goal-demo/evidence/demo_ok.txt` exits 0
2. `gating`: `git status --porcelain` has no product-tree changes
## Task checklist
- [ ] mkdir evidence and write marker
- [ ] run verification commands
"""
        self.assertEqual(plan_quality_issues(md, "demo goal usage, no code"), [])

    def test_soft_existence_only_verification_rejected(self):
        md = """# Plan: ship auth
## Acceptance criteria
1. Marker at `.claude/goals/x/evidence/ok.txt`
2. Plan file present
## Verification plan
1. `gating`: `test -f .claude/goals/x/evidence/ok.txt`
2. `gating`: file exists demo_ok
## Task checklist
- [ ] write marker
- [ ] claim done
"""
        issues = plan_quality_issues(md, "ship authentication module")
        self.assertTrue(any("soft" in i.lower() or "substantive" in i.lower()
                            for i in issues), issues)

    def test_baseline_freeze_blocks_thinned_complete(self):
        from goal_plan import complete_claim_preflight, sample_concrete_plan
        g = GoalTracker()
        g.create("ship widget feature")
        plan = sample_concrete_plan("ship widget feature", g.goal_id, checklist_done=True)
        ack = g.accept_plan(plan, plan_path="/tmp/not-used-plan.md")
        self.assertTrue(ack.get("ok"))
        self.assertTrue((g.plan_baseline or "").strip())
        # Hostile thinned disk rewrite
        thinned = """# Plan: ship widget feature
## Acceptance criteria
1. something vaguely done
## Verification plan
1. looks fine
## Task checklist
- [x] all done
"""
        # Host body stays frozen; preflight vs baseline if body somehow thinned
        bad = complete_claim_preflight(
            "ship widget fully done with tests",
            thinned,
            "ship widget feature",
            plan_baseline=g.plan_baseline,
        )
        self.assertTrue(bad, "thinned plan must not pass preflight")
        self.assertTrue(
            any("thinned" in i.lower() or "frozen" in i.lower() or "missing" in i.lower()
                for i in bad),
            bad,
        )

    def test_schema_prompt_names_required_headings(self):
        from goal_plan import plan_schema_for_prompt
        s = plan_schema_for_prompt()
        self.assertIn("## Acceptance criteria", s)
        self.assertIn("## Verification plan", s)
        self.assertIn("≥2", s)
        self.assertIn("Minimal valid example", s)

    def test_write_and_default_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = default_plan_path(td, "gid99")
            self.assertIn(".claude/goals/gid99/plan.md", path.replace("\\", "/"))
            md = sample_concrete_plan("obj X", goal_id="gid99")
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
        md = sample_concrete_plan("obj", goal_id=g.goal_id)
        ack = g.accept_plan(md, plan_path="/tmp/plan.md")
        self.assertTrue(ack["ok"])
        self.assertEqual(g.phase, "executing")
        self.assertTrue(g.has_plan())
        self.assertTrue(g.should_continue())

    def test_accept_after_user_pause_mid_planning(self):
        """Esc pauses mid-plan; valid plan.md must still accept (auto-resume)."""
        g = GoalTracker()
        g.create("ship widget feature")
        self.assertEqual(g.phase, "planning")
        g.pause("user", "Interrupted by user")
        self.assertEqual(g.status, "user_paused")
        self.assertEqual(g.phase, "planning")  # not wiped to idle forever
        md = sample_concrete_plan("ship widget feature", goal_id=g.goal_id)
        ack = g.accept_plan(md, plan_path="/tmp/plan-paused.md")
        self.assertTrue(ack.get("ok"), ack)
        self.assertEqual(g.status, "active")
        self.assertEqual(g.phase, "executing")
        self.assertTrue(g.has_plan())

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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        g.set_tokens_used(50)
        self.assertFalse(g.budget_exceeded())
        g.set_tokens_used(100)
        self.assertTrue(g.enforce_budget())
        self.assertEqual(g.status, "budget_limited")

    def test_verify_achieved(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        g.apply_update(completed=True, message="done", mid_turn=True)
        g.begin_verify()
        g.apply_verdict(False, gaps=["missing test"])
        self.assertTrue(g.is_active())
        self.assertEqual(g.gaps, ["missing test"])
        self.assertTrue(g.should_continue())

    def test_verify_cap(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
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
        md = sample_concrete_plan(
            "lifecycle obj", goal_id=g.goal_id, checklist_done=True)
        g.accept_plan(md, plan_path="/proj/.claude/goals/x/plan.md")
        phases.append(g.phase)
        self.assertEqual(g.phase, "executing")
        self.assertTrue(g.should_continue())
        g.note_continue()
        g.apply_update(
            completed=True,
            message="lifecycle obj fully shipped with tests",
            mid_turn=True,
        )
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
        g.accept_plan(sample_concrete_plan("ship widget", goal_id=g.goal_id, checklist_done=True))
        c = continuation_directive(g)
        self.assertIn("NOT complete", c)
        self.assertIn("Acceptance criteria", c)
        self.assertIn("ship widget", c)
        self.assertIn("HOST PLAN CONTRACT", c)

    def test_verifier_includes_plan_not_claim_only(self):
        g = GoalTracker()
        g.create("ship widget")
        md = sample_concrete_plan("ship widget", goal_id=g.goal_id)
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
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        k = implementer_kickoff(g)
        self.assertIn("HOST PLAN CONTRACT", k)
        self.assertIn("ACCEPTED the plan", k)

    def test_template_plan_rejected(self):
        g = GoalTracker()
        g.create("ship widget")
        stub = materialize_plan("ship widget", goal_id=g.goal_id)
        self.assertTrue(is_generic_template_plan(stub))
        self.assertTrue(plan_quality_issues(stub, "ship widget"))
        ack = g.accept_plan(stub)
        self.assertFalse(ack["ok"])

    def test_continuation(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        g.gaps = ["a"]
        c = continuation_directive(g)
        self.assertIn("NOT complete", c)
        self.assertIn("a", c)

    def test_verdict_achieved(self):
        ok, gaps = parse_verifier_verdict(
            "checked\nEVIDENCE:\n- path: foo.py proves objective\n"
            "VERDICT: achieved\n"
        )
        self.assertTrue(ok)
        self.assertEqual(gaps, [])

    def test_verdict_achieved_requires_evidence(self):
        ok, gaps = parse_verifier_verdict("looks good\nVERDICT: achieved\n")
        self.assertFalse(ok)
        self.assertTrue(any("EVIDENCE" in g for g in gaps))

    def test_verdict_not_achieved(self):
        ok, gaps = parse_verifier_verdict(
            "nope\nVERDICT: not_achieved\nGAPS:\n- missing file\n"
        )
        self.assertFalse(ok)
        self.assertIn("missing file", gaps)

    def test_verdict_default(self):
        ok, gaps = parse_verifier_verdict("I think it's fine")
        self.assertFalse(ok)

    def test_verdict_partial_is_fail(self):
        ok, gaps = parse_verifier_verdict(
            "EVIDENCE:\n- something\nVERDICT: partially achieved\n"
        )
        self.assertFalse(ok)

    def test_verdict_achieved_with_gaps_fails(self):
        ok, gaps = parse_verifier_verdict(
            "EVIDENCE:\n- x\nVERDICT: achieved\nGAPS:\n- still broken\n"
        )
        self.assertFalse(ok)
        self.assertTrue(any("broken" in g for g in gaps))

    def test_verdict_markdown_bold(self):
        ok, gaps = parse_verifier_verdict(
            "EVIDENCE:\n- file f.py\n**VERDICT: achieved**\n"
        )
        self.assertTrue(ok)


class TestToolVerdict(unittest.TestCase):
    def test_record_and_take(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        g.pending_completed_message = "done"
        claim = g.begin_verify()
        self.assertIsNotNone(claim)
        r = g.record_tool_verdict(
            achieved=True,
            evidence=["tests/test_x.py passed"],
            gaps=[],
            message="ok",
        )
        self.assertTrue(r.get("ok"))
        self.assertTrue(r.get("achieved"))
        v = g.take_tool_verdict()
        self.assertTrue(v["achieved"])
        self.assertEqual(v["evidence"], ["tests/test_x.py passed"])
        self.assertIsNone(g.take_tool_verdict())

    def test_achieved_without_evidence_rejected(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        g.pending_completed_message = "done"
        g.begin_verify()
        r = g.record_tool_verdict(achieved=True, evidence=[], gaps=[])
        self.assertTrue(r.get("ok"))
        self.assertFalse(r.get("achieved"))
        v = g.take_tool_verdict()
        self.assertFalse(v["achieved"])

    def test_not_in_verify_phase(self):
        g = GoalTracker()
        g.create("obj")
        g.accept_plan(sample_concrete_plan("obj", goal_id=g.goal_id, checklist_done=True))
        r = g.record_tool_verdict(achieved=False, gaps=["x"])
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("rejected"))



class TestCompletePreflight(unittest.TestCase):
    def test_open_checklist_blocks_complete(self):
        g = GoalTracker()
        g.create("ship widget")
        # open boxes
        g.accept_plan(sample_concrete_plan("ship widget", goal_id=g.goal_id, checklist_done=False))
        r = g.apply_update(completed=True, message="implemented and tested fully")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("rejected"))
        self.assertTrue(any("checklist" in i.lower() for i in r.get("issues") or []))

    def test_partial_language_blocks_complete(self):
        g = GoalTracker()
        g.create("ship widget")
        g.accept_plan(sample_concrete_plan("ship widget", goal_id=g.goal_id, checklist_done=True))
        r = g.apply_update(
            completed=True,
            message="one sprint shipped the easy slice; TAA deferred",
        )
        self.assertFalse(r["ok"])
        self.assertTrue(any("partial" in i.lower() or "defer" in i.lower() for i in (r.get("issues") or [r.get("error","")])))

    def test_done_claim_with_closed_checklist_ok(self):
        g = GoalTracker()
        g.create("ship widget")
        g.accept_plan(sample_concrete_plan("ship widget", goal_id=g.goal_id, checklist_done=True))
        r = g.apply_update(
            completed=True,
            message="ship widget: src/goal_feature.py + tests green",
            mid_turn=True,
        )
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(r.get("deferred"))

    def test_helpers_partial_and_open(self):
        self.assertIsNotNone(partial_claim_language("first slice only for now"))
        self.assertIsNone(partial_claim_language("objective fully delivered with tests"))
        plan = sample_concrete_plan("x", checklist_done=False)
        self.assertGreaterEqual(len(open_checklist_items(plan)), 1)
        plan2 = sample_concrete_plan("x", checklist_done=True)
        self.assertEqual(open_checklist_items(plan2), [])

    def test_disk_checklist_flip_allows_complete(self):
        """Realistic path: accept open checklist, implementer flips [x] on disk.

        Host merges checkbox marks only — complete is not permanently rejected.
        """
        g = GoalTracker()
        g.create("ship widget")
        with tempfile.TemporaryDirectory() as td:
            path = default_plan_path(td, g.goal_id)
            open_md = sample_concrete_plan(
                "ship widget", goal_id=g.goal_id, checklist_done=False)
            write_plan_file(path, open_md)
            ack = g.accept_plan(open_md, plan_path=path)
            self.assertTrue(ack["ok"], ack)
            frozen_ac = extract_acceptance_criteria(g.plan_body)
            # Frozen body still has open boxes → complete fails
            r_fail = g.apply_update(
                completed=True,
                message="ship widget fully done with tests",
            )
            self.assertFalse(r_fail["ok"])
            self.assertTrue(any("checklist" in (i or "").lower()
                                for i in (r_fail.get("issues") or [])))
            # Implementer flips checklist on disk (same contract sections)
            closed_md = sample_concrete_plan(
                "ship widget", goal_id=g.goal_id, checklist_done=True)
            write_plan_file(path, closed_md)
            # Without calling sync manually — apply_update must merge marks
            r_ok = g.apply_update(
                completed=True,
                message="ship widget fully done with tests",
                mid_turn=True,
            )
            self.assertTrue(r_ok.get("ok"), r_ok)
            self.assertTrue(r_ok.get("deferred"))
            self.assertTrue(g.has_pending_complete())
            # plan_body should reflect closed boxes after sync
            self.assertEqual(open_checklist_items(g.plan_body), [])
            # AC frozen at accept — not replaced by disk rewrite
            self.assertEqual(extract_acceptance_criteria(g.plan_body), frozen_ac)

    def test_disk_delete_checklist_keeps_open_and_blocks(self):
        """Deleting ## Task checklist on disk must not unlock complete."""
        g = GoalTracker()
        g.create("ship widget")
        with tempfile.TemporaryDirectory() as td:
            path = default_plan_path(td, g.goal_id)
            open_md = sample_concrete_plan(
                "ship widget", goal_id=g.goal_id, checklist_done=False)
            write_plan_file(path, open_md)
            g.accept_plan(open_md, plan_path=path)
            frozen_open = open_checklist_items(g.plan_body)
            self.assertGreaterEqual(len(frozen_open), 1)
            # Hostile disk rewrite: drop checklist entirely
            thinned = "\n".join([
                "# Plan: ship widget",
                "",
                "## Acceptance criteria",
                "1. **Code path**: Implementation in `src/goal_feature.py` for ship widget",
                "2. **Tests**: `tests/test_goal_feature.py` via pytest.",
                "",
                "## Verification plan",
                "1. `gating`: `pytest tests/test_goal_feature.py -q`",
                "2. `gating`: check `src/goal_feature.py` entry points",
                "",
            ])
            write_plan_file(path, thinned)
            r = g.apply_update(
                completed=True,
                message="ship widget fully done with tests",
            )
            self.assertFalse(r.get("ok"), r)
            self.assertTrue(any("checklist" in (i or "").lower()
                                for i in (r.get("issues") or [])))
            # Frozen open items preserved
            self.assertEqual(open_checklist_items(g.plan_body), frozen_open)

    def test_disk_thin_ac_keeps_frozen_criteria(self):
        """Thinning Acceptance criteria on disk must not replace frozen AC."""
        g = GoalTracker()
        g.create("ship widget")
        with tempfile.TemporaryDirectory() as td:
            path = default_plan_path(td, g.goal_id)
            open_md = sample_concrete_plan(
                "ship widget", goal_id=g.goal_id, checklist_done=False)
            write_plan_file(path, open_md)
            g.accept_plan(open_md, plan_path=path)
            frozen_ac = extract_acceptance_criteria(g.plan_body)
            self.assertGreaterEqual(len(frozen_ac), 2)
            # Disk: only one vague criterion + all [x]
            bad = sample_concrete_plan(
                "ship widget", goal_id=g.goal_id, checklist_done=True)
            # Strip AC to one line
            bad = bad.replace(
                "1. **Code path**: Implementation lives in `src/goal_feature.py` "
                "(or package equivalent) for: ship widget\n"
                "2. **Tests**: `tests/test_goal_feature.py` covers the happy path "
                "and one failure case via pytest.\n"
                "3. **CLI/UI**: User-visible entry (command or function) exercises "
                "the feature without manual steps.\n",
                "1. something vaguely done\n",
            )
            write_plan_file(path, bad)
            g.sync_plan_body_from_disk()
            # AC unchanged; checklist marks may flip if keys still match
            self.assertEqual(extract_acceptance_criteria(g.plan_body), frozen_ac)


if __name__ == "__main__":
    unittest.main()
