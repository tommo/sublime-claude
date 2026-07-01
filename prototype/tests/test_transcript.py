#!/usr/bin/env python3
"""Pure unit tests for cc_transcript — no claude, no Sublime, fast.

Validates the record→params mapping against the live transcript snapshot at
/tmp/snap.jsonl (write it by `cp <a real transcript.jsonl> /tmp/snap.jsonl`).
Anything that doesn't need real claude lives here.
"""
import os
import sys
import json
import tempfile
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # prototype/
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # repo root
import cc_transcript as ct  # noqa

SNAP = "/tmp/snap.jsonl"
results = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    return cond


# ── 1. project_slug + transcript_path ─────────────────────────────────────────
def test_slug():
    # macOS realpath /tmp → /private/tmp, must be reflected
    s = ct.project_slug("/tmp/whatever")
    check("slug realpath /tmp → /private/tmp",
          s == "-private-tmp-whatever", "got %r" % s)
    s = ct.project_slug("/opt/projects/sublime-claude")
    check("slug non-alnum → '-'",
          s == "-opt-projects-sublime-claude", "got %r" % s)
    # Trailing slash + dots are stripped to '-' (no collapsing)
    s = ct.project_slug("/Users/a/.b")
    check("slug preserves doubles (a/.b → -Users-a--b)",
          s == "-Users-a--b", "got %r" % s)
    p = ct.transcript_path("/opt/projects/sublime-claude", "abc-123")
    check("transcript_path composes",
          p.endswith("/.claude/projects/-opt-projects-sublime-claude/abc-123.jsonl"),
          "got %r" % p)


# ── 2. record_to_params coverage ───────────────────────────────────────────────
def test_assistant_text():
    rec = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello world"}]}}
    out = list(ct.record_to_params(rec, {}))
    check("assistant text → text param",
          out == [{"type": "text", "text": "hello world"}], "got %r" % out)

def test_assistant_blank_text_skipped():
    rec = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "   \n  "}]}}
    out = list(ct.record_to_params(rec, {}))
    check("blank assistant text is skipped", out == [], "got %r" % out)

def test_assistant_tool_use_input_shape():
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Edit",
         "input": {"file_path": "/x", "old_string": "a", "new_string": "b"}}]}}
    id2 = {}
    out = list(ct.record_to_params(rec, id2))
    p = out[0]
    check("tool_use keys exactly match _on_msg_tool_use",
          set(p.keys()) == {"type", "name", "input", "id"} and
          p["type"] == "tool_use" and p["name"] == "Edit" and p["id"] == "t1" and
          p["input"]["old_string"] == "a", "got %r" % p)
    check("id2name updated", id2 == {"t1": "Edit"})

def test_user_tool_result_keys():
    rec = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "stdout text", "is_error": False}]}}
    out = list(ct.record_to_params(rec, {}))
    p = out[0]
    # _on_msg_tool_result reads "tool_use_id" (NOT "id") and "content"
    check("tool_result uses 'tool_use_id' not 'id'",
          p.get("tool_use_id") == "t1" and "id" not in p,
          "got %r" % p)
    check("tool_result.is_error bool",
          p["is_error"] is False, "got %r" % p)

def test_user_tool_result_list_content():
    rec = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t",
         "content": [{"type": "text", "text": "line1"},
                     {"type": "text", "text": "line2"}],
         "is_error": False}]}}
    out = list(ct.record_to_params(rec, {}))
    check("tool_result list content flattened",
          out and "line1" in out[0]["content"] and "line2" in out[0]["content"],
          "got %r" % out)

def test_user_string_prompts_skipped():
    rec = {"type": "user", "message": {"content": "typed by user"}}
    out = list(ct.record_to_params(rec, {}))
    check("plain string user content is skipped (engine renders prompts)",
          out == [], "got %r" % out)

def test_meta_sidechain_skipped():
    rec = {"type": "user", "isMeta": True,
           "message": {"content": [{"type": "tool_result",
                                    "tool_use_id": "x", "content": "z"}]}}
    out = list(ct.record_to_params(rec, {}))
    check("isMeta records are skipped", out == [], "got %r" % out)
    rec["isMeta"] = False
    rec["isSidechain"] = True
    out = list(ct.record_to_params(rec, {}))
    check("isSidechain records are skipped", out == [], "got %r" % out)

def test_system_subtypes():
    # turn_duration → "result" with duration_ms (real number)
    rec = {"type": "system", "subtype": "turn_duration", "durationMs": 12345}
    out = list(ct.record_to_params(rec, {}))
    check("turn_duration → result with duration_ms",
          out == [{"type": "result", "duration_ms": 12345, "total_cost_usd": 0}],
          "got %r" % out)
    rec = {"type": "system", "subtype": "compact_boundary"}
    out = list(ct.record_to_params(rec, {}))
    check("compact_boundary → system/compact_boundary",
          out == [{"type": "system", "subtype": "compact_boundary", "data": {}}],
          "got %r" % out)
    rec = {"type": "system", "subtype": "api_error",
           "error": {"message": "rate-limited"}, "retryAttempt": 2, "maxRetries": 5}
    out = list(ct.record_to_params(rec, {}))
    check("api_error → text with retry counter",
          out and "rate-limited" in out[0]["text"] and "retry 2/5" in out[0]["text"],
          "got %r" % out)
    # stop_hook_summary surfaces ONLY on errors
    rec = {"type": "system", "subtype": "stop_hook_summary",
           "hookCount": 1, "hookErrors": [], "preventedContinuation": False}
    out = list(ct.record_to_params(rec, {}))
    check("stop_hook_summary silent on success", out == [], "got %r" % out)
    rec["hookErrors"] = ["boom"]
    rec["preventedContinuation"] = True
    rec["stopReason"] = "blocked"
    out = list(ct.record_to_params(rec, {}))
    check("stop_hook_summary surfaces on errors",
          out and "blocked" in out[0]["text"] and "boom" in out[0]["text"],
          "got %r" % out)


# ── 3. AskUserQuestion surface formatter ──────────────────────────────────────
def test_ask_user_format():
    inp = {"questions": [
        {"question": "pick one", "header": "h", "multiSelect": False,
         "options": [{"label": "A"}, {"label": "B"}]}]}
    out = ct._format_ask_user(inp)
    check("_format_ask_user mentions question + options + header",
          "pick one" in out and "[h]" in out and "1)" in out and "2)" in out,
          "got %r" % out)

def test_ask_user_with_tool_use_emits_both():
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
         "input": {"questions": [{"question": "?", "options": [{"label": "Y"}]}]}}]}}
    out = list(ct.record_to_params(rec, {}))
    check("AskUserQuestion emits tool_use + text surface",
          len(out) == 2 and out[0]["type"] == "tool_use" and
          out[1]["type"] == "text", "got %r" % out)


# ── 4. Tailer (file-tail thread) ──────────────────────────────────────────────
def test_tailer_reads_appended_lines():
    tmpf = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    tmpf.close()
    got = []
    tailer = ct.Tailer(tmpf.name, lambda p: got.append(p), poll=0.05)
    tailer.start()
    time.sleep(0.2)  # let it start
    # Append assistant text record
    with open(tmpf.name, "a") as f:
        f.write(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "hi from tail"}]}}) + "\n")
        f.flush()
    # Append second record after a short delay
    time.sleep(0.3)
    with open(tmpf.name, "a") as f:
        f.write(json.dumps({"type": "system", "subtype": "turn_duration",
                            "durationMs": 999}) + "\n")
        f.flush()
    time.sleep(0.4)
    tailer.stop()
    os.remove(tmpf.name)
    check("Tailer emitted assistant text",
          any(p.get("text") == "hi from tail" for p in got), "got %r" % got)
    check("Tailer emitted result (from turn_duration)",
          any(p.get("type") == "result" and p.get("duration_ms") == 999 for p in got),
          "got %r" % got)

def test_tailer_handles_partial_line():
    tmpf = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    tmpf.close()
    got = []
    tailer = ct.Tailer(tmpf.name, lambda p: got.append(p), poll=0.05)
    tailer.start()
    time.sleep(0.2)
    # Write a record in two halves, no newline between
    rec = json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "split"}]}})
    half = len(rec) // 2
    with open(tmpf.name, "a") as f:
        f.write(rec[:half])
        f.flush()
    time.sleep(0.2)
    with open(tmpf.name, "a") as f:
        f.write(rec[half:] + "\n")
        f.flush()
    time.sleep(0.4)
    tailer.stop()
    os.remove(tmpf.name)
    check("Tailer buffered partial line + parsed on newline",
          any(p.get("text") == "split" for p in got), "got %r" % got)


# ── 5. Snapshot smoke test ────────────────────────────────────────────────────
def test_snapshot_smoke():
    if not os.path.exists(SNAP):
        results.append(("SKIP", "snapshot smoke (no /tmp/snap.jsonl)", ""))
        return
    id2 = {}
    from collections import Counter
    cnt = Counter()
    with open(SNAP) as f:
        for line in f:
            try: rec = json.loads(line)
            except json.JSONDecodeError: continue
            for p in ct.record_to_params(rec, id2):
                cnt[p["type"]] += 1
    check("snapshot produces text/tool_use/tool_result/turn_usage/result",
          all(t in cnt for t in ("text","tool_use","tool_result","turn_usage","result")),
          "got types=%r" % dict(cnt))
    check("tool_use ≈ tool_result count (paired)",
          abs(cnt["tool_use"] - cnt["tool_result"]) <= 10,
          "tool_use=%d tool_result=%d" % (cnt["tool_use"], cnt["tool_result"]))


def main():
    for fn in [
        test_slug, test_assistant_text, test_assistant_blank_text_skipped,
        test_assistant_tool_use_input_shape, test_user_tool_result_keys,
        test_user_tool_result_list_content, test_user_string_prompts_skipped,
        test_meta_sidechain_skipped, test_system_subtypes,
        test_ask_user_format, test_ask_user_with_tool_use_emits_both,
        test_tailer_reads_appended_lines, test_tailer_handles_partial_line,
        test_snapshot_smoke,
    ]:
        try: fn()
        except Exception as e: results.append(("FAIL", fn.__name__, "exception: %r" % e))
    p = sum(1 for s,_,_ in results if s == "PASS")
    f = sum(1 for s,_,_ in results if s == "FAIL")
    s = sum(1 for s,_,_ in results if s == "SKIP")
    for status, name, detail in results:
        line = "[%s] %s" % (status, name)
        if detail and status != "PASS": line += "  — " + detail
        print(line)
    print("\n%d passed, %d failed, %d skipped" % (p, f, s))
    sys.exit(0 if f == 0 else 1)


if __name__ == "__main__":
    main()
