"""Sandbox extractor: lock the real Kimi bash/bg session shapes."""
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SB = os.path.join(_ROOT, "sandbox", "kimi_bg")
if _SB not in sys.path:
    sys.path.insert(0, _SB)

import extract as ex  # noqa: E402

_FIX = os.path.join(_SB, "fixtures")


class TestKimiBgExtract(unittest.TestCase):
    def test_fixture_report(self):
        sess = os.path.join(_FIX, "session")
        log = os.path.join(_FIX, "kimi_bridge.log")
        r = ex.extract(sess, log)
        self.assertEqual(r["n_task_files"], 1)
        self.assertEqual(r["task_statuses"], {"completed": 1})
        self.assertEqual(r["wire_events"], 2)
        c = r["correlation"]
        self.assertEqual(c["n_detached"], 1)
        self.assertEqual(c["n_with_terminal"], 1)
        self.assertEqual(c["n_with_taskoutput"], 1)
        self.assertEqual(c["starting_background_log_hits"], 0)
        pats = r["patterns"]
        self.assertTrue(any("Starting background" in p for p in pats))
        self.assertTrue(any("terminal/wait_for_exit" in p for p in pats))
        self.assertTrue(any("Running:" in p for p in pats))

    def test_cli_fixture(self):
        sess = os.path.join(_FIX, "session")
        log = os.path.join(_FIX, "kimi_bridge.log")
        rc = ex.main(["--session", sess, "--bridge-log", log])
        self.assertEqual(rc, 0)

    def test_host_gate_matches_fixture_extract(self):
        import check_host_gate as gate
        rc = gate.main([])
        self.assertEqual(rc, 0, "run sandbox/kimi_bg/check_host_gate.py")


if __name__ == "__main__":
    unittest.main()
