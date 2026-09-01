"""Host-injected sidecar skill for Sublime-hosted sessions."""
from __future__ import annotations

import os
from typing import List

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(PLUGIN_DIR, "skills")
SKILL_DIR = os.path.join(SKILLS_DIR, "sidecar")
SKILL_PATH = os.path.join(SKILL_DIR, "SKILL.md")

RULE = (
    'Unqualified "sidecar" in this Sublime session means SUBLIME SIDECAR: '
    "MCP sublime spawn_session (another editor sheet), not grok/kimi/codex CLI. "
    "Named CLI drivers (kimi sidecar, codex sidecar) still win. "
    f"Skill: {SKILL_PATH}"
)


def additional_skill_dirs() -> List[str]:
    return [SKILLS_DIR] if os.path.isdir(SKILLS_DIR) else []
