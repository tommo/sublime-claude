"""ACP fs/read_text_file: directories list, not Errno 21."""
import asyncio
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from acp_base import AcpBridge  # noqa: E402


class _Fs(AcpBridge):
    def __init__(self):
        self.fs_read_max_chars = 2 * 1024 * 1024
        self._mcp_enable_read_image = False

    def file_log(self, msg):
        pass


class TestAcpFsReadDir(unittest.TestCase):
    def test_directory_returns_listing(self):
        b = _Fs()
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "sub"))
            with open(os.path.join(tmp, "a.txt"), "w") as f:
                f.write("x")
            out = asyncio.run(b._acp_fs_read({"path": tmp}))
        text = out["content"]
        self.assertIn("Directory:", text)
        self.assertIn("a.txt", text)
        self.assertIn("sub/", text)
        self.assertIn("read a file inside", text)


if __name__ == "__main__":
    unittest.main()
