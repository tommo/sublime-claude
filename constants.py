"""Constants and configuration values for Claude Code plugin."""
from pathlib import Path

# ─── Application ──────────────────────────────────────────────────────────────
APP_NAME = "Claude"
DEFAULT_SESSION_NAME = "Claude"
PLUGIN_NAME = "ClaudeCode"

# ─── User Directories ─────────────────────────────────────────────────────────
USER_HOME = Path.home()
USER_SETTINGS_DIR = USER_HOME / ".claude"
USER_SETTINGS_FILE = USER_HOME / ".claude.json"  # User-level settings (MCP, etc.)
USER_PROFILES_DIR = USER_HOME / ".claude-sublime"

# ─── Project Directories ──────────────────────────────────────────────────────
PROJECT_SETTINGS_DIR = ".claude"
PROJECT_SUBLIME_TOOLS_DIR = ".claude/sublime_tools"

# ─── File Names ───────────────────────────────────────────────────────────────
SETTINGS_FILE = "settings.json"
PROFILES_FILE = "profiles.json"
SESSIONS_FILE = ".sessions.json"
MCP_CONFIG_FILE = ".mcp.json"

# ─── Socket & IPC ─────────────────────────────────────────────────────────────
MCP_SOCKET_PATH = "/tmp/sublime_claude_mcp.sock"

# ─── Logging ──────────────────────────────────────────────────────────────────
BRIDGE_LOG_PATH = "/tmp/claude_bridge.log"
LOG_PREFIX_INFO = "  "
LOG_PREFIX_ERROR = "ERROR: "

# ─── View Settings ────────────────────────────────────────────────────────────
OUTPUT_VIEW_SETTING = "claude_output"
FONT_SIZE = 12

# ─── Status Indicators ────────────────────────────────────────────────────────
# Status prefixes for view titles
STATUS_ACTIVE_WORKING = "◉"    # Active session, working
STATUS_ACTIVE_IDLE = "◇"       # Active session, idle
STATUS_INACTIVE_WORKING = "•"  # Inactive session, working
STATUS_INACTIVE_IDLE = ""      # Inactive session, idle

# Spinner frames for loading
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ─── Input Mode ───────────────────────────────────────────────────────────────
INPUT_MARKER = "◎ "

# ─── Timing ───────────────────────────────────────────────────────────────────
CONTEXT_DEBOUNCE_MS = 300  # Debounce for context menu after goto
INPUT_RETRY_DELAY_MS = 500  # Retry delay for query when not initialized
RECONNECT_DELAY_MS = 100    # Delay before reconnecting orphaned view

# ─── Limits ───────────────────────────────────────────────────────────────────
DEFAULT_FIND_FILE_LIMIT = 20
DEFAULT_GET_SYMBOLS_LIMIT = 10
DEFAULT_TERMINAL_READ_LINES = 100
MAX_LINE_LENGTH = 2000  # Max chars per line in read results

# ─── Buffer Sizes ─────────────────────────────────────────────────────────────
BRIDGE_BUFFER_SIZE = 1073741824  # 1GB for StreamReader

# ─── Permissions ──────────────────────────────────────────────────────────────
PERMISSION_MODE_DEFAULT = "default"
PERMISSION_MODE_ACCEPT_EDITS = "acceptEdits"
PERMISSION_MODE_BYPASS = "bypassPermissions"

PERMISSION_MODES = [
    PERMISSION_MODE_DEFAULT,
    PERMISSION_MODE_ACCEPT_EDITS,
    PERMISSION_MODE_BYPASS
]

PERMISSION_MODE_LABELS = {
    PERMISSION_MODE_DEFAULT: "Default (prompt for all)",
    PERMISSION_MODE_ACCEPT_EDITS: "Accept Edits (auto-allow Read/Edit/Write)",
    PERMISSION_MODE_BYPASS: "Bypass All (auto-allow everything)"
}

# ─── Context Triggers ─────────────────────────────────────────────────────────
CONTEXT_TRIGGER_CHAR = "@"

# ─── Tool Status ──────────────────────────────────────────────────────────────
TOOL_STATUS_PENDING = "pending"
TOOL_STATUS_DONE = "done"
TOOL_STATUS_ERROR = "error"

TOOL_STATUS_SYMBOLS = {
    TOOL_STATUS_PENDING: "☐",
    TOOL_STATUS_DONE: "✔",
    TOOL_STATUS_ERROR: "✘",
}

# ─── Session State ────────────────────────────────────────────────────────────
SESSION_STATE_UNINITIALIZED = "uninitialized"
SESSION_STATE_INITIALIZING = "initializing"
SESSION_STATE_READY = "ready"
SESSION_STATE_WORKING = "working"
SESSION_STATE_ERROR = "error"
