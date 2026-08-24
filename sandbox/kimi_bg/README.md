# Kimi bg/bash pattern extraction

Host code has been guessing (`Starting background` titles). Real sessions do not look like that. Extract first.

```bash
python3 sandbox/kimi_bg/extract.py --latest
python3 sandbox/kimi_bg/check_host_gate.py
python3 sandbox/kimi_bg/check_e2e.py
```

Observed (2026-08-13, pil animator session):

- Native jobs: `agents/main/tasks/bash-*.json` (`detached: true`, `timeoutMs: 600000`)
- Wire: `{type: task.started|task.terminated, info: {taskId, command, …}}`
- ACP: `tool_call` title `Running: <cmd>` `kind=execute` → `terminal/create` → **`terminal/wait_for_exit`** → spam `terminal/output`
- Poll: title `Reading output of task bash-…` (TaskOutput) — not a new ⚙
- **Zero** ACP titles `Starting background` — `_looks_like_background_tool` never matches
- Every `bash-*` command also has a `terminal/create` (same cmd)

Recovery e2e (`python3 sandbox/kimi_bg/check_recovery.py`):

- Keeps reading AFTER `session/prompt` returns
- Kimi `end_turn`s while `wait_for_exit` is still pending
- FAIL if host `_on_done` would `@done` while agent fs/perm/tools continue

Live (kimi 0.37.2):

- After wait replies, Kimi self-wakes (`fs`, `Write`) with **no**
  `session/update` unless the host opens a new `session/prompt`
- `wake` strategy: second prompt after wait → `SANDBOX_WOKE` streams
- Host: `notify_action("kimi")` is `query`, not `surface`

```bash
python3 sandbox/kimi_bg/check_recovery.py
python3 sandbox/kimi_bg/check_recovery.py after
python3 sandbox/kimi_bg/check_recovery.py wake
```

Live e2e (`python3 sandbox/kimi_bg/check_e2e.py`, kimi 0.37.2):

- `run_in_background` still issues `terminal/create` + `wait_for_exit` + `release`
- `pid` in the Bash result is always `0` (`AcpTerminalProcess.pid = 0`)
- `TaskOutput` while running is `retrieval_status: not_ready` with no log
- Host must open the Bash row as ⚙ (`background=true`) or the ack `tool_result`
  paints ✔ and there is nothing to control
