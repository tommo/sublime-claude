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

    def test_followup_includes_dropped_questions(self):
        answers = {
            "How should the FSM view be rebuilt?": "Reuse pui.node_graph",
            "How should blend trees be shown?":
                "Tree + visual diagrams (Recommended)",
        }
        text = AcpBridge._kimi_followup_answers([Q0, Q1], answers)
        self.assertIn("do NOT treat this as dismissed", text)
        self.assertIn("blend trees", text.lower())


if __name__ == "__main__":
    unittest.main()
