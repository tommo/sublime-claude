#!/usr/bin/env python3
"""CLI client for ClaudeCode plugin devtools (talks to live Sublime).

Usage:
  python3 devtools_cli.py ping
  python3 devtools_cli.py sessions
  python3 devtools_cli.py snapshot [view_id]
  python3 devtools_cli.py composer [view_id]
  python3 devtools_cli.py log [--tail N] [--grep SUBSTR]
  python3 devtools_cli.py event "note for ring buffer"
  python3 devtools_cli.py eval 'return list(sublime._claude_sessions)'
  python3 devtools_cli.py reload              # soft package reload (no ST restart)
  python3 devtools_cli.py reload --hard       # ignored_packages full cycle
  python3 devtools_cli.py goal "objective…"   # start /goal on active session
  python3 devtools_cli.py goal status
  python3 devtools_cli.py help
  python3 devtools_cli.py install   # ensure Packages/ClaudeCode symlink

Requires Sublime Text running with ClaudeCode loaded (socket up).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def send(req: dict, timeout: float = 10.0) -> dict:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in chunk:
                break
        sock.close()
        if not data.strip():
            return {"error": "empty response from Sublime"}
        return json.loads(data.decode())
    except FileNotFoundError:
        return {
            "error": f"socket missing: {SOCKET_PATH}",
            "hint": "Launch Sublime Text with ClaudeCode loaded, then retry.",
        }
    except ConnectionRefusedError:
        return {"error": "connection refused", "socket": SOCKET_PATH}
    except Exception as e:
        return {"error": str(e)}


def unwrap(resp: dict):
    """Normalize {result,error} envelope → body."""
    if not isinstance(resp, dict):
        return resp
    if resp.get("error"):
        return {"error": resp["error"]}
    if "result" in resp:
        return resp["result"]
    return resp


def debug_via_eval(action: str, kwargs: dict, timeout: float = 15.0) -> dict:
    """Hot-import ClaudeCode.devtools inside the live plugin process.

    Works even before package reload picks up the new op=debug handler.
    importlib.reload keeps agent-side edits live while iterating.
    """
    # kwargs must be JSON-serializable; embed as literal
    kw_lit = json.dumps(kwargs, default=str)
    code = (
        "import importlib\n"
        "import ClaudeCode.devtools as _dt\n"
        "_dt = importlib.reload(_dt)\n"
        f"return _dt.dispatch({action!r}, **{kw_lit})\n"
    )
    return unwrap(send({"code": code}, timeout=timeout))


def debug_call(action: str, kwargs: dict = None, timeout: float = 15.0) -> dict:
    kwargs = dict(kwargs or {})
    # Prefer native op=debug (after package reload)
    resp = send({"op": "debug", "action": action, **kwargs}, timeout=timeout)
    body = unwrap(resp)
    # Old ST: ignores op → result null. Or unknown action error.
    need_fallback = (
        body is None
        or body == {}
        or (isinstance(body, dict) and body.get("error")
            and "unknown" in str(body.get("error")).lower())
    )
    if need_fallback:
        return debug_via_eval(action, kwargs, timeout=timeout)
    return body


def packages_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Sublime Text/Packages"
        )
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or ""
        return os.path.join(appdata, "Sublime Text", "Packages")
    return os.path.expanduser("~/.config/sublime-text/Packages")


def cmd_install() -> int:
    """Symlink this tree into Packages/ClaudeCode if needed."""
    pkg = packages_dir()
    target = os.path.join(pkg, "ClaudeCode")
    src = PLUGIN_DIR
    print(f"plugin source: {src}")
    print(f"packages path: {target}")
    os.makedirs(pkg, exist_ok=True)
    if os.path.islink(target):
        cur = os.readlink(target)
        real_cur = os.path.realpath(target)
        real_src = os.path.realpath(src)
        print(f"existing link → {cur} (real {real_cur})")
        if real_cur == real_src:
            print("OK: already installed to this tree")
            return 0
        print("WARNING: link points elsewhere; not changing automatically.")
        print(f"  to repoint: ln -sfn {src!r} {target!r}")
        return 1
    if os.path.exists(target):
        print("ERROR: Packages/ClaudeCode exists and is not a symlink")
        return 1
    os.symlink(src, target)
    print(f"created symlink {target} → {src}")
    print("Reload ClaudeCode in Sublime (or restart ST) to pick up modules.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="ClaudeCode plugin devtools CLI")
    p.add_argument("--tail", type=int, default=80)
    p.add_argument("--grep", default=None)
    p.add_argument("--view-id", type=int, default=None)
    p.add_argument("--hard", action="store_true", help="reload: full ignored_packages cycle")
    p.add_argument("--mode", default=None, help="reload mode: soft|hard")
    p.add_argument("--wait", type=float, default=2.0, help="reload: seconds to wait then re-ping")
    p.add_argument("--raw", action="store_true", help="print raw JSON only")
    # Positionals after optionals so `goal --view-id 28 "obj…"` works
    p.add_argument(
        "action",
        nargs="?",
        default="help",
        help="ping|sessions|snapshot|composer|log|event|reload|goal|eval|install|help",
    )
    p.add_argument("rest", nargs="*", help="action args (view_id, message, code…)")
    args = p.parse_args(argv)

    action = args.action.lower()
    if action == "install":
        return cmd_install()

    if action == "eval":
        code = " ".join(args.rest) if args.rest else "return None"
        body = unwrap(send({"code": code}, timeout=30.0))
    elif action in ("reload", "reload_plugin"):
        mode = args.mode or ("hard" if args.hard else "soft")
        body = debug_call("reload", {"mode": mode})
        print(json.dumps(body, indent=2, default=str))
        # Wait for MCP socket to come back after package unload/load
        wait = max(0.0, float(args.wait or 0))
        if wait and isinstance(body, dict) and body.get("ok"):
            import time as _time
            print(f"… waiting {wait:.1f}s for plugin reload …", flush=True)
            _time.sleep(wait)
            # Probe until socket answers or timeout
            deadline = _time.time() + max(wait, 3.0)
            last = None
            while _time.time() < deadline:
                last = debug_call("ping", {})
                if isinstance(last, dict) and last.get("ok"):
                    break
                _time.sleep(0.25)
            print(json.dumps({"post_reload_ping": last}, indent=2, default=str))
            return 0 if isinstance(last, dict) and last.get("ok") else 3
        return 0 if isinstance(body, dict) and body.get("ok") else 2
    else:
        kwargs = {}
        view_id = args.view_id
        if args.rest and action in ("snapshot", "composer", "sessions"):
            try:
                view_id = int(args.rest[0])
            except ValueError:
                pass
        if view_id is not None:
            kwargs["view_id"] = view_id
        if action == "log":
            kwargs["tail"] = args.tail
            if args.grep:
                kwargs["grep"] = args.grep
        if action == "event":
            kwargs["message"] = " ".join(args.rest) if args.rest else ""
        if action == "goal":
            kwargs["args"] = " ".join(args.rest) if args.rest else "status"
            if view_id is not None:
                kwargs["view_id"] = view_id
        body = debug_call(action, kwargs)

    text = json.dumps(body, indent=2, default=str)
    print(text)
    if isinstance(body, dict) and body.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
