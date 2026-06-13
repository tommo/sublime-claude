#!/usr/bin/env python3
"""Static diagnostic for the CC tool-result spill pattern.

When a tool_result content exceeds CC's inline threshold, the actual bytes are
written to `<session-id>/tool-results/<tool_use_id>.txt` and the main .jsonl
record keeps a placeholder marker like:

    <persisted-output>
    Output too large (26KB). Full output saved to: <abs path>
    ...
    </persisted-output>

This script scans real transcripts and reports the spill format observed,
sidefile locations, marker shape, and content sample. Use it to verify the
pattern still holds on a new CC version — if the marker text or sidefile
naming changes, the engine's rendering (which currently shows only the marker)
will need to follow.

Usage:
  python3 prototype/tests/diagnose_spill.py                     # scans ~/.claude/projects
  python3 prototype/tests/diagnose_spill.py <transcript.jsonl>  # one file
  python3 prototype/tests/diagnose_spill.py <project-dir>       # one project's transcripts
"""
import json
import os
import re
import sys
from collections import Counter

PROJECTS = os.path.expanduser("~/.claude/projects")

# These are the LOAD-BEARING patterns the engine + this diagnostic assume:
MARKER_RE = re.compile(
    r"<persisted-output>\s*Output too large \((\d+)KB\)\.\s*"
    r"Full output saved to:\s*(\S+)",
    re.S,
)


def scan_transcript(path):
    """Yield (record_index, tool_use_id, marker_size_kb, sidefile_path,
    sidefile_exists, sidefile_size, structured_keys) for each spilled result
    found in `path`."""
    session_dir = path[:-len(".jsonl")] if path.endswith(".jsonl") else path
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                for b in (rec.get("message", {}).get("content") or []):
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    ct = b.get("content", "")
                    if not isinstance(ct, str):
                        continue
                    m = MARKER_RE.search(ct)
                    if not m:
                        continue
                    tid = b.get("tool_use_id", "")
                    declared_kb = int(m.group(1))
                    declared_path = m.group(2)
                    tur = rec.get("toolUseResult") if isinstance(
                        rec.get("toolUseResult"), dict) else {}
                    # CC currently uses two naming conventions, both rooted at
                    # <session>/tool-results/:
                    #   convention A: <tool_use_id>.txt  (Glob/Grep/etc.)
                    #   convention B: <persistedOutputPath> (Bash; short
                    #                  random id; carried on toolUseResult)
                    expected_A = os.path.join(session_dir, "tool-results",
                                              tid + ".txt")
                    expected_B = tur.get("persistedOutputPath", "")
                    if declared_path == expected_A:
                        convention = "A: tool_use_id"
                    elif expected_B and declared_path == expected_B:
                        convention = "B: persistedOutputPath"
                    else:
                        convention = "UNKNOWN"
                    exists = os.path.exists(declared_path)
                    size = os.path.getsize(declared_path) if exists else 0
                    yield {
                        "record": i, "tool_use_id": tid,
                        "marker_kb": declared_kb,
                        "marker_path": declared_path,
                        "convention": convention,
                        "sidefile_exists": exists,
                        "sidefile_size": size,
                        "toolUseResult_keys": list(tur.keys()) if tur else None,
                    }
    except FileNotFoundError:
        pass


def report(path):
    print(f"\n=== {path}")
    spilled = list(scan_transcript(path))
    if not spilled:
        print("  no spilled tool_results found")
        return spilled
    print(f"  {len(spilled)} spilled tool_result(s):")
    for s in spilled:
        known = s["convention"] != "UNKNOWN"
        ok = "✓" if known else "✗"
        present = "OK" if s["sidefile_exists"] else "cleaned up"
        print(f"  {ok} #{s['record']} tool_use_id={s['tool_use_id'][:14]}…  "
              f"declared={s['marker_kb']}KB  "
              f"convention={s['convention']}  "
              f"sidefile={present} ({s['sidefile_size']}B)")
    # Show a sidefile sample for the first present one
    for s in spilled:
        if s["sidefile_exists"]:
            with open(s["marker_path"]) as f:
                head = f.read(300).replace("\n", " ")
            print(f"  sample[0:300]: {head!r}")
            break
    return spilled


def main():
    targets = sys.argv[1:]
    if not targets:
        # Scan all projects, pick 5 most recently active sessions
        import glob
        files = []
        for pdir in sorted(glob.glob(os.path.join(PROJECTS, "*"))):
            if not os.path.isdir(pdir):
                continue
            files.extend(glob.glob(os.path.join(pdir, "*.jsonl")))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        targets = files[:5]
        print(f"# scanning {len(targets)} most recently active transcripts "
              f"(of {len(files)} total)")
    elif len(targets) == 1 and os.path.isdir(targets[0]):
        import glob
        targets = sorted(glob.glob(os.path.join(targets[0], "*.jsonl")),
                         key=lambda p: os.path.getmtime(p), reverse=True)

    total = 0
    known = 0
    by_convention = Counter()
    for t in targets:
        for r in report(t):
            total += 1
            by_convention[r["convention"]] += 1
            if r["convention"] != "UNKNOWN":
                known += 1

    print(f"\n========== SUMMARY ==========")
    print(f"  total spilled tool_results across {len(targets)} transcripts: {total}")
    print(f"  by convention: {dict(by_convention)}")
    print(f"  recognized: {known}/{total}")
    if total == 0:
        print("  → no spill examples found (either no large results, or "
              "marker format changed entirely)")
        sys.exit(0)
    if known == total:
        print("  → spill pattern intact (both conventions A/B accounted for; "
              "missing sidefiles are normal CC cleanup, not drift)")
        sys.exit(0)
    print("  → DRIFT DETECTED: some spill records use a NEW convention not "
          "covered by the engine. Update cc_pty_session sidefile resolver.")
    sys.exit(1)


if __name__ == "__main__":
    main()
