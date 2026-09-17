# Kimi native Agent / subagent

Kimi `Agent` is **not** Grok `spawn_subagent`. Native: same-process loop,
`agents/agent-N/wire.jsonl`, result text `agent_id: agent-0\nstatus: completed`.
ACP host mapped `Agent` → Claude `Task` ⚙ and kept the spinner until a
`task_notification` that Kimi never sends for Agent.

```bash
python3 sandbox/kimi_agent/extract.py --latest
python3 sandbox/kimi_agent/host_gate.py
python3 sandbox/kimi_agent/check_e2e.py
```

Does not write `~/.kimi-code/mcp.json`.

Live (kimi **0.42.0**):

- One ACP `sessionId` — no Grok-style foreign-session multiplex
- Titles: `Agent` then `Launching explore agent: …`
- Disk: `agents/agent-0/wire.jsonl` (same-process child)
- Parent prompt returns `SANDBOX_PARENT agent_id=agent-0 status=completed`
- Host must **not** treat this as Grok `spawn_subagent` ⚙ (no `task_notification`)
