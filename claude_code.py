"""Claude Code plugin for Sublime Text.

This is the main entry point that imports all components.
Sublime Text requires commands and listeners to be importable from the top-level package.
"""

# Core functionality and session management
from .core import (
    plugin_loaded,
    plugin_unloaded,
    get_session_for_view,
    get_active_session,
    create_session,
)

# All commands
from .commands import (
    ClaudeCodeStartCommand,
    ClaudeCodeQueryCommand,
    ClaudeCodeRestartCommand,
    ClaudeCodeQuerySelectionCommand,
    ClaudeCodeQueryFileCommand,
    ClaudeCodeAddFileCommand,
    ClaudeCodeAddSelectionCommand,
    ClaudeCodeAddOpenFilesCommand,
    ClaudeCodeAddFolderCommand,
    ClaudeCodeClearContextCommand,
    ClaudeCodeInterruptCommand,
    ClaudeCodeClearCommand,
    ClaudeCodeResetInputCommand,
    ClaudeCodeRenameCommand,
    ClaudeCodeToggleCommand,
    ClaudeCodeStopCommand,
    ClaudeCodeResumeCommand,
    ClaudeCodeSwitchCommand,
    ClaudeCodeForkCommand,
    ClaudeCodeForkFromCommand,
    ClaudeCodeAddMcpCommand,
    ClaudeCodeBlackboardCommand,
    ClaudeCodeBlackboardSaveCommand,
    ClaudeCodeTogglePermissionModeCommand,
    ClaudeSubmitInputCommand,
    ClaudeEnterInputModeCommand,
    ClaudeExitInputModeCommand,
    ClaudeInsertNewlineCommand,
    ClaudePermissionAllowCommand,
    ClaudePermissionDenyCommand,
    ClaudePermissionAllowSessionCommand,
    ClaudePermissionAllowAllCommand,
    ClaudeQuickPromptCommand,
    ClaudeCodeQueuePromptCommand,
    ClaudeCodeCopyCommand,
    ClaudeCodeSaveCheckpointCommand,
    ClaudeCodeUsageCommand,
    ClaudeCodeViewHistoryCommand,
)

# Event listeners
from .listeners import (
    ClaudeCodeEventListener,
    ClaudeOutputEventListener,
)
