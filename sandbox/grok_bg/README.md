# Grok timeout:0 wait_for_exit

Live (`/tmp/grok_bridge.93131.log` `term_6820e17b8a`):

- `run_terminal_command` `timeout: 0`
- `terminal/create` `bg=True` (paired to the tool_call)
- `synth host Bash` → leftover ☐ next to ⚙
- `terminal/wait_for_exit` held until interrupt → `{exitCode: null, signal: SIGTERM}`

Grok still issues wait_for_exit for timeout:0. Holding it blocks the turn.
Kimi must **not** get an early wait return (null = killed).

```bash
python3 sandbox/grok_bg/check_wait.py
python3 tests/test_bg_tool_gates.py TestGrokBgWaitAck TestMarkTerminalBg
```
