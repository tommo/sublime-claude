"""Grok models invent {cwd}/.grok/bundled/skills — remap to GROK_HOME."""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIDGE = os.path.join(_ROOT, "bridge")
for p in (_ROOT, _BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

from grok_main import remap_cwd_grok_bundled_path  # noqa: E402


class TestRemapCwdGrokBundled(unittest.TestCase):
    def test_rewrites_missing_cwd_bundled_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.path.join(tmp, "proj")
            home = os.path.join(tmp, "grok-home")
            os.makedirs(cwd)
            real = os.path.join(home, "bundled", "skills", "review", "SKILL.md")
            os.makedirs(os.path.dirname(real))
            with open(real, "w") as f:
                f.write("# review\n")
            guessed = os.path.join(
                cwd, ".grok", "bundled", "skills", "review", "SKILL.md")
            self.assertEqual(
                remap_cwd_grok_bundled_path(guessed, cwd, home), real)

    def test_leaves_existing_cwd_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.path.join(tmp, "proj")
            home = os.path.join(tmp, "grok-home")
            local = os.path.join(
                cwd, ".grok", "bundled", "skills", "review", "SKILL.md")
            os.makedirs(os.path.dirname(local))
            with open(local, "w") as f:
                f.write("local\n")
            self.assertEqual(
                remap_cwd_grok_bundled_path(local, cwd, home), local)

    def test_ignores_project_skills_and_unrelated(self):
        cwd = "/Volumes/prj/pil"
        home = "/Users/tommo/.grok"
        self.assertEqual(
            remap_cwd_grok_bundled_path(
                cwd + "/.grok/skills/review/SKILL.md", cwd, home),
            cwd + "/.grok/skills/review/SKILL.md")
        self.assertEqual(
            remap_cwd_grok_bundled_path(
                "/Users/tommo/.grok/bundled/skills/review/SKILL.md", cwd, home),
            "/Users/tommo/.grok/bundled/skills/review/SKILL.md")


if __name__ == "__main__":
    unittest.main()
