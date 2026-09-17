"""CSI private-mode dispatch must not crash handlers without private=."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "terminal"))

from csi import dispatch_call  # noqa: E402


class TestCsiPrivateKwarg(unittest.TestCase):
    def test_drops_unexpected_private(self):
        def handler(mode):
            return mode

        self.assertEqual(dispatch_call(handler, [6], private=True), 6)
        self.assertEqual(dispatch_call(handler, [5], private=False), 5)

    def test_passes_private_when_accepted(self):
        seen = {}

        def handler(mode, private=False):
            seen["private"] = private
            return mode

        self.assertEqual(dispatch_call(handler, [6], private=True), 6)
        self.assertTrue(seen["private"])


if __name__ == "__main__":
    unittest.main()
