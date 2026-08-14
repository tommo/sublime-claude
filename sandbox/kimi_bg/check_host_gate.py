#!/usr/bin/env python3
"""Assert host bg gate against extract — run this BEFORE changing acp_base.

Extract (README): `Running: <cmd>` is the normal execute title.
Detach lives on bash-*.json `detached: true`, not on that title.
`Starting background` is never used. wait_for_exit still blocks ACP.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_HERE, _ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import extract as ex  # noqa: E402
from acp_base import AcpBridge  # noqa: E402


def check_report(r: dict) -> list:
    fails = []
    gate = AcpBridge._looks_like_background_tool
    titles = (r.get("acp") or {}).get("sample_titles") or []
    if r.get("correlation", {}).get("starting_background_log_hits", 0) != 0:
        fails.append("extract: unexpected Starting background in this corpus")
    for t in titles:
        if not isinstance(t, str):
            continue
        if t.startswith("Running:"):
            if gate({"title": t, "kind": "execute"}):
                fails.append(
                    "FAIL Running: treated as ⚙ (foreground execute): %r" % t[:80])
        if t.lower().startswith("reading output of task"):
            if gate({"title": t}):
                fails.append("FAIL TaskOutput treated as ⚙: %r" % t[:80])
        if t.lower().startswith("starting background"):
            if not gate({"title": t}):
                fails.append("FAIL Starting background not ⚙: %r" % t[:80])
    # API: explicit flags trip the gate. Fixture ACP rawInput is command-only;
    # native detached lives on bash-*.json — do not treat this as extract proof.
    if not gate({"title": "Bash"}, {"command": "x", "detached": True}):
        fails.append("FAIL detached:true input not ⚙")
    if not gate({"title": "Bash"}, {"command": "x", "run_in_background": True}):
        fails.append("FAIL run_in_background input not ⚙")
    if gate({"title": "Bash"}, {"command": "echo ok && which pil"}):
        fails.append("FAIL plain echo treated as ⚙")
    from acp_base import AcpBridge as _B
    if _B._script_from_terminal_params("/bin/bash", ["-c", "pil test"]) != "pil test":
        fails.append("FAIL -c script not extracted from terminal args")
    return fails


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--latest" in argv:
        sess = ex.newest_session()
        log = os.path.join(
            os.environ.get("TMPDIR") or "/tmp", "kimi_bridge.log")
        if not sess:
            print("no ~/.kimi-code session", file=sys.stderr)
            return 2
        r = ex.extract(sess, log)
    else:
        sess = os.path.join(_HERE, "fixtures", "session")
        log = os.path.join(_HERE, "fixtures", "kimi_bridge.log")
        r = ex.extract(sess, log)
    fails = check_report(r)
    print("patterns:")
    for p in r.get("patterns") or []:
        print(" -", p)
    print("sample_titles:", (r.get("acp") or {}).get("sample_titles"))
    if fails:
        print("GATE FAILS:")
        for f in fails:
            print(" -", f)
        return 1
    print("host gate matches extract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
