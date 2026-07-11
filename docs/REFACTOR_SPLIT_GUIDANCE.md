# Refactor guidance: split giant Python modules

**Agent:** pi + deepseek-v4-pro  
**Repo:** `/Volumes/prj/ai/sublime-claude` (Sublime package ClaudeCode)  
**Goal:** Mechanical modularization of oversized modules. **No behavior changes.**

## Priority targets (LOC approximate)

| File | ~LOC | Suggested split |
|------|------|-----------------|
| `commands.py` | 4350 | Group by domain into `commands/` package |
| `output.py` | 3676 | Extract models + phantoms + strip UI; keep OutputView core |
| `session.py` | 3120 | Extract persistence, bookmarks, workflow helpers |
| `mcp_server.py` | 2446 | Extract chatroom/garage/tool handlers |
| `bridge/acp_base.py` | 2765 | Extract plan/permission/terminal tool handlers (optional phase 2) |

Do **commands.py** and **output.py** first. Stop after those two if time is limited; leave a short NOTES.md of remaining work.

## Hard rules

1. **Behavior-preserving only.** No feature adds/removes, no renames of public command classes unless re-exported under the old name.
2. **Sublime Text package constraints:**
   - Window/TextCommand classes must remain importable after package load.
   - Prefer `commands/` package with `commands/__init__.py` re-exporting all public command classes so existing `from .commands import X` and ST plugin loading still work.
   - Plugin entry (`claude_code.py`, `claude_terminal_plugin.py`) must not break.
3. **Imports:** Prefer relative imports within package (`.session`, `.output`). Fix all broken imports.
4. **Comments/docs:** minimal (see CLAUDE.md). Do not add essay comments.
5. **Do not delete** code when testing — comment out if needed.
6. **Do not touch** unrelated dirty areas beyond the split. Do not rewrite terminal mouse code, bridge plan-approve, etc. unless imports force a one-line path update.
7. **Verify** with `python3 -m py_compile` on every new/changed module. If `pytest` exists, run a quick subset; otherwise py_compile is enough.
8. **No git commit** unless the human asked — leave working tree ready for review.

## commands.py split plan

Create package `commands/` (or `cmd/` if `commands` collides — prefer `commands/`):

Suggested modules (adjust names if clearer after reading):

- `commands/session_cmds.py` — start/restart/close/query/interrupt/queue for Claude + backends (Codex/Copilot/Pi/Grok/Dsr start wrappers)
- `commands/provider_cmds.py` — manage providers, custom providers, models, effort, default model/provider
- `commands/context_cmds.py` — add file/selection/folder/open files, clear context
- `commands/ui_cmds.py` — usage, copy, clear, checkpoints, bookmarks UI, settings panels
- `commands/pty_cmds.py` — pty start, reveal, toggle terminal reveal
- `commands/__init__.py` — re-export every public `*Command` class

Keep `commands.py` as a thin shim **only if** something hardcodes the module path:

```python
# commands.py (shim)
from .commands import *  # noqa: F401,F403
```

Or delete `commands.py` after updating all importers — prefer re-export shim for safety.

## output.py split plan

- `output_models.py` — PlanApproval, PermissionRequest, QuestionRequest, ToolCall, TodoItem, GoalState, Conversation, small helpers
- `output_view.py` — OutputView class (the bulk)
- `output_cmds.py` — ClaudeInsert/Replace/ClearAll/UndoClear text commands
- `output.py` — re-export shim for `from .output import OutputView, strip_title_decoration, ...`

Preserve all public names used elsewhere (`strip_title_decoration`, `OutputView`, etc.). Grep for `from .output import` and `import output` before finishing.

## session.py (if time)

- `session_store.py` — load/save sessions, bookmarks path helpers
- `session.py` — Session class only + re-exports from store

## Acceptance checklist

- [ ] `python3 -m py_compile` succeeds on all touched modules
- [ ] `rg "from \.commands import|from \.output import|import commands|import output"` still resolves
- [ ] No circular imports (import package at top level carefully; use late imports only where already used)
- [ ] Working tree shows new modules + thinner originals/shims
- [ ] Write `docs/REFACTOR_SPLIT_RESULT.md` with: what moved where, residual risks, what was not done

## Style

- Match existing project style
- Minimal diffs within moved blocks (prefer cut/paste over reformat)
- Keep private helpers next to their primary consumer when possible
