"""Slash / composer commands for RESTART NEW."""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from command_parser import CommandParser  # noqa: E402


class TestRestartNewParse(unittest.TestCase):
    def test_restart_is_builtin(self):
        self.assertTrue(CommandParser.is_builtin("restart"))
        self.assertTrue(CommandParser.is_builtin("restart-new"))

    def test_slash_restart(self):
        cmd = CommandParser.parse("/restart")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.name, "restart")
        self.assertEqual(cmd.args, "")

    def test_slash_restart_new(self):
        cmd = CommandParser.parse("/restart new")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.name, "restart")
        self.assertEqual(cmd.args, "new")

    def test_slash_restart_new_hyphen(self):
        cmd = CommandParser.parse("/restart-new")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.name, "restart-new")

    def test_plain_prompt_is_not_slash(self):
        self.assertIsNone(CommandParser.parse("RESTART NEW"))


if __name__ == "__main__":
    unittest.main()
