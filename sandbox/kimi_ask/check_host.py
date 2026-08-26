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
    kimi_sees = kimi_outcome_to_answers(q0, oid)
    elicitation_content = AcpBridge._elicitation_content_from_answers(
        qs, [f"q{i}" for i in range(len(qs))], answers)

    print("host first label", label)
    print("host optionId", oid)
    print("permission kimi_sees", kimi_sees)
    print("elicitation content", elicitation_content)

    if oid != "q0_opt_0":
        fails.append("host should map Full suite → q0_opt_0, got %r" % oid)
    if kimi_sees is None or len(kimi_sees) != 1:
        fails.append("permission outcomeToQuestionAnswer is q0-only")
    if elicitation_content.get("q1") != "Bake to 3D LUT (Recommended)":
        fails.append("elicitation must send Q1 listed label, got %r" %
                     elicitation_content.get("q1"))
    if elicitation_content.get("q2") != "API space only (Recommended)":
        fails.append("elicitation must send Q2 listed label")

    # Live 2-question Other: Kimi elicitationResponseToQuestionAnswers
    # drops values not in declared option labels.
    live_qs = [
        {"question": "What should the new procedural animation package be named?",
         "header": "Pkg name",
         "options": [{"label": "procanim"}, {"label": "procmotion"},
                     {"label": "panim"}]},
        {"question": "Confirm Phase 1 scope (pure math modules, no ECS coupling yet)?",
         "header": "Phase 1",
         "options": [
             {"label": "dynamics + noise_motion (Recommended)"},
             {"label": "dynamics only"},
             {"label": "dynamics + verlet"},
         ]},
    ]
    live_answers = {
        live_qs[0]["question"]: "procmotion",
        live_qs[1]["question"]: "all",
    }
    content = AcpBridge._elicitation_content_from_answers(
        live_qs, ["q0", "q1"], live_answers)
    kimi_kept = {}
    for i, q in enumerate(live_qs):
        val = content.get(f"q{i}")
        labels = [o["label"] for o in q["options"]]
        if isinstance(val, str) and val in labels:
            kimi_kept[q["question"]] = val
    print("live Other elicitation content", content)
    print("kimi_kept after enum filter", kimi_kept)
    if "q1" in content and content["q1"] == "all":
        fails.append("host must not put Other 'all' in q1 — Kimi drops it")
    if live_qs[1]["question"] in kimi_kept:
        fails.append("Kimi enum filter should drop Other 'all'")
    path = os.path.join(_ROOT, "bridge", "acp_base.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    inj = src.find("    def _inject_ask_user_followup")
    inj_end = src.find("    def _match_option_id_for_label", inj)
    live = []
    for line in src[inj:inj_end].splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        live.append(line)
    if any("notification_wake" in ln for ln in live):
        fails.append("inject_ask_user_followup still sends notification_wake")
    hq = src.find("    async def handle_query")
    hq_end = src.find("    def _build_prompt_blocks", hq)
    hq_live = "\n".join(
        ln for ln in src[hq:hq_end].splitlines()
        if not ln.lstrip().startswith("#")
    )
    if "_pending_ask_followup" not in hq_live or "_send_prompt(extra_blocks)" not in hq_live:
        fails.append("handle_query must chain freetext session/prompt after end_turn")
    if "session/cancel" in hq_live.split("_pending_ask_followup")[-1][:900]:
        fails.append("freetext followup must not session/cancel")

    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("elicitation sends every listed label; Other omitted; no inject")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
