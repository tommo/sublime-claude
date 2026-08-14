# Kimi bg/bash pattern extraction

Host code has been guessing (`Starting background` titles). Real sessions do not look like that. Extract first.

```bash
python3 sandbox/kimi_bg/extract.py --latest
python3 sandbox/kimi_bg/extract.py --session ~/.kimi-code/sessions/wd_…/session_… \
  --bridge-log "$TMPDIR/kimi_bridge.log" -o /tmp/kimi_bg.json
```

Observed (2026-08-13, pil animator session):

- Native jobs: `agents/main/tasks/bash-*.json` (`detached: true`, `timeoutMs: 600000`)
- Wire: `{type: task.started|task.terminated, info: {taskId, command, …}}`
- ACP: `tool_call` title `Running: <cmd>` `kind=execute` → `terminal/create` → **`terminal/wait_for_exit`** → spam `terminal/output`
- Poll: title `Reading output of task bash-…` (TaskOutput) — not a new ⚙
- **Zero** ACP titles `Starting background` — `_looks_like_background_tool` never matches
- Every `bash-*` command also has a `terminal/create` (same cmd)

Do not change host mapping until a new extract still shows this, or shows a different pattern.
