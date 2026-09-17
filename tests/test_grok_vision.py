"""Grok catalog: DeepSeek V4 gets MCP read_image; pre-v4 does not."""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from grok_backend import model_supports_vision  # noqa: E402


class TestGrokModelVision(unittest.TestCase):
    def test_native_grok(self):
        self.assertTrue(model_supports_vision("grok-4.6"))
        self.assertTrue(model_supports_vision(""))

    def test_deepseek_v4_family(self):
        for mid in (
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek-v4.1-flash-expires-on-0910",
            "deepseek-v41-flash-vision-exp",
            "ds-flash",
            "ds-pro",
            "ds-vision",
        ):
            self.assertTrue(model_supports_vision(mid), mid)

    def test_legacy_deepseek_no_v4(self):
        self.assertFalse(model_supports_vision("deepseek-chat"))
        self.assertFalse(model_supports_vision("deepseek-reasoner"))


if __name__ == "__main__":
    unittest.main()
