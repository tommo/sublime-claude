# Refactor Split Result

**Date:** 2026-07-11  
**Agent:** pi + deepseek-v4-pro  
**Repo:** `/Volumes/prj/ai/sublime-claude`

## What was done

### commands.py (4350 lines → commands/ package)

Original file split into a package with 6 domain modules + `__init__.py` + shim:

| Module | Lines | Classes | Contents |
|--------|-------|---------|----------|
| `commands/__init__.py` | 96 | — | Re-exports all 83 command classes |
| `commands/session_cmds.py` | 1090 | 24 | Session lifecycle: start/restart/close/query/interrupt/queue, all backend start wrappers (Claude/Codex/Copilot/DeepSeek/Pi/Grok/Dsr), fork/switch/resume/sleep/wake |
| `commands/provider_cmds.py` | 1002 | 9 | Provider management: custom providers CRUD, model selection, default model/provider, effort, refresh models |
| `commands/context_cmds.py` | 176 | 7 | Context management: add file/selection/folder/open files, clear context, query selection/file |
| `commands/ui_cmds.py` | 318 | 7 | UI/panels: clear/copy/checkpoint/usage, search sessions, view history, reset input |
| `commands/pty_cmds.py` | 58 | 3 | PTY terminal: PtyStart, RevealCli, ToggleTerminalReveal |
| `commands/text_cmds.py` | 1530 | 33 | Everything else: submit input, permission buttons, inline questions, quick prompts, orders, edits, image paste, link open, retain commands |

`commands.py` is now a 2-line re-export shim (`from .commands import *`).

### output.py (3676 lines → 3 modules + shim)

| Module | Lines | Classes | Contents |
|--------|-------|---------|----------|
| `output_models.py` | 199 | 7 | All dataclasses (PlanApproval, PermissionRequest, QuestionRequest, ToolCall, TodoItem, GoalState, Conversation) + constants (PENDING/DONE/ERROR/BACKGROUND/PERM_*/PLAN_*) + title helpers (strip_title_decoration, _title_abbrev_tokens) + todo/goal helpers |
| `output_view.py` | 3454 | 1 | OutputView class (the bulk — ~3400 lines) |
| `output_cmds.py` | 35 | 4 | TextCommand helpers: ClaudeInsertCommand, ClaudeReplaceCommand, ClaudeClearAllCommand, ClaudeUndoClearCommand |

`output.py` is now a 15-line re-export shim that re-exports all public names used by external importers.

## Import compatibility

### output.py shim re-exports
Verified all external importers still resolve:
- `listeners.py`: `from .output import strip_title_decoration` ✓
- `session.py`: `OutputView`, `strip_title_decoration`, `BACKGROUND`, `DONE`, `ERROR`, `PERM_ALLOW`, `PERM_ALLOW_ALL`, `PERM_ALLOW_SESSION`, `PLAN_APPROVE` ✓
- `tool_formatters.py`: `OutputView`, `ToolCall` ✓
- `tool_formatters_sublime.py`: `OutputView`, `ToolCall` ✓

### commands/ package re-exports
- `claude_code.py`: `from .commands import (83 specific classes)` — all 83 classes re-exported from `commands/__init__.py` ✓

## Verification

- `python3 -m py_compile` passes on all 12 touched modules (6 command submodules + __init__ + shim + output_models + output_view + output_cmds + output shim)
- No cross-module code dependencies between command submodules (all ST command classes are independent)
- No circular imports introduced
- `output_view.py` imports from `output_models.py` ✓

## What was NOT done

Per REFACTOR_SPLIT_GUIDANCE.md:
- `session.py` (3120 LOC) — NOT split. Contains session persistence, bookmarks, workflow helpers
- `mcp_server.py` (2446 LOC) — NOT split. Contains chatroom/garage/tool handlers
- `bridge/acp_base.py` (2765 LOC) — NOT split. Optional phase 2

## Residual risks

1. **commands.py shim is likely dead code** — Python resolves `from .commands import X` to the `commands/` package, not the `.py` shim file. The shim exists only for hypothetical `import commands` direct imports. Could be deleted in cleanup.

2. **No runtime test** — Sublime Text modules (`sublime`, `sublime_plugin`) are not available outside ST. Only `py_compile` (syntax) verification was possible. A full ST restart smoke test is recommended.

3. **Duplicate imports across sub-modules** — Each command sub-module has its own copy of the top-level imports (16 lines each). This is benign (Python caches modules) but slightly wasteful. Could be consolidated into a shared `commands/_shared.py` later.

## Post-review fixes (orchestrator)

1. Fixed relative imports in `commands/*` (`from .core` → `from ..core`, etc.) — the pi agent left package-root imports that would fail at runtime inside the subpackage.
2. Restored 3 command classes dropped during the split: `ClaudeGarageSearchCommand`, `ClaudeCodeAddMcpCommand`, `ClaudeCodeTogglePermissionModeCommand` (into `ui_cmds.py` + `__init__` exports).
3. Fixed `ClaudeCodeAddMcpCommand` plugin_dir to package root (`dirname(dirname(__file__))`).
4. Class count matches pre-split HEAD (86).
