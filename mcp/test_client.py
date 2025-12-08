#!/usr/bin/env python3
"""Test client to verify socket communication with Sublime."""
import json
import socket
import sys

SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"


def send_eval(code: str = None, tool: str = None):
    """Send eval request to Sublime."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET_PATH)
        request = {"code": code or "", "tool": tool}
        sock.sendall(json.dumps(request).encode() + b"\n")

        response = sock.recv(65536).decode()
        return json.loads(response)
    finally:
        sock.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        code = " ".join(sys.argv[1:])
    else:
        code = "return get_open_files()"

    print(f"Sending: {code}")
    result = send_eval(code)
    print(f"Result: {json.dumps(result, indent=2)}")
