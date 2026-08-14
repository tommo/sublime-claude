#!/usr/bin/env python3
"""Replay the real 3-question AskUser against host mapping + Kimi's q0-only rule."""
from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_HERE, _ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import extract as ex  # noqa: E402
from acp_base import AcpBridge  # noqa: E402


def kimi_q0_options(question: dict) -> list:
    """Replica of Kimi questionItemToPermissionOptions(q, 0)."""
    opts = []
    for i, opt in enumerate(question.get("options") or []):
        label = opt.get("label") if isinstance(opt, dict) else str(opt)
        opts.append({
            "optionId": f"q0_opt_{i}",
            "name": label,
            "kind": "allow_once",
        })
    opts.append({"optionId": "q0_skip", "name": "Skip", "kind": "reject_once"})
    return opts


def kimi_outcome_to_answers(question: dict, option_id: str):
    """Replica of Kimi outcomeToQuestionAnswer (q0_opt_N only)."""
    m = re.match(r"^q0_opt_(\d+)$", option_id or "")
    if not m:
        return None
    idx = int(m.group(1))
    options = question.get("options") or []
    if idx < 0 or idx >= len(options):
        return None
    selected = options[idx]
    label = selected.get("label") if isinstance(selected, dict) else str(selected)
    return {question.get("question"): label}


def host_answers_from_ui(questions: list) -> dict:
    """What the Sublime UI stored: keyed by question text (see handle_question_key)."""
    # Real click path from the session the user pasted.
    picked = [
        "Full suite (Recommended)",
        "Bake to 3D LUT (Recommended)",
        "API space only (Recommended)",
    ]
    out = {}
    for q, label in zip(questions, picked):
        out[q.get("question") or q.get("header") or ""] = label
    return out


def main() -> int:
    wire = os.path.join(_HERE, "fixtures", "wire.jsonl")
    r = ex.extract_ask(wire)
    if not r:
        print("no AskUserQuestion in fixture", file=sys.stderr)
        return 2
    qs = r["questions"]
    fails = []
    print("n_questions", r["n_questions"])
    print("wire resolved", json.dumps(r["resolved"], ensure_ascii=False)[:400])
    print("wire tool_result", json.dumps(r["tool_result_answers"], ensure_ascii=False)[:400])

    if r["n_questions"] != 3:
        fails.append("fixture should be the 3-question colorgrading ask")
    if len(r["tool_result_answers"]) != 1:
        fails.append("Kimi tool_result must contain only Q0 (got %s)" % r["tool_result_answers"])
    if not any("Full suite" in str(v) for v in r["tool_result_answers"].values()):
        fails.append("Q0 Full suite missing from tool_result")
    if any("3D LUT" in str(v) or "API space" in str(v) for v in r["tool_result_answers"].values()):
        fails.append("Q1/Q2 leaked into Kimi tool_result — unexpected")

    answers = host_answers_from_ui(qs)
    q0 = qs[0]
    opts = kimi_q0_options(q0)
    label = AcpBridge._first_answer_label(answers, qs)
    oid = AcpBridge._kimi_q0_option_id(opts, qs, label)
    extra = AcpBridge._kimi_followup_answers(qs, answers)
    kimi_sees = kimi_outcome_to_answers(q0, oid)

    print("host first label", label)
    print("host optionId", oid)
    print("kimi_sees", kimi_sees)
    print("followup:\n", extra)

    if oid != "q0_opt_0":
        fails.append("host should map Full suite → q0_opt_0, got %r" % oid)
    if kimi_sees is None or len(kimi_sees) != 1:
        fails.append("Kimi outcomeToQuestionAnswer must return only Q0")
    if "Bake to 3D LUT" not in extra or "API space only" not in extra:
        fails.append("followup text must carry Q1 and Q2")
    if "Full suite" not in extra:
        fails.append("followup should recap Q0 too")

    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("host maps Q0; Kimi drops Q1+; followup is the only channel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
