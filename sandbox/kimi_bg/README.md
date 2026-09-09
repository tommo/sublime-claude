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

Live (kimi **0.39.1**, 2026-09-02):

- `during` (keep working same turn): `prompt_held_until_quiet` — prompt
  RPC stays open until wait + remaining text. Host `@done` is safe.
- `after` (stop at launch ack): `silent_then_self_wake` — prompt returns
  *before* wait_reply, then ~9s later native Write/fs with no new prompt.
  Host `_on_done` would `@done` while agent is live. `notify_action("kimi")`
  is `query`.
- `wake`: second `session/prompt` after wait → `SANDBOX_WOKE`, no agent_busy.

```bash
python3 sandbox/kimi_bg/check_recovery.py
python3 sandbox/kimi_bg/check_recovery.py after
python3 sandbox/kimi_bg/check_recovery.py wake
```

Live e2e (`python3 sandbox/kimi_bg/check_e2e.py`, kimi **0.39.1**):

- `run_in_background` still issues `terminal/create` + `wait_for_exit` + `release`
- **0.39 holds `session/prompt` until wait_for_exit** (0.37 returned early)
- ACP completed update is often a `terminal` block — no `task_id:` text
- Native `tool.result` still has `task_id` + `automatic_notification: true`
- `pid` is always `0` (`AcpTerminalProcess.pid = 0`)
- Host `release` **kills** if still running (e2e sandbox matches that)
