# Grok timeout:0 wait_for_exit

Grok `run_background`: create returns immediately; `wait_for_exit` is held
until the process really exits (does **not** block the model turn). After
wait, Grok `release`s — that kills leftover. Live `terminal/output` must
have `exitStatus` null while running.

Grok bash default `outputByteLimit` is 20k. ACP keeps the **tail**, not the
prefix. Background terminals raise the stored cap to
`terminal_output_max_bytes` (1MB) so long editor logs are not frozen at
startup.

```bash
python3 sandbox/grok_bg/check_wait.py
python3 tests/test_bg_tool_gates.py TestGrokBgWaitAck TestMarkTerminalBg TestTerminalOutputDrain
```
