"""process_cwd must not crash when inherited cwd is gone."""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from rpc_helpers import process_cwd  # noqa: E402


class TestProcessCwd(unittest.TestCase):
    def test_returns_existing_dir(self):
        d = process_cwd()
        self.assertTrue(os.path.isdir(d), d)

    def test_rpc_start_prefers_project_cwd(self):
        import inspect
        from rpc import JsonRpcClient
        src = inspect.getsource(JsonRpcClient.start)
        self.assertIn("cwd", src)
        self.assertIn("os.path.isdir(cwd)", src)

    def test_survives_deleted_cwd(self):
        here = os.getcwd()
        tmp = tempfile.mkdtemp(prefix="gone-cwd-")
        try:
            os.chdir(tmp)
            os.rmdir(tmp)
            d = process_cwd()
            self.assertTrue(os.path.isdir(d), d)
        finally:
            try:
                os.chdir(here)
            except OSError:
                os.chdir(os.path.expanduser("~"))


if __name__ == "__main__":
    unittest.main()
