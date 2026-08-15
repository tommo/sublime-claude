"""Pull post-end_turn leftover patterns from a kimi/grok bridge log."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROMPT_RE = re.compile(
    r"→ acp session/prompt \(id=(\d+)\).*?sessionId\": \"([^\"]+)\"")
RESULT_RE = re.compile(
    r"← acp session/prompt \(id=(\d+)\) result: (\{.*\})")
CREATE_RE = re.compile(r"← acp REQ terminal/create \(id=(\d+)\)")
SYNTH_RE = re.compile(r"synth host Bash for (term_\w+)")
END_RE = re.compile(r"stopReason")


def extract(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    last_end = None
    leftover_create = []
    synth = []
    prompts = []
    for i, line in enumerate(lines, 1):
        m = PROMPT_RE.search(line)
        if m:
            prompts.append({"line": i, "id": int(m.group(1)), "session": m.group(2)})
        m = RESULT_RE.search(line)
        if m:
            last_end = {"line": i, "id": int(m.group(1)), "raw": m.group(2)[:200]}
        if last_end and CREATE_RE.search(line):
            leftover_create.append({"line": i, "after_end_line": last_end["line"]})
        m = SYNTH_RE.search(line)
        if m and last_end:
            synth.append({"line": i, "term": m.group(1), "after_end": True})
    return {
        "log": str(path),
        "prompts": prompts[-8:],
        "last_end_turn": last_end,
        "terminal_create_after_end": leftover_create[:20],
        "synth_bash_after_end": synth[:20],
        "pattern": (
            "end_turn then leftover terminal/create+synth Bash"
            if leftover_create or synth
            else "no leftover create after last end_turn"
        ),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--bridge-log", required=True)
    args = p.parse_args()
    out = extract(Path(args.bridge_log))
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
