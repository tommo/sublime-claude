"""Sandbox: which spawn actually lets kimi 0.37 session/new + stdio MCP live.

Does not write ~/.kimi-code/mcp.json. Prints one PASS/FAIL line per strategy.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time

KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")
CWD = "/Volumes/prj/pil"
PY = sys.executable
MCP_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "mcp", "server.py",
)


def _echo_mcp():
    """Tiny stdio MCP stand-in so we do not need Sublime for the handshake."""
    return {
        "name": "sandbox_echo",
        "command": PY,
        "args": ["-c", "import sys; sys.stdin.read()"],
    }


def recv_until_id(proc, rid, timeout=25):
    end = time.time() + timeout
    buf = b""
    last = None
    while time.time() < end:
        if proc.poll() is not None:
            err = b""
            if proc.stderr:
                err = proc.stderr.read() or b""
            return None, f"exit {proc.returncode} {err.decode(errors='replace')[-400:]}"
        r, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not r:
            continue
        b = proc.stdout.read(1)
        if not b:
            return None, "eof"
        if b != b"\n":
            buf += b
            continue
        if not buf.strip():
            buf = b""
            continue
        try:
            msg = json.loads(buf.decode())
        except Exception:
            buf = b""
            continue
        buf = b""
        if msg.get("id") == rid:
            return msg, None
        last = msg.get("method")
    return None, f"timeout last={last}"


def handshake(argv, env, new_params, timeout=25):
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=CWD,
        env=env,
        bufsize=0,
    )
    def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            "clientInfo": {"name": "sandbox", "version": "0"},
        },
    })
    init, err = recv_until_id(proc, 1, timeout)
    if err:
        proc.kill()
        return {"ok": False, "stage": "spawn/init", "err": err}
    if init.get("error"):
        proc.kill()
        return {"ok": False, "stage": "initialize", "err": init["error"]}
    ver = ((init.get("result") or {}).get("agentInfo") or {}).get("version")
    send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": new_params})
    nxt, err = recv_until_id(proc, 2, timeout)
    proc.kill()
    if err:
        return {"ok": False, "stage": "session/new", "err": err, "ver": ver}
    if nxt.get("error"):
        return {"ok": False, "stage": "session/new", "err": nxt["error"], "ver": ver}
    sid = (nxt.get("result") or {}).get("sessionId")
    return {"ok": True, "sessionId": sid, "ver": ver}


def main():
    if not os.path.isfile(KIMI):
        print("FAIL no kimi at", KIMI)
        return 1
    env = os.environ.copy()
    fd, mcp_path = tempfile.mkstemp(prefix="kimi-cc-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"mcpServers": {
            "sandbox_echo": _echo_mcp(),
        }}, f)
    print("mcp_file", mcp_path)

    cases = []

    # 1) flag as user specified
    cases.append((
        "flag --mcp-config-file before acp",
        [KIMI, "--mcp-config-file", mcp_path, "acp"],
        env,
        {"cwd": CWD, "mcpServers": []},
    ))
    cases.append((
        "flag --mcp-config-file after acp",
        [KIMI, "acp", "--mcp-config-file", mcp_path],
        env,
        {"cwd": CWD, "mcpServers": []},
    ))
    cases.append((
        "flag --mcp-config inline",
        [KIMI, "--mcp-config", open(mcp_path).read(), "acp"],
        env,
        {"cwd": CWD, "mcpServers": []},
    ))

    # 2) ACP wire stdio (what we used to send)
    py_mcp = {
        "name": "sandbox_echo",
        "type": "stdio",
        "command": PY,
        "args": ["-c", "import sys; sys.stdin.read()"],
        "env": [],
    }
    cases.append((
        "acp type=stdio",
        [KIMI, "acp"],
        env,
        {"cwd": CWD, "mcpServers": [py_mcp]},
    ))
    no_type = dict(py_mcp)
    no_type.pop("type")
    cases.append((
        "acp type-absent stdio",
        [KIMI, "acp"],
        env,
        {"cwd": CWD, "mcpServers": [no_type]},
    ))
    cases.append((
        "acp empty mcpServers",
        [KIMI, "acp"],
        env,
        {"cwd": CWD, "mcpServers": []},
    ))

    # 3) isolated KIMI_CODE_HOME with copied auth + our mcp.json
    home = tempfile.mkdtemp(prefix="kimi-cc-home-")
    src = os.path.expanduser("~/.kimi-code")
    for name in ("credentials", "oauth", "config.toml", "device_id", "region"):
        a, b = os.path.join(src, name), os.path.join(home, name)
        if os.path.isdir(a):
            shutil.copytree(a, b)
        elif os.path.isfile(a):
            shutil.copy2(a, b)
    with open(os.path.join(home, "mcp.json"), "w") as f:
        json.dump({"mcpServers": {"sandbox_echo": _echo_mcp()}}, f)
    env_home = env.copy()
    env_home["KIMI_CODE_HOME"] = home
    cases.append((
        "KIMI_CODE_HOME temp + mcp.json",
        [KIMI, "acp"],
        env_home,
        {"cwd": CWD, "mcpServers": []},
    ))

    failed = 0
    for label, argv, e, params in cases:
        try:
            r = handshake(argv, e, params)
        except Exception as ex:
            r = {"ok": False, "stage": "exc", "err": str(ex)}
        status = "PASS" if r.get("ok") else "FAIL"
        if not r.get("ok"):
            failed += 1
        print(f"{status}  {label}")
        print(f"       argv={argv[:6]}…")
        print(f"       {r}")
    return 1 if failed == len(cases) else 0


if __name__ == "__main__":
    raise SystemExit(main())
