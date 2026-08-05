"""Host evidence validation — block green-checklist theater."""
import os
import tempfile
import unittest

from goal_evidence import (
    goal_is_visual,
    validate_evidence_for_achieved,
    extract_path_candidates,
)


class TestGoalEvidence(unittest.TestCase):
    def test_visual_detection(self):
        self.assertTrue(goal_is_visual("Ship SSR Hi-Z pass", ""))
        self.assertTrue(goal_is_visual("", "## TAA object velocity"))
        self.assertFalse(goal_is_visual("Add argparse CLI flag", "code only"))

    def test_narrative_only_rejected(self):
        ok, gaps, _ = validate_evidence_for_achieved(
            [
                "Structure: contact_shadow/{mod,api,impl}",
                "API: addContactShadowPass",
                "READMEs: ssr docs updated",
            ],
            message="Plan ACs satisfied under v1 scope",
            objective="SSR Hi-Z and froxel fog",
            plan_body="## SSR\n## fog froxel",
            plan_path="",
            cwd="",
        )
        self.assertFalse(ok)
        blob = " ".join(gaps).lower()
        self.assertTrue("narrative" in blob or "image" in blob or "grounded" in blob)

    def test_residual_message_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            evid = os.path.join(td, "evidence")
            os.makedirs(evid)
            logp = os.path.join(evid, "t.log")
            with open(logp, "w") as f:
                f.write("OK " * 20)
            plan = os.path.join(td, "plan.md")
            with open(plan, "w") as f:
                f.write("# plan\n")
            ok, gaps, _ = validate_evidence_for_achieved(
                [f"unit test → evidence/t.log exit 0"],
                message=(
                    "ACs met. Residual non-blockers: froxel is not true grid; "
                    "SSR Hi-Z is mip0-only (documented)."
                ),
                objective="CLI tool",
                plan_body="",
                plan_path=plan,
                cwd=td,
            )
            self.assertFalse(ok)
            self.assertTrue(any("residual" in g.lower() or "non-blocker" in g.lower() for g in gaps))

    def test_visual_needs_image(self):
        with tempfile.TemporaryDirectory() as td:
            evid = os.path.join(td, "evidence")
            os.makedirs(evid)
            logp = os.path.join(evid, "ssr_test.log")
            with open(logp, "w") as f:
                f.write("2 OK exit 0 " * 10)
            plan = os.path.join(td, "plan.md")
            with open(plan, "w") as f:
                f.write("# SSR plan\n")
            ok, gaps, _ = validate_evidence_for_achieved(
                [
                    "ssr pil test → evidence/ssr_test.log 2 OK",
                    "another → evidence/ssr_test.log",
                ],
                message="SSR done",
                objective="SSR Hi-Z screen-space reflections",
                plan_body="ssr pass",
                plan_path=plan,
                cwd=td,
            )
            self.assertFalse(ok)
            self.assertTrue(any("image" in g.lower() for g in gaps))

    def test_visual_with_capture_ok(self):
        with tempfile.TemporaryDirectory() as td:
            evid = os.path.join(td, "evidence")
            os.makedirs(evid)
            logp = os.path.join(evid, "ssr_test.log")
            with open(logp, "w") as f:
                f.write("2 OK exit 0 " * 10)
            png = os.path.join(evid, "ssr_present.png")
            with open(png, "wb") as f:
                f.write(b"\x89PNG" + b"\x00" * 300)
            plan = os.path.join(td, "plan.md")
            with open(plan, "w") as f:
                f.write("# SSR plan\n")
            ok, gaps, notes = validate_evidence_for_achieved(
                [
                    "ssr pil test → evidence/ssr_test.log 2 OK",
                    "capture evidence/ssr_present.png non-black",
                ],
                message="SSR capture green",
                objective="SSR screen-space reflections",
                plan_body="ssr pass",
                plan_path=plan,
                cwd=td,
            )
            self.assertTrue(ok, gaps)
            self.assertEqual(gaps, [])

    def test_path_extract(self):
        paths = extract_path_candidates(
            "HOST re-run: ssr → evidence/ssr_test.log; ssr_present.png mean~76"
        )
        self.assertTrue(any("ssr_test.log" in p for p in paths))
        self.assertTrue(any("ssr_present.png" in p for p in paths))


if __name__ == "__main__":
    unittest.main()
