"""Shared settings loading utilities."""
import json
import os
from pathlib import Path
from typing import Dict, Tuple

# Use absolute imports for bridge compatibility
try:
    from .constants import (
        USER_SETTINGS_DIR,
        SETTINGS_FILE,
        PROJECT_SETTINGS_DIR,
        MCP_CONFIG_FILE,
        USER_PROFILES_DIR,
        PROFILES_FILE
    )
    from .error_handler import safe_json_load
except ImportError:
    # Fallback for standalone bridge script
    from constants import (
        USER_SETTINGS_DIR,
        SETTINGS_FILE,
        PROJECT_SETTINGS_DIR,
        MCP_CONFIG_FILE,
        USER_PROFILES_DIR,
        PROFILES_FILE
    )
    from error_handler import safe_json_load

USER_SETTINGS_PATH = USER_SETTINGS_DIR / SETTINGS_FILE


def load_project_settings(cwd: str = None) -> dict:
    """Load and merge user-level and project settings.

    User settings from ~/.claude/settings.json are loaded first,
    then project settings override them.
    """
    # Start with user-level settings
    user_settings = safe_json_load(str(USER_SETTINGS_PATH), default={})

    if not cwd:
        return user_settings

    # Load project settings
    # Try .claude/settings.json first
    settings_path = os.path.join(cwd, PROJECT_SETTINGS_DIR, SETTINGS_FILE)
    project_settings = safe_json_load(settings_path, default={})

    # Try .mcp.json (MCP servers only) if no project settings
    if not project_settings:
        mcp_path = os.path.join(cwd, MCP_CONFIG_FILE)
        project_settings = safe_json_load(mcp_path, default={})

    # Merge settings: project overrides user
    return merge_settings(user_settings, project_settings)


def merge_settings(user: dict, project: dict) -> dict:
    """Deep merge project settings into user settings."""
    result = user.copy()

    for key, value in project.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Deep merge dictionaries
            result[key] = {**result[key], **value}
        else:
            # Project value overrides user value
            result[key] = value

    return result


def load_profiles_and_checkpoints(project_path: str = None) -> Tuple[Dict, Dict]:
    """Load profiles and checkpoints with cascade: user < project.

    Args:
        project_path: Optional project-specific profiles path

    Returns:
        Tuple of (profiles dict, checkpoints dict)
    """
    profiles = {}
    checkpoints = {}

    # Load user-level
    user_profiles_path = USER_PROFILES_DIR / PROFILES_FILE
    data = safe_json_load(str(user_profiles_path), default={})
    profiles.update(data.get("profiles", {}))
    checkpoints.update(data.get("checkpoints", {}))

    # Load project-level (overrides user)
    if project_path:
        data = safe_json_load(project_path, default={})
        profiles.update(data.get("profiles", {}))
        checkpoints.update(data.get("checkpoints", {}))

    return profiles, checkpoints
