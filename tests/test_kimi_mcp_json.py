"""Kimi ACP MCP: docs say http/stdio/sse; 0.37.2 stdio relay throws — HTTP wrap."""
import json
import os
import sys
import unittest
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from stdio_http_mcp import start_stdio_http_mcp  # noqa: E402


class TestStdioHttpMcp(unittest.TestCase):
    def test_stdio_http_wrap_tools_list(self):
        child = (
            "import json,sys\n"
            "while True:\n"
            "    line=sys.stdin.readline()\n"
            "    if not line: break\n"
            "    m=json.loads(line)\n"
            "    if m.get('method')=='tools/list':\n"
            "        print(json.dumps({'jsonrpc':'2.0','id':m['id'],"
            "'result':{'tools':[{'name':'ping'}]}}), flush=True)\n"
            "    elif 'id' in m:\n"
            "        print(json.dumps({'jsonrpc':'2.0','id':m['id'],"
            "'result':{}}), flush=True)\n"
        )
        url, httpd = start_stdio_http_mcp(
            sys.executable, ["-c", child], {})
        self.addCleanup(httpd.shutdown)
        req = urllib.request.Request(
            url, data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            }).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(body["result"]["tools"][0]["name"], "ping")


if __name__ == "__main__":
    unittest.main()
