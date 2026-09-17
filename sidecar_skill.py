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
    "Reuse warm sheets: list_sessions first, then send_to_session(agent_id=…) "
    "an idle/sleeping child; spawn_session only if none fit. "
    "Honor the user's model: spawn_session(backend=…, model=…) with the "
    "requested id (list_backends if unsure); do not substitute a default. "
    "Reuse only a warm child that already has that model. "
    "Child MUST use project knowledge (list_profile_docs, irr, existing code) "
    "before inventing. "
    "Child MUST report done with MCP sublime signal_complete as its own last "
    "tool step — not send_to_session(parent), not a CLI/fake complete. "
    f"Skill: {SKILL_PATH}"
)


def additional_skill_dirs() -> List[str]:
    return [SKILLS_DIR] if os.path.isdir(SKILLS_DIR) else []
