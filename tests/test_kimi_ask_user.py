"""Kimi AskUserQuestion must return q0_opt_N or the SDK reports dismissed."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402


OPTS = [
    {"optionId": "q0_opt_0", "name": "Custom FSM canvas (Recommended)",
     "kind": "allow_once"},
    {"optionId": "q0_opt_1", "name": "Reuse pui.node_graph",
     "kind": "allow_once"},
    {"optionId": "q0_skip", "name": "Skip", "kind": "reject_once"},
]

Q0 = {
    "question": "How should the FSM view be rebuilt?",
    "header": "FSM view",
    "options": [
        {"label": "Custom FSM canvas (Recommended)",
         "description": "Unity/Godot-style boxes"},
        {"label": "Reuse pui.node_graph",
         "description": "check graf package, might be worth having a look"},
    ],
    "multiSelect": False,
}
Q1 = {
    "question": "How should blend trees be shown?",
    "header": "Blend trees",
    "options": [
        {"label": "Tree + visual diagrams (Recommended)", "description": ""},
    ],
    "multiSelect": False,
}


class TestKimiAskUserMapping(unittest.TestCase):
    def test_exact_label_maps_to_q0_opt(self):
        oid = AcpBridge._kimi_q0_option_id(
            OPTS, [Q0], "Custom FSM canvas (Recommended)")
        self.assertEqual(oid, "q0_opt_0")

    def test_description_maps_to_same_option(self):
        # User picked by description text (or Other echoed the description).
        oid = AcpBridge._kimi_q0_option_id(
            OPTS, [Q0], "check graf package, might be worth having a look")
        self.assertEqual(oid, "q0_opt_1")

    def test_freeform_is_not_a_q0_opt(self):
        oid = AcpBridge._kimi_q0_option_id(
            OPTS, [Q0], "something I typed")
        self.assertEqual(oid, "")
        self.assertTrue(AcpBridge._is_kimi_q0_opt(
            AcpBridge._kimi_first_q0_option_id(OPTS)))
        self.assertFalse(AcpBridge._is_kimi_q0_opt("something I typed"))
        self.assertFalse(AcpBridge._is_kimi_q0_opt("q0_skip"))

    def test_first_answer_prefers_header_key(self):
        answers = {
            "FSM view": "Reuse pui.node_graph",
            "Blend trees": "Tree + visual diagrams (Recommended)",
        }
        self.assertEqual(
            AcpBridge._first_answer_label(answers, [Q0, Q1]),
            "Reuse pui.node_graph")

    def test_elicitation_sends_every_listed_label(self):
        answers = {
            "How should the FSM view be rebuilt?": "Reuse pui.node_graph",
            "How should blend trees be shown?":
                "Tree + visual diagrams (Recommended)",
        }
        content = AcpBridge._elicitation_content_from_answers(
            [Q0, Q1], ["q0", "q1"], answers)
        self.assertEqual(content, {
            "q0": "Reuse pui.node_graph",
            "q1": "Tree + visual diagrams (Recommended)",
        })

    def test_real_colorgrading_three_answers(self):
        import os
        sys.path.insert(0, os.path.join(_ROOT, "sandbox", "kimi_ask"))
        import extract as ex  # noqa: E402
        wire = os.path.join(
            _ROOT, "sandbox", "kimi_ask", "fixtures", "wire.jsonl")
        r = ex.extract_ask(wire)
        self.assertEqual(r["n_questions"], 3)
        self.assertEqual(len(r["tool_result_answers"]), 1)
        qs = r["questions"]
        answers = {
            qs[0]["question"]: "Full suite (Recommended)",
            qs[1]["question"]: "Bake to 3D LUT (Recommended)",
            qs[2]["question"]: "API space only (Recommended)",
        }
        opts = [
            {"optionId": "q0_opt_0", "name": "Full suite (Recommended)",
             "kind": "allow_once"},
            {"optionId": "q0_opt_1", "name": "Moderate", "kind": "allow_once"},
            {"optionId": "q0_skip", "name": "Skip", "kind": "reject_once"},
        ]
        oid = AcpBridge._kimi_q0_option_id(
            opts, qs, AcpBridge._first_answer_label(answers, qs))
        self.assertEqual(oid, "q0_opt_0")
        content = AcpBridge._elicitation_content_from_answers(
            qs, ["q0", "q1", "q2"], answers)
        self.assertEqual(content.get("q0"), "Full suite (Recommended)")
        self.assertEqual(content.get("q1"), "Bake to 3D LUT (Recommended)")
        self.assertEqual(content.get("q2"), "API space only (Recommended)")


class TestKimiElicitationForm(unittest.TestCase):
    def test_schema_to_questions_and_accept_content(self):
        params = {
            "mode": "form",
            "message": "Pick one\nPick many",
            "requestedSchema": {
                "type": "object",
                "required": ["q0", "q1"],
                "properties": {
                    "q0": {
                        "type": "string",
                        "title": "One",
                        "oneOf": [
                            {"const": "A", "title": "A"},
                            {"const": "B", "title": "B"},
                        ],
                    },
                    "q1": {
                        "type": "array",
                        "title": "Many",
                        "items": {
                            "anyOf": [
                                {"const": "X", "title": "X"},
                                {"const": "Y", "title": "Y"},
                                {"const": "Z", "title": "Z"},
                            ],
                        },
                    },
                },
            },
        }
        qs, keys = AcpBridge._questions_from_elicitation(params)
        self.assertEqual(keys, ["q0", "q1"])
        self.assertEqual(qs[0]["question"], "Pick one")
        self.assertFalse(qs[0]["multiSelect"])
        self.assertTrue(qs[1]["multiSelect"])
        content = AcpBridge._elicitation_content_from_answers(
            qs, keys, {"Pick one": "B", "Pick many": ["Z", "X"]})
        self.assertEqual(content, {"q0": "B", "q1": ["X", "Z"]})
        self.assertFalse(AcpBridge._kimi_answers_dropped(
            qs, {"Pick one": "B", "Pick many": ["Z", "X"]}, content, keys))

    def test_other_freeform_is_sent_like_native(self):
        """Native wire: answers[question]=typed string, method=enter."""
        qs = [
            {"question": "What should the new procedural animation package be named?",
             "header": "Pkg name",
             "options": [{"label": "procanim"}, {"label": "procmotion"},
                         {"label": "panim"}],
             "multiSelect": False},
            {"question": "Confirm Phase 1 scope (pure math modules, no ECS coupling yet)?",
             "header": "Phase 1",
             "options": [
                 {"label": "dynamics + noise_motion (Recommended)"},
                 {"label": "dynamics only"},
                 {"label": "dynamics + verlet"},
             ],
             "multiSelect": False},
        ]
        answers = {
            qs[0]["question"]: "procmotion",
            qs[1]["question"]: "all",
        }
        content = AcpBridge._elicitation_content_from_answers(
            qs, ["q0", "q1"], answers)
        self.assertEqual(content, {"q0": "procmotion", "q1": "all"})
        self.assertTrue(AcpBridge._kimi_has_freetext(qs, answers))
        extra = AcpBridge._kimi_followup_answers(qs, answers)
        self.assertIn("all", extra)
        self.assertIn('"answers"', extra)

    def test_single_question_other_is_not_empty(self):
        q = "AskUserQuestion tool test: which option do you pick?"
        qs = [{
            "question": q, "header": "Test",
            "options": [{"label": "Option A"}, {"label": "Option B"}],
            "multiSelect": False,
        }]
        answers = {q: "Hello, End here"}
        content = AcpBridge._elicitation_content_from_answers(
            qs, ["q0"], answers)
        self.assertEqual(content, {"q0": "Hello, End here"})
        self.assertTrue(AcpBridge._kimi_has_freetext(qs, answers))
        extra = AcpBridge._kimi_followup_answers(qs, answers)
        self.assertIn("Hello, End here", extra)
        self.assertIn(q, extra)

    def test_inject_followup_is_dead(self):
        path = os.path.join(_ROOT, "bridge", "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    def _inject_ask_user_followup")
        end = src.find("    def _match_option_id_for_label", start)
        live = []
        for line in src[start:end].splitlines():
            s = line.lstrip()
            if s.startswith("#"):
                continue
            live.append(line)
        body = "\n".join(live)
        self.assertNotIn("notification_wake", body)
        self.assertNotIn("interrupt", body)
        idx = src.find("self._flush_ask_followup()")
        line = src[src.rfind("\n", 0, idx) + 1:src.find("\n", idx)]
        self.assertTrue(line.lstrip().startswith("#"), line)

    def test_freetext_followup_chains_after_end_turn(self):
        path = os.path.join(_ROOT, "bridge", "acp_base.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    async def handle_query")
        end = src.find("    def _build_prompt_blocks", start)
        live = []
        for line in src[start:end].splitlines():
            s = line.lstrip()
            if s.startswith("#"):
                continue
            live.append(line)
        body = "\n".join(live)
        self.assertIn("_pending_ask_followup", body)
        self.assertIn("_send_prompt(extra_blocks)", body)
        chunk = body.split("_pending_ask_followup")[-1][:1200]
        self.assertNotIn("session/cancel", chunk)
        self.assertNotIn("interrupt: True", chunk)

    def test_cancel_when_no_answers(self):
        self.assertEqual(
            AcpBridge._elicitation_content_from_answers(
                [{"question": "Q", "header": "", "options": [],
                  "multiSelect": False}],
                ["q0"],
                {},
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
